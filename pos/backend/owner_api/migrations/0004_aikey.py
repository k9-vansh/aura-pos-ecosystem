from django.db import migrations, models


class Migration(migrations.Migration):
    """The AI keys the business pastes in the owner app (sealed), one row per AI service."""

    dependencies = [
        ('owner_api', '0003_owner_accounts'),
    ]

    operations = [
        migrations.CreateModel(
            name='AiKey',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('provider', models.CharField(max_length=20, unique=True)),
                ('sealed_key', models.TextField()),
                ('last4', models.CharField(blank=True, default='', max_length=4)),
                ('model', models.CharField(blank=True, default='', max_length=120)),
                ('base_url', models.CharField(blank=True, default='', max_length=300)),
                ('is_primary', models.BooleanField(default=False)),
                ('updated_by', models.CharField(blank=True, default='', max_length=255)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
        ),
    ]
