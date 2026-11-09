from django.contrib import admin
from .models import CustomUser, Message

@admin.register(CustomUser)
class CustomUserAdmin(admin.ModelAdmin):
    list_display = ('email', 'full_name', 'phone', 'role', 'center', 'is_staff')
    search_fields = ('email', 'full_name', 'phone')
    list_filter = ('role', 'center', 'is_staff', 'is_active')

@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ('id', 'sender', 'receiver', 'short_content', 'has_image', 'is_read', 'timestamp')
    list_filter = ('is_read', 'timestamp', 'sender', 'receiver')
    search_fields = ('content', 'sender__email', 'sender__full_name', 'receiver__email', 'receiver__full_name')
    readonly_fields = ('sender', 'receiver', 'content', 'image', 'timestamp', 'is_read')
    ordering = ('-timestamp',)
    date_hierarchy = 'timestamp'
    list_per_page = 50

    @admin.display(description='Content')
    def short_content(self, obj):
        if obj.content:
            return obj.content[:80] + ('...' if len(obj.content) > 80 else '')
        return '— (image only) —'

    @admin.display(boolean=True, description='Image?')
    def has_image(self, obj):
        return bool(obj.image)
