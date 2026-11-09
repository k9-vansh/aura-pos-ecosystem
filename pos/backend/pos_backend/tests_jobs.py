"""The database job queue (pos_backend/jobs.py): outbox semantics and claiming."""

from datetime import timedelta

from django.db import transaction
from django.test import TestCase
from django.utils import timezone

from pos_backend import jobs
from pos_backend.models import BackgroundJob, JobSchedule

CALLS = []


@jobs.job('test.ok')
def _ok(payload, row):
    CALLS.append(('ok', payload))


@jobs.job('test.fail')
def _fail(payload, row):
    raise RuntimeError('boom')


@jobs.job('test.later')
def _later(payload, row):
    raise jobs.RetryLater(120, 'SAP in maintenance')


class EnqueueTests(TestCase):
    def setUp(self):
        CALLS.clear()

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            jobs.enqueue('no.such.kind', {})

    def test_job_rolls_back_with_the_business_transaction(self):
        """Outbox: a job exists only if the change that caused it committed."""
        try:
            with transaction.atomic():
                jobs.enqueue('test.ok', {'n': 1})
                raise RuntimeError('the bill failed to save')
        except RuntimeError:
            pass
        self.assertFalse(BackgroundJob.objects.exists())

    def test_dedupe_key_returns_the_live_job(self):
        a = jobs.enqueue('test.ok', {'n': 1}, dedupe_key='k1')
        b = jobs.enqueue('test.ok', {'n': 2}, dedupe_key='k1')
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(BackgroundJob.objects.count(), 1)

    def test_key_is_free_again_after_the_job_finishes(self):
        jobs.enqueue('test.ok', {}, dedupe_key='k2')
        jobs.process_due('w1')
        again = jobs.enqueue('test.ok', {}, dedupe_key='k2')
        self.assertEqual(again.status, BackgroundJob.QUEUED)
        self.assertEqual(BackgroundJob.objects.count(), 2)


class RunTests(TestCase):
    def setUp(self):
        CALLS.clear()

    def test_success_runs_once_and_is_done(self):
        row = jobs.enqueue('test.ok', {'n': 7})
        self.assertEqual(jobs.process_due('w1'), {BackgroundJob.DONE: 1})
        row.refresh_from_db()
        self.assertEqual(row.status, BackgroundJob.DONE)
        self.assertEqual(row.attempts, 1)
        self.assertEqual(CALLS, [('ok', {'n': 7})])
        self.assertEqual(jobs.process_due('w1'), {})  # nothing left to run

    def test_future_job_is_not_run_early(self):
        jobs.enqueue('test.ok', {}, run_after=timezone.now() + timedelta(hours=1))
        self.assertEqual(jobs.process_due('w1'), {})

    def test_failure_backs_off_then_dies(self):
        row = jobs.enqueue('test.fail', {}, max_attempts=2)
        jobs.process_due('w1')
        row.refresh_from_db()
        self.assertEqual(row.status, BackgroundJob.FAILED)
        self.assertIn('boom', row.last_error)
        self.assertGreater(row.run_after, timezone.now())
        # Make it due again and fail a second time: that is the last attempt.
        BackgroundJob.objects.filter(pk=row.pk).update(run_after=timezone.now())
        jobs.process_due('w1')
        row.refresh_from_db()
        self.assertEqual(row.status, BackgroundJob.DEAD)
        self.assertEqual(row.attempts, 2)

    def test_retry_later_does_not_use_up_an_attempt(self):
        row = jobs.enqueue('test.later', {}, max_attempts=1)
        jobs.process_due('w1')
        row.refresh_from_db()
        self.assertEqual(row.status, BackgroundJob.FAILED)
        self.assertEqual(row.attempts, 0)
        self.assertGreater(row.run_after, timezone.now() + timedelta(seconds=100))

    def test_a_crashed_worker_lease_expires(self):
        row = jobs.enqueue('test.ok', {})
        claimed = jobs.claim('dead-worker')
        self.assertEqual([j.pk for j in claimed], [row.pk])
        # Nobody else may take it while the lease holds...
        self.assertEqual(jobs.claim('w2'), [])
        # ...but once the lease runs out, another worker picks it up.
        BackgroundJob.objects.filter(pk=row.pk).update(locked_until=timezone.now() - timedelta(seconds=1))
        self.assertEqual([j.pk for j in jobs.claim('w2')], [row.pk])

    def test_each_job_goes_to_exactly_one_worker(self):
        for i in range(5):
            jobs.enqueue('test.ok', {'i': i})
        first = jobs.claim('w1', limit=3)
        second = jobs.claim('w2', limit=10)
        ids_1 = {j.pk for j in first}
        ids_2 = {j.pk for j in second}
        self.assertEqual(len(ids_1), 3)
        self.assertEqual(len(ids_2), 2)
        self.assertFalse(ids_1 & ids_2)


class PeriodicTests(TestCase):
    def test_runs_once_per_period(self):
        runs = []
        jobs.periodic('test.tick', every=60)(lambda: runs.append(1))
        try:
            now = timezone.now()
            self.assertIn('test.tick', jobs.run_periodic(now))
            self.assertNotIn('test.tick', jobs.run_periodic(now + timedelta(seconds=30)))
            self.assertIn('test.tick', jobs.run_periodic(now + timedelta(seconds=61)))
            self.assertEqual(len(runs), 2)
            self.assertTrue(JobSchedule.objects.filter(name='test.tick').exists())
        finally:
            jobs._PERIODIC.pop('test.tick', None)
