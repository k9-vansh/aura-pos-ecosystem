from django.contrib import admin
from .models import Center, Role

@admin.register(Center)
class CenterAdmin(admin.ModelAdmin):
    list_display = ('center_name', 'display_name', 'region', 'phone')
    search_fields = ('center_name', 'display_name', 'phone')

@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ('name',)
    search_fields = ('name',)
