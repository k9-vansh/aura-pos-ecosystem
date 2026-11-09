"""A petty cash entry needs a picture of the bill. It is sent as a multipart upload, so the nonsense-number
checks that the JSON sweep (tests_bad_input_sweep) does for other screens are done here for this one."""
import tempfile

from django.test import override_settings

from finance.models import PettyCashCategory, PettyCashEntry
from finance.tests import FinanceTestBase
from pos_backend.testing import bill_picture

URL = '/finance/api/petty-cash/'


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix='pos-test-media-'))
class PettyCashBillTests(FinanceTestBase):
    def setUp(self):
        super().setUp()
        self.category = PettyCashCategory.objects.create(name='Tea', sap_account='5411000003')
        self.body = {'center': self.center.id, 'description': 'Tea', 'amount': '120.00',
                     'comments': 'for guests', 'category': self.category.id}

    def create(self, **extra):
        return self.api.post(URL, {**self.body, **extra}, format='multipart')

    def test_an_entry_without_a_picture_is_refused(self):
        res = self.create()
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn('bill_image', res.data)
        self.assertEqual(PettyCashEntry.objects.count(), 0)
        # Plain JSON cannot carry a picture, so it is refused the same way.
        self.assertEqual(self.api.post(URL, self.body, format='json').status_code, 400)

    def test_an_entry_with_a_picture_is_saved_and_the_picture_can_be_opened(self):
        res = self.create(bill_image=bill_picture())
        self.assertEqual(res.status_code, 201, res.data)
        self.assertTrue(res.data['bill_image_url'].startswith('/media/petty_cash/'), res.data)
        listed = self.api.get(URL, {'center_id': self.center.id}).data
        self.assertEqual(listed[0]['bill_image_url'], res.data['bill_image_url'])

    def test_only_pictures_are_taken(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        res = self.create(bill_image=SimpleUploadedFile('bill.pdf', b'%PDF-1.4 x', content_type='application/pdf'))
        self.assertEqual(res.status_code, 400, res.data)
        res = self.create(bill_image=SimpleUploadedFile('bill.png', b'not really a picture', content_type='image/png'))
        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(PettyCashEntry.objects.count(), 0)

    def test_an_edit_keeps_the_saved_picture_unless_a_new_one_is_sent(self):
        entry = self.create(bill_image=bill_picture()).data
        before = PettyCashEntry.objects.get(pk=entry['id']).bill_image.name
        res = self.api.patch(f"{URL}{entry['id']}/", {'amount': '150.00'}, format='multipart')
        self.assertEqual(res.status_code, 200, res.data)
        row = PettyCashEntry.objects.get(pk=entry['id'])
        self.assertEqual((row.amount, row.bill_image.name), (150, before))
        res = self.api.patch(f"{URL}{entry['id']}/", {'bill_image': bill_picture('new.png')}, format='multipart')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertNotEqual(PettyCashEntry.objects.get(pk=entry['id']).bill_image.name, before)

    def test_an_old_entry_without_a_picture_can_still_be_corrected(self):
        old = PettyCashEntry.objects.create(center=self.center, description='Milk', amount=50,
                                            category=self.category)
        res = self.api.patch(f'{URL}{old.pk}/', {'description': 'Milk packets'}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['bill_image_url'], '')

    def test_nonsense_amounts_are_refused_and_never_crash(self):
        for bad in ('-5', '0', 'abc', '99999999999'):
            res = self.create(amount=bad, bill_image=bill_picture())
            self.assertEqual(res.status_code, 400, (bad, res.status_code))
        self.assertEqual(PettyCashEntry.objects.count(), 0)
