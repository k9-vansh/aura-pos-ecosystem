"""A branch asks for Retail or Technical stock; an approver says yes; the stock
person sends it by a Stock Transfer from wherever they choose; receiving it
fulfils the request. Who may do each step is a permission tick, never a role."""

from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from inventory.models import Product, StockRequest
from notifications.models import Notification
from pos_backend.testing import receive_transfer
from salon_admin.models import Center, Role

D = Decimal
URL = '/inventory/api/stock_requests/'
TRANSFERS = '/inventory/api/stock_transfers/'


def tick(**subs):
    return {'inventory': {k: {'read': True, 'create': True, 'update': True} for k in subs}}


class StockRequestFlowTests(TestCase):
    def setUp(self):
        self.branch = Center.objects.create(center_name='Saket', display_name='Saket', gst_number='07AAAAA0000A1Z5')
        self.other_branch = Center.objects.create(center_name='Noida', display_name='Noida',
                                                  gst_number='09CCCCC0000C1Z5')
        self.warehouse = Center.objects.create(center_name='Main Warehouse', display_name='Main Warehouse',
                                               gst_number='07BBBBB0000B1Z5')

        def user(email, perms, centers, home):
            u = CustomUser.objects.create_user(
                email=email, password='pw', full_name=email.split('@')[0],
                role=Role.objects.create(name=email, permissions=perms), center=home)
            u.centers.add(*centers)
            api = APIClient()
            api.force_authenticate(u)
            return u, api

        self.clerk, self.clerk_api = user('clerk@t.com', tick(stock_requests=1), [self.branch], self.branch)
        self.manager, self.manager_api = user(
            'mgr@t.com', tick(approve_stock_requests=1), [self.branch, self.other_branch], self.branch)
        self.store, self.store_api = user(
            'store@t.com', tick(stock_requests=1, fulfill_stock_requests=1, stock_transfers=1),
            [self.branch, self.other_branch, self.warehouse], self.warehouse)
        self.stranger, self.stranger_api = user('x@t.com', tick(approve_stock_requests=1),
                                                [self.other_branch], self.other_branch)

        def product(code, category, center, stock=0):
            return Product.objects.create(name=code, product_code=code, category=category, price=D('100'),
                                          mrp=D('200'), gst_percent=D('18'), current_stock=stock, center=center)

        self.shampoo = product('SHAMPOO', 'Retail', self.branch)
        self.color = product('COLOR', 'Technical', self.branch)
        self.gel = product('GEL', 'Consumable', self.branch)
        self.blank = product('BLANK', None, self.branch)
        for code, cat in (('SHAMPOO', 'Retail'), ('COLOR', 'Technical')):
            product(code, cat, self.warehouse, stock=50)

    def ask(self, stock_type, *products):
        return self.clerk_api.post(URL, {
            'requesting_center': self.branch.id, 'stock_type': stock_type,
            'items': [{'product': p.id, 'quantity': '3'} for p in products]}, format='json')

    def approved_request(self):
        res = self.ask('Retail', self.shampoo)
        self.manager_api.post(f"{URL}{res.data['id']}/approve/", {}, format='json')
        return StockRequest.objects.get(pk=res.data['id'])

    def transfer_payload(self, req, source=None):
        source = source or self.warehouse
        src = Product.objects.get(center=source, product_code='SHAMPOO')
        return {'from_center': source.id, 'to_center': self.branch.id, 'stock_request': req.id,
                'items': [{'product': src.id, 'quantity': '3'}]}

    # asking
    def test_a_retail_request_takes_retail_products(self):
        res = self.ask('Retail', self.shampoo)
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual((res.data['stock_type'], res.data['status']), ('Retail', 'Pending'))

    def test_a_retail_request_refuses_a_technical_or_consumable_product(self):
        for p in (self.color, self.gel, self.blank):
            self.assertEqual(self.ask('Retail', p).status_code, 400, p.name)
        self.assertEqual(StockRequest.objects.count(), 0)          # nothing half-saved

    def test_every_category_works_the_same_and_a_new_one_needs_no_code(self):
        self.assertEqual(self.ask('Technical', self.color).status_code, 201)
        self.assertEqual(self.ask('Consumable', self.gel).status_code, 201)
        self.assertEqual(self.ask('Uncategorised', self.blank).status_code, 201)
        self.assertEqual(self.ask('Technical', self.shampoo, self.color).status_code, 400)    # one category only
        brand_new = Product.objects.create(name='Mask', product_code='MASK', category='Spa', current_stock=0,
                                           center=self.branch)
        self.assertEqual(self.ask('Spa', brand_new).status_code, 201)

    def test_a_product_switched_off_in_the_item_master_cannot_be_requested(self):
        self.shampoo.is_active = False
        self.shampoo.save(update_fields=['is_active'])
        self.assertEqual(self.ask('Retail', self.shampoo).status_code, 400)
        self.assertEqual(StockRequest.objects.count(), 0)

    def test_a_technical_request_covers_every_non_retail_category(self):
        tool = Product.objects.create(name='Comb', product_code='COMB', category='Tool', current_stock=0, center=self.branch)
        self.assertEqual(self.ask('Technical', self.color, self.gel, self.blank, tool).status_code, 201)
        self.assertEqual(self.ask('Technical', self.shampoo).status_code, 400)       # retail stays out of it
        self.assertEqual(self.ask('Retail', tool).status_code, 400)

    def test_the_category_match_ignores_case_and_spaces(self):
        self.assertEqual(self.ask(' retail ', self.shampoo).status_code, 201)

    def test_the_category_is_required(self):
        self.assertEqual(self.ask('', self.shampoo).status_code, 400)

    def test_a_request_needs_an_item(self):
        self.assertEqual(self.ask('Retail').status_code, 400)

    def test_asking_tells_the_approvers_by_tick(self):
        self.ask('Retail', self.shampoo)
        told = set(Notification.objects.filter(event='stock.request_raised').values_list('user__email', flat=True))
        self.assertIn('mgr@t.com', told)
        self.assertNotIn('x@t.com', told)                          # no access to Saket

    def test_the_status_cannot_be_typed_in(self):
        res = self.ask('Retail', self.shampoo)
        self.clerk_api.patch(f"{URL}{res.data['id']}/", {'status': 'Fulfilled', 'notes': 'urgent'}, format='json')
        req = StockRequest.objects.get(pk=res.data['id'])
        self.assertEqual((req.status, req.notes), ('Pending', 'urgent'))

    # approving
    def test_an_approver_approves_and_the_stock_person_is_told(self):
        res = self.ask('Retail', self.shampoo)
        ok = self.manager_api.post(f"{URL}{res.data['id']}/approve/", {'note': 'ok'}, format='json')
        self.assertEqual(ok.status_code, 200, ok.data)
        self.assertEqual((ok.data['status'], ok.data['decided_by_name']), ('Approved', 'mgr'))
        self.assertTrue(Notification.objects.filter(event='stock.request_approved', user=self.store).exists())
        self.assertTrue(Notification.objects.filter(event='approval.decided', user=self.clerk).exists())

    def test_a_rejection_ends_the_request(self):
        res = self.ask('Retail', self.shampoo)
        self.assertEqual(self.manager_api.post(f"{URL}{res.data['id']}/reject/", {}, format='json').status_code, 200)
        self.assertEqual(StockRequest.objects.get(pk=res.data['id']).status, 'Rejected')
        self.assertEqual(self.manager_api.post(f"{URL}{res.data['id']}/approve/", {}, format='json').status_code, 400)

    def test_without_the_tick_nobody_can_approve(self):
        res = self.ask('Retail', self.shampoo)
        self.assertEqual(self.clerk_api.post(f"{URL}{res.data['id']}/approve/", {}, format='json').status_code, 403)
        self.assertEqual(StockRequest.objects.get(pk=res.data['id']).status, 'Pending')

    def test_an_approver_cannot_decide_another_branchs_request(self):
        res = self.ask('Retail', self.shampoo)
        self.assertIn(self.stranger_api.post(f"{URL}{res.data['id']}/approve/", {}, format='json').status_code,
                      (403, 404))
        self.assertEqual(StockRequest.objects.get(pk=res.data['id']).status, 'Pending')

    def test_the_first_decision_wins(self):
        res = self.ask('Retail', self.shampoo)
        self.manager_api.post(f"{URL}{res.data['id']}/approve/", {}, format='json')
        again = self.manager_api.post(f"{URL}{res.data['id']}/reject/", {}, format='json')
        self.assertEqual(again.status_code, 400)
        self.assertEqual(StockRequest.objects.get(pk=res.data['id']).status, 'Approved')

    # sending
    def test_only_an_approved_request_can_be_sent(self):
        res = self.ask('Retail', self.shampoo)
        req = StockRequest.objects.get(pk=res.data['id'])
        sent = self.store_api.post(TRANSFERS, self.transfer_payload(req), format='json')
        self.assertEqual(sent.status_code, 400, sent.data)

    def test_the_stock_person_sends_it_from_a_place_they_choose_and_receiving_fulfils_it(self):
        req = self.approved_request()
        made = self.store_api.post(TRANSFERS, self.transfer_payload(req), format='json')
        self.assertEqual(made.status_code, 201, made.data)
        self.assertEqual(self.store_api.get(f'{URL}{req.id}/').data['transfer_id'], made.data['id'])

        tid = made.data['id']
        self.assertEqual(self.store_api.patch(f'{TRANSFERS}{tid}/', {'status': 'In Transit'},
                                              format='json').status_code, 200)
        self.assertEqual(StockRequest.objects.get(pk=req.id).status, 'Approved')      # still on the road
        # someone at the branch signs it in (the sender may not)
        receiver = CustomUser.objects.create_user(
            email='recv@t.com', password='pw', full_name='recv', center=self.branch,
            role=Role.objects.create(name='recv', permissions=tick(stock_transfers=1)))
        receiver.centers.add(self.branch)
        recv_api = APIClient()
        recv_api.force_authenticate(receiver)
        self.assertEqual(receive_transfer(recv_api, tid).status_code, 200)
        self.assertEqual(StockRequest.objects.get(pk=req.id).status, 'Fulfilled')
        self.assertEqual(Product.objects.get(center=self.branch, product_code='SHAMPOO').current_stock, D('3'))

    def test_a_branch_can_be_the_source_too(self):
        Product.objects.create(name='SHAMPOO', product_code='SHAMPOO', category='Retail', price=D('100'),
                               mrp=D('200'), current_stock=20, center=self.other_branch)
        req = self.approved_request()
        made = self.store_api.post(TRANSFERS, self.transfer_payload(req, source=self.other_branch), format='json')
        self.assertEqual(made.status_code, 201, made.data)

    def test_sending_needs_the_fulfill_tick(self):
        req = self.approved_request()
        u = CustomUser.objects.create_user(
            email='t@t.com', password='pw', full_name='t', center=self.warehouse,
            role=Role.objects.create(name='t', permissions=tick(stock_transfers=1)))
        u.centers.add(self.warehouse, self.branch)
        api = APIClient()
        api.force_authenticate(u)
        res = api.post(TRANSFERS, self.transfer_payload(req), format='json')
        self.assertEqual(res.status_code, 403, res.data)

    def test_it_must_be_sent_to_the_asking_branch_and_only_once(self):
        req = self.approved_request()
        wrong = dict(self.transfer_payload(req), to_center=self.other_branch.id)
        self.assertEqual(self.store_api.post(TRANSFERS, wrong, format='json').status_code, 400)
        self.assertEqual(self.store_api.post(TRANSFERS, self.transfer_payload(req), format='json').status_code, 201)
        self.assertEqual(self.store_api.post(TRANSFERS, self.transfer_payload(req), format='json').status_code, 400)

    # withdrawing
    def test_a_request_can_be_cancelled_until_a_transfer_exists(self):
        req = self.approved_request()
        self.assertEqual(self.manager_api.post(f'{URL}{req.id}/cancel/', {}, format='json').status_code, 403)
        self.assertEqual(self.clerk_api.post(f'{URL}{req.id}/cancel/', {}, format='json').status_code, 200)
        self.assertEqual(StockRequest.objects.get(pk=req.id).status, 'Cancelled')

    def test_a_request_with_a_transfer_cannot_be_cancelled(self):
        req = self.approved_request()
        self.store_api.post(TRANSFERS, self.transfer_payload(req), format='json')
        self.assertEqual(self.clerk_api.post(f'{URL}{req.id}/cancel/', {}, format='json').status_code, 400)

    # who may touch a request (own request, cancel, change)
    def both_ticks_user(self):
        u = CustomUser.objects.create_user(
            email='both@t.com', password='pw', full_name='Both', center=self.branch,
            role=Role.objects.create(name='both', permissions=tick(stock_requests=1, approve_stock_requests=1)))
        u.centers.add(self.branch)
        api = APIClient()
        api.force_authenticate(u)
        return u, api

    def test_nobody_approves_their_own_request(self):
        u, api = self.both_ticks_user()
        res = api.post(URL, {'requesting_center': self.branch.id, 'stock_type': 'Retail',
                             'items': [{'product': self.shampoo.id, 'quantity': '3'}]}, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        for verb in ('approve', 'reject'):
            self.assertEqual(api.post(f"{URL}{res.data['id']}/{verb}/", {}, format='json').status_code, 403, verb)
        self.assertEqual(StockRequest.objects.get(pk=res.data['id']).status, 'Pending')
        # someone else with the approve tick still can
        self.assertEqual(self.manager_api.post(f"{URL}{res.data['id']}/approve/", {}, format='json').status_code, 200)

    def test_the_asker_is_not_told_to_approve_their_own_request(self):
        u, api = self.both_ticks_user()
        api.post(URL, {'requesting_center': self.branch.id, 'stock_type': 'Retail',
                       'items': [{'product': self.shampoo.id, 'quantity': '3'}]}, format='json')
        told = set(Notification.objects.filter(event='stock.request_raised').values_list('user__email', flat=True))
        self.assertIn('mgr@t.com', told)
        self.assertNotIn('both@t.com', told)

    def test_the_owner_may_approve_their_own_request(self):
        owner = CustomUser.objects.create_superuser(email='boss@t.com', password='pw', full_name='Boss')
        api = APIClient()
        api.force_authenticate(owner)
        res = api.post(URL, {'requesting_center': self.branch.id, 'stock_type': 'Retail',
                             'items': [{'product': self.shampoo.id, 'quantity': '3'}]}, format='json')
        self.assertEqual(api.post(f"{URL}{res.data['id']}/approve/", {}, format='json').status_code, 200)

    def test_only_the_asker_or_an_approver_can_cancel_or_change_a_request(self):
        u, api = self.both_ticks_user()
        res = self.ask('Retail', self.shampoo)                       # made by the clerk
        rid = res.data['id']
        # another person who can make requests, but did not make this one
        self.assertEqual(api.post(f'{URL}{rid}/cancel/', {}, format='json').status_code, 200)   # both_ticks_user is an approver
        res = self.ask('Retail', self.shampoo)
        rid = res.data['id']
        self.assertEqual(self.store_api.post(f'{URL}{rid}/cancel/', {}, format='json').status_code, 403)
        self.assertEqual(self.store_api.patch(f'{URL}{rid}/', {'notes': 'hi'}, format='json').status_code, 403)
        self.assertEqual(self.clerk_api.patch(f'{URL}{rid}/', {'notes': 'hi'}, format='json').status_code, 200)

    def test_a_decided_request_cannot_be_edited(self):
        req = self.approved_request()
        self.assertEqual(self.clerk_api.patch(f'{URL}{req.id}/', {'notes': 'late'}, format='json').status_code, 400)

    def test_the_person_who_sent_a_transfer_cannot_receive_it(self):
        req = self.approved_request()
        res = self.store_api.post(TRANSFERS, self.transfer_payload(req), format='json')
        tid = res.data['id']
        self.assertEqual(self.store_api.patch(f'{TRANSFERS}{tid}/', {'status': 'In Transit'}, format='json').status_code, 200)
        # the store person can reach the branch too, but sent it: someone at the branch must receive
        self.assertEqual(self.store_api.patch(f'{TRANSFERS}{tid}/', {'status': 'Completed'}, format='json').status_code, 403)
        self.assertEqual(StockRequest.objects.get(pk=req.id).status, 'Approved')
