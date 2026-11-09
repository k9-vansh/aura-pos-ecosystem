# Generated migration to add center FK to Client
from django.db import migrations, models

class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0001_initial'),
        ('salon_admin', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='client',
            name='center',
            field=models.ForeignKey(blank=True, null=True, on_delete=models.SET_NULL, to='salon_admin.center'),
        ),
    ]
