"""Admin > Settings: every business value the POS uses, in one place.

Owner decision: nothing business-related is hard-coded. Each setting below is
read by real code - a setting appears here only once something obeys it, so the
screen never offers a value that does nothing.

Where a value comes from, first match wins:
  1. a branch override saved in Admin > Settings (branch-scoped settings only),
  2. the global value saved in Admin > Settings,
  3. the server's Django setting of the same meaning (the old environment
     variables - so an unchanged system behaves exactly as before),
  4. the default below.

Reading:   app_settings.get('wallets.scope')
           app_settings.get('billing.discount_audit_percent', center=invoice.center)
"""

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.conf import settings as dj_settings
from django.core.cache import cache

CACHE_KEY = 'pos:app_settings:v1'
CACHE_SECONDS = 300

GLOBAL_ONLY = ('global',)
GLOBAL_AND_BRANCH = ('global', 'branch')


@dataclass(frozen=True)
class Spec:
    key: str
    group: str
    label: str
    help: str
    type: str                      # 'bool' | 'int' | 'decimal' | 'choice'
    default: object
    scopes: tuple = GLOBAL_ONLY
    choices: tuple = ()            # ((value, label), ...) for 'choice'
    min: object = None
    max: object = None
    django_setting: str = ''       # legacy fallback (step 3 above)
    unit: str = ''
    extra: dict = field(default_factory=dict)


SPECS = [
    Spec(
        key='billing.discount_audit_percent', group='Billing',
        label='Log line discounts from (%)',
        help='A bill line discounted by this percentage or more is written to the '
             "bill's change log so it shows in the audit reports.",
        type='int', default=50, min=1, max=100, unit='%',
        django_setting='DISCOUNT_AUDIT_PERCENT', scopes=GLOBAL_AND_BRANCH,
    ),
    Spec(
        key='billing.package_gst_percent', group='Billing',
        label='GST on package sales (%)',
        help='Tax charged when a prepaid package is sold. Until packages carry '
             'their own GST rate (Marketing, a later phase) this applies to all.',
        type='decimal', default=Decimal('0'), min=Decimal('0'), max=Decimal('28'), unit='%',
        django_setting='PACKAGE_GST_PERCENT',
    ),
    Spec(
        key='billing.payment_change_hours', group='Billing',
        label='Payment method can be changed for',
        help='How long after a bill is completed its payment method may still be changed, '
             'by someone with the "Change payment method" tick (decision #41).',
        type='int', default=6, min=0, max=72, unit='h', scopes=GLOBAL_AND_BRANCH,
    ),
    Spec(
        key='billing.payment_methods', group='Billing',
        label='Payment methods at the till',
        help='The ways a bill can be paid at this branch, comma separated, from: '
             'Cash, UPI, Card, Debit Card, Cheque, Net Banking, Google Pay, PhonePe, Paytm, '
             'BharatPe, Other. Advance, cashback and value cards are always offered '
             'when the client has a balance.',
        type='text', default='Cash, UPI, Card, Cheque', scopes=GLOBAL_AND_BRANCH,
        extra={'pattern': r'^\s*(Cash|UPI|Card|Credit Card|Debit Card|Cheque|Net Banking|Google Pay'
                          r'|PhonePe|Paytm|BharatPe|Other)(\s*,\s*(Cash|UPI|Card|Credit Card|Debit Card'
                          r'|Cheque|Net Banking|Google Pay|PhonePe|Paytm|BharatPe|Other))*\s*$',
               'message': 'must be a comma-separated list of the payment methods named above'},
    ),
    Spec(
        key='billing.receipt_size', group='Billing',
        label='Receipt paper',
        help='Auto fits the receipt to the printer\'s paper; or fix it to 80 mm, 58 mm or A4.',
        type='choice', default='80mm', scopes=GLOBAL_AND_BRANCH,
        choices=(('auto', 'Auto'), ('80mm', '80 mm roll'), ('58mm', '58 mm roll'), ('a4', 'A4 page')),
    ),
    Spec(
        key='billing.receipt_footer', group='Billing',
        label='Receipt footer',
        help='A line printed at the bottom of every receipt, e.g. "Thank you, visit again".',
        type='text', default='Thank you for visiting!', scopes=GLOBAL_AND_BRANCH,
        extra={'pattern': r'^[^<>]{0,160}$', 'message': 'up to 160 characters, without < or >'},
    ),
    Spec(
        key='sap.master_sync_hours', group='SAP mapping',
        label='Refresh vendors, items and consumable lists from SAP every (hours)',
        help='A background job brings new SAP vendors, items and service consumables into the POS '
             'on this schedule. 0 (default) = off until the SAP team has checked it. It never '
             'overwrites a branch\'s stock, names or prices.',
        type='int', default=0, min=0, max=168, unit='h',
    ),
    Spec(
        key='sap.stock_sync_minutes', group='SAP mapping',
        label='Bring stock changed in SAP into the POS every (minutes)',
        help='A background job compares SAP\'s stock with each branch and applies what was added '
             'or removed in SAP (a receipt, issue, count or transfer made there). Movements the POS '
             'has not posted yet are not counted as differences. 0 = off.',
        type='int', default=10, min=0, max=1440, unit='min',
    ),
    Spec(
        key='approvals.discount_free_percent', group='Approvals',
        label='Discount allowed without approval (%)',
        help='When the Discount approval is switched on (Admin > Approvals), a discount typed at '
             'the till above this share of the bill needs an approver. 0 = every typed discount.',
        type='decimal', default=Decimal('0'), min=Decimal('0'), max=Decimal('100'), unit='%',
        scopes=GLOBAL_AND_BRANCH,
    ),
    Spec(
        key='approvals.price_change_free_percent', group='Approvals',
        label='Price change allowed without approval (%)',
        help='When the Price change approval is on, a line sold above or below its list price by '
             'more than this share needs an approver. 0 = every change.',
        type='decimal', default=Decimal('0'), min=Decimal('0'), max=Decimal('100'), unit='%',
        scopes=GLOBAL_AND_BRANCH,
    ),
    Spec(
        key='approvals.valid_minutes', group='Approvals',
        label='An approval can be used for',
        help='After an approver approves, the cashier has this long to finish the bill with it.',
        type='int', default=60, min=5, max=1440, unit='min',
    ),
    Spec(
        key='approvals.request_minutes', group='Approvals',
        label='An unanswered request lapses after',
        help='A request nobody decides is closed after this long; the cashier asks again.',
        type='int', default=30, min=5, max=1440, unit='min',
    ),
    Spec(
        key='approvals.link_minutes', group='Approvals',
        label='Approve / reject link works for',
        help='The link in the approver\'s e-mail or WhatsApp message works once, for this long.',
        type='int', default=15, min=5, max=240, unit='min',
    ),
    Spec(
        key='approvals.stock_link_days', group='Approvals',
        label='Stock request approve / reject link works for',
        help='A stock request waits for a manager, so the link in the approver\'s e-mail or '
             'WhatsApp message lives much longer than a till approval: it works once, for this many days.',
        type='int', default=3, min=1, max=30, unit='days',
    ),
    Spec(
        key='reminders.expiry_days', group='Client reminders',
        label='Remind clients before expiry (days)',
        help='A client is messaged this many days before a value card, membership, package or '
             'cashback expires. 0 switches the reminder off.',
        type='int', default=30, min=0, max=180, unit='days',
    ),
    Spec(
        key='reminders.appointment_hours', group='Client reminders',
        label='Remind clients before an appointment (hours)',
        help='A client is messaged this many hours before a booked appointment. 0 (default) '
             'switches the reminder off.',
        type='int', default=0, min=0, max=72, unit='h', scopes=GLOBAL_AND_BRANCH,
    ),
    Spec(
        key='perks.selling_branch_only', group='Wallets & perks',
        label='Value cards, packages and memberships are used only at the branch that sold them',
        help='Decisions #32 and #36. Advance and cashback follow the setting above.',
        type='bool', default=True,
    ),
    Spec(
        key='billing.require_client', group='Billing',
        label='Every bill needs a client',
        help='On (decision F3, no anonymous bills): a bill cannot be completed without a client '
             'on it, so the till\'s Walk-in Client button is refused at completion. Off: a bill '
             'may be completed for a walk-in with no client record.',
        type='bool', default=False,
    ),
    Spec(
        key='billing.sell_products_without_mrp', group='Billing',
        label='Sell products that have no MRP',
        help='On: a product with no MRP is sold at its purchase price (the MRP column is empty '
             'for most products today). Turn this off once the MRP is loaded, and a product '
             'with no MRP can no longer be billed.',
        type='bool', default=True,
    ),
    Spec(
        key='billing.gst_on_advance', group='Wallets & perks',
        label='Charge GST when an advance is taken',
        help="On (the SAP team's advance scenarios): the GST chosen at the till is charged on the "
             "advance, the client holds the whole amount paid (Rs 20,000 + Rs 1,000 GST = Rs 21,000 "
             "to spend), and spending it releases that GST. Off (decisions #34 and F8): no GST when "
             "an advance is paid in, the normal GST when it is spent.",
        type='bool', default=True,
    ),
    Spec(
        key='billing.require_client_pin', group='Wallets & perks',
        label="Ask for the client's PIN before their money is used",
        help="On: an advance, value card or cashback is spent only after the client says their 4-digit PIN (the one "
             "sent to them when they bought it and shown in their app), or the person at the till enters the "
             "branch's master PIN for the day. Off: no PIN is asked.",
        type='bool', default=True, scopes=GLOBAL_AND_BRANCH,
    ),
    Spec(
        key='billing.pin_for_everyone', group='Wallets & perks',
        label='Hold back clients who have no PIN yet',
        help="Off: a client with no PIN yet (they bought before PINs) is let through, and their next purchase of a "
             "card, membership, package or advance gives them one. On: they too need the master PIN until then.",
        type='bool', default=False, scopes=GLOBAL_AND_BRANCH,
    ),
    Spec(
        key='wallets.scope', group='Wallets & perks',
        label='Advance and cashback can be used at',
        help='Whether a client\'s advance and cashback balance can be spent at any '
             'branch or only at the branch where it was paid in (decision #36: any).',
        type='choice', default='global',
        choices=(('global', 'Any branch'), ('center', 'Only the branch it came from')),
        django_setting='WALLET_SCOPE',
    ),
    Spec(
        key='staff.package_redemption_earns_commission', group='Staff & incentives',
        label='Package redemptions earn commission',
        help='Whether the staff member who performs a service redeemed from a '
             'prepaid package earns commission on it (decision #71: no).',
        type='bool', default=False,
        django_setting='PACKAGE_REDEMPTION_EARNS_COMMISSION',
    ),
    Spec(
        key='apps.client_url', group='Clients',
        label='Address of the client app',
        help='The web address clients sign in at. It is put in their welcome e-mail and WhatsApp message with their PIN. '
             'Leave empty until the client app is hosted (on a development machine it falls back to localhost).',
        type='text', default='',
    ),
    Spec(
        key='apps.staff_url', group='Clients',
        label='Address of the staff app',
        help='The web address the team signs in at. It is put in the e-mail they get with their PIN. '
             'Leave empty until the staff app is hosted (on a development machine it falls back to localhost).',
        type='text', default='',
    ),
    Spec(
        key='clients.welcome_email', group='Clients',
        label='E-mail new clients a welcome',
        help='Sends a short welcome e-mail, signed with the branch name, when a client '
             'with an e-mail address is registered.',
        type='bool', default=True,
    ),
    Spec(
        key='appointments.open_time', group='Appointments',
        label='Bookings from (HH:MM)',
        help='The earliest time a service may start at this branch (decision #20).',
        type='text', default='09:00', scopes=GLOBAL_AND_BRANCH,
        extra={'pattern': r'^([01]\d|2[0-3]):[0-5]\d$'},
    ),
    Spec(
        key='appointments.close_time', group='Appointments',
        label='Bookings until (HH:MM)',
        help='Every service must end by this time at this branch.',
        type='text', default='22:00', scopes=GLOBAL_AND_BRANCH,
        extra={'pattern': r'^(([01]\d|2[0-3]):[0-5]\d|24:00)$'},
    ),
    Spec(
        key='appointments.closed_days', group='Appointments',
        label='Closed on',
        help='Weekdays the branch takes no bookings, comma separated (e.g. "Tue" or '
             '"Sat,Sun"). Leave empty when open every day.',
        type='text', default='', scopes=GLOBAL_AND_BRANCH,
        extra={'pattern': r'^((Mon|Tue|Wed|Thu|Fri|Sat|Sun)(\s*,\s*(Mon|Tue|Wed|Thu|Fri|Sat|Sun))*)?$'},
    ),
    Spec(
        key='appointments.slot_minutes', group='Appointments',
        label='Calendar slot size',
        help='The step of the booking calendar.',
        type='int', default=15, min=5, max=60, unit='min', scopes=GLOBAL_AND_BRANCH,
    ),
    Spec(
        key='appointments.default_duration', group='Appointments',
        label='Default service duration',
        help='Used when a service in the catalogue has no duration of its own.',
        type='int', default=45, min=5, max=480, unit='min', scopes=GLOBAL_AND_BRANCH,
    ),
    Spec(
        key='privacy.log_ip_geolocation', group='Privacy & logs',
        label='Show the location of IP addresses in the activity log',
        help='Looks up the city and country of each sign-in IP with an outside '
             'service. Off keeps every IP address on your own server.',
        type='bool', default=False,
        django_setting='AUDIT_LOG_GEO_LOOKUP',
    ),
]


# ─── SAP mapping ──────────────────────────────────────────────────────────────
# Every account and code the SAP postings use. They were written into
# sap_utils.py (33 literal uses of six accounts behind 13 settings names); the
# defaults below are exactly those values, so nothing posts differently until
# someone changes a value here. One SAP company serves every branch, so these
# are global.

_ACCOUNT = r'^[A-Za-z0-9._-]{1,30}$'


def _sap_account(key, label, help, default, django_setting):
    return Spec(key=key, group='SAP mapping', label=label, help=help, type='text',
                default=default, django_setting=django_setting,
                extra={'pattern': _ACCOUNT})


SPECS += [
    _sap_account('sap.account.cash', 'Cash account',
                 'Where cash taken at the till is posted (incoming payments, advances and '
                 'card / membership / package sales): the Bank A/C.',
                 '1110100007', 'SAP_CASH_ACCOUNT'),
    _sap_account('sap.account.upi', 'UPI parking account',
                 'Where UPI, Google Pay, PhonePe, Paytm and BharatPe payments are posted '
                 'until they reach the bank (Paytm Virtual Parking A/c). The next-day '
                 'settlement journal moves them to the UPI bank account below.',
                 '1110100008', 'SAP_UPI_ACCOUNT'),
    _sap_account('sap.account.upi_bank', 'UPI bank account',
                 'The bank account UPI money lands in; debited by the settlement journal '
                 'that clears the parking account (Paytm Bank A/c).',
                 '1110100009', 'SAP_UPI_BANK_ACCOUNT'),
    _sap_account('sap.account.petty_cash', 'Petty cash paid-from account',
                 'The account petty cash expenses are credited to (Cash in Hand). The '
                 'expense side is the SAP account of the category picked on the entry.',
                 '1110200001', 'SAP_PETTY_CASH_ACCOUNT'),
    _sap_account('sap.account.transfer_default', 'Other online payments account',
                 'Where card and other non-cash, non-UPI payments are posted.',
                 '1110100009', 'SAP_DEFAULT_TRANSFER_ACCOUNT'),
    _sap_account('sap.account.virtual_clearing', 'Card / membership / package sale clearing',
                 'Debited when a value card, membership or package is sold. The UPI parking '
                 'account, so the settlement journal clears it with the rest of the UPI money.',
                 '1110100008', 'SAP_VIRTUAL_CLEARING_ACCOUNT'),
    Spec(key='sap.upi_settlement_days', group='SAP mapping',
         label='UPI reaches the bank after (days)',
         help='A background job posts one journal per branch per day, Dr UPI bank / Cr UPI '
              'parking, this many days after the payments were taken. 0 = off (the money '
              'stays in the parking account until it is moved by hand).',
         type='int', default=1, min=0, max=14, unit='d'),
    Spec(key='sap.upi_settlement_minutes', group='SAP mapping',
         label='UPI reaches the bank after (minutes, demo)',
         help='Demo override. Above 0, UPI payments are moved from parking to the bank this '
              'many minutes after they were taken, instead of after the days above. '
              '0 = use the days setting.',
         type='int', default=0, min=0, max=1440, unit='min'),
    _sap_account('sap.account.advance_liability', 'Advance from customer',
                 'Liability credited when a client pays an advance, and used when it is spent.',
                 '2320100001', 'SAP_ADVANCE_LIABILITY_ACCOUNT'),
    _sap_account('sap.account.valuecard_liability', 'Value card liability',
                 'Liability for value-card balances (sale and redemption journals).',
                 '2320100001', 'SAP_VALUECARD_LIABILITY_ACCOUNT'),
    _sap_account('sap.account.membership_liability', 'Membership liability',
                 'Liability credited when a membership is sold.',
                 '2320100001', 'SAP_MEMBERSHIP_LIABILITY_ACCOUNT'),
    _sap_account('sap.account.package_liability', 'Package liability',
                 'Liability credited when a package is sold.',
                 '2320100001', 'SAP_PACKAGE_LIABILITY_ACCOUNT'),
    _sap_account('sap.account.cashback_liability', 'Cashback account',
                 'The one account a cashback-wallet payment is posted to, when the customer spends '
                 'cashback on a bill (nothing is posted when it is earned). Use a cashback / discount '
                 'EXPENSE account. The default is the advance-from-customer account, which cashback '
                 'was never credited to, so change it to your own account.',
                 '2320100001', 'SAP_CASHBACK_LIABILITY_ACCOUNT'),
    _sap_account('sap.account.cgst_advance_5', 'Output CGST on advances (up to 5%)',
                 'CGST half of the GST on advances, cards, memberships and packages taxed below 12%.',
                 '2341100024', 'SAP_CGST_5_ADVANCE_ACCOUNT'),
    _sap_account('sap.account.sgst_advance_5', 'Output SGST on advances (up to 5%)',
                 'SGST half of the GST on advances, cards, memberships and packages taxed below 12%.',
                 '2341100025', 'SAP_SGST_5_ADVANCE_ACCOUNT'),
    _sap_account('sap.account.cgst_advance_18', 'Output CGST on advances (12% and above)',
                 'Today the same account as the 5% one; set the 18% account here when the SAP team creates it.',
                 '2341100024', 'SAP_CGST_18_ADVANCE_ACCOUNT'),
    _sap_account('sap.account.sgst_advance_18', 'Output SGST on advances (12% and above)',
                 'Today the same account as the 5% one; set the 18% account here when the SAP team creates it.',
                 '2341100025', 'SAP_SGST_18_ADVANCE_ACCOUNT'),
    _sap_account('sap.account.debtor', 'Debtor account for new B2B clients',
                 'Control account given to a business partner created for a client with a GSTIN.',
                 '1160200001', 'SAP_DEBTOR_ACCOUNT'),
    Spec(key='sap.account.sales_returns', group='SAP mapping', label='Sales returns account',
         help='Credited when a bill is cancelled (A/R credit memo). Leave empty to credit the '
              'same revenue account the sale was posted to.',
         type='text', default='', django_setting='SAP_SALES_RETURNS_ACCOUNT',
         extra={'pattern': r'^[A-Za-z0-9._-]{0,30}$'}),
    Spec(key='sap.account.valuecard_forfeiture_income', group='SAP mapping',
         label='Forfeited value-card income',
         help="Credited with the pre-tax value of a value card's unused balance when it "
              'expires (F7). Leave empty and forfeitures wait, unposted, until it is set.',
         type='text', default='', extra={'pattern': r'^[A-Za-z0-9._-]{0,30}$'}),
    Spec(key='sap.b2c_card_code', group='SAP mapping', label='Business partner for B2C bills',
         help='Every bill for a client without a GSTIN (and every walk-in) is posted to this '
              'one business partner. A client with a GSTIN gets a partner of their own.',
         type='text', default='B2C', django_setting='SAP_B2C_CARD_CODE',
         extra={'pattern': _ACCOUNT}),
    Spec(key='sap.items_group_code', group='SAP mapping', label='Item group for new items',
         help='SAP item group given to products and cards the POS creates in SAP.',
         type='int', default=121, min=1, django_setting='SAP_ITEMS_GROUP_CODE'),
    Spec(key='sap.price_list', group='SAP mapping', label='Price list the POS writes',
         help='SAP price list that receives a product\'s selling price.',
         type='int', default=1, min=1, django_setting='SAP_PRICE_LIST'),
    Spec(key='sap.default_location', group='SAP mapping', label='Warehouse location fallback',
         help='SAP warehouse location used for a new branch warehouse when its region '
              'matches no location.',
         type='int', default=2, min=1, django_setting='SAP_DEFAULT_LOCATION'),
    Spec(key='sap.post_timeout_seconds', group='SAP mapping', label='Wait for SAP when posting a document (seconds)',
         help='How long the worker waits for SAP to answer when it posts a bill, payment, credit memo, '
              'receipt or transfer. A bill with many lines can take SAP longer than 15 seconds; a post '
              'that times out is checked in SAP before it is sent again, so it is never posted twice.',
         type='int', default=60, min=10, max=300, unit='s'),
    Spec(key='sap.transfer_price_basis', group='SAP mapping', label='Value of stock sent to another branch',
         help='Stock sent between branches with different GSTINs goes with a tax invoice (A/R at the sender, A/P at '
              'the receiver). This is what one unit is invoiced at. Cost = what SAP says the item costs in the '
              'sending warehouse (the usual basis for a branch transfer). A value of zero cannot be invoiced.',
         type='choice', default='cost',
         choices=(('cost', 'SAP cost of the item in the sending warehouse'),
                  ('product_price', 'The product price in the POS'))),
    Spec(key='sap.auto_recovery', group='SAP mapping', label='Re-send what SAP missed, by itself',
         help='Every few minutes the worker looks for (1) bills posted to SAP whose payment SAP never '
              'got, and (2) postings that gave up only because SAP was slow or unreachable, and sends '
              'them again. Each is checked against SAP first, so nothing is posted twice. Off = they '
              'wait for someone to retry them in Admin > SAP Sync.',
         type='bool', default=True),
    Spec(key='sap.b2b_customer_group', group='SAP mapping', label='Customer group for clients with a GSTIN',
         help='SAP customer group given to the partner created for a client with a GSTIN (118 = Trade '
              'Receivables-General). Without it SAP puts the partner in its first group.',
         type='int', default=118, min=100, max=9999),
    Spec(key='sap.vendor_group', group='SAP mapping', label='Vendor group for new vendors',
         help='SAP vendor group given to a vendor created in the POS (111 = Trade Creditors-Goods). '
              'Without it SAP puts the vendor in its first group.',
         type='int', default=111, min=100, max=9999),
    Spec(key='sap.quantity_decimals', group='SAP mapping', label='Decimals SAP keeps in a quantity',
         help='Administration > General Settings > Decimals > Quantity in SAP. A recipe that works out to '
              'more decimals than this (10 ml of a 300 ml bottle = 0.033) is rounded to it, so the POS stock '
              'and the SAP stock take out the same number.',
         type='int', default=2, min=0, max=6),
]

# One account per way of paying (Q18). Empty means the account above that
# covers the method today: cash -> Cash account; UPI, Google Pay, PhonePe,
# Paytm, BharatPe -> UPI account; everything else -> Other online payments.
PAYMENT_ACCOUNT_METHODS = (
    'Cash', 'UPI', 'Google Pay', 'PhonePe', 'Paytm', 'BharatPe', 'Credit Card',
    'Debit Card', 'Cheque', 'Net Banking', 'Gift Card', 'Other',
)


def payment_account_key(method):
    return 'sap.account.method.' + method.lower().replace(' ', '_')


_PAYMENT_ACCOUNT_HELP = {
    'Gift Card': 'Where a partner brand\'s coupon is posted: the account for what the brand owes the '
                 'business (money to receive, not cash). Empty: Other online payments.',
}

SPECS += [
    Spec(key=payment_account_key(m), group='SAP mapping', label=f'{m} payments account',
         help=_PAYMENT_ACCOUNT_HELP.get(m, f'Where {m} payments are posted. Empty: the account above that covers it.'),
         type='text', default='', extra={'pattern': r'^[A-Za-z0-9._-]{0,30}$'})
    for m in PAYMENT_ACCOUNT_METHODS
]

BY_KEY = {s.key: s for s in SPECS}


class SettingError(ValueError):
    pass


# ─── Values ───────────────────────────────────────────────────────────────────

def coerce(spec, raw):
    """Validate and convert `raw` to the setting's type. Raises SettingError."""
    try:
        if spec.type == 'bool':
            if isinstance(raw, bool):
                value = raw
            elif str(raw).strip().lower() in ('1', 'true', 'yes', 'on'):
                value = True
            elif str(raw).strip().lower() in ('0', 'false', 'no', 'off', ''):
                value = False
            else:
                raise SettingError('must be on or off')
        elif spec.type == 'int':
            if isinstance(raw, bool):
                raise SettingError('must be a whole number')
            value = int(str(raw).strip())
        elif spec.type == 'decimal':
            value = Decimal(str(raw).strip())
            if not value.is_finite():
                raise SettingError('must be a number')
        elif spec.type == 'text':
            value = str(raw).strip()
            pattern = spec.extra.get('pattern')
            if pattern and not re.fullmatch(pattern, value):
                raise SettingError(spec.extra.get('message')
                                   or 'may use only letters, digits, dot, dash and underscore '
                                      '(up to 30 characters)')
        elif spec.type == 'choice':
            value = str(raw).strip().lower()
            if value not in {c[0] for c in spec.choices}:
                raise SettingError('must be one of: ' + ', '.join(c[0] for c in spec.choices))
        else:
            raise SettingError(f'unknown type {spec.type}')
    except (TypeError, ValueError, InvalidOperation) as exc:
        if isinstance(exc, SettingError):
            raise
        raise SettingError(f'must be a valid {spec.type}') from exc

    if spec.min is not None and value < spec.min:
        raise SettingError(f'must be at least {spec.min}')
    if spec.max is not None and value > spec.max:
        raise SettingError(f'must be at most {spec.max}')
    return value


def to_json(spec, value):
    return str(value) if spec.type == 'decimal' else value


def _rows():
    """{(key, scope): stored_value}, cached and invalidated on every save."""
    rows = cache.get(CACHE_KEY)
    if rows is None:
        from pos_backend.models import AppSetting
        rows = {(r.key, r.scope): r.value
                for r in AppSetting.objects.only('key', 'scope', 'value')}
        cache.set(CACHE_KEY, rows, CACHE_SECONDS)
    return rows


def invalidate():
    cache.delete(CACHE_KEY)


def branch_scope(center):
    center_id = getattr(center, 'pk', center)
    return f'center:{int(center_id)}'


def fallback(spec):
    """Step 3/4: the server setting, else the default."""
    if spec.django_setting and hasattr(dj_settings, spec.django_setting):
        try:
            return coerce(spec, getattr(dj_settings, spec.django_setting))
        except SettingError:
            pass
    return spec.default


def get(key, center=None):
    """The effective value of setting `key` (optionally for a branch)."""
    spec = BY_KEY[key]
    try:
        rows = _rows()
    except Exception:
        # The table may not exist yet (first migrate) - behave as before.
        return fallback(spec)

    if center is not None and 'branch' in spec.scopes:
        stored = rows.get((key, branch_scope(center)))
        if stored is not None:
            try:
                return coerce(spec, stored)
            except SettingError:
                pass
    stored = rows.get((key, 'global'))
    if stored is not None:
        try:
            return coerce(spec, stored)
        except SettingError:
            pass
    return fallback(spec)
