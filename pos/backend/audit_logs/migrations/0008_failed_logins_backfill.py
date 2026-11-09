"""Sign-in attempts that were refused used to be logged as successful logins.

A real login always resolves to a person (the token comes back in the answer), so
a LOGIN row with nobody attached can only be a refused attempt. Relabel those, so
the log stops saying "x logged in." for a wrong password.
"""
from django.db import migrations


def relabel(apps, schema_editor):
    SystemLog = apps.get_model('audit_logs', 'SystemLog')
    rows = SystemLog.objects.filter(action='LOGIN', user__isnull=True, user_id_snapshot__isnull=True)
    for log in rows.iterator(chunk_size=500):
        who = log.user_email or 'an unknown address'
        SystemLog.objects.filter(pk=log.pk).update(
            action='LOGIN_FAILED',
            status_code=400,
            human_description=f'Failed sign-in attempt for {who}.',
        )


class Migration(migrations.Migration):
    dependencies = [('audit_logs', '0007_syslog_status_code')]
    operations = [migrations.RunPython(relabel, migrations.RunPython.noop)]
