from django.db import migrations, models


class Migration(migrations.Migration):
    """Adds the SAP posting record. Only a new table; no existing data changes.

    The AlterField brings the queue's `choices` in line with the model (choices
    are not stored in the database, so it does not touch the table)."""

    dependencies = [
        ('pos_backend', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='sapsyncqueue',
            name='object_type',
            field=models.CharField(choices=[
                ('client', 'Client (Business Partner)'),
                ('product', 'Product (Item)'),
                ('vendor', 'Vendor (Supplier BP)'),
                ('invoice', 'Invoice (A/R Invoice)'),
                ('credit_note', 'Credit Note (A/R Credit Memo)'),
                ('purchase_order', 'Purchase Order (GRPO)'),
                ('stock_transfer', 'Stock Transfer (Inventory Transfer)'),
                ('stock_transaction', 'Stock Transaction (Goods Issue/Receipt)'),
                ('center', 'Center (Warehouse)'),
                ('incoming_payment', 'Incoming Payment'),
                ('advance_je', 'Advance (Journal Entry)'),
                ('marketing_je', 'Card / membership / package sale (Journal Entry)'),
            ], max_length=20),
        ),
        migrations.CreateModel(
            name='SapPosting',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('key', models.CharField(max_length=120, unique=True)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('done', 'Posted')], default='pending', max_length=10)),
                ('doc_entry', models.CharField(blank=True, max_length=40)),
                ('amount', models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True)),
                ('detail', models.JSONField(blank=True, default=dict)),
                ('attempts', models.PositiveIntegerField(default=0)),
                ('locked_until', models.DateTimeField(blank=True, null=True)),
                ('last_error', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'db_table': 'pos_backend_sap_posting',
            },
        ),
    ]
