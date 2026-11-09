"""Fill a TEST database with stock, staff, roles and one login per role, so the whole POS can be tried.

    python manage.py seed_test_team --yes

Never run it on a live database: it invents stock, people and logins. It can be run again; whatever is
already there is left alone.

  * stock     every active product with no stock gets some (a warehouse a lot, a branch a little), with an
              opening-balance row in the ledger. Those rows are marked as already in SAP so nothing is sent
              there, and the "bring SAP's stock into the POS" job is switched off, because SAP has no stock
              and would zero it again.
  * staff     a few staff at every active centre.
  * roles     one role per kind of person, made only of ticks (Roles & Permissions shows them).
  * logins    one user per role at three pilot centres (two branches and a warehouse), written to
              TEST-LOGINS.md (next to manage.py's parent folder) with the shared test password.
"""
import secrets
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Case, IntegerField, Value, When

from pos_backend.permission_tree import action_map, offered_verbs

VERBS = {'r': ('read',), 'c': ('read', 'create'), 'u': ('read', 'create', 'update'),
         'd': ('read', 'create', 'update', 'delete')}

#: role -> (description, all_centers, {module: {submodule: 'r'|'c'|'u'|'d' (+ '!' for its action ticks)}})
ROLES = {
    'Branch Stock Requester': (
        'Asks for stock for their own branch. Cannot approve, send or buy.', False,
        {'inventory': {'stock_requests': 'u', 'products': 'r', 'stock_history': 'r'}}),
    'Branch Stock Approver': (
        'Says yes or no to stock requests of their branch. Cannot make requests or send stock.', False,
        {'inventory': {'approve_stock_requests': 'u', 'stock_requests': 'r', 'products': 'r', 'stock_history': 'r'}}),
    'Store Keeper (Sends Stock)': (
        'Sends approved requests: picks where from, makes the transfer, dispatches it.', False,
        {'inventory': {'fulfill_stock_requests': 'u', 'stock_transfers': 'u', 'stock_requests': 'r',
                       'products': 'r', 'stock_history': 'r', 'checkout': 'c', 'audit': 'u'}}),
    'Inventory & Purchasing Manager': (
        'Runs stock: vendors, purchase orders, receiving goods, audits, checkout, transfers.', False,
        {'inventory': {'products': 'd', 'vendors': 'd', 'purchase_orders': 'u', 'po_history': 'r', 'checkout': 'u',
                       'stock_history': 'r', 'audit': 'u', 'stock_transfers': 'u', 'stock_requests': 'r'}}),
    'Cashier / Front Desk': (
        'Bills clients, books appointments, adds clients. No stock, finance or admin.', False,
        {'billing': {'invoices': 'c', 'new_invoice': 'c'},
         'appointments': {'calendar': 'u!'},
         'staff': {'directory': 'r'},
         'admin': {'clients': 'u'},
         'inventory': {'products': 'r'},
         'chat': {'messages': 'c'}}),
    'Branch Manager': (
        'Runs one branch: billing, appointments, staff, approves its stock requests, sees reports.', False,
        {'billing': {'invoices': 'u!', 'new_invoice': 'u'},
         'appointments': {'calendar': 'u!'},
         'staff': {'directory': 'u', 'profile': 'r', 'logs': 'u', 'reports': 'r'},
         'admin': {'clients': 'u!', 'bills': 'r', 'services': 'r'},
         'inventory': {'products': 'r', 'stock_history': 'r', 'stock_requests': 'u', 'approve_stock_requests': 'u',
                       'stock_transfers': 'u'},                  # receives what is sent to the branch
         'marketing': {'usage': 'r', 'promotions': 'r'},
         'dashboard': {'summary': 'r', 'revenues': 'r', 'clients': 'r', 'staff': 'r'},
         'finance': {'register_summary': 'r', 'pettycash': 'u', 'shifts': 'u'},
         'chat': {'messages': 'c'}}),
    'Marketing Executive': (
        'Offers, cards, memberships, packages and messages. Nothing about stock or money.', False,
        {'marketing': {'promotions': 'd', 'cards': 'd', 'memberships': 'd', 'packages': 'd', 'whatsapp': 'u!', 'usage': 'r'},
         'admin': {'clients': 'r'},
         'chat': {'messages': 'c'}}),
    'Finance & Accounts': (
        'Every finance report, petty cash and refunds; sees bills. Cannot bill or touch stock.', True,
        {'finance': {s: ('u!' if s in ('pettycash',) else 'u') for s in (
            'register_summary', 'monthly_sales', 'detailed_revenues', 'refunds', 'procurement', 'tax_report',
            'service_drilldown', 'staff_performance', 'multi_salon', 'incentives', 'pettycash', 'shifts', 'manage')},
         'dashboard': {s: 'r' for s in ('summary', 'revenues', 'clients', 'finance', 'services_products', 'staff', 'usage_report')},
         'admin': {'bills': 'r', 'sap_sync': 'r'},
         'inventory': {'po_history': 'r', 'stock_history': 'r'},
         'staff': {'payrolls': 'r', 'reports': 'r'},
         'chat': {'messages': 'c'}}),
    'Regional Manager (All Centres)': (
        'Sees and approves across every centre: requests, stock, billing, reports.', True,
        {'billing': {'invoices': 'u!', 'new_invoice': 'u'},
         'inventory': {'products': 'u', 'stock_history': 'r', 'stock_requests': 'u', 'approve_stock_requests': 'u',
                       'stock_transfers': 'u', 'po_history': 'r', 'purchase_orders': 'r'},
         'staff': {'directory': 'u', 'reports': 'r', 'logs': 'r'},
         'dashboard': {s: 'r' for s in ('summary', 'revenues', 'clients', 'finance', 'services_products', 'staff', 'usage_report')},
         'finance': {'register_summary': 'r', 'monthly_sales': 'r', 'multi_salon': 'r'},
         'admin': {'bills': 'r', 'clients': 'r', 'services': 'r', 'centers': 'r'},
         'chat': {'messages': 'c'}}),
    'Admin Assistant (Users & Services)': (
        'Looks after users, services and clients. Cannot roles, settings, SAP or money.', True,
        {'admin': {'users': 'u!', 'services': 'd', 'clients': 'u!', 'centers': 'r'},
         'chat': {'messages': 'c'}}),
}

#: (role, who, which pilot centre key or 'all')  -> login  <slug>.<centre>@test.pos
LOGINS = [
    ('Branch Stock Requester', 'requester', 'A'), ('Branch Stock Approver', 'approver', 'A'),
    ('Cashier / Front Desk', 'cashier', 'A'), ('Branch Manager', 'manager', 'A'),
    ('Branch Stock Requester', 'requester', 'B'), ('Branch Stock Approver', 'approver', 'B'),
    ('Cashier / Front Desk', 'cashier', 'B'), ('Branch Manager', 'manager', 'B'),
    ('Store Keeper (Sends Stock)', 'storekeeper', 'WAB'),   # must reach the branches whose requests it sends
    ('Inventory & Purchasing Manager', 'purchasing', 'ABW'),
    ('Marketing Executive', 'marketing', 'AB'),
    ('Finance & Accounts', 'finance', 'all'),
    ('Regional Manager (All Centres)', 'regional', 'all'),
    ('Admin Assistant (Users & Services)', 'adminhelper', 'all'),
]
PILOTS = {'A': 'Downtown Flagship', 'B': 'Metropolis Branch', 'W': 'Central Hub & Warehouse'}

STAFF = [('Stylist', 'Female', 25000, 8), ('Senior Stylist', 'Male', 38000, 10), ('Receptionist', 'Female', 20000, 0)]
FIRST = ['Asha', 'Ravi', 'Neha', 'Imran', 'Pooja', 'Karan', 'Meera', 'Arjun', 'Sana', 'Vikram', 'Divya', 'Rohit']


def permissions_for(spec, all_centers):
    out = {'all_centers': bool(all_centers)}
    actions = action_map()
    verbs = offered_verbs()
    for module, subs in spec.items():
        out[module] = {}
        for sub, level in subs.items():
            wanted = VERBS[level.rstrip('!')]
            entry = {v: True for v in wanted if v in verbs[(module, sub)]}
            entry['read'] = True
            if level.endswith('!') and (module, sub) in actions:
                entry['actions'] = {a: True for a in actions[(module, sub)]}
            out[module][sub] = entry
    return out


class Command(BaseCommand):
    help = 'TEST DATABASES ONLY: add stock, staff, roles and one login per role.'

    def add_arguments(self, parser):
        parser.add_argument('--yes', action='store_true', help='I know this invents data; run it.')
        parser.add_argument('--only', choices=['stock', 'staff', 'roles'], help='Do just one part.')
        parser.add_argument('--reset-passwords', action='store_true', help='Give the test logins a new password.')
        parser.add_argument('--out', default=None, help='Where to write the logins file.')

    def handle(self, *args, **opts):
        if not opts['yes']:
            raise CommandError('This invents stock, staff and logins. Run with --yes on a TEST database only.')
        only = opts['only']
        if only in (None, 'stock'):
            self.stock()
        if only in (None, 'staff'):
            self.staff()
        if only in (None, 'roles'):
            self.roles_and_logins(opts)

    # ── stock ──────────────────────────────────────────────────────────────
    def stock(self):
        from inventory.models import Product, StockTransaction
        from pos_backend import app_settings
        from pos_backend.models import AppSetting
        from salon_admin.models import Center

        AppSetting.objects.update_or_create(key='sap.stock_sync_minutes', scope='global', defaults={'value': 0})
        app_settings.invalidate()
        self.stdout.write('Switched off "bring SAP stock into the POS" (SAP has no stock; it would zero this).')

        total = 0
        for center in Center.objects.filter(is_active=True):
            warehouse = 'warehouse' in (center.display_name or center.center_name or '').lower()
            retail, other = (1000, 1000) if warehouse else (30, 100)
            with transaction.atomic():
                rows = list(Product.objects.filter(center=center, is_active=True, current_stock=0)
                            .values_list('id', 'category'))
                if not rows:
                    continue
                ids = [r[0] for r in rows]
                for start in range(0, len(ids), 900):
                    Product.objects.filter(id__in=ids[start:start + 900]).update(current_stock=Case(
                        When(category__iexact='retail', then=Value(retail)),
                        default=Value(other), output_field=IntegerField()))
                StockTransaction.objects.bulk_create([
                    StockTransaction(
                        product_id=pid, center=center, transaction_type='AUDIT',
                        quantity_change=retail if (cat or '').strip().lower() == 'retail' else other,
                        notes='Opening stock (test data, not posted to SAP)',
                        sap_sync_status=True, sap_sync_message='Test data - not in SAP')
                    for pid, cat in rows], batch_size=2000)
                total += len(rows)
        self.stdout.write(self.style.SUCCESS(f'Stock added to {total} product rows.'))

    # ── staff ──────────────────────────────────────────────────────────────
    def staff(self):
        from salon_admin.models import Center
        from staff.models import Designation, StaffMember

        made = 0
        for center in Center.objects.filter(is_active=True):
            if StaffMember.objects.filter(center=center).exists():
                continue
            for i, (title, gender, salary, commission) in enumerate(STAFF, start=1):
                designation, _ = Designation.objects.get_or_create(
                    name=title, defaults={'salary': salary, 'commission_percentage': commission})
                StaffMember.objects.create(
                    center=center, first_name=FIRST[(center.id + i) % len(FIRST)], last_name=f'T{center.id}-{i}',
                    gender=gender, designation=title, designation_fk=designation,
                    staff_code=f'S{center.id:03d}-{i}', phone=f'90{center.id:03d}{i:05d}',
                    salary=salary, commission_percentage=commission)
                made += 1
        self.stdout.write(self.style.SUCCESS(f'{made} staff added.'))

    # ── roles and logins ───────────────────────────────────────────────────
    def roles_and_logins(self, opts):
        from accounts.models import CustomUser
        from salon_admin.models import Center, Role

        roles = {}
        for name, (description, all_centers, spec) in ROLES.items():
            role, _ = Role.objects.update_or_create(
                name=name, defaults={'description': description, 'permissions': permissions_for(spec, all_centers)})
            roles[name] = role
        self.stdout.write(self.style.SUCCESS(f'{len(roles)} roles ready.'))

        pilots = {}
        for key, name in PILOTS.items():
            center = Center.objects.filter(display_name=name).first() or Center.objects.filter(center_name=name).first()
            if center is None:
                raise CommandError(f'No centre called "{name}" to give logins at.')
            pilots[key] = center

        password = secrets.token_urlsafe(8) + 'a1'
        lines, created = [], 0
        for role_name, who, where in LOGINS:
            if where == 'all':
                centers = list(Center.objects.filter(is_active=True))
                home, label, suffix = pilots['A'], 'all centres', 'all'
            else:
                centers = [pilots[k] for k in where]
                home = centers[0]
                label = ' + '.join(c.display_name or c.center_name for c in centers)
                suffix = where.lower()
            email = f'{who}.{suffix}@test.pos'
            user = CustomUser.objects.filter(email=email).first()
            fresh = user is None or opts['reset_passwords']
            if user is None:
                user = CustomUser.objects.create_user(
                    email=email, password=password, full_name=f'{role_name} ({label})'[:255],
                    role=roles[role_name], center=home, designation=role_name[:255])
                created += 1
            else:
                user.role = roles[role_name]
                if opts['reset_passwords']:
                    user.set_password(password)
                user.save()
            user.centers.set(centers)
            lines.append((role_name, email, label, fresh))

        out = Path(opts['out']) if opts['out'] else Path(__file__).resolve().parents[4] / 'TEST-LOGINS.md'
        body = ['# Test logins (test database only)', '',
                'One shared password for all of them, set when they were first made.' if created else
                'These logins already existed; their password is unchanged (use --reset-passwords for a new one).', '']
        if created or opts['reset_passwords']:
            body += [f'Password: `{password}`', '']
        body += ['| Role | Login | Works at |', '|---|---|---|']
        body += [f'| {r} | {e} | {w} |' for r, e, w, _ in lines]
        if created or opts['reset_passwords'] or not out.exists():
            out.write_text('\n'.join(body) + '\n', encoding='utf-8')
        self.stdout.write(self.style.SUCCESS(f'{created} logins made. Details in {out}'))
