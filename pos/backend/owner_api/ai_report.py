"""The weekly report: the week's figures, then a short written reading of them by an AI model.

The figures come from the POS (`queries`), the model only writes about the figures it is handed and is told not
to invent any. With no key set the figures are still saved and the report says the written part is not set up.

Which AI writes it: the key the owner pasted in the owner app (Settings > AI keys, any service in `ai_providers`: the one
in use first, the others as a fallback). A server that never had one pasted still uses OPENROUTER_API_KEY (and
OWNER_AI_MODEL) from backend .env.
"""
import calendar
import datetime
import json
import logging
from decimal import Decimal

from . import ai_providers, queries
from .models import OwnerWeeklyReport

logger = logging.getLogger(__name__)

WEEK_DAYS = ['Fri', 'Sat', 'Sun', 'Mon', 'Tue', 'Wed', 'Thu']


def week_of(today=None, back=0):
    """The Friday-to-Thursday week that last finished (`back` weeks earlier): (start, end)."""
    today = today or datetime.date.today()
    end = today - datetime.timedelta(days=(today.weekday() - 3) % 7 or 7)      # the latest Thursday before today
    end -= datetime.timedelta(days=7 * back)
    return end - datetime.timedelta(days=6), end


def _weekly_target(cids, start, end):
    """The monthly targets of the days of the week, taken pro rata."""
    total = Decimal('0')
    for n in range(7):
        day = start + datetime.timedelta(days=n)
        total += queries.month_target(cids, day.year, day.month) / calendar.monthrange(day.year, day.month)[1]
    return total


def week_numbers(user, cids, center_id, start, end):
    """Every figure of a report, for the week start..end (end is the Thursday)."""
    now = queries.sales_block(user, cids, center_id, start, end)
    p_start, p_end = start - datetime.timedelta(days=7), end - datetime.timedelta(days=7)
    before = queries.sales_block(user, cids, center_id, p_start, p_end)
    target = _weekly_target(cids, start, end)

    weeks = []
    for k in range(7, -1, -1):
        s, e = start - datetime.timedelta(days=7 * k), end - datetime.timedelta(days=7 * k)
        sales = now['sales_ex_tax'] if k == 0 else (before['sales_ex_tax'] if k == 1
                                                      else queries.sales_block(user, cids, center_id, s, e)['sales_ex_tax'])
        weeks.append({'start': s.isoformat(), 'sales': sales,
                      'target': queries.num(_weekly_target(cids, s, e))})

    per_day = {r['d']: r for r in queries.invoices_for(cids, start, end).annotate(d=queries.TruncDate('created_at'))
               .values('d').annotate(sub=queries.Sum('subtotal'), disc=queries.Sum('discount'))}
    rhythm = []
    for n in range(7):
        day = start + datetime.timedelta(days=n)
        row = per_day.get(day)
        rhythm.append({'day': WEEK_DAYS[n], 'date': day.isoformat(),
                       'sales': queries.num((row['sub'] or 0) - (row['disc'] or 0)) if row else 0.0})

    from billing.models import InvoiceItem
    top_services = [{'name': r['description'], 'count': int(r['n'] or 0), 'revenue': queries.num(r['amount'])}
                    for r in (InvoiceItem.objects
                              .filter(invoice__in=queries.invoices_for(cids, start, end), is_redemption=False,
                                      content_type__model='servicemaster')
                              .values('description').annotate(n=queries.Sum('quantity'), amount=queries.LINE_VALUE)
                              .order_by('-amount')[:5])]

    def change(a, b):
        return round((a - b) / b * 100, 1) if b else None

    bills, avg = now['bills'], (now['sales_incl_tax'] / now['bills'] if now['bills'] else 0)
    return {
        'week': {'start': start.isoformat(), 'end': end.isoformat()},
        'sales': now['sales_ex_tax'], 'sales_before': before['sales_ex_tax'],
        'sales_change_pct': change(now['sales_ex_tax'], before['sales_ex_tax']),
        'target': queries.num(target), 'target_pct': round(now['sales_ex_tax'] / float(target) * 100) if target else None,
        'work_delivered': round(now['breakdown']['services']['amount'] + now['breakdown']['products']['amount'], 2),
        'work_delivered_before': round(before['breakdown']['services']['amount'] + before['breakdown']['products']['amount'], 2),
        'bills': bills, 'bills_before': before['bills'], 'bills_change_pct': change(bills, before['bills']),
        'clients': now['clients'], 'clients_before': before['clients'],
        'avg_bill': round(avg, 2),
        'avg_bill_before': round(before['sales_incl_tax'] / before['bills'], 2) if before['bills'] else 0,
        'breakdown': {k: {**v, 'before': before['breakdown'][k]['amount'],
                          'change_pct': change(v['amount'], before['breakdown'][k]['amount'])}
                      for k, v in now['breakdown'].items()},
        'payments': now['payments'], 'redemptions': now['redemptions'],
        'weeks': weeks, 'rhythm': rhythm, 'top_services': top_services,
        'top_staff': queries.top_staff(cids, start, end, 5),
    }


PROMPT = """You are writing the weekly business reading for the owner of a hair and beauty salon in India.
You are given the salon's figures for one week (Friday to Thursday) as JSON, with the week before for comparison.
Money is in rupees, ex-tax unless a name says otherwise.

Write ONLY about the figures given. Never invent a number, a reason, a promotion or an event. If something
cannot be known from the figures, do not say it. Prefer plain words. Use lakh / crore style for large rupee
amounts (for example 9.08 lakh). Do not use markdown.

Answer with one JSON object and nothing else:
{
  "headline": "one short sentence, the single most important thing about the week",
  "summary": "two or three sentences: how the week went against the week before and against target",
  "bullets": ["3 to 5 short observations, each backed by a figure from the data: what grew, what fell, which days or services carried the week"],
  "watch": ["0 to 3 short things the owner should look at next week, each tied to a figure"]
}"""


def write_narrative(numbers, salon_name):
    """(narrative dict, model name). Never raises: a missing key or a failed call is told in the narrative."""
    keys = ai_providers.saved()
    if not keys:
        return {'status': 'not_configured',
                'headline': 'The written reading is not set up yet.',
                'summary': 'The figures below are real. Paste an AI key in Settings > AI keys to get the written part.',
                'bullets': [], 'watch': []}, ''
    user_text = json.dumps({'salon': salon_name, **numbers})
    model = ''
    for provider, key, chosen_model, base_url in keys:           # the one in use first, then the others
        model = chosen_model or ai_providers.PROVIDERS[provider]['model']
        try:
            text = ai_providers.complete(provider, key, chosen_model, PROMPT, user_text, base_url=base_url)
            parsed = json.loads(text[text.index('{'):text.rindex('}') + 1])
            return {'status': 'ok', 'headline': str(parsed.get('headline', ''))[:300],
                    'summary': str(parsed.get('summary', ''))[:900],
                    'bullets': [str(b)[:400] for b in (parsed.get('bullets') or [])][:6],
                    'watch': [str(b)[:400] for b in (parsed.get('watch') or [])][:4]}, model
        except (ai_providers.AiError, ValueError, AttributeError) as exc:      # next key, if there is one
            logger.warning('Owner weekly report: %s did not give a reading: %s', provider, exc)
    return {'status': 'failed', 'headline': 'The written reading could not be made this time.',
            'summary': 'The figures below are real. Ask for the report again in a few minutes.',
            'bullets': [], 'watch': []}, model


def build_report(user, center, start=None, end=None, force=False):
    """The saved report of a salon's week, made now when there is none (or `force`, or its text failed)."""
    if start is None:
        start, end = week_of()
    existing = OwnerWeeklyReport.objects.filter(center=center, week_start=start).first()
    status = existing.narrative.get('status') if existing else None
    # A report waiting for a key is made again as soon as one is pasted.
    if existing and not force and (status == 'ok' or (status == 'not_configured' and not ai_providers.any_configured())):
        return existing
    numbers = week_numbers(user, [center.id], center.id, start, end)
    narrative, model = write_narrative(numbers, center.display_name or center.center_name)
    report, _ = OwnerWeeklyReport.objects.update_or_create(
        center=center, week_start=start,
        defaults={'week_end': end, 'numbers': numbers, 'narrative': narrative, 'model_name': model})
    return report
