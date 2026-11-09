from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('salon_admin', '__first__'),
    ]

    operations = [
        migrations.CreateModel(
            name='OwnerWeeklyReport',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('week_start', models.DateField()),
                ('week_end', models.DateField()),
                ('numbers', models.JSONField(default=dict)),
                ('narrative', models.JSONField(blank=True, default=dict)),
                ('model_name', models.CharField(blank=True, default='', max_length=120)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('center', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='owner_reports',
                                             to='salon_admin.center')),
            ],
            options={
                'ordering': ['-week_start'],
                'unique_together': {('center', 'week_start')},
            },
        ),
    ]
