"""
Tests for the spreadsheet import layer.

Bulk upload had no tests, and the handlers ended in a bare
`except Exception` that returned the traceback to the caller and logged
nothing - which is why the repeated 400s in django.log carry no diagnostic
information at all.
"""

import io
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from inventory.models import Product, StockTransaction
from pos_backend.bulk_upload import (
    BulkUploadError, ImportReport, cell, cell_bool, cell_date, cell_decimal,
    cell_int, cell_percent, normalize_column, read_tabular_file, require_columns,
)
from pos_backend.money import money
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role


def csv_file(text, name='products.csv'):
    return SimpleUploadedFile(name, text.encode('utf-8'), content_type='text/csv')


class CellParsingTests(TestCase):
    """The shared cell parsers, which replaced four divergent copies."""

    def test_normalize_column(self):
        for raw, expected in [
            (' Product Name ', 'productname'),
            ('product_name', 'productname'),
            ('Product-Name', 'productname'),
            ('GST %', 'gst%'),
            (None, ''),
        ]:
            self.assertEqual(normalize_column(raw), expected)

    def test_cell_treats_pandas_blanks_as_empty(self):
        """pandas turns an empty cell into the string 'nan', which the old code
        checked for inconsistently and sometimes stored verbatim."""
        for blank in ['', ' ', 'nan', 'NaN', 'None', 'null', 'NA', 'n/a', '-']:
            self.assertEqual(cell({'x': blank}, 'x'), '', repr(blank))

    def test_cell_falls_through_to_the_next_key(self):
        row = {'productname': '', 'name': 'Shampoo'}
        self.assertEqual(cell(row, 'productname', 'name'), 'Shampoo')

    def test_cell_decimal_tolerates_formatting(self):
        self.assertEqual(cell_decimal({'v': '1,250.50'}, 'v'), Decimal('1250.50'))
        self.assertEqual(cell_decimal({'v': '₹900'}, 'v'), Decimal('900'))
        self.assertEqual(cell_decimal({'v': ' 12 '}, 'v'), Decimal('12'))

    def test_cell_decimal_returns_default_on_junk(self):
        for junk in ['abc', '', 'nan', None]:
            self.assertIsNone(cell_decimal({'v': junk}, 'v'))
        self.assertEqual(cell_decimal({'v': 'abc'}, 'v', default=Decimal('0')),
                         Decimal('0'))

    def test_cell_int_accepts_excel_floats(self):
        """Excel hands back '5.0' for an integer column."""
        self.assertEqual(cell_int({'v': '5.0'}, 'v'), 5)
        self.assertEqual(cell_int({'v': '5'}, 'v'), 5)
        self.assertIsNone(cell_int({'v': 'five'}, 'v'))

    def test_cell_bool(self):
        for truthy in ['true', 'TRUE', '1', 'yes', 'Y', 'active']:
            self.assertTrue(cell_bool({'v': truthy}, 'v'))
        for falsy in ['false', '0', 'no', 'N', 'inactive']:
            self.assertFalse(cell_bool({'v': falsy}, 'v'))
        self.assertTrue(cell_bool({'v': ''}, 'v', default=True))

    def test_cell_percent_does_not_treat_one_as_a_fraction(self):
        """The old code did `if 0 < v <= 1: v *= 100`, turning 1% into 100%."""
        self.assertEqual(cell_percent({'v': '1'}, 'v'), Decimal('1'))
        self.assertEqual(cell_percent({'v': '18'}, 'v'), Decimal('18'))
        self.assertEqual(cell_percent({'v': '18%'}, 'v'), Decimal('18'))

    def test_cell_percent_rejects_out_of_range(self):
        self.assertIsNone(cell_percent({'v': '-5'}, 'v'))
        self.assertIsNone(cell_percent({'v': '450'}, 'v'))

    def test_cell_percent_falls_back_when_blank(self):
        self.assertEqual(cell_percent({'v': ''}, 'v', default=Decimal('18')),
                         Decimal('18'))

    def test_cell_date_accepts_the_formats_people_type(self):
        import datetime
        expected = datetime.date(2026, 3, 15)
        for raw in ['2026-03-15', '15-03-2026', '15/03/2026', '15-Mar-2026',
                    '2026/03/15', '2026-03-15 00:00:00']:
            self.assertEqual(cell_date({'v': raw}, 'v'), expected, raw)

    def test_cell_date_returns_default_on_junk(self):
        self.assertIsNone(cell_date({'v': 'sometime'}, 'v'))


class FileReadingTests(TestCase):
    def test_missing_file_gives_an_actionable_message(self):
        with self.assertRaises(BulkUploadError) as ctx:
            read_tabular_file(None)
        self.assertIn('.csv', ctx.exception.message)

    def test_unsupported_extension_names_what_is_supported(self):
        with self.assertRaises(BulkUploadError) as ctx:
            read_tabular_file(csv_file('a,b\n1,2', name='data.pdf'))
        self.assertIn('supported', ctx.exception.context)

    def test_headings_with_no_rows_is_explained(self):
        with self.assertRaises(BulkUploadError) as ctx:
            read_tabular_file(csv_file('productname,price\n'))
        self.assertIn('no data rows', ctx.exception.message)

    def test_row_limit_is_enforced_and_reported(self):
        body = 'productname\n' + '\n'.join(f'Item {i}' for i in range(20))
        with self.assertRaises(BulkUploadError) as ctx:
            read_tabular_file(csv_file(body), max_rows=5)
        self.assertIn('row limit', ctx.exception.message)
        self.assertEqual(ctx.exception.context['limit'], 5)

    def test_a_corrupt_file_does_not_leak_internals(self):
        bad = SimpleUploadedFile('x.xlsx', b'this is not really xlsx',
                                 content_type='application/vnd.ms-excel')
        with self.assertRaises(BulkUploadError) as ctx:
            read_tabular_file(bad)
        self.assertIn('could not be opened', ctx.exception.message)
        self.assertNotIn('Traceback', ctx.exception.message)

    def test_columns_are_normalised_on_read(self):
        rows = read_tabular_file(csv_file('Product Name,GST %\nShampoo,18'))
        self.assertIn('productname', rows[0])

    def test_require_columns_lists_what_it_found(self):
        rows = [{'nonsense': 'x', 'rubbish': 'y'}]
        with self.assertRaises(BulkUploadError) as ctx:
            require_columns(rows, {'productname'}, {'productname'})
        self.assertIn('columns_found', ctx.exception.context)
        self.assertIn('nonsense', ctx.exception.context['columns_found'])

    def test_require_columns_reports_a_missing_required_column(self):
        rows = [{'category': 'Retail'}]
        with self.assertRaises(BulkUploadError) as ctx:
            require_columns(rows, {'category', 'productname'}, {'productname'})
        self.assertIn('need_one_of', ctx.exception.context)


class ImportReportTests(TestCase):
    def test_truncation_is_declared(self):
        """The old code sliced warnings to 20 silently, so 500 problems looked
        like 20."""
        report = ImportReport(max_reported=5)
        for i in range(12):
            report.skip(i, 'bad row')
        payload = report.as_dict()
        self.assertEqual(len(payload['warnings']), 5)
        self.assertEqual(payload['warnings_truncated'], 7)
        self.assertEqual(payload['total_warnings'], 12)

    def test_no_truncation_key_when_all_fit(self):
        report = ImportReport(max_reported=50)
        report.skip(2, 'bad')
        self.assertNotIn('warnings_truncated', report.as_dict())


class ProductImportTests(TestCase):
    URL = '/inventory/api/products/bulk_upload/'

    def setUp(self):
        self.center_a = Center.objects.create(center_name='Andheri', display_name='Andheri')
        self.center_b = Center.objects.create(center_name='Bandra', display_name='Bandra')
        self.owner_role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.owner = CustomUser.objects.create_user(
            email='owner@test.com', password='pw', full_name='Owner',
            role=self.owner_role, center=self.center_a,
        )
        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def upload(self, text, **params):
        return self.api.post(self.URL, {'file': csv_file(text), **params},
                             format='multipart')

    def test_creates_a_product(self):
        response = self.upload(
            'productName,category,price,tax,barcode\nArgan Shampoo,Retail,900,18,890123',
            center_id=self.center_a.id,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['created'], 1)

        product = Product.objects.get(name='Argan Shampoo', center=self.center_a)
        self.assertEqual(money(product.price), Decimal('900.00'))
        self.assertEqual(money(product.gst_percent), Decimal('18.00'))

    def test_a_blank_tax_column_uses_the_default_not_zero(self):
        """`safe_float(row.get('tax', 0.18))` returned 0.0 for a present-but-empty
        cell, importing the product at 0% GST."""
        response = self.upload('productName,price,tax\nShampoo,900,',
                               center_id=self.center_a.id)
        self.assertEqual(response.status_code, 200, response.data)
        product = Product.objects.get(name='Shampoo', center=self.center_a)
        self.assertEqual(money(product.gst_percent), Decimal('18.00'))

    def test_a_one_percent_tax_stays_one_percent(self):
        """`if 0 < gst <= 1: gst *= 100` turned 1% into 100%."""
        response = self.upload('productName,price,tax\nSpecial,900,1',
                               center_id=self.center_a.id)
        self.assertEqual(response.status_code, 200, response.data)
        product = Product.objects.get(name='Special', center=self.center_a)
        self.assertEqual(money(product.gst_percent), Decimal('1.00'))

    def test_opening_stock_writes_a_ledger_entry(self):
        """A seeded stock figure used to leave current_stock and the ledger
        permanently out of step, which made stock_history wrong."""
        response = self.upload('productName,price,currentStock\nShampoo,900,25',
                               center_id=self.center_a.id)
        self.assertEqual(response.status_code, 200, response.data)

        product = Product.objects.get(name='Shampoo', center=self.center_a)
        self.assertEqual(product.current_stock, 25)

        ledger = sum(t.quantity_change
                     for t in StockTransaction.objects.filter(product=product))
        self.assertEqual(ledger, 25)
        self.assertEqual(response.data['opening_stock_recorded'], 1)

    def test_a_multi_center_import_applies_the_stock_to_every_center(self):
        """Owner's request (see inventory/views.py product bulk_upload): an
        "all locations" import gives each branch the file's opening stock, and
        each branch's ledger records it so stock and ledger agree."""
        response = self.upload('productName,price,currentStock\nShampoo,900,25')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['opening_stock_recorded'], 2)
        for center in (self.center_a, self.center_b):
            product = Product.objects.get(name='Shampoo', center=center)
            self.assertEqual(product.current_stock, 25)
            self.assertEqual(
                StockTransaction.objects.filter(product=product).values_list(
                    'quantity_change', flat=True).first(), 25)

    def test_an_update_does_not_overwrite_stock_from_the_file(self):
        Product.objects.create(name='Shampoo', center=self.center_a,
                               price=Decimal('900.00'), current_stock=40)
        response = self.upload('productName,price,currentStock\nShampoo,950,5',
                               center_id=self.center_a.id)
        self.assertEqual(response.status_code, 200, response.data)
        product = Product.objects.get(name='Shampoo', center=self.center_a)
        self.assertEqual(product.current_stock, 40)
        self.assertEqual(money(product.price), Decimal('950.00'))

    def test_a_row_with_no_name_is_skipped_and_reported(self):
        response = self.upload('productName,price\n,900\nShampoo,900',
                               center_id=self.center_a.id)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['created'], 1)
        self.assertEqual(response.data['skipped'], 1)
        self.assertIn('Row 2', str(response.data['warnings']))

    def test_a_negative_price_is_skipped(self):
        response = self.upload('productName,price\nBad,-50', center_id=self.center_a.id)
        self.assertEqual(response.data['skipped'], 1)
        self.assertFalse(Product.objects.filter(name='Bad').exists())

    def test_a_negative_stock_is_skipped(self):
        response = self.upload('productName,price,currentStock\nBad,900,-5',
                               center_id=self.center_a.id)
        self.assertEqual(response.data['skipped'], 1)

    def test_unrecognised_headings_are_explained(self):
        """A bare 400 with no explanation is what the operator used to get."""
        response = self.upload('foo,bar\n1,2', center_id=self.center_a.id)
        self.assertEqual(response.status_code, 400)
        self.assertIn('columns_found', response.data)
        self.assertIn('hint', response.data)

    def test_a_missing_name_column_is_explained(self):
        response = self.upload('category,price\nRetail,900',
                               center_id=self.center_a.id)
        self.assertEqual(response.status_code, 400)
        self.assertIn('need_one_of', response.data)

    def test_no_file_is_a_clean_400(self):
        response = self.api.post(self.URL, {}, format='multipart')
        self.assertEqual(response.status_code, 400)
        self.assertIn('error', response.data)

    def test_a_failure_never_returns_a_traceback(self):
        """Three handlers shipped `traceback.format_exc()` to the caller."""
        for payload in ['foo,bar\n1,2', 'productname\n']:
            response = self.upload(payload, center_id=self.center_a.id)
            body = str(response.data)
            self.assertNotIn('Traceback', body)
            self.assertNotIn('site-packages', body)
            self.assertNotIn('.py", line', body)

    def test_a_failure_is_logged_server_side(self):
        """These modules had no logging at all, which is why the 400s in the
        production log carry no reason."""
        bad = SimpleUploadedFile('x.xlsx', b'not really xlsx')
        with self.assertLogs('bulk_upload', level='INFO') as captured:
            response = self.api.post(self.URL, {'file': bad}, format='multipart')
        self.assertEqual(response.status_code, 400)
        self.assertTrue(captured.output)

    def test_importing_to_a_forbidden_center_is_refused(self):
        limited_role = Role.objects.create(name='Clerk A', permissions={
            'inventory': {'products': {'read': True, 'create': True, 'update': True}},
        })
        clerk = CustomUser.objects.create_user(
            email='clerk@test.com', password='pw', full_name='Clerk',
            role=limited_role, center=self.center_a,
        )
        self.api.force_authenticate(clerk)
        response = self.upload('productName,price\nShampoo,900',
                               center_id=self.center_b.id)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Product.objects.filter(center=self.center_b).exists())

    def test_a_thousands_separator_in_the_price_parses(self):
        response = self.upload('productName,price\nPremium,"1,250.50"',
                               center_id=self.center_a.id)
        self.assertEqual(response.status_code, 200, response.data)
        product = Product.objects.get(name='Premium', center=self.center_a)
        self.assertEqual(money(product.price), Decimal('1250.50'))

    def test_matching_is_scoped_per_center(self):
        self.upload('productName,price\nShampoo,900', center_id=self.center_a.id)
        self.upload('productName,price\nShampoo,900', center_id=self.center_b.id)
        self.assertEqual(Product.objects.filter(name='Shampoo').count(), 2)

    def test_reimporting_updates_rather_than_duplicating(self):
        self.upload('productName,price\nShampoo,900', center_id=self.center_a.id)
        response = self.upload('productName,price\nShampoo,950',
                               center_id=self.center_a.id)
        self.assertEqual(response.data['updated'], 1)
        self.assertEqual(Product.objects.filter(name='Shampoo',
                                                center=self.center_a).count(), 1)
