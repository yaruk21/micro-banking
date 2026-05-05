from celery import shared_task

from .services import process_transfer


@shared_task
def process_transfer_task(transaction_id: int) -> None:
    process_transfer(transaction_id=transaction_id)
