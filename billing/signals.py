from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Invoice


@receiver(post_save, sender=Invoice)
def queue_automatic_payment(sender, instance, created, **kwargs):
    """New invoices for members who pay automatically are queued for the scheduler to collect."""
    if created and not getattr(instance, 'skip_autopay', False):
        from .autopay import mark_due
        transaction.on_commit(lambda: mark_due(instance))


@receiver(post_save, sender=Invoice)
def session_bundle_paid(sender, instance, **kwargs):
    """A paid invoice for a session bundle adds its sessions to the member's wallet; a cancelled one drops it."""
    from . import bundles
    if instance.status == Invoice.Status.PAID:
        bundles.activate_paid(instance)
    elif instance.status == Invoice.Status.CANCELLED:
        bundles.cancel_unpaid(instance)
