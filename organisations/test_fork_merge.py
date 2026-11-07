"""
What this fork keeps now that DojoUK main is merged in: the access every
coach had, helpers on registers, portal sign-in by email and monthly Stripe
subscriptions, alongside DojoUK's own versions of the same areas.
"""
import importlib
import json
from datetime import date, timedelta

from django.apps import apps
from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from billing.models import BillingPolicy, Invoice
from billing.test_payments import WEBHOOK_SECRET, signed
from classes.models import Class, ClassCoach, ClassHelper, ClassMember, Session, SessionHelper
from members.models import Member
from organisations.features import FEATURES_BY_KEY
from organisations.models import Organisation, OrganisationMember
from organisations.optional_fields import OPTIONAL_FIELDS_BY_KEY

FORK_COACH_ACCESS = (
    'manage_billing', 'view_all_attendance', 'manage_gradings',
    'generate_sessions', 'manage_class_members', 'view_stock',
)


class ForkTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organisation.objects.create(
            name='Test Club', slug='test-club',
            features={key: True for key in FEATURES_BY_KEY},
            optional_fields={key: True for key in OPTIONAL_FIELDS_BY_KEY},
        )
        cls.admin = User.objects.create_user('admin', password='pw')
        OrganisationMember.objects.create(user=cls.admin, organisation=cls.org, role='org_admin')

    def url(self, name, **kwargs):
        return reverse(name, kwargs={'org_slug': self.org.slug, **kwargs})


class CoachAccessTests(ForkTestCase):
    def test_existing_coaches_keep_what_the_fork_let_every_coach_do(self):
        coach = OrganisationMember.objects.create(
            user=User.objects.create_user('coach'), organisation=self.org, role='coach',
        )
        admin = OrganisationMember.objects.get(user=self.admin)
        migration = importlib.import_module('organisations.migrations.0016_coaches_keep_fork_access')
        migration.grant_existing_coaches(apps, None)
        coach.refresh_from_db()
        admin.refresh_from_db()
        for name in FORK_COACH_ACCESS:
            self.assertTrue(getattr(coach, f'can_{name}'), name)
            self.assertFalse(getattr(admin, f'can_{name}'), name)  # admins hold everything anyway
        self.assertFalse(coach.can_message_all_members)
        self.assertFalse(coach.can_take_payments)

    def test_a_coach_added_on_the_staff_page_starts_with_the_same_access(self):
        self.client.force_login(self.admin)
        self.client.post(self.url('org_staff'), {
            'action': 'add', 'username': 'newcoach', 'password': 'a-long-password', 'role': 'coach',
        })
        coach = OrganisationMember.objects.get(user__username='newcoach')
        self.assertEqual(sorted(coach.granted_capabilities()), sorted(
            (name, label) for name, label, _ in OrganisationMember.CAPABILITIES if name in FORK_COACH_ACCESS
        ))

    def test_finance_and_expenses_stay_admin_only(self):
        coach = OrganisationMember.objects.create(
            user=User.objects.create_user('coach'), organisation=self.org, role='coach',
            **{f'can_{name}': True for name in FORK_COACH_ACCESS},
        )
        self.client.force_login(coach.user)
        self.assertEqual(self.client.get(self.url('invoice_list')).status_code, 200)
        self.assertEqual(self.client.get(self.url('org_finance')).status_code, 403)
        self.assertEqual(self.client.get(self.url('expense_list')).status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(self.url('org_finance')).status_code, 200)
        self.assertEqual(self.client.get(self.url('expense_list')).status_code, 200)


class HelperRegisterTests(ForkTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.coach = User.objects.create_user('coach')
        OrganisationMember.objects.create(user=cls.coach, organisation=cls.org, role='coach')
        cls.cls = Class.objects.create(organisation=cls.org, name='Juniors')
        ClassCoach.objects.create(assigned_class=cls.cls, user=cls.coach)
        cls.member = Member.objects.create(organisation=cls.org, name='Mia Member')
        ClassMember.objects.create(assigned_class=cls.cls, member=cls.member)
        cls.helper = Member.objects.create(
            organisation=cls.org, name='Hal Helper', emergency_contact_name='Hal Mum', emergency_contact_phone='07700 900555',
        )
        ClassHelper.objects.create(assigned_class=cls.cls, member=cls.helper)
        cls.session = Session.objects.create(assigned_class=cls.cls, date=date.today() - timedelta(days=1))

    def register_url(self):
        return self.url('session_register', pk=self.cls.pk, session_pk=self.session.pk)

    def test_helpers_are_marked_on_the_register(self):
        self.client.force_login(self.admin)
        page = self.client.get(self.register_url())
        self.assertContains(page, 'Hal Helper')
        self.assertContains(page, 'Hal Mum')  # their emergency contact, as for members
        self.client.post(self.register_url(), {
            'present': [self.member.pk], 'coach_present': [self.coach.pk], 'helper_present': [self.helper.pk],
        })
        self.assertTrue(SessionHelper.objects.get(session=self.session, helper=self.helper).present)
        self.assertRegex(self.client.get(self.register_url()).content.decode(),
                         rf'name="helper_present" value="{self.helper.pk}"[^>]*checked')

    def test_a_refused_save_keeps_the_helper_ticks(self):
        self.client.force_login(self.admin)
        response = self.client.post(self.register_url(), {'helper_present': [self.helper.pk]})  # no coach ticked
        self.assertContains(response, 'At least one coach must be marked present')
        self.assertRegex(response.content.decode(), rf'name="helper_present" value="{self.helper.pk}"[^>]*checked')
        self.assertFalse(SessionHelper.objects.exists())

    def test_the_register_form_check_is_on_the_register_not_the_logout_form(self):
        self.client.force_login(self.admin)
        html = self.client.get(self.register_url()).content.decode()
        self.assertIn('<form method="post" id="register-form">', html)
        self.assertIn("document.getElementById('register-form').addEventListener('submit'", html)
        self.assertNotIn("document.querySelector('form').addEventListener", html)


class PortalSignInTests(ForkTestCase):
    def test_a_member_gets_their_link_by_email(self):
        member = Member.objects.create(organisation=self.org, name='Pat Portal', email='pat@example.com')
        response = self.client.post(self.url('portal_login_request'), {'email': 'PAT@example.com'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['pat@example.com'])
        self.assertEqual(mail.outbox[0].subject, 'Your Test Club member portal link')
        self.assertIn(reverse('member_portal', kwargs={'token': member.token}), mail.outbox[0].body)

    def test_an_unknown_address_gets_the_same_page_and_no_email(self):
        Member.objects.create(organisation=self.org, name='Pat Portal', email='pat@example.com')
        known = self.client.post(self.url('portal_login_request'), {'email': 'pat@example.com'}).content
        mail.outbox.clear()
        unknown = self.client.post(self.url('portal_login_request'), {'email': 'nobody@example.com'}).content
        self.assertEqual(mail.outbox, [])
        self.assertEqual(known, unknown)


@override_settings(STRIPE_PUBLIC_KEY='pk_test_x', STRIPE_SECRET_KEY='sk_test_x', STRIPE_WEBHOOK_SECRET=WEBHOOK_SECRET)
class SubscriptionWebhookTests(ForkTestCase):
    """Monthly subscriptions started from the portal (DojoUK removed these; this fork keeps them)."""

    def post(self, event_type, obj):
        payload = json.dumps({'id': 'evt_1', 'object': 'event', 'type': event_type, 'data': {'object': obj}})
        return self.client.post(reverse('stripe_webhook'), payload, content_type='application/json',
                                HTTP_STRIPE_SIGNATURE=signed(payload, WEBHOOK_SECRET))

    def test_subscription_lifecycle(self):
        member = Member.objects.create(organisation=self.org, name='Sam Sub', monthly_fee=30)
        self.post('checkout.session.completed', {
            'id': 'cs_1', 'object': 'checkout.session', 'mode': 'subscription', 'subscription': 'sub_1',
            'metadata': {'member_pk': str(member.pk)},
        })
        member.refresh_from_db()
        self.assertEqual((member.stripe_subscription_id, member.subscription_status), ('sub_1', 'active'))

        self.post('invoice.payment_succeeded', {
            'id': 'in_1', 'object': 'invoice', 'subscription': 'sub_1', 'amount_paid': 3000,
            'billing_reason': 'subscription_cycle', 'period_end': 1793491200,
        })
        invoice = Invoice.objects.get(member=member)
        self.assertEqual((invoice.status, invoice.amount, invoice.payments.count()), ('paid', 30, 1))

        self.post('customer.subscription.deleted', {'id': 'sub_1', 'object': 'subscription', 'status': 'canceled'})
        member.refresh_from_db()
        self.assertEqual((member.stripe_subscription_id, member.subscription_status), ('', 'cancelled'))


class BillingPolicyFormTests(ForkTestCase):
    def test_a_bad_amount_is_an_error_not_a_crash(self):
        self.client.force_login(self.admin)
        response = self.client.post(self.url('billing_policy_create'), {
            'name': 'Monthly', 'billing_cycle': 'monthly', 'amount': '-5',
        })
        self.assertRedirects(response, self.url('billing_policies'), fetch_redirect_response=False)
        self.assertFalse(BillingPolicy.objects.exists())
