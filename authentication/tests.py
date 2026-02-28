from django.contrib.auth.models import User
from django.test import Client, TestCase
import json


class AuthEndpointsTests(TestCase):
    def setUp(self):
        self.client = Client()

    def test_register_creates_auth_user_and_role_user(self):
        resp = self.client.post(
            "/api/auth/register/",
            data=json.dumps({"username": "Alice", "email": "alice@example.com", "password": "password123"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(User.objects.filter(email__iexact="alice@example.com").exists())
        data = resp.json()
        self.assertEqual(data["user"]["role"], "user")

    def test_login_sets_session_and_returns_role(self):
        User.objects.create_user(username="bob", email="bob@example.com", password="password123")

        resp = self.client.post(
            "/api/auth/login/",
            data=json.dumps({"username": "bob@example.com", "password": "password123"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["user"]["email"], "bob@example.com")
        self.assertEqual(data["user"]["role"], "user")

        # session should allow /me
        me = self.client.get("/api/auth/me/")
        self.assertEqual(me.status_code, 200)

    def test_me_requires_auth(self):
        resp = self.client.get("/api/auth/me/")
        self.assertEqual(resp.status_code, 401)

    def test_admin_role_when_staff(self):
        u = User.objects.create_user(username="admin1", email="admin@example.com", password="password123")
        u.is_staff = True
        u.save(update_fields=["is_staff"])

        resp = self.client.post(
            "/api/auth/login/",
            data=json.dumps({"username": "admin@example.com", "password": "password123"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["user"]["role"], "admin")

        me = self.client.get("/api/auth/me/")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["user"]["role"], "admin")

    def test_login_with_username(self):
        User.objects.create_user(username="testuser", email="test@example.com", password="pass123")

        resp = self.client.post(
            "/api/auth/login/",
            data=json.dumps({"username": "testuser", "password": "pass123"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["user"]["email"], "test@example.com")
        self.assertEqual(data["user"]["role"], "user")

        # Verify session works
        me_resp = self.client.get("/api/auth/me/")
        self.assertEqual(me_resp.status_code, 200)