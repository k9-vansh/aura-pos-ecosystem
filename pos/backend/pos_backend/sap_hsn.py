"""SAP's HSN (goods) and SAC (services) lists, read from HANA.

SAP does not take an HSN code as text on an item: the item points at a row of its HSN list
(`ChapterID` = that row's number). A GST document for an item that points at none is refused
("You must select HSN in line 1, since GST tax code is selected"). So the POS needs both
directions: SAP's row number -> the code to show on the bill (when items are pulled), and the
code the POS holds -> SAP's row number (when a product is sent).

The service layer has no entity for these lists, so HANA (the same connection the batch lookup uses)
is read. When HANA cannot be read the lists come back empty and the callers carry on without them.
"""
import logging

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

_KEY = 'sap_hsn_lists'


def _read():
    from hdbcli import dbapi

    conn = dbapi.connect(address=settings.HANA_ADDRESS, port=settings.HANA_PORT, user=settings.HANA_USER,
                         password=settings.HANA_PASSWORD, currentSchema=settings.HANA_SCHEMA)
    try:
        cur = conn.cursor()
        # The code is in the column called Dscription (HSN: 33059090) and ServCode (SAC: 999721).
        cur.execute('SELECT "AbsEntry","Dscription" FROM "OCHP"')
        hsn = {int(a): str(c).strip() for a, c in cur.fetchall() if c}
        cur.execute('SELECT "AbsEntry","ServCode" FROM "OSAC"')
        sac = {int(a): str(c).strip() for a, c in cur.fetchall() if c}
        return {'hsn': hsn, 'sac': sac}
    finally:
        conn.close()


def lists(refresh=False):
    """{'hsn': {row number: code}, 'sac': {row number: code}}; empty when HANA cannot be read."""
    found = None if refresh else cache.get(_KEY)
    if found is None:
        try:
            found = _read()
            cache.set(_KEY, found, 60 * 60)
        except Exception as exc:                                    # noqa: BLE001
            logger.warning('SAP: could not read the HSN/SAC lists from HANA: %s', exc)
            return {'hsn': {}, 'sac': {}}
    return found


def hsn_code(entry):
    """The HSN code SAP's row number `entry` stands for, or ''."""
    return lists()['hsn'].get(int(entry), '') if entry not in (None, '', -1) else ''


def sac_code(entry):
    return lists()['sac'].get(int(entry), '') if entry not in (None, '', -1) else ''


def sac_entry(code):
    """SAP's row number for a service (SAC) code typed in the POS, or None. 999721 and 00999721 are one code."""
    wanted = ''.join(ch for ch in str(code or '') if ch.isalnum()).lstrip('0')
    if not wanted:
        return None
    for entry, known in lists()['sac'].items():
        if ''.join(ch for ch in known if ch.isalnum()).lstrip('0') == wanted:
            return entry
    return None


def hsn_entry(code):
    """SAP's row number for an HSN code typed in the POS (dots and spaces ignored), or None."""
    wanted = ''.join(ch for ch in str(code or '') if ch.isalnum())
    if not wanted:
        return None
    for entry, known in lists()['hsn'].items():
        if ''.join(ch for ch in known if ch.isalnum()) == wanted:
            return entry
    return None
