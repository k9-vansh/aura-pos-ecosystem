"""Read-only questions put straight to SAP's database (HANA).

The Service Layer answers in pages of 100 items, with every warehouse of every item inside each
page: the stock of the whole item master is about 22 requests of 16 MB each and takes minutes.
HANA answers the same question in one query, in about a second. Everything here is a plain
SELECT: nothing is ever written to SAP through this module, and it refuses any statement that is
not a SELECT.

When HANA cannot be reached (driver missing, host down, SAP switched off) `HanaUnavailable` is
raised, and each caller carries on with the Service Layer, exactly as it did before.
"""
import logging
import re

from django.conf import settings

logger = logging.getLogger(__name__)

# A connection that cannot be made must fail in seconds, not hang a worker or a request.
CONNECT_TIMEOUT_MS = 4000
COMMUNICATION_TIMEOUT_MS = 60000

_SELECT = re.compile(r'^\s*SELECT\b', re.IGNORECASE)


class HanaUnavailable(Exception):
    """HANA cannot be read right now; use the Service Layer."""


def available():
    """True when SAP is on and HANA is configured (it may still be down)."""
    return bool(getattr(settings, 'SAP_ENABLED', False)
                and getattr(settings, 'HANA_ADDRESS', '') and getattr(settings, 'HANA_USER', ''))


def _connect():
    if not available():
        raise HanaUnavailable('SAP is switched off or HANA is not configured')
    try:
        from hdbcli import dbapi
    except ImportError as exc:
        raise HanaUnavailable('the HANA driver (hdbcli) is not installed') from exc
    try:
        return dbapi.connect(
            address=settings.HANA_ADDRESS, port=settings.HANA_PORT, user=settings.HANA_USER,
            password=settings.HANA_PASSWORD, currentSchema=settings.HANA_SCHEMA,
            connectTimeout=CONNECT_TIMEOUT_MS, communicationTimeout=COMMUNICATION_TIMEOUT_MS)
    except Exception as exc:                                        # noqa: BLE001
        raise HanaUnavailable(f'{type(exc).__name__}: {str(exc)[:160]}') from exc


def rows(sql, params=()):
    """Every row of one SELECT, as a list of tuples. Raises HanaUnavailable when HANA cannot answer."""
    if not _SELECT.match(sql):
        raise ValueError('Only SELECT statements may be sent to SAP HANA.')
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(sql, tuple(params))
        return cursor.fetchall()
    except HanaUnavailable:
        raise
    except Exception as exc:                                        # noqa: BLE001
        raise HanaUnavailable(f'{type(exc).__name__}: {str(exc)[:160]}') from exc
    finally:
        try:
            conn.close()
        except Exception:                                           # noqa: BLE001
            pass


def many(queries):
    """Several SELECTs over one connection: [(sql, params)] -> [rows, rows, ...]."""
    for sql, _ in queries:
        if not _SELECT.match(sql):
            raise ValueError('Only SELECT statements may be sent to SAP HANA.')
    conn = _connect()
    try:
        cursor = conn.cursor()
        out = []
        for sql, params in queries:
            cursor.execute(sql, tuple(params))
            out.append(cursor.fetchall())
        return out
    except Exception as exc:                                        # noqa: BLE001
        raise HanaUnavailable(f'{type(exc).__name__}: {str(exc)[:160]}') from exc
    finally:
        try:
            conn.close()
        except Exception:                                           # noqa: BLE001
            pass
