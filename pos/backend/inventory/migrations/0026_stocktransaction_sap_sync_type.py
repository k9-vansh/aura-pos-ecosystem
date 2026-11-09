from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0025_connections_check'),
    ]

    operations = [
        migrations.AlterField(
            model_name='stocktransaction',
            name='transaction_type',
            field=models.CharField(choices=[('CHECKOUT', 'Checkout for salon use'), ('AUDIT', 'Stock Audit Adjustment'), ('PO_RECEIPT', 'Purchase Order Receipt'), ('SALE', 'Retail Sale'), ('REFUND', 'Refunded Sale'), ('TRANSFER_OUT', 'Stock transfer out'), ('TRANSFER_IN', 'Stock transfer in'), ('BOM_CONSUMPTION', 'Service consumables'), ('SAP_SYNC', 'Changed in SAP')], max_length=50),
        ),
    ]
