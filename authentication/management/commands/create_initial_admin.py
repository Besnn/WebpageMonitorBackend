import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create (or update) an initial admin user from env vars."

    def add_arguments(self, parser):
        parser.add_argument("--username", default=None)
        parser.add_argument("--email", default=None)
        parser.add_argument("--password", default=None)

    def handle(self, *args, **options):
        User = get_user_model()

        username = options["username"] or os.getenv("INITIAL_ADMIN_USERNAME") or "admin"
        email = options["email"] or os.getenv("INITIAL_ADMIN_EMAIL") or "admin@example.com"
        password = options["password"] or os.getenv("INITIAL_ADMIN_PASSWORD") or "admin12345"

        user, created = User.objects.get_or_create(username=username, defaults={"email": email})
        if not user.email:
            user.email = email

        user.is_staff = True
        user.is_superuser = True
        user.set_password(password)
        user.save()

        if created:
            self.stdout.write(self.style.SUCCESS(f"Created admin user '{username}' ({email})"))
        else:
            self.stdout.write(self.style.SUCCESS(f"Updated admin user '{username}' ({email})"))

