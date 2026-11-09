from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    """The owner app gets accounts of its own (not POS users). The sign-ins made with POS users are dropped: everyone
    signs in again with their owner account."""

    dependencies = [
        ('salon_admin', '__first__'),
        ('owner_api', '0002_ownertoken'),
    ]

    operations = [
        migrations.DeleteModel(name='OwnerToken'),
        migrations.CreateModel(
            name='OwnerAccount',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=255)),
                ('phone', models.CharField(max_length=15, unique=True)),
                ('email', models.EmailField(blank=True, default='', max_length=254)),
                ('password', models.CharField(max_length=128)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('last_login', models.DateTimeField(blank=True, null=True)),
                ('centers', models.ManyToManyField(blank=True, related_name='owner_accounts', to='salon_admin.center')),
                ('role', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                           related_name='owner_accounts', to='salon_admin.role')),
            ],
        ),
        migrations.CreateModel(
            name='OwnerToken',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('key', models.CharField(max_length=40, unique=True)),
                ('fingerprint', models.CharField(blank=True, default='', max_length=32)),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('last_used', models.DateTimeField(blank=True, null=True)),
                ('account', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='tokens',
                                              to='owner_api.owneraccount')),
            ],
        ),
    ]
