"""Test runner that starts every test from a system with no approval rules.

A new database now ships with the discount and price-change approvals switched
on (approvals migration 0002), so a bill sold below list price needs an
approver's yes. The test-suite was written when a system with no rule needed no
approval, and the tests that exercise approvals create the rules they want, so
the shipped defaults are removed once the test database is built.
"""

from django.test.runner import DiscoverRunner


class PosTestRunner(DiscoverRunner):
    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        # Audit rows are normally written by a background thread on its own
        # database connection, so they were committed outside every test's
        # transaction and leaked into whichever test ran next (the audit-log
        # purge tests then counted hundreds of rows they did not create). Written
        # in-line they roll back with the test that caused them.
        from django.conf import settings
        settings.AUDIT_LOG_ASYNC = False

    def setup_databases(self, **kwargs):
        config = super().setup_databases(**kwargs)
        from approvals.models import ApprovalPolicy
        ApprovalPolicy.objects.all().delete()
        return config
