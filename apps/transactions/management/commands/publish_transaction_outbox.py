from django.core.management.base import BaseCommand

from apps.transactions.application import publish_pending_transaction_outbox


class Command(BaseCommand):
    help = "Dispatch pending transaction outbox entries to Celery."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=100,
            help="Maximum number of pending outbox entries to dispatch.",
        )

    def handle(self, *args, **options):
        published_count = publish_pending_transaction_outbox(
            limit=options["limit"]
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Dispatched {published_count} transaction outbox entrie(s)."
            )
        )
