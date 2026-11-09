"""Phones unique across the business (F2), consent flags (B5.3).

Steps: add the key column without the unique rule, fill it - normalising
each phone - keeping the OLDEST record of each number as its owner, then
switch the unique rule on. The other records of a shared number keep their
phone but no key; Admin > Clients > duplicates lists them for merging.
Records without a phone (the 34,334 legacy B2B clients, Q1) get no key.
"""

from django.db import migrations, models


def fill_keys(apps, schema_editor):
    from clients.identity import normalise_phone

    Client = apps.get_model('clients', 'Client')
    owners = set()
    for client in Client.objects.order_by('-is_active', 'id').only('id', 'phone', 'is_active').iterator():
        phone = normalise_phone(client.phone)
        key = phone or None
        if key in owners:
            key = None                      # a duplicate: listed for merging
        elif key:
            owners.add(key)
        Client.objects.filter(pk=client.pk).update(phone=phone, phone_key=key)


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0017_clientmembership_cm_expiry_idx_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='client',
            name='phone_key',
            field=models.CharField(blank=True, editable=False, max_length=20, null=True),
        ),
        migrations.AddField(
            model_name='client',
            name='sms_consent',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='client',
            name='whatsapp_consent',
            field=models.BooleanField(default=True),
        ),
        migrations.RunPython(fill_keys, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='client',
            name='phone_key',
            field=models.CharField(blank=True, editable=False, max_length=20, null=True, unique=True),
        ),
    ]
