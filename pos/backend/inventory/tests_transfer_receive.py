"""The whole stock round, one person per step: the centre manager asks, the regional manager approves, the stock
person counts and sends, and the centre manager counts what arrived, attaches the bill and signs it in."""
import json
import tempfile
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import CustomUser
from inventory.models import Product, StockRequest, StockTransfer
from notifications.models import Notification
from salon_admin.models import Center, Role

D = Decimal
REQ = '/inventory/api/stock_requests/'
ST = '/inventory/api/stock_transfers/'


def tick(**subs):
    return {'inventory': {k: {'read': True, 'create': True, 'update': True} for k in subs}}


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix='pos-test-media-'))
class StockRoundTests(TestCase):
    def setUp(self):
        self.branch = Center.objects.create(center_name='Saket', display_name='Saket', gst_number='07AAAAA0000A1Z5')
        self.warehouse = Center.objects.create(center_name='Warehouse', display_name='Warehouse',
                                               gst_number='07BBBBB0000B1Z5')

        def user(email, perms, centers, home):
            u = CustomUser.objects.create_user(email=email, password='pw', full_name=email.split('@')[0],
                                               role=Role.objects.create(name=email, permissions=perms), center=home)
            u.centers.add(*centers)
            api = APIClient()
            api.force_authenticate(u)
            return u, api

        self.manager, self.manager_api = user('manager@t.com', tick(stock_requests=1, stock_transfers=1),
                                              [self.branch], self.branch)
        self.regional, self.regional_api = user('regional@t.com', tick(approve_stock_requests=1),
                                                [self.branch, self.warehouse], self.warehouse)
        self.store, self.store_api = user('store@t.com', tick(stock_requests=1, fulfill_stock_requests=1,
                                                              stock_transfers=1),
                                          [self.branch, self.warehouse], self.warehouse)
        self.stranger, self.stranger_api = user('stranger@t.com', tick(stock_transfers=1), [self.warehouse],
                                                self.warehouse)
        mk = lambda code, center, stock: Product.objects.create(  # noqa: E731
            name=code, product_code=code, barcode=code, category='Retail', price=D('100'), mrp=D('200'),
            gst_percent=D('18'), current_stock=stock, center=center)
        self.wh_a, self.wh_b = mk('AAA', self.warehouse, 50), mk('BBB', self.warehouse, 50)
        self.br_a, self.br_b = mk('AAA', self.branch, 0), mk('BBB', self.branch, 0)
        self.req_a, self.req_b = mk('AAA', self.branch, 0), None

    # -- helpers
    def sent_transfer(self, qty_a=10, qty_b=5, counted=True):
        """Manager asks, regional approves, store makes the transfer with counts, then sends it."""
        res = self.manager_api.post(REQ, {'requesting_center': self.branch.id, 'stock_type': 'Retail', 'items': [
            {'product': self.br_a.id, 'quantity': str(qty_a)}, {'product': self.br_b.id, 'quantity': str(qty_b)}]},
            format='json')
        self.assertEqual(res.status_code, 201, res.data)
        req_id = res.data['id']
        self.assertEqual(self.regional_api.post(f'{REQ}{req_id}/approve/', {}, format='json').status_code, 200)
        line = lambda p, q: dict(  # noqa: E731
            product=p.id, quantity=str(q), **({'scanned_quantity': str(q - 2), 'manual_quantity': '2'} if counted else {}))
        made = self.store_api.post(ST, {'from_center': self.warehouse.id, 'to_center': self.branch.id,
                                        'stock_request': req_id,
                                        'items': [line(self.wh_a, qty_a), line(self.wh_b, qty_b)]}, format='json')
        self.assertEqual(made.status_code, 201, made.data)
        sent = self.store_api.patch(f'{ST}{made.data["id"]}/', {'status': 'In Transit'}, format='json')
        self.assertEqual(sent.status_code, 200, sent.data)
        return req_id, StockTransfer.objects.get(pk=made.data['id'])

    def receive(self, api, transfer, rows=None, note='', bill='yes'):
        data = {'note': note}
        if rows is not None:
            data['lines'] = json.dumps(rows)
        if bill:
            data['bill'] = SimpleUploadedFile('bill.pdf', b'%PDF-1.4 test', content_type='application/pdf')
        return api.post(f'{ST}{transfer.pk}/receive/', data, format='multipart')

    def full_rows(self, transfer):
        return [{'id': i.pk, 'scanned': float(i.quantity) - 1, 'manual': 1} for i in transfer.items.all()]

    def stock(self, product):
        return Product.objects.get(pk=product.pk).current_stock

    # -- the round
    def test_ask_approve_send_receive_with_the_bill(self):
        req_id, transfer = self.sent_transfer()
        self.assertEqual((self.stock(self.wh_a), self.stock(self.wh_b)), (40, 45))        # left the warehouse
        self.assertEqual((self.stock(self.br_a), self.stock(self.br_b)), (0, 0))          # not at the branch yet
        res = self.receive(self.manager_api, transfer, self.full_rows(transfer))
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual((res.data['status'], res.data['has_difference']), ('Completed', False))
        self.assertTrue(res.data['bill_url'].endswith('.pdf') or '.pdf' in res.data['bill_url'])
        self.assertEqual((self.stock(self.br_a), self.stock(self.br_b)), (10, 5))
        self.assertEqual(StockRequest.objects.get(pk=req_id).status, 'Fulfilled')
        self.assertEqual(res.data['received_by_name'], 'manager')

    def test_receiving_with_no_counts_takes_everything_that_was_sent(self):
        _, transfer = self.sent_transfer(counted=False)
        res = self.receive(self.manager_api, transfer, rows=None)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual((self.stock(self.br_a), self.stock(self.br_b)), (10, 5))

    def test_the_bill_is_required_to_sign_in(self):
        _, transfer = self.sent_transfer()
        res = self.receive(self.manager_api, transfer, self.full_rows(transfer), bill=None)
        self.assertEqual(res.status_code, 400)
        self.assertIn('bill', str(res.data).lower())
        self.assertEqual((StockTransfer.objects.get(pk=transfer.pk).status, self.stock(self.br_a)), ('In Transit', 0))

    def test_a_plain_status_update_cannot_sign_a_transfer_in_without_the_bill(self):
        _, transfer = self.sent_transfer()
        res = self.manager_api.patch(f'{ST}{transfer.pk}/', {'status': 'Completed'}, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertEqual((StockTransfer.objects.get(pk=transfer.pk).status, self.stock(self.br_a)), ('In Transit', 0))

    def test_a_product_cannot_be_on_a_request_or_transfer_twice(self):
        twice = [{'product': self.br_a.id, 'quantity': '1'}, {'product': self.br_a.id, 'quantity': '2'}]
        res = self.manager_api.post(REQ, {'requesting_center': self.branch.id, 'stock_type': 'Retail', 'items': twice},
                                    format='json')
        self.assertEqual(res.status_code, 400)
        self.assertEqual(StockRequest.objects.count(), 0)
        both = [{'product': self.wh_a.id, 'quantity': '1'}, {'product': self.wh_a.id, 'quantity': '2'}]
        res = self.store_api.post(ST, {'from_center': self.warehouse.id, 'to_center': self.branch.id, 'items': both},
                                  format='json')
        self.assertEqual(res.status_code, 400)
        self.assertEqual(StockTransfer.objects.count(), 0)

    def test_the_value_of_a_request_and_a_transfer_is_shown(self):
        Product.objects.filter(pk__in=[self.br_a.pk, self.wh_a.pk]).update(price=D('100'), gst_percent=D('18'))
        Product.objects.filter(pk__in=[self.br_b.pk, self.wh_b.pk]).update(price=D('50'), gst_percent=D('5'))
        req_id, transfer = self.sent_transfer(qty_a=10, qty_b=5)
        # 10 x 100 + 5 x 50 = 1250; GST 180 + 12.50 = 192.50
        asked = self.manager_api.get(f'{REQ}{req_id}/').data
        self.assertEqual(asked['amounts'], {'value': 1250.0, 'gst': 192.5, 'total': 1442.5})
        self.assertEqual(asked['items'][0]['product_price'], 100.0)
        sent = self.manager_api.get(f'{ST}{transfer.pk}/').data
        self.assertEqual((sent['amounts']['value'], sent['amounts']['total']), (1250.0, 1442.5))
        # 3 of the 10 AAA missing: 7 x 100 x 1.18 + 5 x 50 x 1.05 = 826 + 262.5
        a = transfer.items.order_by('id').first()
        b = transfer.items.order_by('-id').first()
        done = self.receive(self.manager_api, transfer, [{'id': a.pk, 'scanned': 7, 'manual': 0},
                                                         {'id': b.pk, 'scanned': 5, 'manual': 0}], note='3 missing')
        self.assertEqual(done.data['amounts']['accepted_total'], 1088.5)

    def test_less_arriving_needs_a_note_and_only_what_arrived_is_added(self):
        _, transfer = self.sent_transfer()
        a, b = list(transfer.items.order_by('id'))
        rows = [{'id': a.pk, 'scanned': 7, 'manual': 0}, {'id': b.pk, 'scanned': 5, 'manual': 0}]
        refused = self.receive(self.manager_api, transfer, rows, note='')
        self.assertEqual(refused.status_code, 400)
        self.assertEqual((self.stock(self.br_a), StockTransfer.objects.get(pk=transfer.pk).status), (0, 'In Transit'))
        ok = self.receive(self.manager_api, transfer, rows, note='3 AAA were missing from the box')
        self.assertEqual(ok.status_code, 200, ok.data)
        self.assertTrue(ok.data['has_difference'])
        self.assertEqual((self.stock(self.br_a), self.stock(self.br_b)), (7, 5))
        self.assertEqual(ok.data['received_note'], '3 AAA were missing from the box')
        lines = {i['product_code']: i for i in ok.data['items']}
        self.assertEqual((lines['AAA']['received_quantity'], lines['AAA']['quantity']), (7.0, 10.0))

    def test_more_than_was_sent_is_refused(self):
        _, transfer = self.sent_transfer()
        a = transfer.items.order_by('id').first()
        res = self.receive(self.manager_api, transfer, [{'id': a.pk, 'scanned': 11, 'manual': 0}])
        self.assertEqual(res.status_code, 400)
        self.assertEqual(StockTransfer.objects.get(pk=transfer.pk).status, 'In Transit')

    def test_the_bill_must_be_a_pdf_or_a_picture_and_not_huge(self):
        _, transfer = self.sent_transfer()
        for name, size in (('bill.exe', 10), ('bill.pdf', 9 * 1024 * 1024)):
            res = self.manager_api.post(f'{ST}{transfer.pk}/receive/', {
                'bill': SimpleUploadedFile(name, b'x' * size)}, format='multipart')
            self.assertEqual(res.status_code, 400, name)
        self.assertEqual(StockTransfer.objects.get(pk=transfer.pk).status, 'In Transit')

    def test_only_the_receiving_branch_signs_it_in_and_not_the_sender(self):
        _, transfer = self.sent_transfer()
        self.assertEqual(self.receive(self.store_api, transfer).status_code, 403)       # sent it
        self.assertEqual(self.receive(self.stranger_api, transfer).status_code, 403)    # other branch
        self.assertEqual(self.receive(self.manager_api, transfer).status_code, 200)
        self.assertEqual(self.receive(self.manager_api, transfer).status_code, 400)     # only once

    def test_counts_that_do_not_add_up_stop_the_goods_leaving(self):
        res = self.manager_api.post(REQ, {'requesting_center': self.branch.id, 'stock_type': 'Retail', 'items': [
            {'product': self.br_a.id, 'quantity': '10'}]}, format='json')
        self.regional_api.post(f'{REQ}{res.data["id"]}/approve/', {}, format='json')
        made = self.store_api.post(ST, {'from_center': self.warehouse.id, 'to_center': self.branch.id,
                                        'stock_request': res.data['id'], 'items': [
                                            {'product': self.wh_a.id, 'quantity': '10', 'scanned_quantity': '6',
                                             'manual_quantity': '2'}]}, format='json')
        sent = self.store_api.patch(f'{ST}{made.data["id"]}/', {'status': 'In Transit'}, format='json')
        self.assertEqual(sent.status_code, 400)
        self.assertEqual(self.stock(self.wh_a), 50)
        item = StockTransfer.objects.get(pk=made.data['id']).items.first()
        self.store_api.post(f'{ST}{made.data["id"]}/verify/', {'sent': [{'id': item.pk, 'scanned': 6, 'manual': 4}]},
                            format='json')
        self.assertEqual(self.store_api.patch(f'{ST}{made.data["id"]}/', {'status': 'In Transit'},
                                              format='json').status_code, 200)

    def test_the_bill_and_the_receipt_cannot_be_written_through_the_plain_update(self):
        _, transfer = self.sent_transfer()
        self.store_api.patch(f'{ST}{transfer.pk}/', {'has_difference': True, 'received_note': 'x'}, format='json')
        t = StockTransfer.objects.get(pk=transfer.pk)
        self.assertEqual((t.has_difference, t.received_note), (False, ''))

    def test_each_step_tells_the_next_person(self):
        req_id, transfer = self.sent_transfer()
        told = lambda event: set(Notification.objects.filter(event=event).values_list('user__email', flat=True))  # noqa: E731
        self.assertIn('regional@t.com', told('stock.request_raised'))
        self.assertIn('store@t.com', told('stock.request_approved'))
        self.assertIn('manager@t.com', told('stock.transfer_sent'))                    # the branch is told it is coming
        self.assertNotIn('store@t.com', told('stock.transfer_sent'))
        a = transfer.items.order_by('id').first()
        self.receive(self.manager_api, transfer, [{'id': a.pk, 'scanned': 8, 'manual': 0},
                                                  {'id': transfer.items.order_by('-id').first().pk, 'scanned': 5,
                                                   'manual': 0}], note='2 broken')
        got = Notification.objects.filter(event='stock.transfer_received')
        self.assertTrue({'store@t.com', 'regional@t.com'} <= set(got.values_list('user__email', flat=True)))
        self.assertIn('difference', got.first().title)

    def test_the_bills_screen_lists_received_transfers_with_a_summary(self):
        Product.objects.filter(pk__in=[self.br_a.pk, self.wh_a.pk]).update(price=D('100'), gst_percent=D('18'))
        Product.objects.filter(pk__in=[self.br_b.pk, self.wh_b.pk]).update(price=D('50'), gst_percent=D('5'))
        _, with_bill = self.sent_transfer(qty_a=10, qty_b=5)
        a = with_bill.items.order_by('id').first()
        b = with_bill.items.order_by('-id').first()
        self.receive(self.manager_api, with_bill, [{'id': a.pk, 'scanned': 7, 'manual': 0},
                                                   {'id': b.pk, 'scanned': 5, 'manual': 0}], note='3 missing')
        _, second = self.sent_transfer(qty_a=4, qty_b=4)
        self.receive(self.manager_api, second)
        self.sent_transfer(qty_a=2, qty_b=2)                       # still on the road: not a bill yet
        viewer, viewer_api = self.make_viewer('bills@t.com', tick(transfer_bills=1), [self.warehouse, self.branch])
        res = viewer_api.get('/inventory/api/transfer_bills/')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual((res.data['summary']['count'], res.data['summary']['with_bill'],
                          res.data['summary']['with_difference']), (2, 2, 1))
        # 7x100x1.18 + 5x50x1.05 = 1088.5 ; 4x100x1.18 + 4x50x1.05 = 682
        self.assertEqual(res.data['summary']['total'], 1770.5)
        self.assertEqual(res.data['summary']['by_centre'][0]['centre'], 'Saket')
        row = [r for r in res.data['results'] if r['id'] == with_bill.pk][0]
        self.assertEqual((row['from_name'], row['to_name'], row['received_by_name'], row['has_difference']),
                         ('Warehouse', 'Saket', 'manager', True))
        self.assertTrue(row['bill_url'])
        only_diff = viewer_api.get('/inventory/api/transfer_bills/?difference=1')
        self.assertEqual([r['id'] for r in only_diff.data['results']], [with_bill.pk])
        self.assertEqual(len(viewer_api.get(f'/inventory/api/transfer_bills/?q=ST-{second.pk}').data['results']), 1)

    def test_the_bills_screen_needs_its_tick_and_shows_only_your_centres(self):
        _, transfer = self.sent_transfer()
        self.receive(self.manager_api, transfer)
        # the centre manager holds Stock Transfers but not Transfer Bills
        self.assertEqual(self.manager_api.get('/inventory/api/transfer_bills/').status_code, 403)
        other = Center.objects.create(center_name='Elsewhere', display_name='Elsewhere')
        _, outsider_api = self.make_viewer('out@t.com', tick(transfer_bills=1), [other])
        res = outsider_api.get('/inventory/api/transfer_bills/')
        self.assertEqual((res.status_code, res.data['summary']['count']), (200, 0))
        # the bill itself is never shown for a transfer you have no part in
        self.assertEqual(res.data['results'], [])

    def make_viewer(self, email, perms, centers):
        user = CustomUser.objects.create_user(email=email, password='pw', full_name=email.split('@')[0],
                                              role=Role.objects.create(name=email, permissions=perms),
                                              center=centers[0])
        user.centers.add(*centers)
        api = APIClient()
        api.force_authenticate(user)
        return user, api

    def test_the_request_shows_where_the_goods_are(self):
        req_id, transfer = self.sent_transfer()
        data = self.manager_api.get(f'{REQ}{req_id}/').data
        self.assertEqual((data['status'], data['transfer_status']), ('Approved', 'In Transit'))
        self.receive(self.manager_api, transfer)
        data = self.manager_api.get(f'{REQ}{req_id}/').data
        self.assertEqual((data['status'], data['transfer_status']), ('Fulfilled', 'Completed'))
