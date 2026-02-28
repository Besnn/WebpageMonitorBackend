from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.admin.sites import AlreadyRegistered

User = get_user_model()

try:
    admin.site.unregister(User)
except AlreadyRegistered:
    pass

@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    pass


