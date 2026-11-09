from django.db import models


class ClientToken(models.Model):
    """A sign-in of the client app on one phone. It stops working when the client's PIN changes (`fingerprint`), when the
    client is switched off, after `CLIENT_TOKEN_DAYS` (default 60), or when they sign out."""
    key = models.CharField(max_length=40, unique=True)
    client = models.ForeignKey('clients.Client', on_delete=models.CASCADE, related_name='app_tokens')
    fingerprint = models.CharField(max_length=32, blank=True, default='')
    created = models.DateTimeField(auto_now_add=True)
    last_used = models.DateTimeField(null=True, blank=True)
