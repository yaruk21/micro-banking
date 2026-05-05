from apps.accounts.models import Account
from apps.accounts.cache import refresh_account_balance_cache
from core.cache_utils import bump_user_cache_version


def bump_pending_transaction_caches(
    *,
    from_account: Account,
    to_account: Account,
) -> None:
    affected_user_ids = {from_account.owner_id, to_account.owner_id}
    for user_id in affected_user_ids:
        bump_user_cache_version(namespace="transactions_list", user_id=user_id)


def bump_transfer_related_caches(
    *,
    from_account: Account,
    to_account: Account,
) -> None:
    affected_accounts = {from_account.id: from_account, to_account.id: to_account}
    for account in affected_accounts.values():
        refresh_account_balance_cache(account=account)

    affected_user_ids = {from_account.owner_id, to_account.owner_id}
    for user_id in affected_user_ids:
        bump_user_cache_version(namespace="accounts_list", user_id=user_id)
        bump_user_cache_version(namespace="transactions_list", user_id=user_id)


def bump_failed_transaction_caches(
    *,
    from_account: Account,
    to_account: Account,
) -> None:
    affected_user_ids = {from_account.owner_id, to_account.owner_id}
    for user_id in affected_user_ids:
        bump_user_cache_version(namespace="transactions_list", user_id=user_id)
