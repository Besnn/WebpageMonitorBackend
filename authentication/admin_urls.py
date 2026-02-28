from django.urls import path

from .views import admin_user_search, admin_user_sites

urlpatterns = [
    path('users/', admin_user_search, name='admin_user_search'),
    path('users/<int:user_id>/sites/', admin_user_sites, name='admin_user_sites'),
]

