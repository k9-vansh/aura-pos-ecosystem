"""The product import finds each row's product in memory, and finds the same one the database queries did."""
import random

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from accounts.models import CustomUser
from inventory.models import Product, StockTransaction
from inventory.product_index import ProductIndex
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role


def by_queries(center, code, id_str, name):
    """What the import asked the database before: code, then ID, then name; case-insensitive; first by name."""
    found = None
    if code:
        found = Product.objects.filter(product_code__iexact=code, center=center).first()
    if not found and id_str:
        found = Product.objects.filter(product_id_str__iexact=id_str, center=center).first()
    if not found:
        found = Product.objects.filter(name__iexact=name, center=center).first()
    return found.pk if found else None


class ProductIndexMatchesTheDatabase(TestCase):
    def test_it_finds_what_the_three_queries_found(self):
        rng = random.Random(3)
        center = Center.objects.create(center_name='A', display_name='A')
        other = Center.objects.create(center_name='B', display_name='B')
        words = ['Serum', 'Gel', 'Oil', 'Mask', 'Wax']
        for n in range(60):
            Product.objects.create(
                center=rng.choice([center, center, other]), name=rng.choice(words) + str(rng.randint(0, 6)),
                product_code=rng.choice([None, '', f'C{rng.randint(0, 25)}', f'c{rng.randint(0, 25)}']),
                product_id_str=rng.choice([None, '', f'I{rng.randint(0, 25)}']))
        index = ProductIndex(center)
        for _ in range(300):
            code = rng.choice(['', f'C{rng.randint(0, 28)}', f'c{rng.randint(0, 28)}'])
            id_str = rng.choice(['', f'I{rng.randint(0, 28)}', f'i{rng.randint(0, 28)}'])
            name = rng.choice([str.lower, str.upper, str])(rng.choice(words) + str(rng.randint(0, 7)))
            self.assertEqual(index.find(code, id_str, name), by_queries(center, code, id_str, name),
                             (code, id_str, name))

    def test_other_branches_are_never_matched(self):
        a = Center.objects.create(center_name='A', display_name='A')
        b = Center.objects.create(center_name='B', display_name='B')
        Product.objects.create(center=b, name='Gel', product_code='G1')
        self.assertIsNone(ProductIndex(a).find('G1', '', 'Gel'))

    def test_added_and_renamed_products_are_found_afterwards(self):
        a = Center.objects.create(center_name='A', display_name='A')
        index = ProductIndex(a)
        self.assertIsNone(index.find('N1', '', 'New'))
        index.add(7, 'N1', '', 'New')
        self.assertEqual(index.find('n1', '', 'x'), 7)
        index.remove(7, 'N1', '', 'New')
        index.add(7, 'N2', '', 'Renamed')
        self.assertIsNone(index.find('N1', '', 'New'))
        self.assertEqual(index.find('', '', 'RENAMED'), 7)


class ImportThroughTheEndpoint(TestCase):
    def setUp(self):
        self.a = Center.objects.create(center_name='A', display_name='A', gst_number='07AAAAA0000A1Z5')
        self.b = Center.objects.create(center_name='B', display_name='B', gst_number='09CCCCC0000C1Z5')
        role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.user = CustomUser.objects.create_user(email='o@t.com', password='pw', full_name='O', role=role, center=self.a)
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def upload(self, lines, **data):
        header = 'productName,productCode,category,defaultPrice,tax,currentStock'
        f = SimpleUploadedFile('p.csv', '\n'.join([header] + lines).encode(), content_type='text/csv')
        res = self.api.post('/inventory/api/products/bulk_upload/', {'file': f, **data}, format='multipart')
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()

    def test_a_later_row_finds_the_product_an_earlier_row_made(self):
        out = self.upload(['Gel,G1,Retail,100,18,5', 'Gel Renamed,g1,Retail,120,18,9'], center_id=self.a.id)
        self.assertEqual((out['created'], out['updated']), (1, 1))
        (product,) = Product.objects.filter(center=self.a)
        self.assertEqual((product.name, product.price, product.current_stock), ('Gel Renamed', 120, 5))
        self.assertEqual(StockTransaction.objects.filter(product=product).count(), 1)

    def test_a_row_finds_a_product_an_earlier_row_renamed(self):
        Product.objects.create(center=self.a, name='Old Name', product_code='OLD')
        out = self.upload(['New Name,OLD,Retail,10,18,0', 'NEW NAME,,Retail,20,18,0'], center_id=self.a.id)
        self.assertEqual((out['created'], out['updated']), (0, 2))
        (product,) = Product.objects.filter(center=self.a)
        self.assertEqual(float(product.price), 20.0)

    def test_every_branch_gets_its_own_rows_and_a_rerun_updates_them(self):
        first = self.upload(['Gel,G1,Retail,100,18,5', 'Oil,O1,Retail,50,18,2'])
        self.assertEqual((first['created'], first['updated']), (4, 0))
        again = self.upload(['gel,g1,Retail,110,18,0', 'Oil,O1,Retail,55,18,0'])
        self.assertEqual((again['created'], again['updated']), (0, 4))
        self.assertEqual(Product.objects.count(), 4)                      # nothing was duplicated
        self.assertEqual({float(p.price) for p in Product.objects.filter(product_code__iexact='G1')}, {110.0})

    def test_the_queries_do_not_grow_with_the_rows(self):
        Product.objects.bulk_create([Product(center=self.a, name=f'P{n}', product_code=f'C{n}') for n in range(40)])

        def cost(count):
            lines = [f'P{n},C{n},Retail,10,18,0' for n in range(count)]
            with CaptureQueriesContext(connection) as ctx:
                self.upload(lines, center_id=self.a.id)
            return len(ctx)

        few, many = cost(5), cost(35)
        # Two queries per updated row (read it, save it) and nothing per row for finding it.
        self.assertLessEqual(many - few, 2 * 30 + 2)
