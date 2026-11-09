from django.db import migrations


def mask(apps, schema_editor):
    """Keep only the last four digits of every stored Aadhaar number (X16, Q9)."""
    Staff = apps.get_model('staff', 'StaffMember')
    for row in Staff.objects.exclude(aadhar_number__isnull=True).exclude(aadhar_number='').iterator():
        digits = ''.join(ch for ch in row.aadhar_number if ch.isdigit())
        masked = ('X' * 8 + digits[-4:]) if digits else ''
        if masked != row.aadhar_number:
            Staff.objects.filter(pk=row.pk).update(aadhar_number=masked)


class Migration(migrations.Migration):
    dependencies = [('staff', '0025_widen_app_password_for_hash')]
    operations = [migrations.RunPython(mask, migrations.RunPython.noop)]
