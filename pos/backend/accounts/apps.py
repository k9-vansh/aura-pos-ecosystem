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
            from django.core.management import call_command
            threading.Thread(target=lambda: call_command('seed_demo_accounts'), daemon=True).start()
        except Exception:
            pass
