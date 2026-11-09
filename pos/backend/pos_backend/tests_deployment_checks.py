"""
The deployment safety checks, and the log retention command.

These exist because of what was found on the live server: `backend/.env`
contained exactly one line, `DJANGO_DEBUG=True`, and every guard in settings.py
is written `if not DEBUG:`. That single line disabled the refusal to boot with
the committed SECRET_KEY, the refusal to boot without DB_PASSWORD, secure
cookies, the SSL redirect, HSTS, and the CORS allowlist - all at once. The
running process was confirmed as Daphne's *development* server.

The SECRET_KEY is in git history, and Django signs session cookies and the
staff/client app tokens with it. Anyone with the repository could mint a session
for any user.

`manage.py check --deploy` covers some of this, but nobody runs it. These run on
every `manage.py check`, so every runserver, migrate and test run surfaces them.
"""

import datetime
import gzip
import io as _io
import json
import os
import tempfile

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from audit_logs.models import SystemLog
from pos_backend.deployment_checks import check_deployment_safety, check_serving_stack


def ids(issues):
    return {issue.id for issue in issues}


class DeploymentCheckTests(TestCase):
    """Each check must fire on the real unsafe value and stay quiet otherwise."""

    def run_checks(self):
        return check_deployment_safety(None) + check_serving_stack(None)

    # ── the signing key ──────────────────────────────────────────────────

    @override_settings(SECRET_KEY='django-insecure-9k^bakokugv8=whatever')
    def test_the_committed_secret_key_is_flagged(self):
        self.assertIn('deployment.W001', ids(self.run_checks()))

    @override_settings(SECRET_KEY='a-real-freshly-generated-key-nobody-else-has')
    def test_a_real_secret_key_is_not_flagged(self):
        self.assertNotIn('deployment.W001', ids(self.run_checks()))

    def test_the_secret_key_warning_says_what_the_risk_is(self):
        with override_settings(SECRET_KEY='django-insecure-abc'):
            issue = next(i for i in self.run_checks() if i.id == 'deployment.W001')
        # An operator who reads only the hint must understand the consequence.
        self.assertIn('forge', issue.hint)
        self.assertIn('get_random_secret_key', issue.hint)

    # ── DEBUG ────────────────────────────────────────────────────────────

    @override_settings(DEBUG=True)
    def test_debug_is_flagged(self):
        self.assertIn('deployment.W002', ids(self.run_checks()))

    @override_settings(DEBUG=False)
    def test_debug_off_is_not_flagged(self):
        self.assertNotIn('deployment.W002', ids(self.run_checks()))

    @override_settings(DEBUG=True)
    def test_the_debug_warning_explains_the_knock_on_effects(self):
        """The point is that DEBUG=True disables everything else too."""
        issue = next(i for i in self.run_checks() if i.id == 'deployment.W002')
        for phrase in ('secure cookies', 'traceback', 'DJANGO_DEBUG=False'):
            self.assertIn(phrase, issue.hint)

    # ── CORS and transport ───────────────────────────────────────────────

    @override_settings(CORS_ALLOW_ALL_ORIGINS=True)
    def test_open_cors_is_flagged(self):
        self.assertIn('deployment.W003', ids(self.run_checks()))

    @override_settings(CORS_ALLOW_ALL_ORIGINS=False,
                       CORS_ALLOWED_ORIGINS=['http://38.45.94.56:4200'])
    def test_plain_http_origins_are_flagged(self):
        issues = self.run_checks()
        self.assertIn('deployment.W004', ids(issues))
        issue = next(i for i in issues if i.id == 'deployment.W004')
        self.assertIn('38.45.94.56', issue.hint)

    @override_settings(CORS_ALLOW_ALL_ORIGINS=False,
                       CORS_ALLOWED_ORIGINS=['http://localhost:4200'])
    def test_localhost_over_http_is_not_flagged(self):
        """Developing against localhost is not a TLS problem."""
        self.assertNotIn('deployment.W004', ids(self.run_checks()))

    @override_settings(CORS_ALLOW_ALL_ORIGINS=False,
                       CORS_ALLOWED_ORIGINS=['https://pos.example.com'])
    def test_https_origins_are_not_flagged(self):
        self.assertNotIn('deployment.W004', ids(self.run_checks()))

    # ── the shared cache ─────────────────────────────────────────────────

    @override_settings(CACHES={'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
    def test_a_per_process_cache_is_flagged(self):
        issues = self.run_checks()
        self.assertIn('deployment.W006', ids(issues))
        issue = next(i for i in issues if i.id == 'deployment.W006')
        # This is the one whose consequence is money, so it must be spelled out.
        self.assertIn('twice', issue.hint)

    @override_settings(CACHES={'default': {
        'BACKEND': 'django_redis.cache.RedisCache',
        'LOCATION': 'redis://127.0.0.1:6379/1'}})
    def test_a_shared_cache_is_not_flagged(self):
        self.assertNotIn('deployment.W006', ids(self.run_checks()))

    # ── the whole live configuration at once ─────────────────────────────

    @override_settings(
        DEBUG=True,
        SECRET_KEY='django-insecure-9k^bakokugv8=live',
        CORS_ALLOW_ALL_ORIGINS=True,
        CORS_ALLOWED_ORIGINS=['http://38.45.94.56:4200'],
        CACHES={'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    )
    def test_the_live_configuration_raises_every_relevant_warning(self):
        """Exactly what the production box was running."""
        found = ids(self.run_checks())
        for expected in ('deployment.W001', 'deployment.W002',
                         'deployment.W003', 'deployment.W004',
                         'deployment.W006'):
            self.assertIn(expected, found, f'{expected} did not fire')

    @override_settings(
        DEBUG=False,
        SECRET_KEY='a-real-key',
        CORS_ALLOW_ALL_ORIGINS=False,
        CORS_ALLOWED_ORIGINS=['https://pos.example.com'],
        SESSION_COOKIE_SECURE=True,
        CACHES={'default': {'BACKEND': 'django_redis.cache.RedisCache',
                            'LOCATION': 'redis://127.0.0.1:6379/1'}},
        SERVE_MEDIA_FILES=False,
    )
    def test_a_properly_configured_deployment_is_silent(self):
        """The checks must be quiet when things are right, or they get ignored."""
        noisy = {i.id for i in self.run_checks()} - {'deployment.W008'}
        self.assertEqual(noisy, set(), f'unexpected warnings: {noisy}')


class PurgeAuditLogTests(TestCase):
    """SystemLog gains a row per write request and nothing removed them."""

    def setUp(self):
        # Start from an empty log whatever an earlier test (or its background
        # writer) left behind.
        SystemLog.objects.all().delete()

    def seed(self, count, days_old):
        when = timezone.now() - datetime.timedelta(days=days_old)
        for i in range(count):
            row = SystemLog.objects.create(
                user_name='Asha', user_email='asha@test.com', action='Create',
                module='billing', entity_type='Invoice', entity_id=str(i),
                human_description='Create billing', path='/billing/api/')
            SystemLog.objects.filter(pk=row.pk).update(timestamp=when)

    def call(self, *args):
        out = _io.StringIO()
        call_command('purge_audit_logs', *args, stdout=out, stderr=out)
        return out.getvalue()

    def test_it_is_a_dry_run_by_default(self):
        """Deleting audit history on a typo would be unrecoverable."""
        self.seed(5, days_old=400)
        output = self.call('--older-than', '365')

        self.assertIn('DRY RUN', output)
        self.assertEqual(SystemLog.objects.count(), 5)

    def test_apply_deletes_only_the_old_rows(self):
        self.seed(5, days_old=400)
        self.seed(3, days_old=10)

        self.call('--older-than', '365', '--apply')

        self.assertEqual(SystemLog.objects.count(), 3)

    def test_it_refuses_a_dangerously_short_window(self):
        from django.core.management.base import CommandError

        self.seed(1, days_old=400)
        with self.assertRaises(CommandError) as caught:
            self.call('--older-than', '7', '--apply')
        self.assertIn('90', str(caught.exception))
        self.assertEqual(SystemLog.objects.count(), 1)

    def test_force_allows_a_short_window(self):
        self.seed(2, days_old=30)
        self.call('--older-than', '7', '--apply', '--force')
        self.assertEqual(SystemLog.objects.count(), 0)

    def test_nothing_to_do_is_reported_clearly(self):
        self.seed(3, days_old=10)
        output = self.call('--older-than', '365', '--apply')
        self.assertIn('Nothing to do', output)
        self.assertEqual(SystemLog.objects.count(), 3)

    def test_the_archive_preserves_every_row_before_deleting(self):
        """"Trimmed" must not have to mean "gone"."""
        self.seed(4, days_old=400)

        with tempfile.TemporaryDirectory() as directory:
            self.call('--older-than', '365', '--apply', '--archive-to', directory)

            files = os.listdir(directory)
            self.assertEqual(len(files), 1, files)
            path = os.path.join(directory, files[0])

            with gzip.open(path, 'rt', encoding='utf-8') as handle:
                rows = [json.loads(line) for line in handle if line.strip()]

        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]['user_email'], 'asha@test.com')
        self.assertEqual(rows[0]['module'], 'billing')
        # The timestamp has to survive, or the archive cannot answer anything.
        self.assertTrue(rows[0]['timestamp'])
        self.assertEqual(SystemLog.objects.count(), 0)

    def test_it_warns_when_deleting_without_an_archive(self):
        self.seed(2, days_old=400)
        output = self.call('--older-than', '365', '--apply')
        self.assertIn('gone for good', output)

    def test_batching_does_not_change_the_outcome(self):
        """Batches exist so a big purge does not hold one long lock."""
        self.seed(25, days_old=400)
        self.call('--older-than', '365', '--apply', '--batch-size', '4')
        self.assertEqual(SystemLog.objects.count(), 0)

    def test_the_report_states_what_would_go_and_what_would_stay(self):
        self.seed(7, days_old=400)
        self.seed(2, days_old=5)
        output = self.call('--older-than', '365')
        self.assertIn('Rows to remove: 7', output)
        self.assertIn('Rows to keep  : 2', output)
