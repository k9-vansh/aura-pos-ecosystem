"""Helpers for tests.

`full_access_permissions()` is what a role named "Owner" used to get implicitly
from its name. Since owner decision #6 nothing is granted by a role's name, so a
test that needs an all-powerful (but non-superuser) role says so explicitly -
exactly as migration salon_admin.0014 does for real "owner" roles.
"""

from pos_backend.permission_tree import action_map, submodule_map


def chat_only_role():
    """A role that may use Chat and nothing else (Chat is a module since #6)."""
    from salon_admin.models import Role

    role, _ = Role.objects.get_or_create(
        name='Chat user',
        defaults={'permissions': {'chat': {'messages': {'read': True, 'create': True}}}})
    return role


def bill_and_pay(api, payload, method='Cash', format='json'):
    """Raise a completed bill the way the till does since Phase 3.

    A bill's status follows the money taken (finding R19), so "complete this
    bill" with no payment is refused. A test that does not care about the
    payment holds the bill first, reads the server's total, then completes the
    held bill paying exactly that - the till's own Hold -> Complete path,
    with the same request body, so promotions and memberships are applied as
    at a real checkout. Returns the creating response carrying the completed
    bill's data, so `.status_code` (201) and `.data` read as before.
    """
    from decimal import Decimal

    if payload.get('payments') or str(payload.get('status', 'paid')).lower() == 'draft':
        return api.post('/billing/invoices/', payload, format='json')

    held = {k: v for k, v in payload.items() if k != 'payments'}
    held['status'] = 'draft'
    created = api.post('/billing/invoices/', held, format='json')
    if created.status_code != 201:
        return created

    # What is left to pay after a value card, if the bill uses one.
    total = Decimal(str(created.data.get('paid_now', created.data['total_amount'])))
    complete = dict(payload, status='paid',
                    payments=[{'payment_method': method, 'amount': str(total)}] if total > 0 else [])
    done = api.put(f"/billing/invoices/{created.data['id']}/", complete, format='json')
    if done.status_code != 200:
        return done
    created.data = done.data
    return created


def receive_transfer(api, transfer_id, rows=None, note=''):
    """Sign a transfer in through the Receive call, with a small PDF as the bill."""
    import json
    import tempfile

    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.test import override_settings

    data = {'note': note, 'bill': SimpleUploadedFile('bill.pdf', b'%PDF-1.4 test', content_type='application/pdf')}
    if rows is not None:
        data['lines'] = json.dumps(rows)
    with override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix='pos-test-media-')):
        return api.post(f'/inventory/api/stock_transfers/{transfer_id}/receive/', data, format='multipart')


def bill_picture(name='bill.png'):
    """A real one-pixel picture, as a petty cash entry needs for its bill."""
    import base64

    from django.core.files.uploadedfile import SimpleUploadedFile

    png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==')
    return SimpleUploadedFile(name, png, content_type='image/png')


def full_access_permissions():
    perms = {'all_centers': True}
    actions = action_map()
    for module, submodules in submodule_map().items():
        perms[module] = {}
        for sub in submodules:
            entry = {'read': True, 'create': True, 'update': True, 'delete': True}
            if (module, sub) in actions:
                entry['actions'] = {a: True for a in actions[(module, sub)]}
            perms[module][sub] = entry
    return perms


def set_pin_gate(on):
    """Turn "ask for the client's PIN before their money is used" on or off for a test. Tests about wallets, cards and
    the books spend money without caring about the PIN, so their base classes switch it off; the PIN tests switch it on."""
    from pos_backend import app_settings
    from pos_backend.models import AppSetting

    AppSetting.objects.update_or_create(key='billing.require_client_pin', scope='global', defaults={'value': bool(on)})
    app_settings.invalidate()


def pin_gate_off():
    set_pin_gate(False)


def pin_gate_on():
    set_pin_gate(True)
