from django.db import migrations

# A fresh system used to sell at any price and give any discount with no
# approval, because a kind with no policy row needs none. Switch the two
# till-side controls on for a database that has never been configured; a
# database that already holds a row for the kind keeps its owner's choice.
SWITCHED_ON = ('discount', 'price_change')


def switch_on(apps, schema_editor):
    Policy = apps.get_model('approvals', 'ApprovalPolicy')
    for kind in SWITCHED_ON:
        Policy.objects.get_or_create(kind=kind, defaults={'enabled': True})


class Migration(migrations.Migration):

    dependencies = [
        ('approvals', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(switch_on, migrations.RunPython.noop),
    ]
