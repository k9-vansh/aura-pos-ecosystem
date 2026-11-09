"""The short memory of the heavy screens, and the staff fast path that keeps a big month quick."""
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.test import override_settings

from billing.models import InvoiceItem
from finance.views import credited_shares
from owner_api import queries
from owner_api.tests import BASE, OwnerBase, account, signed_in
from services.models import ServiceMaster
from staff.models import StaffMember
import datetime


class CacheTests(OwnerBase):
    @override_settings(OWNER_CACHE=True)
    def test_a_repeat_is_answered_from_memory_and_only_for_the_same_salons(self):
        cache.clear()
        self.bill()
        first = self.api.get(BASE + 'today/', {'center_id': self.a.id}).data
        self.bill()
        again = self.api.get(BASE + 'today/', {'center_id': self.a.id}).data
        self.assertEqual((first['bills'], again['bills']), (1, 1))             # remembered for a few seconds
        cache.clear()
        self.assertEqual(self.api.get(BASE + 'today/', {'center_id': self.a.id}).data['bills'], 2)
        # Someone who sees other salons never gets this answer.
        second = account(name='Two', phone='9811100005', centers=[self.b], today={'read': True})
        other = signed_in(second)
        self.assertEqual(other.get(BASE + 'today/', {'center_id': 'all'}).data['bills'], 0)


class StaffFastPathTests(OwnerBase):
    def test_the_database_sum_equals_the_posts_line_by_line_rule(self):
        """Whole lines to one person are summed in the database, shared lines are split in code: together they must
        equal crediting every line one by one, the way Finance > Staff Performance does."""
        a = StaffMember.objects.create(center=self.a, first_name='A', designation='S', salary=Decimal('1'))
        b = StaffMember.objects.create(center=self.a, first_name='B', designation='S', salary=Decimal('1'))
        service_ct = ContentType.objects.get_for_model(ServiceMaster)
        invoice = self.bill()
        InvoiceItem.objects.filter(invoice=invoice).update(staff=a)
        for price, members in (('333.33', [a, b]), ('100.01', [b]), ('50.00', [a, b]), ('77.77', [])):
            item = InvoiceItem.objects.create(invoice=invoice, content_type=service_ct, object_id=self.service.id,
                                              description='x', unit_price=Decimal(price), quantity=2,
                                              total_price=Decimal(price), staff=b if not members or len(members) == 1 else a)
            item.staff_members.set(members)
        today = datetime.date.today()
        fast = queries.credited_by_staff([self.a.id], today, today)
        slow = {}
        for item in InvoiceItem.objects.filter(invoice=invoice, is_redemption=False):
            for member, share, _ in credited_shares(item):
                row = slow.setdefault(member.pk, [Decimal('0'), 0])
                row[0] += share
                row[1] += int(item.quantity)
        self.assertEqual({k: (v['services'] + v['products'] + v['other'], v['items']) for k, v in fast.items()},
                         {k: (v[0], v[1]) for k, v in slow.items()})
