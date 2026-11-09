from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('finance', '0013_repair_rule_tiers'),
    ]

    operations = [
        migrations.AddField(
            model_name='pettycashcategory',
            name='sap_account',
            field=models.CharField(blank=True, default='', max_length=30,
                                   help_text='SAP expense account the entries of this category are debited to.'),
        ),
        migrations.AddField(
            model_name='pettycashentry',
            name='sap_sync_status',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='pettycashentry',
            name='sap_sync_message',
            field=models.TextField(blank=True, null=True),
        ),
    ]
