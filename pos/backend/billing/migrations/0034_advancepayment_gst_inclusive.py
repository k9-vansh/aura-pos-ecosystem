from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0033_invoice_date_indexes'),
    ]

    operations = [
        migrations.AddField(
            model_name='advancepayment',
            name='gst_inclusive',
            field=models.BooleanField(default=False),
        ),
    ]
