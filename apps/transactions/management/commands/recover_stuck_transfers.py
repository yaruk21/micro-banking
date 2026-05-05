from django.core.management.base import BaseCommand

from apps.transactions.workers.celery_tasks import recover_stuck_transfers_task


class Command(BaseCommand):
    help = "Requeue transactions stuck in pending/processing beyond the recovery threshold."

    def handle(self, *args, **options):
        recovered_count = recover_stuck_transfers_task()
        self.stdout.write(
            self.style.SUCCESS(
                f"Requeued {recovered_count} stuck transaction(s)."
            )
        )
