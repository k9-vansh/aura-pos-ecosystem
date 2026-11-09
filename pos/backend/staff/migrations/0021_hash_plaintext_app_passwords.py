"""
Hash any staff app password that is still stored in plaintext.

`staff_app_login` had a plaintext-equality fallback that accepted an unhashed
`app_password` as a credential, re-hashing it opportunistically on the next
successful login. Combined with `app_password` being writable through the staff
API, that allowed account takeover.

The fallback is now removed, so a staff member whose password sits in plaintext
would be locked out. This hashes those values in place, keeping their existing
PIN working while removing the cleartext.

Irreversible by design.
"""

from django.contrib.auth.hashers import identify_hasher, make_password
from django.db import migrations


def is_hashed(value):
    if not value:
        return False
    try:
        identify_hasher(value)
        return True
    except Exception:
        return False


def hash_plaintext_passwords(apps, schema_editor):
    StaffMember = apps.get_model('staff', 'StaffMember')

    hashed = 0
    cleared = 0
    qs = StaffMember.objects.exclude(app_password__isnull=True).exclude(app_password='')
    for staff in qs.iterator():
        if is_hashed(staff.app_password):
            continue
        raw = str(staff.app_password).strip()
        if not raw:
            staff.app_password = None
            staff.save(update_fields=['app_password'])
            cleared += 1
            continue
        staff.app_password = make_password(raw)
        staff.save(update_fields=['app_password'])
        hashed += 1

    if hashed or cleared:
        print(f'  hashed {hashed} plaintext staff app password(s); '
              f'cleared {cleared} unusable one(s)')
    else:
        print('  no plaintext staff app passwords found')


class Migration(migrations.Migration):

    dependencies = [
        ('staff', '0020_payrollrecord_breakdown_payrollrecord_generated_at_and_more'),
    ]

    operations = [
        migrations.RunPython(hash_plaintext_passwords, migrations.RunPython.noop),
    ]
