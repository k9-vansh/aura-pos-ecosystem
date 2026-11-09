from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '__first__'),
        ('owner_api', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='OwnerToken',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('key', models.CharField(max_length=40, unique=True)),
                ('fingerprint', models.CharField(blank=True, default='', max_length=32)),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('last_used', models.DateTimeField(blank=True, null=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='owner_tokens',
                                           to='accounts.customuser')),
            ],
        ),
    ]
