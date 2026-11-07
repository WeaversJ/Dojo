"""
Cancelling an invoice (this fork's #16) alongside DojoUK's billing: a
cancelled invoice is kept for the record but is never paid, collected, sent,
chased or shown as owed, and any credit it used goes back to the member.
"""
from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from billing import autopay
from billing.credit import apply_member_credit, member_credit
from billing.models import Invoice, Payment
from billing.payments import pay_token
from members.models import Member
from organisations.features import FEATURES_BY_KEY
from organisations.models import Organisation, OrganisationMember


@override_settings(STRIPE_PUBLIC_KEY='', STRIPE_SECRET_KEY='', STRIPE_WEBHOOK_SECRET='')
class CancellationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organisation.objects.create(name='Test Club', slug='test-club',
                                              features={key: True for key in FEATURES_BY_KEY})
        cls.admin = User.objects.create_user('admin', password='pw')
        OrganisationMember.objects.create(user=cls.admin, organisation=cls.org, role='org_admin')
        cls.ann = Member.objects.create(organisation=cls.org, name='Ann Lee', email='ann@example.com')

    def setUp(self):
        self.client.force_login(self.admin)

    def url(self, name, **kwargs):
        return reverse(name, kwargs={'org_slug': self.org.slug, **kwargs})

    def invoice(self, amount='30.00', period='March 2027', **extra):
        return Invoice.objects.create(organisation=self.org, member=self.ann, amount=Decimal(amount), period=period,
                                      due_date=date(2027, 3, 31), **extra)

    def cancel(self, invoice, reason='Issued in error'):
        return self.client.post(self.url('invoice_cancel', pk=invoice.pk), {'reason': reason})

    def test_cancelling_keeps_the_record(self):
        invoice = self.invoice()
        self.cancel(invoice)
        invoice.refresh_from_db()
        self.assertEqual((invoice.status, invoice.cancelled_by, invoice.cancel_reason),
                         ('cancelled', self.admin, 'Issued in error'))
        self.assertIsNotNone(invoice.cancelled_at)
        self.assertEqual(invoice.balance_due, Decimal('0'))
        page = self.client.get(self.url('invoice_detail', pk=invoice.pk))
        self.assertContains(page, 'Cancelled')
        self.assertNotContains(page, 'Mark as paid')
        self.assertNotContains(page, 'Record payment')

    def test_an_invoice_with_payments_cannot_be_cancelled(self):
        invoice = self.invoice()
        Payment.objects.create(invoice=invoice, amount=Decimal('5'), paid_at=timezone.now())
        self.cancel(invoice)
        self.assertEqual(Invoice.objects.get(pk=invoice.pk).status, 'unpaid')

    def test_a_cancelled_invoice_is_not_paid_sent_or_chased(self):
        invoice = self.invoice()
        self.cancel(invoice)
        self.client.post(self.url('invoice_mark_paid', pk=invoice.pk))
        self.client.post(self.url('invoice_record_payment', pk=invoice.pk), {'amount': '30.00', 'method': 'cash'})
        with mock.patch('billing.emails.send_invoice_email') as send, \
                mock.patch('billing.emails.send_reminder_email') as remind:
            self.client.post(self.url('invoice_send_email', pk=invoice.pk))
            self.client.post(self.url('invoice_send_reminder', pk=invoice.pk))
        send.assert_not_called()
        remind.assert_not_called()
        invoice.refresh_from_db()
        self.assertEqual((invoice.status, invoice.payments.count()), ('cancelled', 0))

    def test_the_pay_page_and_portal_show_nothing_to_pay(self):
        invoice = self.invoice()
        self.cancel(invoice)
        pay_page = reverse('invoice_pay', kwargs={'token': pay_token(invoice)})
        self.assertContains(self.client.get(pay_page), "has cancelled this invoice, so there's nothing to pay")
        with mock.patch('stripe.checkout.Session.create') as checkout:
            self.client.post(pay_page)
        checkout.assert_not_called()
        portal = self.client.get(reverse('member_portal', kwargs={'token': self.ann.token}))
        self.assertEqual(portal.context['outstanding'], [])
        payments_page = self.client.get(reverse('portal_payments', kwargs={'token': self.ann.token}))
        self.assertEqual(payments_page.context['outstanding'], [])

    def test_automatic_collection_is_called_off(self):
        invoice = self.invoice(autopay_status='due')
        self.cancel(invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.autopay_status, '')
        with mock.patch('billing.autopay.collect') as collect:
            autopay.collect_due()
        collect.assert_not_called()

    def test_not_while_a_payment_is_being_collected(self):
        invoice = self.invoice(autopay_status='processing')
        self.cancel(invoice)
        self.assertEqual(Invoice.objects.get(pk=invoice.pk).status, 'unpaid')

    def test_credit_it_used_goes_back_to_the_member(self):
        overpaid = self.invoice(amount='20.00', period='January 2027', status='paid')
        Payment.objects.create(invoice=overpaid, amount=Decimal('30.00'), paid_at=timezone.now())
        later = self.invoice(amount='25.00')
        apply_member_credit(later)
        later.refresh_from_db()
        self.assertEqual((later.amount, member_credit(self.ann)), (Decimal('15.00'), Decimal('0')))
        self.cancel(later)
        later.refresh_from_db()
        self.assertEqual(member_credit(self.ann), Decimal('10.00'))
        self.assertIn('£10.00 credit given back', later.notes)
        # The next invoice takes it again.
        nxt = self.invoice(amount='25.00', period='April 2027')
        apply_member_credit(nxt)
        self.assertEqual(Invoice.objects.get(pk=nxt.pk).amount, Decimal('15.00'))

    def test_credit_noted_by_the_forks_earlier_wording_goes_back_too(self):
        overpaid = self.invoice(amount='20.00', period='January 2027', status='paid', overpayment_credited=Decimal('4'))
        Payment.objects.create(invoice=overpaid, amount=Decimal('30.00'), paid_at=timezone.now())
        later = self.invoice(amount='21.00', notes='Includes £4.00 credit applied from a previous overpayment.')
        self.cancel(later)
        self.assertEqual(member_credit(self.ann), Decimal('10.00'))

    def test_cancelling_needs_manage_billing(self):
        coach = User.objects.create_user('coach')
        om = OrganisationMember.objects.create(user=coach, organisation=self.org, role='coach')
        invoice = self.invoice()
        self.client.force_login(coach)
        self.assertEqual(self.cancel(invoice).status_code, 403)
        om.can_manage_billing = True
        om.save()
        self.cancel(invoice)
        self.assertEqual(Invoice.objects.get(pk=invoice.pk).status, 'cancelled')

    def test_a_cancelled_invoice_can_be_issued_again_in_bulk(self):
        self.ann.monthly_fee = Decimal('30.00')
        self.ann.save()
        invoice = self.invoice(period='February 2027')
        self.cancel(invoice)
        self.client.post(self.url('invoice_bulk'), {
            'period': 'February 2027', 'due_date': '2027-02-28', 'member_ids': [self.ann.pk],
        })
        self.assertEqual(Invoice.objects.filter(member=self.ann, period='February 2027', status='unpaid').count(), 1)

    def test_weeks_to_generate_can_be_typed(self):
        from classes.models import Class, ClassCoach
        cls = Class.objects.create(organisation=self.org, name='Juniors', schedule=[{'day': 0, 'time': '17:00'}])
        ClassCoach.objects.create(assigned_class=cls, user=self.admin)
        page = self.client.get(self.url('coach_class_detail', pk=cls.pk)).content.decode()
        self.assertRegex(page, r'<input type="text" name="weeks" value="6" inputmode="numeric"')
        self.client.post(self.url('class_generate_sessions', pk=cls.pk), {'weeks': '3'})
        self.assertEqual(cls.sessions.count(), 3)
