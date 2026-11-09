from django.db import migrations, models


def reverse_phones(apps, schema_editor):
    """Fill `phone_reversed` for the customers that already exist."""
    connection = schema_editor.connection
    if connection.vendor in ('mysql', 'postgresql'):
        # In slices that each commit: one UPDATE of a crore rows is one huge transaction.
        Client = apps.get_model('clients', 'Client')
        edges = Client.objects.aggregate(low=models.Min('pk'), high=models.Max('pk'))
        if edges['low'] is None:
            return
        step = 200_000
        with connection.cursor() as cursor:
            for start in range(edges['low'], edges['high'] + 1, step):
                cursor.execute("UPDATE clients_client SET phone_reversed = REVERSE(phone) "
                               "WHERE id >= %s AND id < %s AND phone <> ''", [start, start + step])
        return
    Client = apps.get_model('clients', 'Client')
    batch = []
    for pk, phone in Client.objects.exclude(phone='').values_list('pk', 'phone').iterator(chunk_size=2000):
        batch.append(Client(pk=pk, phone_reversed=phone[::-1]))
        if len(batch) >= 1000:
            Client.objects.bulk_update(batch, ['phone_reversed'])
            batch = []
    if batch:
        Client.objects.bulk_update(batch, ['phone_reversed'])


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0020_address_pin_code'),
    ]

    operations = [
        migrations.AddField(
            model_name='client',
            name='phone_reversed',
            field=models.CharField(blank=True, default='', editable=False, max_length=20),
        ),
        # atomic=False: on MySQL each slice commits on its own instead of one transaction over every customer.
        migrations.RunPython(reverse_phones, migrations.RunPython.noop, atomic=False),
        migrations.AddIndex(
            model_name='client',
            index=models.Index(fields=['first_name'], name='client_first_name_idx'),
        ),
        migrations.AddIndex(
            model_name='client',
            index=models.Index(fields=['last_name'], name='client_last_name_idx'),
        ),
        migrations.AddIndex(
            model_name='client',
            index=models.Index(fields=['gst_number'], name='client_gst_idx'),
        ),
        migrations.AddIndex(
            model_name='client',
            index=models.Index(fields=['phone_reversed'], name='client_phone_rev_idx'),
        ),
    ]
