"""
Audit middleware privacy and integrity tests.

Three findings, all in code that runs on every write request:

  * `_cached_geo` sent the caller's IP address to freeipapi.com. Staff at the
    till and customers using the client app were disclosed to a third party on
    each new address, and the 1 second timeout stalled the four audit writer
    threads on a counter with no internet until rows started being dropped.
  * the recorded IP came from `X-Forwarded-For` with no proxy check. That is a
    request header, so any caller could choose the address written against their
    own actions - in an audit log.
  * `_cached_user_from_token` and `_cached_centre_name` were `lru_cache`d for the
    life of the process, so a renamed branch, a moved user or a revoked token
    kept resolving to stale values in every later audit row.
"""

import sys
import types
from contextlib import contextmanager
from unittest import mock

from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from rest_framework.authtoken.models import Token

from accounts.models import CustomUser
from audit_logs.middleware import (
    _cached_centre_name, _cached_geo, _cached_user_from_token, _client_ip,
)
from salon_admin.models import Center, Role


@contextmanager
def stub_requests(get):
    """Install a stand-in `requests` module.

    `requests` is listed in requirements.txt but is not necessarily installed -
    it is not in this environment - so the real module cannot be patched. The
    handler imports it lazily inside the function, which makes a sys.modules
    stub the honest way to observe whether a call would have been made.
    """
    module = types.ModuleType('requests')
    module.get = get
    previous = sys.modules.get('requests')
    sys.modules['requests'] = module
    try:
        yield get
    finally:
        if previous is None:
            sys.modules.pop('requests', None)
        else:
            sys.modules['requests'] = previous


class GeoLookupTests(TestCase):
    def setUp(self):
        _cached_geo.cache_clear()

    def tearDown(self):
        _cached_geo.cache_clear()

    def test_no_outbound_request_is_made_by_default(self):
        """The default must not disclose a caller's IP to a third party."""
        with stub_requests(mock.Mock()) as get:
            result = _cached_geo('8.8.8.8')
        get.assert_not_called()
        self.assertEqual(result['country'], '')

    @override_settings(AUDIT_LOG_GEO_LOOKUP=True)
    def test_the_lookup_happens_when_explicitly_enabled(self):
        response = mock.Mock()
        response.json.return_value = {
            'cityName': 'Mumbai', 'regionName': 'Maharashtra',
            'countryName': 'India', 'countryCode': 'IN',
        }
        with stub_requests(mock.Mock(return_value=response)) as get:
            result = _cached_geo('1.1.1.1')
        get.assert_called_once()
        self.assertEqual(result['city'], 'Mumbai')
        self.assertEqual(result['country_code'], 'IN')

    @override_settings(AUDIT_LOG_GEO_LOOKUP=True)
    def test_a_private_address_is_never_looked_up(self):
        with stub_requests(mock.Mock()) as get:
            result = _cached_geo('192.168.1.20')
        get.assert_not_called()
        self.assertEqual(result['city'], 'Local network')

    @override_settings(AUDIT_LOG_GEO_LOOKUP=True)
    def test_a_failing_lookup_does_not_raise(self):
        with stub_requests(mock.Mock(side_effect=OSError('no route to host'))):
            result = _cached_geo('9.9.9.9')
        self.assertEqual(result['country'], '')

    @override_settings(AUDIT_LOG_GEO_LOOKUP=True)
    def test_a_missing_requests_package_does_not_raise(self):
        """`requests` is in requirements.txt but was absent from this
        environment, so the lookup raised ImportError on every write request -
        caught by a bare `except`, which logged a traceback each time."""
        previous = sys.modules.pop('requests', None)
        try:
            with mock.patch.dict(sys.modules, {'requests': None}):
                result = _cached_geo('8.8.4.4')
        finally:
            if previous is not None:
                sys.modules['requests'] = previous
        self.assertEqual(result['country'], '')


class ClientIpTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_a_forged_forwarded_header_is_ignored_by_default(self):
        """Anyone could choose the IP recorded against their own actions."""
        request = self.factory.post(
            '/billing/api/invoices/',
            HTTP_X_FORWARDED_FOR='8.8.8.8', REMOTE_ADDR='10.0.0.5')
        self.assertEqual(_client_ip(request), '10.0.0.5')

    @override_settings(AUDIT_LOG_TRUST_FORWARDED_FOR=True)
    def test_the_header_is_used_when_the_deployment_says_so(self):
        # nginx appends the real peer to whatever the caller sent, so the LAST
        # entry is the trustworthy one; the first is whatever the caller claimed.
        request = self.factory.post(
            '/billing/api/invoices/',
            HTTP_X_FORWARDED_FOR='1.2.3.4, 8.8.8.8', REMOTE_ADDR='10.0.0.5')
        self.assertEqual(_client_ip(request), '8.8.8.8')

    @override_settings(AUDIT_LOG_TRUST_FORWARDED_FOR=True)
    def test_x_real_ip_wins_when_nginx_sets_it(self):
        request = self.factory.post(
            '/billing/api/invoices/', HTTP_X_REAL_IP='49.36.1.9',
            HTTP_X_FORWARDED_FOR='1.2.3.4, 49.36.1.9', REMOTE_ADDR='127.0.0.1')
        self.assertEqual(_client_ip(request), '49.36.1.9')

    @override_settings(AUDIT_LOG_TRUST_FORWARDED_FOR=True)
    def test_an_empty_header_falls_back_to_the_socket_address(self):
        request = self.factory.post(
            '/billing/api/invoices/',
            HTTP_X_FORWARDED_FOR='', REMOTE_ADDR='10.0.0.5')
        self.assertEqual(_client_ip(request), '10.0.0.5')

    def test_a_missing_address_is_an_empty_string_not_a_crash(self):
        request = self.factory.post('/billing/api/invoices/')
        request.META.pop('REMOTE_ADDR', None)
        self.assertEqual(_client_ip(request), '')

    @override_settings(AUDIT_LOG_TRUST_FORWARDED_FOR=True)
    def test_an_overlong_header_is_truncated_to_the_column_width(self):
        request = self.factory.post(
            '/billing/api/invoices/',
            HTTP_X_FORWARDED_FOR='x' * 500, REMOTE_ADDR='10.0.0.5')
        self.assertLessEqual(len(_client_ip(request)), 45)


class StaleLookupTests(TestCase):
    def setUp(self):
        cache.clear()
        self.center = Center.objects.create(center_name='Bandra')
        self.role = Role.objects.create(name='Manager', permissions={})
        self.user = CustomUser.objects.create_user(
            email='m@test.com', password='StrongPass!234', full_name='Manager',
            role=self.role, center=self.center)
        self.token = Token.objects.create(user=self.user).key

    def tearDown(self):
        cache.clear()

    def test_a_renamed_branch_is_not_remembered_forever(self):
        self.assertEqual(_cached_centre_name(self.center.id), 'Bandra')

        Center.all_objects.filter(pk=self.center.pk).update(center_name='Bandra West')
        cache.clear()  # stands in for the 60 second TTL expiring

        self.assertEqual(_cached_centre_name(self.center.id), 'Bandra West')

    def test_an_unknown_branch_id_is_an_empty_string(self):
        self.assertEqual(_cached_centre_name(99999999), '')

    def test_a_missing_branch_id_is_an_empty_string(self):
        self.assertEqual(_cached_centre_name(None), '')

    def test_a_token_resolves_to_its_user(self):
        result = _cached_user_from_token(self.token)
        self.assertIsNotNone(result)
        self.assertEqual(result[0], self.user.pk)
        self.assertEqual(result[2], 'm@test.com')

    def test_a_revoked_token_stops_resolving(self):
        """A permanent cache kept attributing actions to a deleted token."""
        self.assertIsNotNone(_cached_user_from_token(self.token))

        Token.objects.filter(key=self.token).delete()
        cache.clear()

        self.assertIsNone(_cached_user_from_token(self.token))

    def test_an_unknown_token_is_none_and_is_not_requeried_immediately(self):
        self.assertIsNone(_cached_user_from_token('not-a-token'))
        # The negative answer is cached, so a flood of bad tokens does not
        # become a flood of queries.
        with mock.patch('audit_logs.middleware._resolve_user_from_token') as resolve:
            self.assertIsNone(_cached_user_from_token('not-a-token'))
        resolve.assert_not_called()

    def test_an_empty_token_is_none_without_a_query(self):
        with mock.patch('audit_logs.middleware._resolve_user_from_token') as resolve:
            self.assertIsNone(_cached_user_from_token(''))
        resolve.assert_not_called()
