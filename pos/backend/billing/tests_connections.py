"""Billing's connections to Services, Marketing, Inventory, Staff and Appointments.

From the 2026-10-21 connections check (CONNECTIONS-CHECK-2026-10-21.md): the
till hides what a branch does not sell, but the server must refuse it too, and
the records a bill touches must stay in step with the bill.
"""
import datetime
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from appointments.models import Appointment
from billing.models import BillChangeLog, Invoice
from clients.models import Client
from inventory.models import Product, StockBatch, StockTransaction
from marketing.models import Membership, Package, Promotion, ValueCard
from pos_backend import app_settings
from pos_backend.models import AppSetting
from pos_backend.testing import bill_and_pay, full_access_permissions
from salon_admin.models import Center, Role
from services.models import CenterService, ServiceBOM, ServiceMaster
from staff.models import ServiceLog, StaffMember, StaffTransfer
from staff.utils import sync_staff_transfers_and_tools

TODAY = datetime.date.today()


class Base(TestCase):
    def setUp(self):
        app_settings.invalidate()
        self.addCleanup(app_settings.invalidate)
        self.center = Center.objects.create(center_name='Saket', display_name='Saket',
                                            gst_number='07AAAAA0000A1Z5', sap_warehouse_code='WH-SAKET')
        self.other = Center.objects.create(center_name='Noida', display_name='Noida',
                                           gst_number='09AAAAA0000A1Z5', sap_warehouse_code='WH-NOIDA')
        role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.user = CustomUser.objects.create_user(email='o@test.com', password='pw', full_name='O',
                                                   role=role, center=self.center)
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.cl = Client.objects.create(first_name='Asha', phone='9990000001', center=self.center)
        self.staff = StaffMember.objects.create(center=self.center, first_name='Ravi', designation='Stylist')
        self.svc = ServiceMaster.objects.create(name='Hair wash', default_price=Decimal('300'),
                                                tax_percentage=Decimal('18'))

    def set(self, key, value):
        AppSetting.objects.update_or_create(key=key, scope='global', defaults={'value': value})
        app_settings.invalidate()

    def payload(self, ct, obj_id, desc, price, **extra):
        p = {'client': self.cl.id, 'center': self.center.id, 'status': 'paid', 'discount': '0.00',
             'items': [{'content_type': ct, 'object_id': obj_id, 'description': desc,
                        'unit_price': str(price), 'discount': '0.00', 'quantity': 1,
                        'tax_percentage': '18', 'staff_members': [self.staff.id]}]}
        p.update(extra)
        return p

    def svc_payload(self, svc=None, **extra):
        s = svc or self.svc
        return self.payload('services.servicemaster', s.id, s.name, s.default_price, **extra)

    def product(self, **extra):
        values = dict(name='Serum', product_code='SER', price=Decimal('100'), mrp=Decimal('500'),
                      gst_percent=Decimal('18'), current_stock=5, center=self.center)
        values.update(extra)
        return Product.objects.create(**values)

    def appointment(self, center=None, status='Scheduled', client=None, phone='9990000001'):
        return Appointment.objects.create(center=center or self.center, client=client, client_phone=phone,
                                          client_name='X', date=TODAY, start_time=datetime.time(11, 0),
                                          status=status)


# ─── Services ─────────────────────────────────────────────────────────────

class ServiceAvailability(Base):
    def test_service_switched_off_at_the_branch_is_refused(self):
        CenterService.objects.create(center=self.center, service=self.svc, price=Decimal('500'), is_active=False)
        res = bill_and_pay(self.api, self.svc_payload())
        self.assertEqual(res.status_code, 400, getattr(res, 'data', None))
        self.assertIn('switched off', str(res.data))
        self.assertEqual(Invoice.objects.exclude(status='draft').count(), 0)

    def test_it_is_still_sold_at_a_branch_where_it_is_on(self):
        CenterService.objects.create(center=self.other, service=self.svc, is_active=False)
        CenterService.objects.create(center=self.center, service=self.svc, price=Decimal('500'))
        # The till sends the branch price; the server's list price for the branch is the same,
        # so nothing is logged as a price change.
        res = bill_and_pay(self.api, self.payload('services.servicemaster', self.svc.id, 'Hair wash', 500))
        self.assertEqual(res.status_code, 201, getattr(res, 'data', None))
        self.assertEqual(Decimal(res.data['subtotal']), Decimal('500.00'))
        self.assertFalse(BillChangeLog.objects.filter(action='Price Adjusted').exists())

    def test_branch_only_service_of_another_branch_is_refused(self):
        far = ServiceMaster.objects.create(name='Noida special', default_price=Decimal('100'),
                                           tax_percentage=Decimal('18'), level='Center')
        far.centers.add(self.other)
        res = bill_and_pay(self.api, self.svc_payload(far))
        self.assertEqual(res.status_code, 400)

    def test_branch_only_service_is_sold_at_its_own_branch(self):
        mine = ServiceMaster.objects.create(name='Saket special', default_price=Decimal('100'),
                                            tax_percentage=Decimal('18'), level='Center')
        mine.centers.add(self.center)
        res = bill_and_pay(self.api, self.svc_payload(mine))
        self.assertEqual(res.status_code, 201, getattr(res, 'data', None))

    def test_a_package_redemption_is_not_blocked_by_a_switched_off_service(self):
        # The client already paid for that service when they bought the package.
        from clients.models import ClientPackage
        pack = Package.objects.create(name='Wash x3', price=Decimal('900'), validity_days=30,
                                      services_json=[{'id': self.svc.id, 'pkgQty': 3}])
        ClientPackage.objects.create(client=self.cl, package=pack, center=self.center,
                                     services_remaining={str(self.svc.id): 3},
                                     start_date=TODAY, expiry_date=TODAY + datetime.timedelta(days=30))
        CenterService.objects.create(center=self.center, service=self.svc, is_active=False)
        payload = self.svc_payload()
        payload['items'][0]['is_redemption'] = True
        res = bill_and_pay(self.api, payload)
        self.assertEqual(res.status_code, 201, getattr(res, 'data', None))


# ─── Marketing ────────────────────────────────────────────────────────────

class MarketingAvailability(Base):
    def test_switched_off_membership_is_refused(self):
        m = Membership.objects.create(name='Gold', pre_tax_price=Decimal('1000'), expiry_days=30,
                                      discount_percent=Decimal('10'), is_active=False)
        res = bill_and_pay(self.api, self.payload('marketing.membership', m.id, 'Gold', 1000))
        self.assertEqual(res.status_code, 400)

    def test_switched_off_package_and_card_are_refused(self):
        p = Package.objects.create(name='Pack', price=Decimal('900'), validity_days=30, is_active=False,
                                   services_json=[{'id': self.svc.id, 'pkgQty': 3}])
        v = ValueCard.objects.create(title='Card', pre_tax_price=Decimal('1000'), value=Decimal('1200'),
                                     expiry_days=365, is_active=False)
        self.assertEqual(bill_and_pay(self.api, self.payload('marketing.package', p.id, 'Pack', 900)).status_code, 400)
        self.assertEqual(bill_and_pay(self.api, self.payload('marketing.valuecard', v.id, 'Card', 1000)).status_code, 400)

    def test_package_of_another_branch_is_refused(self):
        p = Package.objects.create(name='Noida pack', price=Decimal('900'), validity_days=30,
                                   level='Center', center=self.other,
                                   services_json=[{'id': self.svc.id, 'pkgQty': 3}])
        res = bill_and_pay(self.api, self.payload('marketing.package', p.id, 'Noida pack', 900))
        self.assertEqual(res.status_code, 400)

    def test_own_branch_and_organisation_wide_perks_are_sold(self):
        mine = Package.objects.create(name='Saket pack', price=Decimal('900'), validity_days=30,
                                      level='Center', center=self.center,
                                      services_json=[{'id': self.svc.id, 'pkgQty': 3}])
        org = Package.objects.create(name='Org pack', price=Decimal('900'), validity_days=30,
                                     services_json=[{'id': self.svc.id, 'pkgQty': 3}])
        for pack in (mine, org):
            res = bill_and_pay(self.api, self.payload('marketing.package', pack.id, pack.name, 900))
            self.assertEqual(res.status_code, 201, getattr(res, 'data', None))

    def _promo(self, **config):
        return Promotion.objects.create(
            name='Wash 20', promo_type='Discount', discount_type='Percentage', discount_value=Decimal('20'),
            start_date=TODAY - datetime.timedelta(days=1), end_date=TODAY + datetime.timedelta(days=5),
            config=config)

    def test_campaign_follows_a_renamed_service(self):
        # Saved through the API, as the Marketing screen does: the service's id is worked out.
        res = self.api.post('/marketing/api/promotions/', {
            'name': 'Wash 20', 'promo_type': 'Discount', 'discount_type': 'Percentage',
            'discount_value': '20', 'level': 'Organisation',
            'start_date': TODAY.isoformat(), 'end_date': (TODAY + datetime.timedelta(days=5)).isoformat(),
            'config': {'target': 'Specific Service', 'specific_service': 'Hair wash'}}, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data['config']['specific_service_ids'], [self.svc.id])
        self.svc.name = 'Hair wash & blow dry'
        self.svc.save()
        bill = bill_and_pay(self.api, self.svc_payload(promo_id=res.data['id']))
        self.assertEqual(bill.status_code, 201, getattr(bill, 'data', None))
        self.assertEqual(Decimal(bill.data['discount']), Decimal('60.00'))
        # And the form shows the service's name today, so editing it keeps the choice.
        shown = self.api.get(f"/marketing/api/promotions/{res.data['id']}/")
        self.assertEqual(shown.data['config']['specific_service'], 'Hair wash & blow dry')

    def test_a_campaign_saved_before_ids_still_matches_by_name(self):
        promo = self._promo(target='Specific Service', specific_service='Hair wash')
        bill = bill_and_pay(self.api, self.svc_payload(promo_id=promo.id))
        self.assertEqual(bill.status_code, 201, getattr(bill, 'data', None))
        self.assertEqual(Decimal(bill.data['discount']), Decimal('60.00'))

    def test_campaign_does_not_hit_a_different_service(self):
        res = self.api.post('/marketing/api/promotions/', {
            'name': 'Wash 20', 'promo_type': 'Discount', 'discount_type': 'Percentage',
            'discount_value': '20', 'level': 'Organisation',
            'start_date': TODAY.isoformat(), 'end_date': (TODAY + datetime.timedelta(days=5)).isoformat(),
            'config': {'target': 'Specific Service', 'specific_service': 'Hair wash'}}, format='json')
        other = ServiceMaster.objects.create(name='Facial', default_price=Decimal('500'),
                                             tax_percentage=Decimal('18'))
        bill = bill_and_pay(self.api, self.svc_payload(other, promo_id=res.data['id']))
        self.assertEqual(bill.status_code, 400)
        self.assertIn('which is not on this bill', str(bill.data))

    def test_trigger_deal_follows_renamed_services(self):
        spa = ServiceMaster.objects.create(name='Spa', default_price=Decimal('500'), tax_percentage=Decimal('18'))
        res = self.api.post('/marketing/api/promotions/', {
            'name': 'Combo', 'promo_type': 'Trigger', 'level': 'Organisation',
            'start_date': TODAY.isoformat(), 'end_date': (TODAY + datetime.timedelta(days=5)).isoformat(),
            'config': {'trigger_services': ['Hair wash', 'Spa'], 'trigger_discount': '10'}}, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data['config']['trigger_service_ids'], [[self.svc.id], [spa.id]])
        spa.name = 'Deep spa'
        spa.save()
        payload = self.svc_payload(promo_id=res.data['id'])
        payload['items'].append(self.svc_payload(spa)['items'][0])
        bill = bill_and_pay(self.api, payload)
        self.assertEqual(bill.status_code, 201, getattr(bill, 'data', None))
        self.assertEqual(Decimal(bill.data['discount']), Decimal('80.00'))


# ─── Inventory ────────────────────────────────────────────────────────────

class InventoryLinks(Base):
    def test_switched_off_product_is_refused(self):
        p = self.product(is_active=False)
        res = bill_and_pay(self.api, self.payload('inventory.product', p.id, 'Serum', 500))
        self.assertEqual(res.status_code, 400)

    def test_product_of_another_branch_is_refused(self):
        p = self.product(center=self.other)
        res = bill_and_pay(self.api, self.payload('inventory.product', p.id, 'Serum', 500))
        self.assertEqual(res.status_code, 400)

    def test_product_with_no_mrp_sells_unless_the_setting_forbids_it(self):
        p = self.product(mrp=None)
        res = bill_and_pay(self.api, self.payload('inventory.product', p.id, 'Serum', 100))
        self.assertEqual(res.status_code, 201, getattr(res, 'data', None))      # today's behaviour
        self.set('billing.sell_products_without_mrp', 'false')
        res = bill_and_pay(self.api, self.payload('inventory.product', p.id, 'Serum', 100))
        self.assertEqual(res.status_code, 400)
        self.assertIn('no MRP', str(res.data))

    def test_cancel_gives_each_batch_back_what_it_took(self):
        p = self.product(current_stock=5)
        soon = StockBatch.objects.create(product=p, batch_number='B1', quantity=Decimal('2'),
                                         expiry_date=TODAY + datetime.timedelta(days=30))
        later = StockBatch.objects.create(product=p, batch_number='B2', quantity=Decimal('3'),
                                          expiry_date=TODAY + datetime.timedelta(days=90))
        payload = self.payload('inventory.product', p.id, 'Serum', 500)
        payload['items'][0]['quantity'] = 3          # 2 from B1, 1 from B2
        res = bill_and_pay(self.api, payload)
        self.assertEqual(res.status_code, 201, getattr(res, 'data', None))
        soon.refresh_from_db(), later.refresh_from_db()
        self.assertEqual((soon.quantity, later.quantity), (Decimal('0'), Decimal('2')))

        out = self.api.post(f"/billing/invoices/{res.data['id']}/cancel/", {'reason': 'mistake'}, format='json')
        self.assertEqual(out.status_code, 200, getattr(out, 'data', None))
        soon.refresh_from_db(), later.refresh_from_db(), p.refresh_from_db()
        self.assertEqual(p.current_stock, Decimal('5'))
        self.assertEqual((soon.quantity, later.quantity), (Decimal('2'), Decimal('3')))
        back = StockTransaction.objects.filter(transaction_type='REFUND', product=p)
        self.assertEqual(sorted((r.batch_id, r.quantity_change) for r in back),
                         sorted([(soon.id, Decimal('2')), (later.id, Decimal('1'))]))

    def test_the_same_product_on_two_lines_is_given_back_once(self):
        p = self.product(current_stock=6)
        batch = StockBatch.objects.create(product=p, batch_number='B1', quantity=Decimal('6'),
                                          expiry_date=TODAY + datetime.timedelta(days=30))
        payload = self.payload('inventory.product', p.id, 'Serum', 500)
        payload['items'].append(dict(payload['items'][0], quantity=2))
        res = bill_and_pay(self.api, payload)
        self.assertEqual(res.status_code, 201, getattr(res, 'data', None))
        batch.refresh_from_db()
        self.assertEqual(batch.quantity, Decimal('3'))
        self.api.post(f"/billing/invoices/{res.data['id']}/cancel/", {'reason': 'mistake'}, format='json')
        batch.refresh_from_db(), p.refresh_from_db()
        self.assertEqual((p.current_stock, batch.quantity), (Decimal('6'), Decimal('6')))

    def test_a_sale_with_no_batches_comes_back_as_before(self):
        p = self.product(current_stock=5)
        res = bill_and_pay(self.api, self.payload('inventory.product', p.id, 'Serum', 500))
        self.api.post(f"/billing/invoices/{res.data['id']}/cancel/", {'reason': 'mistake'}, format='json')
        p.refresh_from_db()
        self.assertEqual(p.current_stock, Decimal('5'))

    def test_a_service_consumable_stays_used_when_the_bill_is_cancelled(self):
        """Owner ruling 2026-10-21: what a haircut or facial used cannot come back."""
        gel = self.product(name='Gel', product_code='GEL', current_stock=5)
        batch = StockBatch.objects.create(product=gel, batch_number='G1', quantity=Decimal('5'),
                                          expiry_date=TODAY + datetime.timedelta(days=30))
        ServiceBOM.objects.create(service=self.svc, product_code='GEL', quantity=Decimal('2'))
        res = bill_and_pay(self.api, self.svc_payload())
        self.assertEqual(res.status_code, 201, getattr(res, 'data', None))
        batch.refresh_from_db()
        self.assertEqual(batch.quantity, Decimal('3'))
        self.api.post(f"/billing/invoices/{res.data['id']}/cancel/", {'reason': 'mistake'}, format='json')
        batch.refresh_from_db(), gel.refresh_from_db()
        self.assertEqual((gel.current_stock, batch.quantity), (Decimal('3'), Decimal('3')))
        self.assertFalse(StockTransaction.objects.filter(transaction_type='REFUND', product=gel).exists())


# ─── Staff ────────────────────────────────────────────────────────────────

class StaffLinks(Base):
    def test_deactivated_staff_cannot_earn_a_line(self):
        StaffMember.objects.filter(pk=self.staff.pk).update(is_active=False)
        res = bill_and_pay(self.api, self.svc_payload())
        self.assertEqual(res.status_code, 400)
        self.assertIn('no longer active', str(res.data))

    def test_value_card_sale_is_logged_as_a_value_card(self):
        vc = ValueCard.objects.create(title='Gold card', pre_tax_price=Decimal('1000'), value=Decimal('1200'),
                                      expiry_days=365, gst_percent=Decimal('18'))
        res = bill_and_pay(self.api, self.payload('marketing.valuecard', vc.id, 'Gold card', 1000))
        self.assertEqual(res.status_code, 201, getattr(res, 'data', None))
        self.assertEqual(ServiceLog.objects.get().service_type, 'Value Card')

    def test_commission_report_counts_cards_apart_from_products(self):
        vc = ValueCard.objects.create(title='Gold card', pre_tax_price=Decimal('1000'), value=Decimal('1200'),
                                      expiry_days=365, gst_percent=Decimal('18'))
        bill_and_pay(self.api, self.payload('marketing.valuecard', vc.id, 'Gold card', 1000))
        res = self.api.get('/staff/api/members/commission_report/',
                           {'start_date': TODAY.isoformat(), 'end_date': TODAY.isoformat()})
        self.assertEqual(res.status_code, 200, res.data)
        row = res.data['results'][0]
        self.assertEqual(Decimal(str(row['product_revenue'])), Decimal('0'))
        self.assertEqual(Decimal(str(row['value_card_revenue'])), Decimal('1000.00'))
        self.assertEqual(Decimal(str(row['total_revenue'])), Decimal('1000.00'))


class TransferDates(Base):
    def transfer(self, start, end=None, kind='Temporary'):
        body = {'staff': self.staff.id, 'from_center': self.center.id, 'to_center': self.other.id,
                'transfer_type': kind, 'start_date': start.isoformat(), 'reason': 'cover'}
        if end:
            body['end_date'] = end.isoformat()
        return self.api.post('/staff/api/transfers/', body, format='json')

    def where(self):
        self.staff.refresh_from_db()
        return self.staff.center_id

    def test_a_future_transfer_waits_for_its_start_date(self):
        start = TODAY + datetime.timedelta(days=7)
        res = self.transfer(start, start + datetime.timedelta(days=3))
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(self.where(), self.center.id)
        sync_staff_transfers_and_tools()
        self.assertEqual(self.where(), self.center.id)            # still not started

        StaffTransfer.objects.update(start_date=TODAY)            # the day arrives
        sync_staff_transfers_and_tools()
        self.assertEqual(self.where(), self.other.id)
        self.assertTrue(StaffTransfer.objects.get().applied)

    def test_cancelling_a_transfer_that_never_started_moves_nobody(self):
        start = TODAY + datetime.timedelta(days=7)
        res = self.transfer(start, start + datetime.timedelta(days=3))
        self.staff.center = self.other                             # moved by hand meanwhile
        self.staff.save()
        out = self.api.patch(f"/staff/api/transfers/{res.data['id']}/", {'status': 'Cancelled'}, format='json')
        self.assertEqual(out.status_code, 200, out.data)
        self.assertEqual(self.where(), self.other.id)

    def test_temporary_transfer_is_still_in_force_on_its_end_date(self):
        res = self.transfer(TODAY - datetime.timedelta(days=2), TODAY)
        self.assertEqual(res.status_code, 201, res.data)
        sync_staff_transfers_and_tools()
        self.assertEqual(self.where(), self.other.id)              # last day at the other branch
        self.assertEqual(StaffTransfer.objects.get().status, 'Active')

    def test_temporary_transfer_ends_the_day_after_its_end_date(self):
        res = self.transfer(TODAY - datetime.timedelta(days=3), TODAY - datetime.timedelta(days=1))
        self.assertEqual(res.status_code, 201, res.data)
        sync_staff_transfers_and_tools()
        self.assertEqual(self.where(), self.center.id)
        self.assertEqual(StaffTransfer.objects.get().status, 'Completed')

    def test_a_started_transfer_is_not_reapplied_after_a_hand_move_back(self):
        self.transfer(TODAY, kind='Permanent')
        self.assertEqual(self.where(), self.other.id)
        self.staff.center = self.center                            # moved back by hand
        self.staff.save()
        sync_staff_transfers_and_tools()
        self.assertEqual(self.where(), self.center.id)

    def test_billing_follows_the_person_once_the_transfer_starts(self):
        self.transfer(TODAY, TODAY + datetime.timedelta(days=2))
        payload = self.svc_payload()
        payload['center'] = self.other.id
        res = bill_and_pay(self.api, payload)
        self.assertEqual(res.status_code, 201, getattr(res, 'data', None))
        self.assertEqual(ServiceLog.objects.get().center_id, self.other.id)


# ─── Appointments ─────────────────────────────────────────────────────────

class AppointmentLinks(Base):
    def test_billing_the_visit_completes_it_and_links_the_bill(self):
        appt = self.appointment(client=self.cl)
        res = bill_and_pay(self.api, self.svc_payload(appointment=appt.id))
        self.assertEqual(res.status_code, 201, getattr(res, 'data', None))
        appt.refresh_from_db()
        self.assertEqual(appt.status, 'Completed')
        self.assertEqual(Invoice.objects.exclude(status='draft').get().appointment_id, appt.id)

    def test_an_appointment_with_only_a_phone_matches_the_client_by_number(self):
        appt = self.appointment(client=None, phone='+91 99900 00001')
        res = bill_and_pay(self.api, self.svc_payload(appointment=appt.id))
        self.assertEqual(res.status_code, 201, getattr(res, 'data', None))

    def test_another_branchs_appointment_is_refused(self):
        appt = self.appointment(center=self.other)
        res = bill_and_pay(self.api, self.svc_payload(appointment=appt.id))
        self.assertEqual(res.status_code, 400)
        appt.refresh_from_db()
        self.assertEqual(appt.status, 'Scheduled')

    def test_another_clients_appointment_is_refused(self):
        stranger = Client.objects.create(first_name='Zed', phone='9990000009', center=self.center)
        for appt in (self.appointment(client=stranger, phone='9990000009'),
                     self.appointment(client=None, phone='9990000009')):
            res = bill_and_pay(self.api, self.svc_payload(appointment=appt.id))
            self.assertEqual(res.status_code, 400, getattr(res, 'data', None))
            appt.refresh_from_db()
            self.assertEqual(appt.status, 'Scheduled')

    def test_cancelled_noshow_and_billed_appointments_are_refused(self):
        for status in ('Cancelled', 'No-show', 'Completed'):
            appt = self.appointment(client=self.cl, status=status)
            res = bill_and_pay(self.api, self.svc_payload(appointment=appt.id))
            self.assertEqual(res.status_code, 400, (status, getattr(res, 'data', None)))
            appt.refresh_from_db()
            self.assertEqual(appt.status, status)

    def test_appointment_id_is_the_same_as_appointment(self):
        appt = self.appointment(client=self.cl)
        res = bill_and_pay(self.api, self.svc_payload(appointment_id=appt.id))
        self.assertEqual(res.status_code, 201, getattr(res, 'data', None))
        inv = Invoice.objects.exclude(status='draft').get()
        self.assertEqual(inv.appointment_id, appt.id)
        out = self.api.post(f'/billing/invoices/{inv.id}/cancel/', {'reason': 'mistake'}, format='json')
        self.assertEqual(out.status_code, 200)
        appt.refresh_from_db()
        self.assertEqual(appt.status, 'Cancelled')

    def test_appointment_id_is_checked_like_appointment(self):
        appt = self.appointment(center=self.other)
        res = bill_and_pay(self.api, self.svc_payload(appointment_id=appt.id))
        self.assertEqual(res.status_code, 400)

    def test_a_held_bill_keeps_its_appointment_until_it_is_completed(self):
        appt = self.appointment(client=self.cl)
        held = self.api.post('/billing/invoices/', dict(self.svc_payload(appointment=appt.id), status='draft'),
                             format='json')
        self.assertEqual(held.status_code, 201, held.data)
        appt.refresh_from_db()
        self.assertEqual(appt.status, 'Scheduled')
        done = self.api.put(f"/billing/invoices/{held.data['id']}/",
                            dict(self.svc_payload(appointment=appt.id), status='paid',
                                 payments=[{'payment_method': 'Cash', 'amount': held.data['total_amount']}]),
                            format='json')
        self.assertEqual(done.status_code, 200, done.data)
        appt.refresh_from_db()
        self.assertEqual(appt.status, 'Completed')

    def test_cancelling_a_bill_leaves_a_noshow_alone(self):
        appt = self.appointment(client=self.cl)
        res = bill_and_pay(self.api, self.svc_payload(appointment=appt.id))
        Appointment.objects.filter(pk=appt.id).update(status='No-show')
        self.api.post(f"/billing/invoices/{res.data['id']}/cancel/", {'reason': 'mistake'}, format='json')
        appt.refresh_from_db()
        self.assertEqual(appt.status, 'No-show')


# ─── Clients ──────────────────────────────────────────────────────────────

class ClientRequirement(Base):
    def test_a_walk_in_bill_is_allowed_by_default(self):
        payload = self.svc_payload()
        payload.pop('client')
        res = bill_and_pay(self.api, payload)
        self.assertEqual(res.status_code, 201, getattr(res, 'data', None))

    def test_a_walk_in_buys_services_and_products_only(self):
        m = Membership.objects.create(name='Gold', pre_tax_price=Decimal('1000'), expiry_days=30,
                                      discount_percent=Decimal('10'))
        pack = Package.objects.create(name='Pack', price=Decimal('900'), validity_days=30,
                                      services_json=[{'id': self.svc.id, 'pkgQty': 3}])
        card = ValueCard.objects.create(title='Card', pre_tax_price=Decimal('1000'), value=Decimal('1200'),
                                        expiry_days=365)
        for ct, obj, desc, price in (('marketing.membership', m.id, 'Gold', 1000),
                                     ('marketing.package', pack.id, 'Pack', 900),
                                     ('marketing.valuecard', card.id, 'Card', 1000)):
            payload = self.payload(ct, obj, desc, price)
            payload.pop('client')
            res = bill_and_pay(self.api, payload)
            self.assertEqual(res.status_code, 400, (ct, getattr(res, 'data', None)))
            self.assertIn('needs a client', str(res.data))
        # A service and a product are fine.
        prod = self.product()
        payload = self.payload('inventory.product', prod.id, 'Serum', 500)
        payload.pop('client')
        self.assertEqual(bill_and_pay(self.api, payload).status_code, 201)

    def test_with_the_setting_on_a_bill_needs_a_client_to_complete(self):
        self.set('billing.require_client', 'true')
        payload = self.svc_payload()
        payload.pop('client')
        res = bill_and_pay(self.api, payload)
        self.assertEqual(res.status_code, 400)
        self.assertIn('client', str(res.data))
        # Holding one without a client is still fine; it cannot be completed.
        held = self.api.post('/billing/invoices/', dict(payload, status='draft'), format='json')
        self.assertEqual(held.status_code, 201, held.data)

    def test_with_the_setting_on_a_bill_with_a_client_completes(self):
        self.set('billing.require_client', 'true')
        res = bill_and_pay(self.api, self.svc_payload())
        self.assertEqual(res.status_code, 201, getattr(res, 'data', None))
