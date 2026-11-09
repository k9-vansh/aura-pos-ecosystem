from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('salon_admin', '__first__'),
        ('clients', '0021_client_search_indexes'),
    ]

    operations = [
        migrations.AddField(
            model_name='client',
            name='pin_sealed',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='client',
            name='pin_set_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name='MasterPin',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date', models.DateField()),
                ('sealed', models.TextField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('center', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='master_pins',
                                             to='salon_admin.center')),
            ],
            options={'ordering': ['-date'], 'unique_together': {('center', 'date')}},
        ),
        migrations.CreateModel(
            name='PinAttempt',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('kind', models.CharField(choices=[('client', 'Client PIN'), ('master', 'Master PIN'),
                                                   ('send', 'PIN sent'), ('login', 'Client app sign-in')], max_length=10)),
                ('ok', models.BooleanField(default=False)),
                ('user_id', models.IntegerField(blank=True, null=True)),
                ('via', models.CharField(blank=True, default='', max_length=30)),
                ('at', models.DateTimeField(auto_now_add=True)),
                ('center', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                                             to='salon_admin.center')),
                ('client', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                                             related_name='pin_attempts', to='clients.client')),
            ],
            options={'ordering': ['-at']},
        ),
        migrations.AddIndex(
            model_name='pinattempt',
            index=models.Index(fields=['kind', 'client', 'at'], name='pinatt_client_idx'),
        ),
        migrations.AddIndex(
            model_name='pinattempt',
            index=models.Index(fields=['kind', 'center', 'at'], name='pinatt_center_idx'),
        ),
    ]
