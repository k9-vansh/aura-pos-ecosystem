"""
Hash any client app PIN that is still stored in plaintext.

`client_app_update_profile` did `client.app_pin = pin` - so every customer who
changed their PIN through the app had it written to the database unhashed, and
the login path had a plaintext-equality fallback that accepted it as a
credential.

That fallback is now removed, which means a customer whose PIN is stored in
plaintext would be locked out. This migration hashes those values in place, so
their existing PIN keeps working and the cleartext stops being at rest.

Deliberately does NOT clear the field: the PIN value itself is not compromised
by being hashed, and clearing it would force every affected customer back to
the front desk.

Irreversible by design - un-hashing is not possible, and re-introducing
plaintext credentials would not be a sensible thing to offer.
"""

from django.contrib.auth.hashers import identify_hasher, make_password
from django.db import migrations


def is_hashed(value):
    """True if `value` is already a Django password hash."""
    if not value:
        return False
    try:
        identify_hasher(value)
        return True
    except Exception:
        return False


def hash_plaintext_pins(apps, schema_editor):
    Client = apps.get_model('clients', 'Client')

    hashed = 0
    cleared = 0
    for client in Client.objects.exclude(app_pin__isnull=True).exclude(app_pin='').iterator():
        if is_hashed(client.app_pin):
            continue
        raw = str(client.app_pin).strip()
        if not raw:
            # Whitespace-only: not a usable credential, so clear it rather than
            # hashing something nobody can type.
            client.app_pin = None
            client.save(update_fields=['app_pin'])
            cleared += 1
            continue
        client.app_pin = make_password(raw)
        client.save(update_fields=['app_pin'])
        hashed += 1

    if hashed or cleared:
        print(f'  hashed {hashed} plaintext client PIN(s); cleared {cleared} unusable one(s)')
    else:
        print('  no plaintext client PINs found')


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0013_alter_clientmembership_start_date_and_more'),
    ]

    operations = [
        migrations.RunPython(hash_plaintext_pins, migrations.RunPython.noop),
    ]
