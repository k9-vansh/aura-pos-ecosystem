from django.contrib import admin
from django.contrib.auth.hashers import identify_hasher

from .models import OwnerAccount, OwnerToken


@admin.register(OwnerAccount)
class OwnerAccountAdmin(admin.ModelAdmin):
    """Owner app accounts: type a new password into the password box and it is stored hashed."""
    list_display = ('name', 'phone', 'role', 'is_active', 'last_login')
    list_filter = ('is_active', 'role')
    search_fields = ('name', 'phone', 'email')
    filter_horizontal = ('centers',)
    readonly_fields = ('last_login', 'created_at')

    def save_model(self, request, obj, form, change):
        try:
            identify_hasher(obj.password)
        except ValueError:
            obj.set_password(obj.password)              # a plain password was typed: store it hashed
        super().save_model(request, obj, form, change)


@admin.register(OwnerToken)
class OwnerTokenAdmin(admin.ModelAdmin):
    list_display = ('account', 'created', 'last_used')
    exclude = ('key',)
    readonly_fields = ('account', 'created', 'last_used', 'fingerprint')
