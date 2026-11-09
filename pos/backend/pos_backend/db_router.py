"""Big reports read from a replica database, when one is configured.

Set DB_REPLICA_HOST (and optionally DB_REPLICA_PORT / DB_REPLICA_NAME / DB_REPLICA_USER / DB_REPLICA_PASSWORD) and the
big reports (pos_backend/heavy_reports.py HEAVY_PATHS) read from that server instead of the one the tills write to, so
an owner opening an all-centre dashboard cannot slow billing at all. Everything else - every write, every till
screen, login and tokens - stays on the primary, so nothing waits on replication lag.

With no DB_REPLICA_HOST this module is not even installed (settings.py): the app behaves exactly as before.
"""

import threading

from django.db import connections

_state = threading.local()

# Models that must always be read from the primary, even inside a report: who is signed in, and the bookkeeping that
# a request writes as it goes. A replica that is a second behind must never sign someone out.
PRIMARY_ONLY_APPS = {'authtoken', 'accounts', 'sessions', 'auth', 'contenttypes', 'audit_logs', 'admin'}


def use_replica(on):
    """Called by the heavy-report gate around a report request."""
    _state.on = bool(on)


def replica_alias():
    return 'replica' if 'replica' in connections.databases else None


class ReportsReadFromReplica:
    def db_for_read(self, model, **hints):
        if getattr(_state, 'on', False) and model._meta.app_label not in PRIMARY_ONLY_APPS:
            return replica_alias()
        return None

    def db_for_write(self, model, **hints):
        return None

    def allow_relation(self, obj1, obj2, **hints):
        return True

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        return db == 'default'
