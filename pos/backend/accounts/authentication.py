import datetime
from django.conf import settings
from django.utils import timezone
from rest_framework.authentication import TokenAuthentication
from rest_framework.exceptions import AuthenticationFailed

class ExpiringTokenAuthentication(TokenAuthentication):
    def authenticate_credentials(self, key):
        model = self.get_model()
        try:
            token = model.objects.select_related('user').get(key=key)
        except model.DoesNotExist:
            raise AuthenticationFailed('Invalid token.')

        if not token.user.is_active:
            raise AuthenticationFailed('User inactive or deleted.')

        # Check token expiration
        max_age_days = getattr(settings, 'API_TOKEN_MAX_AGE_DAYS', 7)
        if token.created < timezone.now() - datetime.timedelta(days=max_age_days):
            raise AuthenticationFailed('Token has expired.')

        return (token.user, token)
