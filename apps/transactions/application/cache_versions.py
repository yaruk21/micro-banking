from apps.accounts.models import Account
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
