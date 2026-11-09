"""Phase 7: decimal quantities, batches with earliest-expiry-first, the transfer
flow (dispatch, in transit, receive) and stock requests live in tests_stock_request_flow."""

import datetime
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from inventory.models import (Product, StockBatch, StockRequest, StockRequestItem, StockTransaction,
                              StockTransfer, StockTransferItem)
from pos_backend import app_settings
from pos_backend.testing import bill_and_pay, full_access_permissions
from salon_admin.models import Center, Role
from services.models import ServiceBOM, ServiceMaster
from staff.models import StaffMember
from clients.models import Client

D = Decimal
TODAY = datetime.date.today()


class Base(TestCase):
    def setUp(self):
        app_settings.invalidate()
        self.addCleanup(app_settings.invalidate)
        self.center = Center.objects.create(center_name='Saket', display_name='Saket', gst_number='07AAAAA0000A1Z5')
        self.other = Center.objects.create(center_name='Noida', display_name='Noida', gst_number='09CCCCC0000C1Z5')
        role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.user = CustomUser.objects.create_user(email='s@test.com', password='pw', full_name='Store',
                                                   role=role, center=self.center)
        self.user.centers.add(self.center, self.other)
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.product = Product.objects.create(name='Serum', product_code='SER01', price=D('300'), mrp=D('590'),
                                              gst_percent=D('18'), current_stock=0, center=self.center)

    def receive(self, qty, batch, days, product=None):
        """Put stock in the way a goods receipt does."""
        from inventory.stock import add_batch
        product = product or self.product
        product.current_stock = D(product.current_stock) + D(qty)
        product.save(update_fields=['current_stock'])
        return add_batch(product, D(qty), batch, TODAY + datetime.timedelta(days=days))

    def sell(self, qty):
        client = Client.objects.create(first_name='A', phone='9990000001', center=self.center)
        staff = StaffMember.objects.create(center=self.center, first_name='R', designation='S')
        line = {'content_type': 'inventory.product', 'object_id': self.product.id, 'description': 'Serum',
                'unit_price': '590.00', 'discount': '0.00', 'quantity': qty, 'staff_members': [staff.id]}
        return bill_and_pay(self.api, {'client': client.id, 'center': self.center.id, 'status': 'paid',
                                       'discount': '0.00', 'items': [line]})


class QuantityTests(Base):
    def test_a_fractional_consumable_is_deducted_exactly(self):
        """X8: 0.5 ml per service used to truncate to 0 and consume nothing."""
        self.product.current_stock = D('100')
        self.product.unit = 'ml'
        self.product.save()
        svc = ServiceMaster.objects.create(name='Spa', default_price=D('1000'), tax_percentage=D('5'))
        ServiceBOM.objects.create(service=svc, product_code='SER01', quantity=D('2.5'))
        client = Client.objects.create(first_name='A', phone='9990000002', center=self.center)
        staff = StaffMember.objects.create(center=self.center, first_name='R', designation='S')
        line = {'content_type': 'services.servicemaster', 'object_id': svc.id, 'description': 'Spa',
                'unit_price': '1000.00', 'discount': '0.00', 'quantity': 3, 'staff_members': [staff.id]}
        res = bill_and_pay(self.api, {'client': client.id, 'center': self.center.id, 'status': 'paid',
                                      'discount': '0.00', 'items': [line]})
        self.assertEqual(res.status_code, 201, res.data)
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, D('92.500'))             # 100 - 3 x 2.5
        row = StockTransaction.objects.get(product=self.product)
        self.assertEqual((row.transaction_type, row.quantity_change), ('BOM_CONSUMPTION', D('-7.500')))

    def test_checkout_takes_a_decimal(self):
        self.product.current_stock = D('10')
        self.product.save()
        res = self.api.post('/inventory/api/products/checkout/', {
            'center_id': self.center.id, 'items': [{'product_id': self.product.id, 'quantity': '0.25'}]},
            format='json')
        self.assertEqual(res.status_code, 200, res.data)
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, D('9.750'))


class FefoTests(Base):
    def test_a_sale_takes_the_earliest_expiry_first_and_the_ledger_says_which(self):
        late = self.receive(10, 'B-LATE', 300)
        soon = self.receive(4, 'B-SOON', 30)
        res = self.sell(6)
        self.assertEqual(res.status_code, 201, res.data)
        soon.refresh_from_db()
        late.refresh_from_db()
        self.assertEqual((soon.quantity, late.quantity), (D('0'), D('8')))      # 4 from soon, 2 from late
        rows = list(StockTransaction.objects.filter(product=self.product, transaction_type='SALE')
                    .order_by('id').values_list('batch__batch_number', 'quantity_change'))
        self.assertEqual(rows, [('B-SOON', D('-4')), ('B-LATE', D('-2'))])
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, D('8'))

    def test_unbatched_stock_is_used_after_the_batches(self):
        self.product.current_stock = D('5')                  # five old units with no batch
        self.product.save()
        batch = self.receive(3, 'B1', 60)
        self.sell(4)
        batch.refresh_from_db()
        self.assertEqual(batch.quantity, D('0'))             # batch first
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, D('4'))

    def test_a_goods_receipt_makes_a_batch(self):
        from inventory.models import PurchaseOrder, PurchaseOrderItem, Vendor
        vendor = Vendor.objects.create(name='V', center=self.center)
        po = PurchaseOrder.objects.create(vendor=vendor, center=self.center, status='Ordered')
        item = PurchaseOrderItem.objects.create(purchase_order=po, product=self.product, quantity=10,
                                                rate=D('300'), total_price=D('3000'))
        res = self.api.post(f'/inventory/api/purchase-orders/{po.id}/receive_grpo/', {'items': [{
            'id': item.id, 'received_quantity': 6, 'batch_number': 'LOT9',
            'expiry_date': str(TODAY + datetime.timedelta(days=90))}]}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        batch = StockBatch.objects.get(product=self.product)
        self.assertEqual((batch.batch_number, batch.quantity), ('LOT9', D('6')))

    def _receipt(self, **line):
        from inventory.models import PurchaseOrder, PurchaseOrderItem, Vendor
        vendor = Vendor.objects.create(name='V2', center=self.center)
        po = PurchaseOrder.objects.create(vendor=vendor, center=self.center, status='Ordered')
        item = PurchaseOrderItem.objects.create(purchase_order=po, product=self.product, quantity=10,
                                                rate=D('300'), total_price=D('3000'))
        body = {'id': item.id, 'received_quantity': 2, 'batch_number': 'LOTX'}
        body.update(line)
        return self.api.post(f'/inventory/api/purchase-orders/{po.id}/receive_grpo/', {'items': [body]}, format='json')

    def test_a_batch_cannot_expire_before_it_was_made(self):
        res = self._receipt(mfg_date=str(TODAY), expiry_date=str(TODAY - datetime.timedelta(days=30)))
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn('before the manufacturing date', res.data['error'])
        self.assertFalse(StockBatch.objects.filter(product=self.product).exists())

    def test_an_already_expired_batch_is_not_received_into_stock(self):
        res = self._receipt(expiry_date=str(TODAY - datetime.timedelta(days=1)))
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn('already expired', res.data['error'])

    def test_a_garbled_date_is_refused_not_a_server_error(self):
        self.assertEqual(self._receipt(expiry_date='soon').status_code, 400)
        self.assertEqual(self._receipt(mfg_date='31/02/2026').status_code, 400)

    def test_a_batch_with_no_dates_is_still_received(self):
        self.assertEqual(self._receipt().status_code, 200)


from pos_backend.testing import receive_transfer  # noqa: E402


class TransferTests(Base):
    def make(self, qty=5):
        transfer = StockTransfer.objects.create(from_center=self.center, to_center=self.other, status='Draft')
        StockTransferItem.objects.create(transfer=transfer, product=self.product, quantity=D(qty))
        return transfer

    def patch(self, transfer, status):
        return self.api.patch(f'/inventory/api/stock_transfers/{transfer.id}/', {'status': status}, format='json')

    def patch_as_receiver(self, transfer, status):
        from accounts.models import CustomUser
        from pos_backend.testing import full_access_permissions
        from salon_admin.models import Role
        user = CustomUser.objects.filter(email='receiver@test.com').first() or CustomUser.objects.create_user(
            email='receiver@test.com', password='pw', full_name='Receiver', center=self.other,
            role=Role.objects.create(name='Receiver', permissions=full_access_permissions()))
        client = APIClient()
        client.force_authenticate(user)
        if status == 'Completed':
            return receive_transfer(client, transfer.id)
        return client.patch(f'/inventory/api/stock_transfers/{transfer.id}/', {'status': status}, format='json')

    def test_dispatch_takes_from_the_source_and_receive_gives_to_the_destination(self):
        self.receive(10, 'B1', 60)
        transfer = self.make(4)
        self.assertEqual(self.patch(transfer, 'In Transit').status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, D('6'))
        self.assertFalse(Product.objects.filter(center=self.other, product_code='SER01').exists())   # on the road

        # the person who sent it does not receive it: someone else signs it in
        self.assertEqual(self.patch(transfer, 'Completed').status_code, 403)
        self.assertEqual(self.patch_as_receiver(transfer, 'Completed').status_code, 200)
        target = Product.objects.get(center=self.other, product_code='SER01')
        self.assertEqual(target.current_stock, D('4'))
        batch = StockBatch.objects.get(product=target)
        self.assertEqual((batch.batch_number, batch.quantity), ('B1', D('4')))            # same batch arrives
        types = sorted(StockTransaction.objects.filter(notes__contains=f'ST-{transfer.id}')
                       .values_list('transaction_type', flat=True))
        self.assertEqual(types, ['TRANSFER_IN', 'TRANSFER_OUT'])

    def test_a_draft_cannot_be_signed_in_without_being_sent_and_billed(self):
        self.receive(10, 'B1', 60)
        transfer = self.make(3)
        # the plain status update no longer signs a transfer in: it is sent first, then received with the bill
        self.assertEqual(self.patch(transfer, 'Completed').status_code, 400)
        self.assertFalse(Product.objects.filter(center=self.other, product_code='SER01').exists())
        self.assertEqual(self.patch(transfer, 'In Transit').status_code, 200)
        self.assertEqual(self.patch_as_receiver(transfer, 'Completed').status_code, 200)
        self.assertEqual(Product.objects.get(center=self.other, product_code='SER01').current_stock, D('3'))

    def test_cannot_dispatch_more_than_is_in_stock(self):
        self.receive(2, 'B1', 60)
        transfer = self.make(5)
        self.assertEqual(self.patch(transfer, 'In Transit').status_code, 400)
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, D('2'))

    def test_a_dispatched_transfer_cannot_go_back(self):
        self.receive(10, 'B1', 60)
        transfer = self.make(2)
        self.patch(transfer, 'In Transit')
        self.assertEqual(self.patch(transfer, 'Draft').status_code, 400)

    def test_only_the_receiving_branch_can_receive(self):
        self.receive(10, 'B1', 60)
        transfer = self.make(2)
        self.patch(transfer, 'In Transit')
        sender = CustomUser.objects.create_user(
            email='x@test.com', password='pw', full_name='X',
            role=Role.objects.create(name='Store', permissions={'inventory': {
                'stock_transfers': {'read': True, 'create': True, 'update': True}}}), center=self.center)
        sender.centers.add(self.center)
        api = APIClient()
        api.force_authenticate(sender)
        res = api.patch(f'/inventory/api/stock_transfers/{transfer.id}/', {'status': 'Completed'}, format='json')
        self.assertEqual(res.status_code, 403)

