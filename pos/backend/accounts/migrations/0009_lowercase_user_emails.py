"""
Lowercase existing user emails.

`CustomUserManager.normalize_email` now lowercases the whole address, but rows
created before that keep whatever case was typed. Leaving them mixed reproduces
exactly the bug the change was meant to remove - and it behaves differently on
the two backends:

  * on SQLite `=` is byte-exact, so a user stored as `Asha@x.com` cannot log in
    by typing `asha@x.com`;
  * on MySQL the default collation is case-insensitive, so the unique index
    treats the two as the same and rejects the second.

Collisions are reported and left alone rather than merged. Two accounts that
differ only in case are two real accounts with their own permissions and audit
history, and picking a winner automatically could hand one person another's
access.
"""

from collections import defaultdict

from django.db import migrations


def lowercase_emails(apps, schema_editor):
    CustomUser = apps.get_model('accounts', 'CustomUser')

    by_lowered = defaultdict(list)
    for user in CustomUser.objects.all().only('id', 'email'):
        if user.email:
            by_lowered[user.email.lower()].append(user)

    collisions = []
    for lowered, users in by_lowered.items():
        if len(users) > 1:
            collisions.append((lowered, [u.email for u in users]))
            continue
        user = users[0]
        if user.email != lowered:
            user.email = lowered
            user.save(update_fields=['email'])

    if collisions:
        # Printed, not raised: refusing to migrate would block every other
        # change behind a data problem only a human can settle.
        print('\n  Accounts differing only by capitalisation, left untouched:')
        for lowered, variants in collisions:
            print(f'    {lowered}: {", ".join(variants)}')
        print('  Merge or rename these by hand. Until then they cannot be '
              'stored on MySQL, whose unique index treats them as the same.')


def noop(apps, schema_editor):
    """Not reversible: the original capitalisation is not recorded anywhere."""


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0008_messagereaction_alter_chatroom_options_and_more'),
    ]

    operations = [
        migrations.RunPython(lowercase_emails, noop),
    ]
