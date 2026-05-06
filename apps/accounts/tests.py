from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.transactions.application import process_transfer
from apps.transactions.models import Transaction

from .models import Account
from .selectors import list_user_accounts
from .serializers import AccountReadSerializer

User = get_user_model()


class AccountApiTests(APITestCase):
    """Test account api test behavior."""
    def setUp(self):
        """Handle set up."""
        cache.clear()
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
        """Test that create account."""
        response = self.client.post(
            reverse("account-list-create"),
            {"currency": Account.Currency.USD},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Account.objects.filter(owner=self.user).count(), 1)
        self.assertEqual(response.data["balance"], "1000.00")

    def test_list_only_user_accounts(self):
        """Test that list only user accounts."""
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
        """Test that register returns tokens."""
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
        """Test that register rejects duplicate username."""
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

    def test_account_balance_is_served_from_cache_until_invalidated(self):
        """Test that account balance is served from cache until invalidated."""
        own_account = Account.objects.create(
            owner=self.user,
            iban="MBA00000000000000000000000000002",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )

        initial_payload = AccountReadSerializer(own_account).data

        Account.objects.filter(id=own_account.id).update(balance=Decimal("250.00"))
        own_account.refresh_from_db()
        cached_payload = AccountReadSerializer(own_account).data

        self.assertEqual(initial_payload["balance"], "100.00")
        self.assertEqual(cached_payload["balance"], "100.00")

    def test_completed_transfer_refreshes_cached_balances(self):
        """Test that completed transfer refreshes cached balances."""
        sender_account = Account.objects.create(
            owner=self.user,
            iban="MBA00000000000000000000000000003",
            currency=Account.Currency.USD,
            balance=Decimal("100.00"),
        )
        recipient_account = Account.objects.create(
            owner=User.objects.create_user(username="carol", password="testpass123"),
            iban="MBC00000000000000000000000000001",
            currency=Account.Currency.USD,
            balance=Decimal("10.00"),
        )
        transfer = Transaction.objects.create(
            initiated_by=self.user,
            from_account=sender_account,
            to_account=recipient_account,
            idempotency_key="account-cache-transfer-1",
            request_fingerprint="account-cache-transfer-1",
            amount=Decimal("25.00"),
            status=Transaction.Status.PENDING,
        )

        self.assertEqual(AccountReadSerializer(sender_account).data["balance"], "100.00")
        self.assertEqual(AccountReadSerializer(recipient_account).data["balance"], "10.00")

        process_transfer(transaction_id=transfer.id)

        sender_account.refresh_from_db()
        recipient_account.refresh_from_db()

        self.assertEqual(AccountReadSerializer(sender_account).data["balance"], "75.00")
        self.assertEqual(
            AccountReadSerializer(recipient_account).data["balance"],
            "35.00",
        )


class AccountSelectorReplicaRoutingTests(SimpleTestCase):
    """Test account selector replica routing behavior."""

    @override_settings(
        READ_REPLICA_ENABLED=True,
        DATABASES={
            "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"},
            "replica": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"},
        },
    )
    def test_list_user_accounts_uses_replica_when_enabled(self):
        """Test that list_user_accounts uses replica when read replica is enabled."""

        queryset = list_user_accounts(user=User(id=1))

        self.assertEqual(queryset.db, "replica")

    @override_settings(READ_REPLICA_ENABLED=False)
    def test_list_user_accounts_uses_primary_when_replica_disabled(self):
        """Test that list_user_accounts uses primary when replica is disabled."""

        queryset = list_user_accounts(user=User(id=1))

        self.assertEqual(queryset.db, "default")
