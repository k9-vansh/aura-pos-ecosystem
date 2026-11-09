from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('finance', '0015_daily_closing_gift_card'),
    ]

    operations = [
        migrations.AddField(
            model_name='pettycashentry',
            name='bill_image',
            field=models.ImageField(blank=True, null=True, upload_to='petty_cash/'),
        ),
    ]
