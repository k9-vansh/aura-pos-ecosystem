"""A staff member's detail page shows counts per service, per month and per bill."""
from datetime import date, time, timedelta
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from salon_admin.models import Center
from staff.models import ServiceLog, StaffMember


class StaffSummaryTests(TestCase):
    URL = '/staff/api/logs/staff_summary/'

    def setUp(self):
        self.center = Center.objects.create(center_name='Saket', display_name='Saket')
        owner = CustomUser.objects.create_user(
            email='o@test.com', password='pw', full_name='Owner', is_superuser=True, is_staff=True, center=self.center)
        self.api = APIClient()
        self.api.force_authenticate(owner)
        self.staff = StaffMember.objects.create(
            center=self.center, first_name='Asha', designation='Stylist',
            commission_percentage=Decimal('10'), product_commission_percentage=Decimal('5'))
        self.today = date.today()

    def log(self, name, price, kind='Service', days_ago=0, client='Ria'):
        return ServiceLog.objects.create(
            staff=self.staff, center=self.center, client_name=client, service_name=name,
            service_type=kind, price=Decimal(price), date=self.today - timedelta(days=days_ago),
            time=time(10, 0))

    def get(self, **extra):
        params = {'staff_id': self.staff.id, 'start_date': str(self.today - timedelta(days=6)),
                  'end_date': str(self.today)}
        params.update(extra)
        return self.api.get(self.URL, params)

    def test_counts_per_service_with_revenue_and_commission(self):
        self.log('Haircut', '500')
        self.log('Haircut', '300', client='Mina')
        self.log('Shampoo', '200', kind='Product')
        r = self.get()
        self.assertEqual(r.status_code, 200, r.data)
        rows = {x['service_name']: x for x in r.data['by_service']}
        self.assertEqual(rows['Haircut']['count'], 2)
        self.assertEqual(Decimal(str(rows['Haircut']['revenue'])), Decimal('800'))
        self.assertEqual(Decimal(str(rows['Haircut']['commission'])), Decimal('80'))
        self.assertEqual(Decimal(str(rows['Shampoo']['commission'])), Decimal('10'))
        self.assertEqual(r.data['totals']['services'], 3)
        self.assertEqual(r.data['totals']['clients'], 2)

    def test_range_only_bounds_the_service_table_not_the_month_history(self):
        self.log('Haircut', '500')
        self.log('Facial', '900', days_ago=40)
        r = self.get()
        self.assertEqual([x['service_name'] for x in r.data['by_service']], ['Haircut'])
        self.assertEqual(sum(m['services'] for m in r.data['by_month']), 2)

    def test_a_bill_groups_its_lines(self):
        self.log('Haircut', '500')
        self.log('Facial', '900')
        r = self.get()
        self.assertEqual(len(r.data['by_bill']), 2)  # two manual entries, no invoice
        self.assertEqual(r.data['by_bill'][0]['invoice_number'], 'Manual entry')

    def test_every_category_is_listed_even_when_zero(self):
        self.log('Haircut', '500')
        self.log('Gold Card', '5000', kind='Value Card')
        r = self.get()
        cats = {c['category']: c for c in r.data['by_category']}
        self.assertEqual(set(cats), {'Service', 'Product', 'Membership', 'Package', 'Value Card'})
        self.assertEqual(cats['Value Card']['sold'], 1)
        self.assertEqual(Decimal(str(cats['Value Card']['revenue'])), Decimal('5000'))
        self.assertEqual(cats['Membership']['sold'], 0)

    def _invoice(self, **extra):
        from billing.models import Invoice
        return Invoice.objects.create(center=self.center, status='paid', subtotal=Decimal('0'),
                                      total_amount=Decimal('0'), paid_amount=Decimal('0'), **extra)

    def test_a_package_redemption_is_told_apart_from_a_sale(self):
        from billing.models import InvoiceItem
        inv = self._invoice()
        InvoiceItem.objects.create(invoice=inv, description='Facial', unit_price=Decimal('0'),
                                   quantity=1, total_price=Decimal('0'), is_redemption=True)
        ServiceLog.objects.create(
            invoice=inv, staff=self.staff, center=self.center, client_name='Ria',
            service_name='Facial', service_type='Service', price=Decimal('0'),
            date=self.today, time=time(11, 0))
        self.log('Facial', '900')   # a real sale of the same service
        r = self.get()
        rows = {(x['service_name'], x['is_redemption']): x for x in r.data['by_service']}
        self.assertEqual(rows[('Facial', True)]['count'], 1)
        self.assertEqual(rows[('Facial', False)]['count'], 1)
        cat = {c['category']: c for c in r.data['by_category']}['Service']
        self.assertEqual((cat['sold'], cat['redeemed']), (1, 1))

    def test_promotions_used_on_the_staffs_bills_are_listed(self):
        from marketing.models import Promotion, PromotionUsage
        inv = self._invoice()
        ServiceLog.objects.create(
            invoice=inv, staff=self.staff, center=self.center, client_name='Ria',
            service_name='Haircut', service_type='Service', price=Decimal('450'),
            date=self.today, time=time(11, 0))
        promo = Promotion.objects.create(name='Diwali 10', start_date=self.today, end_date=self.today)
        PromotionUsage.objects.create(promotion=promo, invoice=inv, bill_amount_before=Decimal('500'),
                                      bill_amount_after=Decimal('450'), revenue_generated=Decimal('450'))
        r = self.get()
        self.assertEqual(r.data['promotions'][0]['name'], 'Diwali 10')
        self.assertEqual(r.data['promotions'][0]['bills'], 1)
        self.assertEqual(Decimal(str(r.data['promotions'][0]['discount_given'])), Decimal('50'))
        self.assertEqual(r.data['by_bill'][0]['promotions'], ['Diwali 10'])

    def test_totals_match_the_staff_commission_report(self):
        self.log('Haircut', '500')
        self.log('Shampoo', '200', kind='Product')
        self.log('Gold Card', '5000', kind='Value Card')
        self.log('Annual', '3000', kind='Membership')
        mine = self.get().data
        rep = self.api.get('/staff/api/members/commission_report/', {
            'start_date': str(self.today - timedelta(days=6)), 'end_date': str(self.today),
            'center_id': self.center.id}).data['results'][0]
        cats = {c['category']: Decimal(str(c['revenue'])) for c in mine['by_category']}
        self.assertEqual(cats['Service'], Decimal(str(rep['service_revenue'])))
        self.assertEqual(cats['Product'], Decimal(str(rep['product_revenue'])))
        self.assertEqual(cats['Membership'], Decimal(str(rep['membership_revenue'])))
        self.assertEqual(cats['Value Card'], Decimal(str(rep['value_card_revenue'])))
        self.assertEqual(Decimal(str(mine['totals']['revenue'])), Decimal(str(rep['total_revenue'])))
        self.assertEqual(Decimal(str(mine['totals']['commission'])), Decimal(str(rep['total_commission'])))

    def test_staff_id_and_dates_are_required(self):
        self.assertEqual(self.api.get(self.URL).status_code, 400)
        self.assertEqual(self.api.get(self.URL, {'staff_id': self.staff.id}).status_code, 400)
