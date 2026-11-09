"""Make (or update) an owner app account: a person with their own mobile number and password for the owner app.

    python manage.py create_owner_account --name "Aura Executive" --phone 9811122233 --all-salons
    python manage.py create_owner_account --name "Area Head" --phone 9822233344 --salons 3,5,9 --role "Owner app - read only"

Without --role the account gets the role "Owner app" (every Owner App screen ticked, Master PINs included); create or edit
roles in the POS under Admin > Roles to give different people different screens. The password is asked for when it is
not given (and is never printed).
"""
import getpass

from django.core.management.base import BaseCommand, CommandError

from owner_api.models import OwnerAccount
from salon_admin.models import Center, Role

SCREENS = ('today', 'month', 'invoices', 'staff', 'ai_report', 'settings', 'master_pins', 'ai_keys')


def default_role(all_salons):
    perms = {'owner_app': {s: {'read': True} for s in SCREENS}}
    perms['owner_app']['ai_report']['create'] = True
    perms['owner_app']['settings']['update'] = True
    perms['owner_app']['ai_keys']['update'] = True
    if all_salons:
        perms['all_centers'] = True
    role, _ = Role.objects.get_or_create(name='Owner app' + (' (all salons)' if all_salons else ''), defaults={'permissions': perms})
    return role


class Command(BaseCommand):
    help = 'Create or update an owner app account (its own phone and password, not a POS user).'

    def add_arguments(self, parser):
        parser.add_argument('--name', required=True)
        parser.add_argument('--phone', required=True, help='10-digit mobile number: the sign-in name')
        parser.add_argument('--email', default='')
        parser.add_argument('--password', default='')
        parser.add_argument('--role', default='', help='Name of an existing role (Admin > Roles)')
        parser.add_argument('--all-salons', action='store_true', help='Sees every salon')
        parser.add_argument('--salons', default='', help='Comma-separated salon ids this account sees')
        parser.add_argument('--off', action='store_true', help='Switch the account off')

    def handle(self, *args, **opts):
        phone = OwnerAccount.clean_phone(opts['phone'])
        if len(phone) != 10:
            raise CommandError('The phone must be a 10-digit mobile number.')
        account = OwnerAccount.objects.filter(phone=phone).first() or OwnerAccount(phone=phone)
        created = account.pk is None
        account.name = opts['name']
        if opts['email']:
            account.email = opts['email']
        password = opts['password']
        if not password and (created or not account.password):
            password = getpass.getpass('Password for this account: ')
        if password:
            if len(password) < 8:
                raise CommandError('Use a password of at least 8 characters.')
            account.set_password(password)
        if opts['role']:
            role = Role.objects.filter(name=opts['role']).first()
            if role is None:
                raise CommandError(f'No role called "{opts["role"]}". Create it in the POS under Admin > Roles first.')
        else:
            role = account.role or default_role(opts['all_salons'])
        account.role = role
        account.is_active = not opts['off']
        account.save()
        if opts['salons']:
            ids = [int(x) for x in opts['salons'].split(',') if x.strip()]
            found = Center.objects.filter(id__in=ids)
            if found.count() != len(set(ids)):
                raise CommandError('Some of those salon ids do not exist.')
            account.centers.set(found)
        self.stdout.write(self.style.SUCCESS(
            f'{"Made" if created else "Updated"} the owner app account of {account.name} ({account.phone}), role "{role.name}", '
            f'{"all salons" if (role.permissions or {}).get("all_centers") else f"{account.centers.count()} salon(s)"}.'))
