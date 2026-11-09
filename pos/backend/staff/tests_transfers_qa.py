"""QA report H5: staff transfers must not strand or steal people."""
import datetime
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role
from staff.models import StaffMember, StaffTransfer
from staff.utils import sync_staff_transfers_and_tools


class TransferTests(TestCase):
    def setUp(self):
        self.a = Center.objects.create(center_name='A', display_name='A')
        self.b = Center.objects.create(center_name='B', display_name='B')
        self.c = Center.objects.create(center_name='C', display_name='C')
        role = Role.objects.create(name='HQ', permissions=full_access_permissions())
        self.user = CustomUser.objects.create_user(email='hq@x.test', password='Zq7!vLm29xTr', full_name='HQ',
                                                   role=role, center=self.a)
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.person = StaffMember.objects.create(center=self.a, first_name='Gita', designation='Stylist',
                                                 salary=Decimal('1'), commission_percentage=Decimal('10'))

    def transfer(self, **extra):
        body = {'staff': self.person.id, 'from_center': self.a.id, 'to_center': self.b.id,
                'transfer_type': 'Permanent', 'start_date': datetime.date.today().isoformat(),
                'reason': 'cover'}
        body.update(extra)
        return self.api.post('/staff/api/transfers/', body, format='json')

    def where(self):
        self.person.refresh_from_db()
        return self.person.center_id

    def test_a_transfer_moves_the_person_and_cancelling_returns_them(self):
        r = self.transfer()
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(self.where(), self.b.id)
        r = self.api.patch(f"/staff/api/transfers/{r.data['id']}/", {'status': 'Cancelled'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self.where(), self.a.id)

    def test_a_transfer_from_a_centre_the_person_does_not_work_at_is_refused(self):
        r = self.transfer(from_center=self.c.id)
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(self.where(), self.a.id)

    def test_a_temporary_transfer_ends_by_itself(self):
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        r = self.transfer(transfer_type='Temporary', start_date=(yesterday - datetime.timedelta(days=3)).isoformat(),
                          end_date=yesterday.isoformat())
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(self.where(), self.b.id)
        sync_staff_transfers_and_tools()
        self.assertEqual(self.where(), self.a.id)
        self.assertEqual(StaffTransfer.objects.get().status, 'Completed')
