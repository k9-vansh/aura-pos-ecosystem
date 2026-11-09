# Generated migration to add scope and center to ServiceMaster
from django.db import migrations, models

class Migration(migrations.Migration):

    dependencies = [
        ('services', '0001_initial'),
        ('salon_admin', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='servicemaster',
            name='scope',
            field=models.CharField(choices=[('org', 'Organisation'), ('center', 'Center Specific')], default='org', max_length=10),
        ),
        migrations.AddField(
            model_name='servicemaster',
            name='center',
            field=models.ForeignKey(blank=True, null=True, on_delete=models.SET_NULL, related_name='owned_services', to='salon_admin.center'),
        ),
    ]
