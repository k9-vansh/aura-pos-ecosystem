"""App config for the project package itself, so its system checks get loaded.

Django only imports a package's `checks` if the package is an installed app.
`pos_backend` was not one, so `deployment_checks.py` would never run.
"""

from django.apps import AppConfig


class PosBackendConfig(AppConfig):
    name = 'pos_backend'
    verbose_name = 'POS platform'

    def ready(self):
        # Registers the deployment safety checks. Importing for the side effect
        # is the documented way to register system checks.
        from . import deployment_checks  # noqa: F401

        # No export writes a formula on purpose; stop customer-typed text that
        # starts with "=" from becoming one in a downloaded spreadsheet.
        try:
            from .xlsx_safe import install as _install_xlsx_guard
            _install_xlsx_guard()
        except ImportError:
            pass

        # Start the background SAP retry worker. start_scheduler() decides
        # whether this process should run it (server processes only, and only
        # when SAP_SCHEDULER and SAP_ENABLED are on); a database lease keeps it
        # to one process at a time.
        from .scheduler import start_scheduler
        start_scheduler()
