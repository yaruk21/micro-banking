from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from .models import Account

User = get_user_model()


class AccountApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="testpass123")
        other_user = User.objects.create_user(username="bob", password="testpass123")

        self.account = Account.objects.create(
            owner=other_user,
            iban="MBO00000000000000000000000000001",
            currency=Account.Currency.EUR,
        )

        token_response = self.client.post(
            reverse("token_obtain_pair"),
            {"username": "alice", "password": "testpass123"},
            format="json",
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {token_response.data['access']}"
        )

    def test_create_account(self):
        response = self.client.post(
            reverse("account-list-create"),
            {"currency": Account.Currency.USD},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Account.objects.filter(owner=self.user).count(), 1)
        self.assertEqual(response.data["balance"], "0.00")

    def test_list_only_user_accounts(self):
        own_account = Account.objects.create(
            owner=self.user,
            iban="MBA00000000000000000000000000001",
            currency=Account.Currency.USD,
        )

        response = self.client.get(reverse("account-list-create"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["id"], own_account.id)

    def test_register_returns_tokens(self):
        response = self.client.post(
            reverse("register"),
            {
                "username": "charlie",
                "email": "charlie@example.com",
                "password": "StrongPass123!",
                "password_confirm": "StrongPass123!",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)
        self.assertEqual(response.data["user"]["username"], "charlie")
        self.assertTrue(User.objects.filter(username="charlie").exists())

    def test_register_rejects_duplicate_username(self):
        response = self.client.post(
            reverse("register"),
            {
                "username": "alice",
                "email": "alice@example.com",
                "password": "StrongPass123!",
                "password_confirm": "StrongPass123!",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("username", response.data)
