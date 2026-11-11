import sys
from django.apps import AppConfig

class AccountsConfig(AppConfig):
    name = 'accounts'

    def ready(self):
        # Skip during build commands
        if any(cmd in sys.argv for cmd in ['makemigrations', 'migrate', 'collectstatic']):
            return
        try:
            import threading
            from pos_backend.demo_seeder import ensure_demo_accounts
            threading.Thread(target=ensure_demo_accounts, daemon=True).start()
        except Exception:
            pass
