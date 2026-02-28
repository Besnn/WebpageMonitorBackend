from django.contrib import admin

from .models import MonitoredPage

# Register your models here.

@admin.register(MonitoredPage)
class MonitoredPageAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'url', 'created_at')
    search_fields = ('url', 'user__email', 'user__username')
    list_filter = ('created_at',)
