from django.db import models


class StaffToken(models.Model):
    """A sign-in of the staff app on one phone. It stops working when the staff member's app PIN changes
    (`fingerprint`), when they are switched off, after `STAFF_TOKEN_DAYS` (default 30), or when they sign out."""
    key = models.CharField(max_length=40, unique=True)
    staff = models.ForeignKey('staff.StaffMember', on_delete=models.CASCADE, related_name='app_tokens')
    fingerprint = models.CharField(max_length=32, blank=True, default='')
    created = models.DateTimeField(auto_now_add=True)
    last_used = models.DateTimeField(null=True, blank=True)
