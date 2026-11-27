"""Session bundles: members buy a number of sessions, and each session they book uses one."""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.urls import reverse
from django.utils import timezone

from billing import bundles
from billing.calculator import calculate_invoice_amount
from billing.models import BillingPolicy, BundleUse, Invoice, Payment, SessionBundle
from classes import booking
from classes.models import Attendance, Session, SessionBooking
from classes.test_bookings import BookingTestBase


class BundleCase(BookingTestBase):
    def setUp(self):
        self.pack = BillingPolicy.objects.create(
            organisation=self.org, name='10-session pack', billing_cycle='custom', amount=Decimal('60.00'),
            pricing_model=BillingPolicy.PricingModel.BUNDLE, bundle_sessions=3, bundle_cancel_returns=True,
            member_selectable=True,
        )
        self.member.billing_policy = self.pack
        self.member.save(update_fields=['billing_policy'])
        self.later = Session.objects.create(assigned_class=self.juniors, date=timezone.localdate() + timedelta(days=8))
        self.third = Session.objects.create(assigned_class=self.juniors, date=timezone.localdate() + timedelta(days=9))

    def paid_bundle(self, policy=None, member=None):
        bundle = bundles.buy(member or self.member, policy or self.pack)
        bundle.invoice.status = 'paid'
        bundle.invoice.save()
        bundle.refresh_from_db()
        return bundle

    def left(self, member=None):
        return bundles.sessions_left(member or self.member)


class WalletTests(BundleCase):
    def test_an_unpaid_bundle_has_no_sessions_until_its_invoice_is_paid(self):
        bundle = bundles.buy(self.member, self.pack)
        self.assertEqual((bundle.status, bundle.invoice.amount, bundle.sessions), ('pending', Decimal('60.00'), 3))
        self.assertEqual(self.left(), 0)
        with self.assertRaisesMessage(booking.BookingError, 'no sessions left in your bundle'):
            booking.book_session(self.member, self.session)
        self.assertFalse(SessionBooking.objects.exists())

        bundle.invoice.status = 'paid'
        bundle.invoice.save()   # however it's paid: Stripe, or the club recording it
        bundle.refresh_from_db()
        self.assertEqual(bundle.status, 'active')
        self.assertIsNone(bundle.expires_on)
        self.assertEqual(self.left(), 3)

    def test_booking_uses_a_session_and_not_turning_up_keeps_it_used(self):
        self.paid_bundle()
        booking.book_session(self.member, self.session)
        self.assertEqual(self.left(), 2)
        Attendance.objects.create(session=self.session, member=self.member, present=False)
        self.assertEqual(self.left(), 2)

    def test_no_sessions_left_means_no_booking(self):
        self.pack.bundle_sessions = 1
        self.pack.save()
        self.paid_bundle()
        booking.book_session(self.member, self.session)
        with self.assertRaisesMessage(booking.BookingError, 'Buy another bundle'):
            booking.book_session(self.member, self.later)
        self.assertFalse(SessionBooking.objects.filter(session=self.later).exists())
        self.paid_bundle()   # another bundle
        booking.book_session(self.member, self.later)
        self.assertEqual(self.left(), 0)

    def test_cancelling_gives_the_session_back_when_the_club_allows_it(self):
        self.paid_bundle()
        booking.book_session(self.member, self.session)
        booking.cancel_session_booking(self.member, self.session)
        self.assertEqual(self.left(), 3)
        self.assertEqual(BundleUse.objects.get().returned_why, 'Booking cancelled')

    def test_cancelling_keeps_the_session_when_the_club_says_so(self):
        self.pack.bundle_cancel_returns = False
        self.pack.save()
        self.paid_bundle()
        booking.book_session(self.member, self.session)
        booking.cancel_session_booking(self.member, self.session)
        self.assertEqual(self.left(), 2)
        booking.book_session(self.member, self.session)   # booking it again uses another
        self.assertEqual(self.left(), 1)

    def test_a_bundles_terms_are_fixed_when_its_bought(self):
        self.paid_bundle()   # bought while cancelling gives sessions back
        self.pack.bundle_cancel_returns = False
        self.pack.save()
        booking.book_session(self.member, self.session)
        booking.cancel_session_booking(self.member, self.session)
        self.assertEqual(self.left(), 3)

    def test_the_club_cancelling_a_session_gives_it_back(self):
        self.pack.bundle_cancel_returns = False   # even when cancelling by the member wouldn't
        self.pack.save()
        self.paid_bundle()
        booking.book_session(self.member, self.session)
        self.client.force_login(self.admin)
        url = reverse('session_cancel', kwargs={'org_slug': self.org.slug, 'pk': self.juniors.pk,
                                                 'session_pk': self.session.pk})
        self.client.post(url)
        self.assertEqual(self.left(), 3)
        self.client.post(url)   # reinstated: the booking uses a session again
        self.assertEqual(self.left(), 2)

    def test_sessions_are_used_soonest_expiring_first_and_only_until_then(self):
        self.pack.bundle_valid_weeks = 1   # bought today: covers the next 7 days
        self.pack.save()
        short = self.paid_bundle()
        self.pack.bundle_valid_weeks = None
        self.pack.save()
        forever = self.paid_bundle()
        self.assertEqual(short.expires_on, timezone.localdate() + timedelta(weeks=1))
        booking.book_session(self.member, self.session)          # in 7 days: the short one
        self.assertEqual(BundleUse.objects.get(session=self.session).bundle, short)
        booking.book_session(self.member, self.later)            # in 8 days: past it, so the other one
        self.assertEqual(BundleUse.objects.get(session=self.later).bundle, forever)

    def test_an_expired_bundle_cant_be_used(self):
        self.pack.bundle_valid_weeks = 1
        self.pack.save()
        self.paid_bundle()
        with self.assertRaises(booking.BookingError):
            booking.book_session(self.member, self.later)        # after the use-by date

    def test_bundle_members_book_sessions_rather_than_enrol(self):
        self.paid_bundle()
        with self.assertRaisesMessage(booking.BookingError, 'session bundle'):
            booking.enrol(self.member, self.juniors)

    def test_bundles_are_never_in_an_invoice_run(self):
        self.assertEqual(calculate_invoice_amount(self.member, self.pack)['net'], Decimal('0'))

    def test_a_cancelled_invoice_drops_the_bundle(self):
        bundle = bundles.buy(self.member, self.pack)
        bundle.invoice.status = 'cancelled'
        bundle.invoice.save()
        bundle.refresh_from_db()
        self.assertEqual(bundle.status, 'cancelled')


class PolicyFormTests(BundleCase):
    def test_creating_a_bundle_policy(self):
        self.client.force_login(self.admin)
        self.client.post(reverse('billing_policy_create', kwargs={'org_slug': self.org.slug}), {
            'name': 'Five pack', 'policy_type': 'bundle', 'amount': '35', 'bundle_sessions': '5',
            'bundle_valid_weeks': '8', 'member_selectable': '1', 'plan_description': 'Any class',
        })
        policy = BillingPolicy.objects.get(name='Five pack')
        self.assertTrue(policy.is_bundle)
        self.assertEqual((policy.billing_cycle, policy.bundle_sessions, policy.bundle_valid_weeks),
                         ('custom', 5, 8))
        self.assertFalse(policy.bundle_cancel_returns)   # unticked
        self.assertEqual(policy.price_summary(), '£35.00 for 5 sessions')
        self.assertEqual(policy.limit_text(), '5 sessions, used within 8 weeks')

    def test_a_bundle_needs_its_number_of_sessions(self):
        self.client.force_login(self.admin)
        self.client.post(reverse('billing_policy_create', kwargs={'org_slug': self.org.slug}), {
            'name': 'Broken', 'policy_type': 'bundle', 'amount': '35'})
        self.assertFalse(BillingPolicy.objects.filter(name='Broken').exists())

    def test_turning_a_bundle_back_into_a_regular_plan(self):
        self.client.force_login(self.admin)
        self.client.post(reverse('billing_policy_edit', kwargs={'org_slug': self.org.slug, 'pk': self.pack.pk}), {
            'name': 'Monthly', 'policy_type': 'regular', 'billing_cycle': 'monthly', 'amount': '40',
            'limit_unit': 'month', 'is_active': '1'})
        self.pack.refresh_from_db()
        self.assertEqual((self.pack.pricing_model, self.pack.billing_cycle), ('flat', 'monthly'))

    def test_the_policies_page_shows_the_bundle(self):
        self.client.force_login(self.admin)
        page = self.client.get(reverse('billing_policies', kwargs={'org_slug': self.org.slug}))
        self.assertContains(page, 'Session bundle')
        self.assertContains(page, 'for 3 sessions')
        self.assertContains(page, 'name="policy_type"')


class MemberPageTests(BundleCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)
        self.add_url = reverse('member_bundle_add', kwargs={'org_slug': self.org.slug, 'pk': self.other.pk})

    def test_adding_a_bundle_paid_in_cash(self):
        self.client.post(self.add_url, {'policy_id': self.pack.pk, 'paid': 'cash'})
        bundle = SessionBundle.objects.get(member=self.other)
        self.assertEqual(bundle.status, 'active')
        self.assertEqual(bundle.invoice.status, 'paid')
        self.assertEqual(bundle.invoice.payments.get().method, Payment.Method.CASH)
        self.assertEqual(bundle.added_by, 'admin')
        self.assertEqual(self.left(self.other), 3)
        self.other.refresh_from_db()
        self.assertEqual(self.other.billing_policy, self.standard)   # already had a plan: left alone

    def test_adding_a_bundle_to_invoice_later(self):
        self.other.billing_policy = None
        self.other.save()
        self.client.post(self.add_url, {'policy_id': self.pack.pk, 'paid': ''})
        bundle = SessionBundle.objects.get(member=self.other)
        self.assertEqual((bundle.status, bundle.invoice.status), ('pending', 'unpaid'))
        self.other.refresh_from_db()
        self.assertEqual(self.other.billing_policy, self.pack)

    def test_the_member_page_shows_the_wallet(self):
        self.paid_bundle()
        booking.book_session(self.member, self.session)
        page = self.client.get(reverse('member_detail', kwargs={'org_slug': self.org.slug, 'pk': self.member.pk}))
        self.assertContains(page, 'Session bundles')
        self.assertContains(page, '2 of 3 left')
        self.assertContains(page, 'Add bundle')


class PortalTests(BundleCase):
    def test_the_plan_page_shows_whats_left_and_buys_another(self):
        self.paid_bundle()
        booking.book_session(self.member, self.session)
        page = self.client.get(self.portal('portal_plans'))
        self.assertContains(page, '<div class="fs-4 fw-bold lh-1" id="sessions-left">2</div>', html=True)
        self.assertContains(page, '2 of 3 left')
        self.assertContains(page, 'Cancel by the time bookings close and you get the session back.')
        self.assertContains(page, 'Buy another bundle: £60.00 for 3 sessions')

    def test_buying_without_card_payments_adds_an_invoice_once(self):
        self.act(action='buy_bundle', policy_id=self.pack.pk)
        self.act(action='buy_bundle', policy_id=self.pack.pk)   # a second press doesn't double it
        bundle = SessionBundle.objects.get(member=self.member)
        self.assertEqual((bundle.status, bundle.invoice.amount), ('pending', Decimal('60.00')))
        page = self.client.get(self.portal('portal_plans'))
        self.assertContains(page, 'Waiting for payment')
        self.assertContains(page, 'Pay £60.00')

    def test_buying_online_goes_to_stripe_and_switches_plan(self):
        self.member.billing_policy = self.standard
        self.member.save()
        with mock.patch('billing.payments.stripe_keys', return_value=('pk', 'sk')), \
                mock.patch('billing.payments.start_checkout', return_value='https://stripe.test/cs') as checkout:
            response = self.act(action='buy_bundle', policy_id=self.pack.pk)
        self.assertEqual(response['Location'], 'https://stripe.test/cs')
        self.assertEqual(checkout.call_args.args[0], SessionBundle.objects.get().invoice)
        self.assertIn('?bought=1', checkout.call_args.kwargs['success_url'])
        self.member.refresh_from_db()
        self.assertEqual(self.member.billing_policy, self.pack)

    def test_the_book_page_shows_sessions_left(self):
        self.paid_bundle()
        page = self.client.get(self.portal('portal_bookings'))
        self.assertContains(page, '3 sessions left')
        self.assertContains(page, 'Uses 1 session')
        self.assertContains(page, 'book Juniors&#x27;s sessions one at a time')

        self.act(action='book', session_id=self.session.pk)
        self.act(action='book', session_id=self.later.pk)
        self.act(action='book', session_id=self.third.pk)
        page = self.client.get(self.portal('portal_bookings'))
        self.assertContains(page, '0 sessions left')
        self.assertContains(page, 'Buy another bundle')

    def test_booking_and_cancelling_say_what_happened_to_the_session(self):
        self.paid_bundle()
        page = self.act(action='book', session_id=self.session.pk)
        self.assertEqual(self.left(), 2)
        page = self.client.get(self.portal('portal_bookings'))
        self.assertContains(page, 'using 1 session from your bundle: 2 left')
        self.act(action='cancel_booking', session_id=self.session.pk)
        page = self.client.get(self.portal('portal_bookings'))
        self.assertContains(page, 'the session is back in your bundle')

    def test_the_portal_home_shows_sessions_left(self):
        self.paid_bundle()
        page = self.client.get(self.portal('member_portal'))
        self.assertContains(page, '3 sessions left in your bundle')
