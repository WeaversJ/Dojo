from django.contrib.auth.models import User
from django.core import mail
from django.test import SimpleTestCase, TestCase, override_settings

from dojo.email import org_sender
from members.emails import send_welcome_email
from members.models import Member
from organisations.models import Organisation


@override_settings(DEFAULT_FROM_EMAIL='Dojo <noreply@club.example>')
class OrgSenderTests(SimpleTestCase):
    def test_sends_from_instance_address_under_org_name(self):
        org = Organisation(name='Example Sports Club', email='contact@example.com')
        self.assertEqual(org_sender(org), {
            'from_email': 'Example Sports Club <noreply@club.example>',
            'reply_to': ['contact@example.com'],
        })

    def test_no_reply_to_without_org_email(self):
        org = Organisation(name='Example Sports Club', email='')
        self.assertIsNone(org_sender(org)['reply_to'])

    def test_quotes_org_name_with_special_characters(self):
        org = Organisation(name='Example Sports Club, Springfield', email='')
        self.assertEqual(
            org_sender(org)['from_email'],
            '"Example Sports Club, Springfield" <noreply@club.example>',
        )

    def test_invalid_org_email_drops_reply_to(self):
        org = Organisation(name='Example Sports Club', email='contact@example.com\nBcc: x@evil.example')
        self.assertIsNone(org_sender(org)['reply_to'])

    def test_line_breaks_in_org_name_are_flattened(self):
        org = Organisation(name='Example Sports\r\nClub', email='')
        self.assertEqual(org_sender(org)['from_email'], 'Example Sports Club <noreply@club.example>')

    def test_bare_default_from_address(self):
        org = Organisation(name='Example Sports Club', email='')
        with self.settings(DEFAULT_FROM_EMAIL='noreply@club.example'):
            self.assertEqual(org_sender(org)['from_email'], 'Example Sports Club <noreply@club.example>')


@override_settings(
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    DEFAULT_FROM_EMAIL='Dojo <noreply@club.example>',
)
class OrgEmailHeadersTests(TestCase):
    def test_member_email_never_sent_from_org_inbox(self):
        org = Organisation.objects.create(name='Example Sports Club', slug='example', email='contact@example.com')
        member = Member.objects.create(organisation=org, name='Sam', email='sam@example.com')

        send_welcome_email(member)

        message = mail.outbox[0].message()
        self.assertEqual(message['From'], 'Example Sports Club <noreply@club.example>')
        self.assertEqual(message['Reply-To'], 'contact@example.com')


class OrgSettingsEmailValidationTests(TestCase):
    def setUp(self):
        self.org = Organisation.objects.create(name='Example Sports Club', slug='example', email='contact@example.com')
        self.client.force_login(User.objects.create_superuser('boss', 'boss@example.com', 'x'))

    def post_settings(self, email):
        return self.client.post(
            f'/org/{self.org.slug}/settings/', {'name': 'Example Sports Club', 'email': email}, secure=True,
        )

    def test_rejects_invalid_email(self):
        self.post_settings('not-an-email')
        self.org.refresh_from_db()
        self.assertEqual(self.org.email, 'contact@example.com')

    def test_saves_valid_email(self):
        self.post_settings('secretary@example.com')
        self.org.refresh_from_db()
        self.assertEqual(self.org.email, 'secretary@example.com')

    def test_allows_clearing_email(self):
        self.post_settings('')
        self.org.refresh_from_db()
        self.assertEqual(self.org.email, '')
