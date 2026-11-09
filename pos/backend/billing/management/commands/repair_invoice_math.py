"""
Repair historical invoice line items corrupted by two audit findings.

1. C1 - every service line was stored at Rs 0.
   InvoiceSerializer.create() gated its price lookup on
   `ct.model == 'service'`, but the ContentType model name is 'servicemaster'.
   Services therefore fell through to Decimal('0') and were written with
   unit_price / tax_amount / total_price all zero. This zeroed service revenue
   reporting, and ServiceLog.price with it, so no service commission was ever
   paid and no staff member's salary multiple could reach an incentive tier.

2. H2 - line totals were stored tax-INCLUSIVE while Invoice.subtotal was
   tax-EXCLUSIVE, so sum(items) never equalled subtotal and the register's
   "including tax" figure double-counted GST.

The command recomputes each line from the catalog, converts totals to
tax-exclusive, regenerates the affected ServiceLog rows, recomputes each
invoice header, and allocates GST serial numbers to invoices that predate the
numbering series.

ALWAYS run with --dry-run first and read the report.

    python manage.py repair_invoice_math --dry-run
    python manage.py repair_invoice_math --dry-run --verbose
    python manage.py repair_invoice_math --apply

Scope options:
    --from 2026-01-01 --to 2026-08-20     restrict by invoice date
    --invoice 1234                        a single invoice
    --skip-service-logs                   leave commissions alone
    --skip-numbering                      do not allocate GST serials
"""

import datetime
from collections import Counter
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Sum

from pos_backend.money import D, money, rupees, split, ZERO
from billing import pricing


class Command(BaseCommand):
    help = 'Recompute historical invoice line prices, taxes and totals (see module docstring).'

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument('--dry-run', action='store_true',
                           help='Report what would change without writing anything.')
        group.add_argument('--apply', action='store_true',
                           help='Write the corrections.')
        parser.add_argument('--from', dest='date_from', help='Earliest invoice date (YYYY-MM-DD).')
        parser.add_argument('--to', dest='date_to', help='Latest invoice date (YYYY-MM-DD).')
        parser.add_argument('--invoice', type=int, help='Repair a single invoice by id.')
        parser.add_argument('--limit', type=int, help='Stop after N invoices (useful for a trial run).')
        parser.add_argument('--skip-service-logs', action='store_true',
                            help='Do not regenerate staff commission logs.')
        parser.add_argument('--skip-numbering', action='store_true',
                            help='Do not allocate GST serial numbers.')
        parser.add_argument('--verbose', action='store_true',
                            help='Print every changed line, not just a summary.')

    def handle(self, *args, **options):
        from billing.models import Invoice

        self.apply = options['apply']
        self.verbose = options['verbose']
        self.skip_logs = options['skip_service_logs']
        self.skip_numbering = options['skip_numbering']

        qs = Invoice.objects.exclude(status='draft').order_by('id')

        if options['invoice']:
            qs = qs.filter(id=options['invoice'])
        if options['date_from']:
            qs = qs.filter(created_at__date__gte=self._parse_date(options['date_from']))
        if options['date_to']:
            qs = qs.filter(created_at__date__lte=self._parse_date(options['date_to']))
        if options['limit']:
            qs = qs[:options['limit']]

        total = qs.count() if not options['limit'] else len(list(qs))
        mode = 'APPLY' if self.apply else 'DRY RUN'
        self.stdout.write(self.style.MIGRATE_HEADING(
            f'\nrepair_invoice_math [{mode}] - {total} invoice(s) in scope\n'
        ))

        stats = Counter()
        deltas = {'subtotal': ZERO, 'tax': ZERO, 'total': ZERO, 'commission': ZERO}
        changed_invoices = []

        for invoice in qs.iterator(chunk_size=200):
            try:
                result = self._repair_invoice(invoice)
            except Exception as exc:  # noqa: BLE001 - report and continue
                stats['errored'] += 1
                self.stdout.write(self.style.ERROR(
                    f'  invoice #{invoice.id}: {type(exc).__name__}: {exc}'
                ))
                continue

            stats['scanned'] += 1
            if not result['changed']:
                continue

            stats['changed'] += 1
            stats['lines_repriced'] += result['lines_repriced']
            stats['logs_rebuilt'] += result['logs_rebuilt']
            stats['numbers_assigned'] += result['number_assigned']
            for key in deltas:
                deltas[key] += result['delta'][key]
            changed_invoices.append(result)

            if self.verbose:
                self._print_invoice(result)

        self._print_summary(stats, deltas, changed_invoices)

        if not self.apply:
            self.stdout.write(self.style.WARNING(
                '\nNothing was written. Re-run with --apply once the numbers above look right.\n'
            ))
        return None

    # ─── Per-invoice repair ───────────────────────────────────────────────

    def _repair_invoice(self, invoice):
        """Recompute one invoice. Writes only when self.apply is True."""
        from billing.models import Invoice
        from staff.models import ServiceLog

        result = {
            'invoice': invoice,
            'changed': False,
            'lines_repriced': 0,
            'logs_rebuilt': 0,
            'number_assigned': 0,
            'lines': [],
            'before': {
                'subtotal': money(invoice.subtotal),
                'tax': money(D(invoice.cgst) + D(invoice.sgst)),
                'total': money(invoice.total_amount),
            },
            'after': {},
            'delta': {'subtotal': ZERO, 'tax': ZERO, 'total': ZERO, 'commission': ZERO},
        }

        items = list(invoice.items.select_related('content_type').prefetch_related('staff_members').all())
        if not items:
            return result

        planned = []
        for item in items:
            ct_label = pricing.content_type_label(item.content_type)
            catalog = pricing.resolve(ct_label, item.object_id, center=invoice.center)

            if item.is_redemption:
                new_unit = money(item.unit_price)
                new_discount = money(item.discount)
                new_taxable = ZERO
                new_tax_pct = ZERO
            elif catalog.resolved:
                # Preserve the discount the operator actually gave. The stored
                # discount is trustworthy; the stored unit_price is not, because
                # the typo overwrote it with zero.
                new_unit = catalog.list_price
                new_discount = money(item.discount)
                gross = new_unit * D(item.quantity or 1)
                if new_discount > gross:
                    new_discount = money(gross)
                new_taxable = money(gross - new_discount)
                new_tax_pct = catalog.tax_percentage
            else:
                # Catalog object gone (deleted service/product) or a custom
                # line. Keep what is stored; only convert the tax convention.
                new_unit = money(item.unit_price)
                new_discount = money(item.discount)
                new_tax_pct = D(item.tax_percentage)
                gross = new_unit * D(item.quantity or 1)
                new_taxable = money(max(gross - new_discount, ZERO))

            planned.append({
                'item': item,
                'unit_price': new_unit,
                'discount': new_discount,
                'tax_percentage': new_tax_pct,
                'taxable': new_taxable,
                'was_unit': money(item.unit_price),
                'was_total': money(item.total_price),
                'was_tax': money(item.tax_amount),
                'catalog_resolved': catalog.resolved,
            })

        subtotal = money(sum((p['taxable'] for p in planned), ZERO))

        global_discount = money(max(D(invoice.discount), ZERO))
        if global_discount > subtotal:
            global_discount = subtotal

        tax_total = ZERO
        for p in planned:
            taxable = p['taxable']
            if taxable <= 0 or p['tax_percentage'] <= 0:
                p['tax_amount'] = ZERO
                continue
            apportioned = (money(global_discount * taxable / subtotal)
                           if subtotal > 0 and global_discount > 0 else ZERO)
            net = taxable - apportioned
            if net < 0:
                net = ZERO
            p['tax_amount'] = money(net * p['tax_percentage'] / D('100'))
            tax_total += p['tax_amount']

        tax_total = money(tax_total)
        halves = split(tax_total, 2) if tax_total > 0 else [ZERO, ZERO]
        total_raw = money(subtotal - global_discount + tax_total)
        total_rounded = rupees(total_raw)

        result['after'] = {
            'subtotal': subtotal,
            'tax': tax_total,
            'total': money(total_rounded),
        }

        header_changed = (
            money(invoice.subtotal) != subtotal
            or money(D(invoice.cgst) + D(invoice.sgst)) != tax_total
            or money(invoice.total_amount) != money(total_rounded)
        )

        for p in planned:
            item = p['item']
            if (money(item.unit_price) != p['unit_price']
                    or money(item.total_price) != p['taxable']
                    or money(item.tax_amount) != p['tax_amount']
                    or D(item.tax_percentage) != p['tax_percentage']):
                result['lines_repriced'] += 1
                result['lines'].append(p)

        if not result['lines_repriced'] and not header_changed:
            if not self.skip_numbering and not invoice.invoice_number and invoice.center_id:
                result['changed'] = True
                result['number_assigned'] = 1
                if self.apply:
                    with transaction.atomic():
                        invoice.assign_invoice_number()
            return result

        result['changed'] = True
        result['delta']['subtotal'] = subtotal - result['before']['subtotal']
        result['delta']['tax'] = tax_total - result['before']['tax']
        result['delta']['total'] = money(total_rounded) - result['before']['total']

        # Commission delta, for the report.
        old_commission = money(
            ServiceLog.all_objects.filter(invoice=invoice).aggregate(
                t=Sum('price')
            )['t'] or 0
        )

        if not self.apply:
            new_commission = self._projected_commission(invoice, planned)
            result['delta']['commission'] = new_commission - old_commission
            return result

        with transaction.atomic():
            locked = Invoice.objects.select_for_update().get(pk=invoice.pk)

            for p in planned:
                item = p['item']
                item.unit_price = p['unit_price']
                item.discount = p['discount']
                item.tax_percentage = p['tax_percentage']
                item.tax_amount = p['tax_amount']
                item.total_price = p['taxable']
                if D(item.manager_discount) > D(item.discount):
                    item.manager_discount = money(item.discount)
                item.save(update_fields=[
                    'unit_price', 'discount', 'tax_percentage', 'tax_amount',
                    'total_price', 'manager_discount',
                ])

            locked.subtotal = subtotal
            locked.discount = global_discount
            locked.cgst = halves[0]
            locked.sgst = halves[1]
            locked.rounding = money(money(total_rounded) - total_raw)
            locked.total_amount = money(total_rounded)
            locked.save(update_fields=[
                'subtotal', 'discount', 'cgst', 'sgst', 'rounding', 'total_amount',
            ])

            if not self.skip_numbering and not locked.invoice_number and locked.center_id:
                locked.assign_invoice_number()
                result['number_assigned'] = 1

            if not self.skip_logs:
                result['logs_rebuilt'] = self._rebuild_service_logs(locked, planned)
                new_commission = money(
                    ServiceLog.all_objects.filter(invoice=locked).aggregate(
                        t=Sum('price')
                    )['t'] or 0
                )
                result['delta']['commission'] = new_commission - old_commission

        return result

    def _projected_commission(self, invoice, planned):
        """What total commission WOULD be after the repair (dry-run only)."""
        from billing.services import _real_money_ratio

        ratio = _real_money_ratio(invoice)
        total = ZERO
        for p in planned:
            item = p['item']
            staff = list(item.staff_members.all())
            if item.staff and item.staff not in staff:
                staff.append(item.staff)
            if not staff:
                continue
            total += money(p['taxable'] * ratio)
        return money(total)

    def _rebuild_service_logs(self, invoice, planned):
        """Replace this invoice's ServiceLog rows with correctly-priced ones."""
        from staff.models import ServiceLog
        from billing.services import _create_service_logs, _real_money_ratio

        existing = ServiceLog.all_objects.filter(invoice=invoice)
        removed = existing.count()
        existing.delete()

        ratio = _real_money_ratio(invoice)
        for p in planned:
            _create_service_logs(invoice, p['item'], ratio)
        return max(removed, ServiceLog.all_objects.filter(invoice=invoice).count())

    # ─── Reporting ────────────────────────────────────────────────────────

    def _print_invoice(self, result):
        inv = result['invoice']
        b, a = result['before'], result['after']
        self.stdout.write(
            f"  #{inv.id} {inv.display_number} ({inv.created_at.date() if inv.created_at else '?'})"
        )
        for p in result['lines'][:20]:
            item = p['item']
            flag = '' if p['catalog_resolved'] else '  [catalog item missing - kept stored price]'
            self.stdout.write(
                f"      {(item.description or '')[:42]:<42} "
                f"unit {p['was_unit']:>9} -> {p['unit_price']:>9}   "
                f"total {p['was_total']:>9} -> {p['taxable']:>9}{flag}"
            )
        self.stdout.write(
            f"      header: subtotal {b['subtotal']} -> {a['subtotal']}   "
            f"tax {b['tax']} -> {a['tax']}   total {b['total']} -> {a['total']}"
        )

    def _print_summary(self, stats, deltas, changed):
        w = self.stdout.write
        w('')
        w(self.style.MIGRATE_HEADING('Summary'))
        w(f"  invoices scanned         {stats['scanned']}")
        w(f"  invoices changed         {stats['changed']}")
        w(f"  line items repriced      {stats['lines_repriced']}")
        w(f"  commission logs rebuilt  {stats['logs_rebuilt']}")
        w(f"  GST numbers assigned     {stats['numbers_assigned']}")
        if stats['errored']:
            w(self.style.ERROR(f"  invoices errored         {stats['errored']}"))
        w('')
        w(self.style.MIGRATE_HEADING('Aggregate movement'))
        w(f"  subtotal    {self._signed(deltas['subtotal'])}")
        w(f"  tax         {self._signed(deltas['tax'])}")
        w(f"  grand total {self._signed(deltas['total'])}")
        w(f"  commission  {self._signed(deltas['commission'])}")
        w('')
        w('  Note: `grand total` movement is expected to be small. It reflects the')
        w('  tax-convention correction and the half-up rounding fix, not a change to')
        w('  what customers were charged. A LARGE movement here means some line items')
        w('  had prices that no longer match the catalog - inspect those with --verbose')
        w('  before applying.')

        if changed:
            biggest = sorted(changed, key=lambda r: abs(r['delta']['total']), reverse=True)[:10]
            w('')
            w(self.style.MIGRATE_HEADING('Largest total movements'))
            for r in biggest:
                inv = r['invoice']
                w(f"  #{inv.id:<8} {self._signed(r['delta']['total']):>14}   "
                  f"{r['before']['total']} -> {r['after']['total']}")

    @staticmethod
    def _signed(value):
        v = money(value)
        return f'+{v}' if v > 0 else str(v)

    @staticmethod
    def _parse_date(text):
        try:
            return datetime.datetime.strptime(text, '%Y-%m-%d').date()
        except (TypeError, ValueError):
            raise CommandError(f'Invalid date {text!r}; expected YYYY-MM-DD.')
