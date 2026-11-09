"""Give every existing branch a branch code (DL01, DL02, UP01 ...).

The state comes from the first two digits of the branch's GSTIN; branches are
numbered within their state in the order they were created. Codes can be
changed in Admin > Centres afterwards. Reversing clears them.
"""

from django.db import migrations


def assign(apps, schema_editor):
    from salon_admin.branch_codes import next_code, state_prefix

    Center = apps.get_model('salon_admin', 'Center')
    taken = set(Center.objects.exclude(branch_code__isnull=True)
                .values_list('branch_code', flat=True))
    for center in Center.objects.filter(branch_code__isnull=True).order_by('id'):
        code = next_code(state_prefix(center.gst_number), taken)
        taken.add(code)
        Center.objects.filter(pk=center.pk).update(branch_code=code)


def clear(apps, schema_editor):
    apps.get_model('salon_admin', 'Center').objects.update(branch_code=None)


class Migration(migrations.Migration):

    dependencies = [
        ('salon_admin', '0015_center_branch_code'),
    ]

    operations = [migrations.RunPython(assign, clear)]
