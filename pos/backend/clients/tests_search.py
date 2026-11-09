"""The till's customer search at 1+ crore customers.

"Any part of the name or phone" read the whole customer table on every keystroke (27-36 s with 1.09 crore customers,
and it starved the other tills). The search now starts from an index: the START of the first name, last name or
GSTIN, the first OR the last digits of the phone; 3 letters or 4 digits to begin; 20 customers at most. What does
not change is what comes back: the same customer rows, with their memberships, packages, value cards, cashback and
advance balance, so the till still shows every perk of the customer it finds.
"""

import datetime
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext

from billing.models import AdvancePayment, CashbackTransaction
from clients.models import Client, ClientMembership, ClientPackage, ClientValueCard
from clients.tests import ClientBase, URL, VALID_GSTIN
from marketing.models import Membership, Package, ValueCard
from services.models import ServiceMaster


class FastSearchTests(ClientBase):
    def setUp(self):
        super().setUp()
        self.asha = Client.objects.create(first_name='Asha', last_name='Rao', phone='9876543210', center=self.delhi)
        self.meena = Client.objects.create(first_name='Meenakshi', last_name='Iyer', phone='9123456780',
                                           center=self.noida, gst_number=VALID_GSTIN)
        self.ravi = Client.objects.create(first_name='Ravi', phone='7000011111', center=self.delhi)

    def names(self, q, **params):
        response = self.api.get(URL, {'q': q, **params})
        self.assertEqual(response.status_code, 200, response.data)
        return [r['first_name'] for r in response.data]

    # ── what is found ───────────────────────────────────────────────────

    def test_a_name_is_found_from_its_start_first_or_last_name_any_case(self):
        self.assertEqual(self.names('ash'), ['Asha'])
        self.assertEqual(self.names('RAO'), ['Asha'])
        self.assertEqual(self.names('iyer'), ['Meenakshi'])
        self.assertEqual(self.names('mee'), ['Meenakshi'])

    def test_the_middle_of_a_name_is_not_searched(self):
        self.assertEqual(self.names('aksh'), [])

    def test_a_phone_is_found_from_its_first_and_its_last_digits(self):
        self.assertEqual(self.names('9876'), ['Asha'])          # first digits
        self.assertEqual(self.names('3210'), ['Asha'])          # last digits
        self.assertEqual(self.names('43210'), ['Asha'])
        self.assertEqual(self.names('6543'), [])                 # the middle is not searched

    def test_a_phone_typed_the_way_people_write_it(self):
        for typed in ('+91 98765 43210', '98765-43210', '09876543210', '9876543210'):
            with self.subTest(typed):
                self.assertEqual(self.names(typed), ['Asha'])

    def test_more_words_narrow_the_search_down(self):
        self.assertEqual(self.names('asha rao'), ['Asha'])
        self.assertEqual(self.names('asha iyer'), [])
        self.assertEqual(self.names('iyer 9123'), ['Meenakshi'])
        self.assertEqual(self.names('mee 3210'), [])
        self.assertEqual(self.names('asha 98'), ['Asha'])        # the short word only narrows

    def test_a_narrowing_word_may_start_any_word_of_a_longer_name(self):
        Client.objects.create(first_name='Ajay', last_name='Kumar Dhir', phone='7000022222', center=self.delhi)
        Client.objects.create(first_name='Kraze', last_name='Wellness-Sec 46, Noida', phone='7000033333', center=self.delhi)
        self.assertEqual(self.names('ajay kumar dhir'), ['Ajay'])      # "dhir" starts the second word of the last name
        self.assertEqual(self.names('kraze noida'), ['Kraze'])
        self.assertEqual(self.names('kraze 46,'), ['Kraze'])
        self.assertEqual(self.names('wellness-sec noida'), ['Kraze'])

    def test_when_the_longest_word_starts_no_name_the_next_one_drives(self):
        Client.objects.create(first_name='Ajay', last_name='Kumar Dhirendra', phone='7000044444', center=self.delhi)
        self.assertEqual(self.names('ajay dhirendra'), ['Ajay'])      # "dhirendra" starts no name; "ajay" does
        self.assertEqual(self.names('dhirendra'), [])                 # alone, the middle of a name is still not searched

    def test_a_gstin_is_found_from_its_start(self):
        self.assertEqual(self.names('27AAAC'), ['Meenakshi'])
        self.assertEqual(self.names('27aaacr5055'), ['Meenakshi'])
        self.assertEqual(self.names('AACR5055'), [])

    def test_it_takes_three_letters_or_four_digits_to_start(self):
        for too_short in ('as', 'a', '987', '12', '+91'):
            with self.subTest(too_short):
                self.assertEqual(self.names(too_short), [])

    def test_a_customer_with_a_single_name_is_found(self):
        self.assertEqual(self.names('ravi'), ['Ravi'])

    def test_wildcard_characters_are_just_characters(self):
        self.assertEqual(self.names('%%%'), [])
        self.assertEqual(self.names('a_h'), [])

    def test_archived_customers_are_not_offered(self):
        Client.objects.filter(pk=self.asha.pk).update(is_active=False)
        self.assertEqual(self.names('asha'), [])

    def test_at_most_twenty_come_back_newest_first(self):
        for i in range(25):
            Client.objects.create(first_name='Priya', last_name=f'N{i}', phone=f'80000{i:05d}', center=self.delhi)
        rows = self.api.get(URL, {'q': 'priya'}).data
        self.assertEqual(len(rows), 20)
        ids = [r['id'] for r in rows]
        self.assertEqual(ids, sorted(ids, reverse=True))
        self.assertEqual(rows[0]['last_name'], 'N24')

    def test_customers_of_every_centre_are_found_from_any_centre(self):
        """#11: customers are global."""
        self.assertEqual(self.names('meen'), ['Meenakshi'])      # a Noida customer, searched by a Delhi user

    def test_one_centre_keeps_the_any_part_search(self):
        """The Admin > Clients screen looking at one centre: few customers, so any part of a name or phone still works."""
        self.assertEqual(self.names('aksh', center_id=self.noida.id), ['Meenakshi'])
        self.assertEqual(self.names('6543', center_id=self.delhi.id), ['Asha'])
        self.assertEqual(self.names('aksh', center_id=self.delhi.id), [])
        self.assertEqual(self.names('AACR5055', center_id=self.noida.id), ['Meenakshi'])

    def test_the_paged_admin_list_uses_the_same_rule(self):
        response = self.api.get(URL, {'q': 'ash', 'page': 1, 'page_size': 10})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([r['first_name'] for r in response.data['results']], ['Asha'])

    # ── what the till needs to show comes with every match ──────────────

    def give_perks(self, customer):
        today = datetime.date.today()
        service = ServiceMaster.objects.create(name=f'Cut{customer.pk}', default_price=Decimal('500.00'),
                                               tax_percentage=Decimal('18.00'))
        gold = Membership.objects.create(name=f'Gold{customer.pk}', pre_tax_price=Decimal('5000.00'),
                                         expiry_days=365, level='Organisation')
        pack = Package.objects.create(name=f'Pack{customer.pk}', price=Decimal('2000.00'), validity_days=365,
                                      services_json=[{'service_id': service.id, 'quantity': 5}],
                                      level='Organisation')
        card = ValueCard.objects.create(title=f'Card{customer.pk}', pre_tax_price=Decimal('1000.00'),
                                        value=Decimal('1200.00'), expiry_days=365, level='Organisation')
        ClientMembership.objects.create(client=customer, membership=gold, start_date=today,
                                        expiry_date=today + datetime.timedelta(days=365))
        ClientPackage.objects.create(client=customer, package=pack, services_remaining={str(service.id): 5},
                                     expiry_date=today + datetime.timedelta(days=365))
        ClientValueCard.objects.create(client=customer, value_card=card, balance=Decimal('900.00'),
                                       expiry_date=today + datetime.timedelta(days=365))
        AdvancePayment.objects.create(client=customer, amount=Decimal('300.00'), notes='top up')
        CashbackTransaction.objects.create(client=customer, amount=Decimal('50.00'), notes='earned')

    def test_memberships_packages_cards_cashback_and_advance_come_with_the_match(self):
        self.give_perks(self.asha)
        row = self.api.get(URL, {'q': 'asha'}).data[0]
        self.assertEqual(len(row['active_memberships']), 1)
        self.assertEqual(len(row['active_packages']), 1)
        self.assertEqual(len(row['active_value_cards']), 1)
        self.assertEqual(Decimal(str(row['active_value_cards'][0]['balance'])), Decimal('900.00'))
        self.assertEqual(Decimal(str(row['advance_balance'])), Decimal('300.00'))
        self.assertEqual(Decimal(str(row['cashback_balance'])), Decimal('50.00'))
        self.assertEqual(row['phone'], '9876543210')

    def test_the_perks_are_the_same_whether_found_by_name_or_by_phone_end(self):
        self.give_perks(self.asha)
        by_name = self.api.get(URL, {'q': 'asha'}).data[0]
        by_end = self.api.get(URL, {'q': '3210'}).data[0]
        self.assertEqual(by_name, by_end)

    def test_finding_more_customers_does_not_cost_more_queries(self):
        for c in (self.asha, self.meena):
            self.give_perks(c)
        with CaptureQueriesContext(connection) as one:
            self.api.get(URL, {'q': 'asha'})
        for i in range(12):
            c = Client.objects.create(first_name='Asha', last_name=f'X{i}', phone=f'81000{i:05d}', center=self.delhi)
            self.give_perks(c)
        with CaptureQueriesContext(connection) as many:
            rows = self.api.get(URL, {'q': 'asha'}).data
        self.assertEqual(len(rows), 13)
        self.assertEqual(len(one), len(many), 'the search costs more queries as more customers match')

    # ── the reversed phone that makes "last digits" an index lookup ──────

    def test_the_reversed_phone_follows_the_phone(self):
        self.assertEqual(self.asha.phone_reversed, '0123456789')
        self.asha.phone = '9000000001'
        self.asha.save(update_fields=['phone'])
        self.asha.refresh_from_db()
        self.assertEqual(self.asha.phone_reversed, '1000000009')
        self.assertEqual(self.names('0001'), ['Asha'])           # last digits of the NEW number
        self.assertEqual(self.names('3210'), [])

    def test_a_client_with_no_phone_has_an_empty_reversed_phone(self):
        legacy = Client.objects.create(first_name='Acme Traders', phone='', center=self.delhi)
        self.assertEqual(legacy.phone_reversed, '')
