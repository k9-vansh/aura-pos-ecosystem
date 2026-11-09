"""Admin > Bills cards: counts and value of every bill the filters match, not one page."""
import datetime
from decimal import Decimal

from billing.models import Invoice
from finance.tests import FinanceTestBase


class InvoiceSummaryTests(FinanceTestBase):
    def setUp(self):
        super().setUp()
        self.when = datetime.datetime.combine(datetime.date.today(), datetime.time(11, 0))
        for total, status in (('1000.00', 'paid'), ('2000.00', 'paid'), ('500.00', 'partial'),
                              ('700.00', 'cancelled'), ('300.00', 'refunded')):
            self.make_invoice(total, status=status, when=self.when)
        self.make_invoice('9999.00', status='draft', when=self.when)       # a held bill is not a bill yet

    def summary(self, **params):
        response = self.api.get('/billing/invoices/summary/', params)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def test_counts_and_value_cover_every_bill(self):
        data = self.summary(exclude_drafts='true')
        self.assertEqual(data['count'], 5)
        self.assertEqual(data['paid'], 3)
        self.assertEqual(data['cancelled'], 1)
        self.assertEqual(data['refunded'], 1)

    def test_value_is_what_was_sold_not_cancelled_or_refunded_bills(self):
        data = self.summary(exclude_drafts='true')
        self.assertEqual(Decimal(str(data['value'])), Decimal('3500.00'))
        self.assertEqual(Decimal(str(data['cancelled_value'])), Decimal('700.00'))
        self.assertEqual(Decimal(str(data['refunded_value'])), Decimal('300.00'))

    def test_it_does_not_depend_on_the_page_asked_for(self):
        """The list is cut into pages; the summary is not."""
        listing = self.api.get('/billing/invoices/', {'exclude_drafts': 'true', 'page_size': 2, 'page': 1})
        self.assertEqual(len(listing.data['results']), 2)
        self.assertEqual(self.summary(exclude_drafts='true', page_size=2, page=1)['count'], 5)

    def test_it_follows_the_date_filter(self):
        old = self.make_invoice('4000.00', status='paid', when=self.when - datetime.timedelta(days=40))
        day = old.created_at.date().isoformat()
        data = self.summary(exclude_drafts='true', start_date=day, end_date=day)
        self.assertEqual(data['count'], 1)
        self.assertEqual(Decimal(str(data['value'])), Decimal('4000.00'))

    def test_a_centre_user_sees_only_their_centres_figures(self):
        Invoice.objects.filter(status='paid').update(center=self.other_center)
        from accounts.models import CustomUser
        from salon_admin.models import Role
        role = Role.objects.create(name='Bills reader', permissions={
            'all_centers': False, 'admin': {'bills': {'read': True}}})
        user = CustomUser.objects.create_user(email='br@test.com', password='pw', full_name='BR',
                                              role=role, center=self.center)
        user.centers.set([self.center])
        self.api.force_authenticate(user)
        data = self.summary(exclude_drafts='true')
        self.assertEqual(data['paid'], 1)            # only the partial bill is left at this centre
        self.assertEqual(data['count'], 3)
