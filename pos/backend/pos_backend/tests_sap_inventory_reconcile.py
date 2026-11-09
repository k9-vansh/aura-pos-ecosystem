"""
Does SAP's stock end up where the POS's stock is?

Stock is moved through the real screens (bills, cancels, salon use, stock count,
purchase receipts, transfers), SAP is replaced by a simulator that keeps its own
stock from the documents it is sent, and the two are compared item by item.
Nothing touches the network.
"""
import re
from decimal import Decimal
from unittest import mock

from django.test import override_settings

from billing.models import Invoice
from inventory.models import Product, PurchaseOrder, PurchaseOrderItem, Vendor
from marketing.models import Membership  # noqa: F401  (keeps app registry complete)
from pos_backend import jobs
from pos_backend import tests_numbers_everywhere as base
from pos_backend.tests_sap_billing_reconcile import drain
from pos_backend.tests_sap_integrity import FakeResponse, FakeSap
from salon_admin.models import Center
from services.models import ServiceBOM, ServiceMaster


class StockSim(FakeSap):
    """FakeSap that also keeps SAP's stock from what it is sent."""

    def __init__(self, opening):
        super().__init__()
        self.opening = dict(opening)           # (item, warehouse) -> qty

    def net(self):
        stock = dict(self.opening)

        def add(item, wh, qty):
            stock[(item, wh)] = stock.get((item, wh), Decimal(0)) + Decimal(str(qty))

        for method, url, body in self.calls:
            if method != 'POST' or not body:
                continue
            kind = url.rsplit('/', 1)[-1]
            for ln in body.get('DocumentLines', []) or []:
                qty = Decimal(str(ln.get('Quantity') or 0))
                if kind == 'Invoices':
                    if ln.get('TreeType') == 'iSalesTree':
                        continue
                    add(ln['ItemCode'], ln['WarehouseCode'], -qty)
                elif kind in ('CreditNotes', 'PurchaseDeliveryNotes', 'InventoryGenEntries'):
                    add(ln['ItemCode'], ln['WarehouseCode'], qty)
                elif kind == 'InventoryGenExits':
                    add(ln['ItemCode'], ln['WarehouseCode'], -qty)
            if kind == 'StockTransfers':
                for ln in body.get('StockTransferLines', []):
                    qty = Decimal(str(ln['Quantity']))
                    add(ln['ItemCode'], ln['FromWarehouseCode'], -qty)
                    add(ln['ItemCode'], ln['WarehouseCode'], qty)
        return stock

    def __call__(self, method, url, timeout=15, **kwargs):
        found = re.search(r"/Items\('([^']+)'\)\?\$select=ItemWarehouseInfoCollection", url)
        if method == 'GET' and found:
            code = found.group(1)
            rows = [{'WarehouseCode': wh, 'InStock': float(q)} for (c, wh), q in self.net().items() if c == code]
            return FakeResponse(200, {'ItemWarehouseInfoCollection': rows})
        return super().__call__(method, url, timeout=timeout, **kwargs)


@override_settings(SAP_ENABLED=True)
class SapInventoryReconcile(base.Everything):
    test_report = None

    def setUp(self):
        super().setUp()
        self.center.sap_warehouse_code = 'WH-A'
        self.center.save()
        self.centre_b = Center.objects.create(center_name='Noida', display_name='Noida', sap_warehouse_code='WH-B')
        self.problems = []

    def note(self, label, sap, pos):
        ok = Decimal(str(sap)) == Decimal(str(pos))
        print(f"{'ok  ' if ok else 'DIFF'} {label:58s} SAP {sap!s:>10}  POS {pos!s:>10}")
        if not ok:
            self.problems.append(label)

    def test_stock_ties_to_sap(self):
        shampoo = Product.objects.create(name='Shampoo', product_code='SHAMPOO', price=Decimal('349'), mrp=Decimal('349'),
                                         gst_percent=Decimal('18'), current_stock=100, center=self.center)
        gel = Product.objects.create(name='Gel', product_code='GEL', price=Decimal('200'), mrp=Decimal('200'),
                                     gst_percent=Decimal('18'), current_stock=50, center=self.center)
        shampoo_b = Product.objects.create(name='Shampoo', product_code='SHAMPOO', price=Decimal('349'), mrp=Decimal('349'),
                                           gst_percent=Decimal('18'), current_stock=30, center=self.centre_b)
        products = [shampoo, gel, shampoo_b]
        sim = StockSim({(p.product_code, p.center.sap_warehouse_code): Decimal(p.current_stock) for p in products})
        self.sap = sim
        for target, repl in (('pos_backend.sap_utils._send_sap_request', sim),
                             ('pos_backend.sap_utils.get_hana_batches', mock.Mock(return_value=[])),
                             ('pos_backend.sap_utils._resolve_bpl_id', mock.Mock(return_value=1))):
            p = mock.patch(target, repl)
            p.start()
            self.addCleanup(p.stop)

        wash = ServiceMaster.objects.create(name='Hair wash', default_price=Decimal('300'), tax_percentage=Decimal('18'))
        ServiceBOM.objects.create(service=wash, product_code='SHAMPOO', quantity=Decimal('0.25'))
        ServiceBOM.objects.create(service=wash, product_code='GEL', quantity=Decimal('2'))
        self.svc['wash'] = wash

        def product_line(prod, qty):
            return {'content_type': 'inventory.product', 'object_id': prod.id, 'description': prod.name,
                    'unit_price': str(prod.mrp), 'discount': '0', 'quantity': qty, 'tax_percentage': '18',
                    'staff_members': [self.staff1.id]}

        cb = self.captureOnCommitCallbacks(execute=True)
        cb.__enter__()

        def flush():
            nonlocal cb
            cb.__exit__(None, None, None)
            drain()
            cb = self.captureOnCommitCallbacks(execute=True)
            cb.__enter__()

        if True:
            # bills: products and a service that consumes stock
            self.raise_bill([product_line(shampoo, 2), self.line('wash', 3)])
            cancelled_product = self.raise_bill([product_line(shampoo, 1)])
            cancelled_service = self.raise_bill([self.line('wash', 1)])
            for inv in (cancelled_product, cancelled_service):
                r = self.api.post(f'/billing/invoices/{inv.pk}/cancel/', {'reason': 'test'}, format='json')
                self.assertEqual(r.status_code, 200, r.data)
            flush()
            # salon use, and a stock count that finds a difference
            r = self.api.post('/inventory/api/products/checkout/', {'center_id': self.center.id, 'items': [
                {'product_id': gel.id, 'quantity': 4}]}, format='json')
            self.assertEqual(r.status_code, 200, r.data)
            shampoo.refresh_from_db()
            r = self.api.post('/inventory/api/products/audit/', {'center_id': self.center.id, 'items': [
                {'product_id': shampoo.id, 'quantity': 90}]}, format='json')
            self.assertEqual(r.status_code, 200, r.data)
            flush()
            # a purchase order received in two parts
            vendor = Vendor.objects.create(name='Loreal', center=self.center, vendor_code='V000001')
            po = PurchaseOrder.objects.create(vendor=vendor, center=self.center, status='Ordered')
            i1 = PurchaseOrderItem.objects.create(purchase_order=po, product=shampoo, quantity=20, rate=Decimal('150'),
                                                  total_price=Decimal('3000'))
            i2 = PurchaseOrderItem.objects.create(purchase_order=po, product=gel, quantity=10, rate=Decimal('80'),
                                                  total_price=Decimal('800'))
            for rows in ([{'id': i1.id, 'received_quantity': 12}, {'id': i2.id, 'received_quantity': 10}],
                         [{'id': i1.id, 'received_quantity': 8}]):
                r = self.api.post(f'/inventory/api/purchase-orders/{po.id}/receive_grpo/', {'items': rows}, format='json')
                self.assertEqual(r.status_code, 200, r.data)
            flush()
            # a transfer to the other branch
            r = self.api.post('/inventory/api/stock_transfers/', {
                'from_center': self.center.id, 'to_center': self.centre_b.id,
                'items': [{'product': shampoo.id, 'quantity': 5}]}, format='json')
            self.assertEqual(r.status_code, 201, r.data)
            tid = r.data['id']
            r = self.api.patch(f"/inventory/api/stock_transfers/{tid}/", {'status': 'In Transit'}, format='json')
            self.assertEqual(r.status_code, 200, r.data)
            # someone at the receiving branch (not the sender) signs it in with the bill
            from accounts.models import CustomUser
            from pos_backend.testing import full_access_permissions, receive_transfer
            from rest_framework.test import APIClient
            from salon_admin.models import Role
            receiver = CustomUser.objects.create_user(
                email='receiver-b@test.com', password='pw', full_name='Receiver B', center=self.centre_b,
                role=Role.objects.create(name='Receiver B', permissions=full_access_permissions()))
            receiver.centers.add(self.centre_b)
            receiver_api = APIClient()
            receiver_api.force_authenticate(receiver)
            r = receive_transfer(receiver_api, tid)
            self.assertEqual(r.status_code, 200, r.data)
        flush()

        net = sim.net()
        for m_, u, b in sim.calls:
            if m_ == 'POST' and b:
                kind = u.rsplit('/', 1)[-1]
                lines = [(l.get('ItemCode'), l.get('Quantity'), l.get('WarehouseCode'), l.get('TreeType', '')) for l in (b.get('DocumentLines') or b.get('StockTransferLines') or [])]
                if kind not in ('IncomingPayments',):
                    print('  SAP', kind, lines)
        print('\nSAP documents:', sorted({u.rsplit('/', 1)[-1] for m_, u, b in sim.calls if m_ == 'POST'}))
        for prod in products:
            prod.refresh_from_db()
            code, wh = prod.product_code, prod.center.sap_warehouse_code
            self.note(f'{prod.name} at {prod.center.display_name}: stock', net.get((code, wh), 0), prod.current_stock)
        print('PROBLEMS:', self.problems)
        # A cancelled service's ingredients stay used in the POS and in SAP alike
        # (owner ruling 2026-10-21), so the two stocks must agree item by item.
        self.assertEqual(self.problems, [])
