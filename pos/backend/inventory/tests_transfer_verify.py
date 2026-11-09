"""A stock transfer's hand count (scanned + typed) and its same-state / interstate kind."""
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from inventory.models import Product, StockTransfer
from inventory.places import center_state, transfer_kind
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role

D = Decimal
T = '/inventory/api/stock_transfers/'


def tick(**subs):
    return {'inventory': {k: {'read': True, 'create': True, 'update': True} for k in subs}}


class TransferVerifyTests(TestCase):
    def setUp(self):
        self.src = Center.objects.create(center_name='Delhi Warehouse', display_name='Delhi WH', gst_number='07AAAAA0000A1Z5')
        self.same = Center.objects.create(center_name='Delhi Shop', display_name='Delhi Shop', gst_number='07BBBBB0000B1Z5')
        self.far = Center.objects.create(center_name='Noida Shop', display_name='Noida Shop', gst_number='09CCCCC0000C1Z5')
        self.nowhere = Center.objects.create(center_name='Mystery', display_name='Mystery')
        role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.user = CustomUser.objects.create_user(email='o@t.com', password='pw', full_name='Owner', role=role, center=self.src)
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.product = Product.objects.create(name='Serum', product_code='SER1', barcode='890123', category='Retail',
                                              price=D('100'), mrp=D('250'), gst_percent=D('18'), sac_code='33049990',
                                              current_stock=50, center=self.src)
        for c in (self.same, self.far):
            Product.objects.create(name='Serum', product_code='SER1', category='Retail', current_stock=0, center=c)

    def make(self, to, **line):
        item = {'product': self.product.id, 'quantity': '10'}
        item.update(line)
        res = self.api.post(T, {'from_center': self.src.id, 'to_center': to.id, 'items': [item]}, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        return res.data

    # same state / interstate
    def test_the_state_comes_from_the_gstin(self):
        self.assertEqual((center_state(self.src), center_state(self.far), center_state(self.nowhere)), ('DL', 'UP', ''))

    def test_a_transfer_inside_one_state_and_one_across_states_are_told_apart(self):
        self.assertEqual(transfer_kind(self.src, self.same), 'same_state')
        self.assertEqual(transfer_kind(self.src, self.far), 'interstate')
        self.assertEqual(transfer_kind(self.src, self.nowhere), 'unknown')
        self.assertEqual(self.make(self.far)['transfer_kind'], 'interstate')
        self.assertEqual(self.make(self.same)['transfer_kind'], 'same_state')

    def test_the_transfer_carries_what_a_printed_challan_needs(self):
        data = self.make(self.far)
        self.assertEqual((data['from_info']['gstin'], data['to_info']['state']), ('07AAAAA0000A1Z5', 'UP'))
        line = data['items'][0]
        self.assertEqual((line['product_code'], line['product_hsn'], line['product_gst'], line['product_price']),
                         ('SER1', '33049990', 18.0, 100.0))
        self.assertEqual(data['created_by_name'], 'Owner')

    # counts
    def test_counts_given_when_the_transfer_is_made_are_kept_and_checked(self):
        line = self.make(self.far, scanned_quantity='6', manual_quantity='4')['items'][0]
        self.assertEqual((line['scanned_quantity'], line['manual_quantity'], line['sent_check']), (6.0, 4.0, 'ok'))
        self.assertEqual(line['received_check'], 'none')
        short = self.make(self.far, scanned_quantity='6', manual_quantity='2')['items'][0]
        over = self.make(self.far, scanned_quantity='9', manual_quantity='3')['items'][0]
        self.assertEqual((short['sent_check'], over['sent_check']), ('short', 'over'))

    def test_the_receiver_counts_what_arrived_without_moving_any_stock(self):
        data = self.make(self.far)
        tid, item_id = data['id'], data['items'][0]['id']
        self.api.patch(f'{T}{tid}/', {'status': 'In Transit'}, format='json')
        before = Product.objects.get(center=self.far, product_code='SER1').current_stock
        res = self.api.post(f'{T}{tid}/verify/', {'received': [{'id': item_id, 'scanned': 7, 'manual': 3}]}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['items'][0]['received_check'], 'ok')
        self.assertEqual(Product.objects.get(center=self.far, product_code='SER1').current_stock, before)

    def test_bad_counts_are_refused(self):
        data = self.make(self.far)
        tid, item_id = data['id'], data['items'][0]['id']
        for bad in (-1, 'abc', 5_000_000):
            res = self.api.post(f'{T}{tid}/verify/', {'sent': [{'id': item_id, 'scanned': bad}]}, format='json')
            self.assertEqual(res.status_code, 400, bad)
        res = self.api.post(f'{T}{tid}/verify/', {'sent': [{'id': 99999, 'scanned': 1}]}, format='json')
        self.assertEqual(res.status_code, 400)

    def test_nothing_can_be_counted_as_received_before_it_was_sent(self):
        data = self.make(self.far)
        res = self.api.post(f'{T}{data["id"]}/verify/', {'received': [{'id': data['items'][0]['id'], 'scanned': 1}]},
                            format='json')
        self.assertEqual(res.status_code, 400)

    def test_each_side_counts_only_at_its_own_branch(self):
        data = self.make(self.far)
        tid, item_id = data['id'], data['items'][0]['id']
        self.api.patch(f'{T}{tid}/', {'status': 'In Transit'}, format='json')
        clerk = CustomUser.objects.create_user(email='c@t.com', password='pw', full_name='Clerk', center=self.same,
                                               role=Role.objects.create(name='clerk', permissions=tick(stock_transfers=1)))
        clerk.centers.add(self.same)
        api = APIClient()
        api.force_authenticate(clerk)
        # the clerk is at neither end of this transfer
        res = api.post(f'{T}{tid}/verify/', {'received': [{'id': item_id, 'scanned': 10}]}, format='json')
        self.assertIn(res.status_code, (403, 404))
