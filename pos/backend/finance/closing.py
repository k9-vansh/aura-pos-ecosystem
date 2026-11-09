"""The day-end closing, worked out by the server (X4, decisions #77-#80).

The browser used to send every figure of the closing - system cash, expenses,
method totals, the difference - so the record said whatever the screen said.
Now only what a person *counts* is typed (cash in hand, cash deposited); the
rest is derived here from the bills and petty cash of that branch and day,
using the same definitions as the Register Summary report so the two always
agree:

    expected cash   = opening balance + cash taken that day - petty cash that day
    difference      = cash counted - expected cash
    closing balance = expected cash - cash deposited      (carried to the next day)
"""

import datetime
from decimal import Decimal

from django.db.models import Sum

from pos_backend.money import ZERO, money

# register-summary bucket -> DailyClosing field
BUCKET_FIELDS = {
    'cash': 'system_cash',
    'credit_card': 'credit_card',
    'upi': 'upi',
    'paytm': 'paytm',
    'bharat_pe': 'bharat_pe',
    'cheque_net_banking': 'cheque_netbanking',
    'google_pay': 'google_pay',
    'phone_pe': 'phone_pe',
    'nearbuy': 'nearbuy',
    'gift_card': 'gift_card',
    'other': 'other',
}

#: Fields the server owns; whatever a client sends for them is ignored.
SERVER_FIELDS = tuple(BUCKET_FIELDS.values()) + (
    'opening_balance', 'todays_expenses', 'difference', 'closing_balance')


def opening_balance_for(center_id, day):
    from finance.models import DailyClosing

    previous = (DailyClosing.objects.filter(center_id=center_id, date__lt=day)
                .order_by('-date').first())
    return money(previous.closing_balance) if previous else ZERO


def compute_day(center_id, day):
    """Every server-owned figure of the closing for `center_id` on `day`."""
    from billing.models import Invoice
    from finance.models import PettyCashEntry
    from finance.views import _compute_payment_breakdown

    start = datetime.datetime.combine(day, datetime.time.min)
    invoices = Invoice.objects.filter(
        center_id=center_id, status__in=('paid', 'partial'),
        created_at__gte=start, created_at__lt=start + datetime.timedelta(days=1))
    breakdown = _compute_payment_breakdown(invoices)

    figures = {field: money(breakdown[bucket]['amount']) for bucket, field in BUCKET_FIELDS.items()}
    figures['opening_balance'] = opening_balance_for(center_id, day)
    spent = PettyCashEntry.objects.filter(
        center_id=center_id, date__gte=start, date__lt=start + datetime.timedelta(days=1)
    ).aggregate(t=Sum('amount'))['t']
    figures['todays_expenses'] = money(spent or 0)
    figures['expected_cash'] = money(
        figures['opening_balance'] + figures['system_cash'] - figures['todays_expenses'])
    return figures


def settle(center_id, day, counted_cash, deposit):
    """The full set of server-owned fields given what was counted."""
    figures = compute_day(center_id, day)
    counted, deposit = money(counted_cash or 0), money(deposit or 0)
    figures['difference'] = money(counted - figures['expected_cash'])
    figures['closing_balance'] = money(figures['expected_cash'] - deposit)
    return figures


def shift_figures(shift):
    """Running figures of a shift (the X report; the same figures close it as the Z report)."""
    from django.db.models import Sum as _Sum

    from billing.models import Invoice
    from finance.models import PettyCashEntry
    from finance.views import _compute_payment_breakdown

    invoices = Invoice.objects.filter(center_id=shift.center_id, status__in=('paid', 'partial'),
                                      created_at__gte=shift.opened_at)
    breakdown = _compute_payment_breakdown(invoices)
    cash = money(breakdown['cash']['amount'])
    spent = PettyCashEntry.objects.filter(center_id=shift.center_id, date__gte=shift.opened_at
                                          ).aggregate(t=_Sum('amount'))['t']
    spent = money(spent or 0)
    return {
        'shift': shift.pk, 'till': shift.till, 'opened_at': shift.opened_at,
        'starting_float': money(shift.starting_float),
        'bills': invoices.count(),
        'methods': {k: money(v['amount']) for k, v in breakdown.items() if v['amount']},
        'cash_sales': cash, 'petty_cash': spent,
        'expected_cash': money(shift.starting_float + cash - spent),
    }
