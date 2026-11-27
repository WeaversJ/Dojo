"""
Session bundles: a member buys a number of sessions up front, and each session they book uses one.

A bundle (SessionBundle) is bought on a session-bundle billing policy, online through Stripe
Checkout or added by the club. Its sessions can be used once its invoice is paid. Booking a session
takes one from the oldest usable bundle (BundleUse). The rules:

- Booking always uses a session, and not turning up doesn't give it back.
- Cancelling the booking (while online changes are open) gives it back only if the bundle allows it:
  the club decides, per policy (copied onto each bundle when it's bought).
- If the club cancels the session, everyone booked on it gets their session back.
- A bundle with a use-by date covers sessions up to that date.
"""
from datetime import timedelta

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone


class NoSessionsLeft(Exception):
    """A member on a session bundle has no sessions left to book with."""


def on_bundle(member):
    policy = member.billing_policy
    return bool(policy and policy.is_bundle)


def _active(member):
    from .models import SessionBundle
    return (SessionBundle.objects.filter(member=member, status=SessionBundle.Status.ACTIVE)
            .annotate(used_count=Count('uses', filter=Q(uses__returned_at__isnull=True))))


def usable(member, day=None):
    """Active bundles with sessions left that can still be used on `day` (default today): the first is used first."""
    day = day or timezone.localdate()
    bundles = [b for b in _active(member) if b.left > 0 and b.usable_on(day)]
    # Those running out soonest first, then the oldest.
    return sorted(bundles, key=lambda b: (b.expires_on is None, b.expires_on or day, b.activated_at or b.created_at))


def sessions_left(member, day=None):
    return sum(b.left for b in usable(member, day))


def wallet(member):
    """For the portal and member page: bundles to show, newest first, and the total left."""
    from .models import SessionBundle
    today = timezone.localdate()
    bundles = list(SessionBundle.objects.filter(member=member).exclude(status=SessionBundle.Status.CANCELLED)
                   .annotate(used_count=Count('uses', filter=Q(uses__returned_at__isnull=True)))
                   .select_related('invoice').order_by('-created_at'))
    for b in bundles:
        b.expired = b.status == SessionBundle.Status.ACTIVE and b.expires_on is not None and b.expires_on < today
    shown = [b for b in bundles if b.status == SessionBundle.Status.PENDING or (b.left and not b.expired)]
    used_up = [b for b in bundles if b not in shown][:5]
    return {'left': sum(b.left for b in bundles if b.status == SessionBundle.Status.ACTIVE and not b.expired),
            'bundles': shown, 'past': used_up,
            'pending': [b for b in bundles if b.status == SessionBundle.Status.PENDING]}


# ── Buying ─────────────────────────────────────────────────────────────────────

def buy(member, policy, added_by=''):
    """
    Start a bundle for `member` on `policy`, with an invoice for its price. Its sessions are added when
    the invoice is paid (see activate_paid). A free bundle is active straight away. Returns the bundle.
    """
    from .models import Invoice, SessionBundle
    if not policy.is_bundle or not policy.bundle_sessions:
        raise ValueError('That isn\'t a session bundle.')
    price = policy.amount or 0
    with transaction.atomic():
        invoice = None
        if price > 0:
            invoice = Invoice(
                organisation=member.organisation, member=member, amount=price,
                period=f'{policy.name}: {policy.bundle_sessions} sessions', due_date=timezone.localdate(),
                notes='Session bundle. The sessions are added when this is paid.',
            )
            invoice.skip_autopay = True   # bought on purpose, paid when bought (or as the club agrees)
            invoice.save()
        bundle = SessionBundle.objects.create(
            organisation=member.organisation, member=member, policy=policy, name=policy.name,
            sessions=policy.bundle_sessions, price=price, cancel_returns=policy.bundle_cancel_returns,
            valid_weeks=policy.bundle_valid_weeks, invoice=invoice, added_by=added_by,
        )
        if invoice is None:
            _activate(bundle)
    return bundle


def unpaid_purchase(member, policy):
    """A bundle on this policy the member started buying but hasn't paid for, if any: reused rather than doubled."""
    from .models import SessionBundle
    return (SessionBundle.objects.filter(member=member, policy=policy, status=SessionBundle.Status.PENDING,
                                         invoice__status='unpaid').select_related('invoice').first())


def _activate(bundle):
    from .models import SessionBundle
    now = timezone.now()
    bundle.status = SessionBundle.Status.ACTIVE
    bundle.activated_at = now
    if bundle.valid_weeks:
        bundle.expires_on = timezone.localdate(now) + timedelta(weeks=bundle.valid_weeks)
    bundle.save(update_fields=['status', 'activated_at', 'expires_on'])


def activate_paid(invoice):
    """An invoice was paid: activate any bundle waiting for it. Called whenever an invoice is saved as paid."""
    from .models import SessionBundle
    if invoice.status != 'paid':
        return []
    activated = []
    with transaction.atomic():
        for bundle in SessionBundle.objects.select_for_update().filter(invoice=invoice, status=SessionBundle.Status.PENDING):
            _activate(bundle)
            activated.append(bundle)
    return activated


def cancel_unpaid(invoice):
    """An invoice was cancelled: a bundle waiting for it won't be paid for."""
    from .models import SessionBundle
    SessionBundle.objects.filter(invoice=invoice, status=SessionBundle.Status.PENDING).update(
        status=SessionBundle.Status.CANCELLED)


# ── Using ──────────────────────────────────────────────────────────────────────

def take(member, session):
    """Use one session from the member's wallet for `session`. Raises NoSessionsLeft. Call inside a transaction."""
    from .models import BundleUse, SessionBundle
    ids = [b.pk for b in usable(member, session.date)]
    # Lock them, then re-check: two bookings at once mustn't both use the last session.
    locked = {b.pk: b for b in SessionBundle.objects.select_for_update().filter(pk__in=ids)}
    for pk in ids:
        bundle = locked.get(pk)
        if bundle is not None and bundle.left > 0 and bundle.usable_on(session.date):
            return BundleUse.objects.create(bundle=bundle, member=member, session=session)
    raise NoSessionsLeft


def current_use(member, session):
    from .models import BundleUse
    return (BundleUse.objects.filter(member=member, session=session, returned_at__isnull=True)
            .select_related('bundle').order_by('-created_at').first())


def give_back(use, why):
    use.returned_at = timezone.now()
    use.returned_why = why
    use.save(update_fields=['returned_at', 'returned_why'])


def booking_cancelled(member, session):
    """
    The member cancelled their booking: give the session back if their bundle allows it. Returns True
    if it was given back, False if it was kept (the bundle's rules), None if it didn't use a bundle.
    """
    use = current_use(member, session)
    if use is None:
        return None
    if use.bundle.cancel_returns:
        give_back(use, 'Booking cancelled')
        return True
    return False


def session_cancelled(session):
    """The club cancelled a session: everyone who used a bundle session on it gets it back."""
    from .models import BundleUse
    for use in BundleUse.objects.filter(session=session, returned_at__isnull=True):
        give_back(use, 'Session cancelled by the club')


def session_reinstated(session):
    """The club un-cancelled a session: bookings on it use a bundle session again, where there's one to use."""
    from classes.models import SessionBooking
    for booking in SessionBooking.objects.filter(session=session, status=SessionBooking.Status.CONFIRMED) \
            .select_related('member__billing_policy'):
        if on_bundle(booking.member) and current_use(booking.member, session) is None:
            try:
                with transaction.atomic():
                    take(booking.member, session)
            except NoSessionsLeft:
                pass   # booked anyway: the club reinstated it
