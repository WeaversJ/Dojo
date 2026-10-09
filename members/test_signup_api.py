import json

from django.core.cache import cache
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse

from documents.models import WaiverTemplate
from members.models import MemberApplication
from organisations.models import Organisation

SITE = 'https://www.club.example'


@override_settings(SIGNUP_API_ORIGINS=[SITE], SIGNUP_API_RATE_LIMIT=3)
class SignupApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.org = Organisation.objects.create(name='Club', slug='club')
        self.url = reverse('member_signup_api', kwargs={'org_slug': 'club'})

    def post(self, data, origin=SITE, **extra):
        headers = {'HTTP_ORIGIN': origin} if origin else {}
        return self.client.post(self.url, json.dumps(data), content_type='application/json', **headers, **extra)

    def test_get_lists_active_waivers(self):
        WaiverTemplate.objects.create(organisation=self.org, name='Consent', file=ContentFile(b'%PDF', name='c.pdf'))
        WaiverTemplate.objects.create(organisation=self.org, name='Old', is_active=False, file=ContentFile(b'%PDF', name='o.pdf'))
        resp = self.client.get(self.url, HTTP_ORIGIN=SITE)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body['organisation'], 'Club')
        self.assertEqual([w['name'] for w in body['waivers']], ['Consent'])
        self.assertTrue(body['waivers'][0]['required'])
        self.assertTrue(body['waivers'][0]['url'].startswith('http'))
        self.assertEqual(resp['Access-Control-Allow-Origin'], SITE)

    def test_post_json_creates_application(self):
        resp = self.post({'name': 'Sam Smith', 'email': 'sam@example.com', 'date_of_birth': '2015-04-01',
                          'guardian_name': 'Pat Smith', 'medical_info': 'Asthma'})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp['Access-Control-Allow-Origin'], SITE)
        app = MemberApplication.objects.get()
        self.assertEqual((app.organisation, app.name, app.guardian_name), (self.org, 'Sam Smith', 'Pat Smith'))
        self.assertEqual(str(app.date_of_birth), '2015-04-01')
        self.assertEqual(app.status, MemberApplication.Status.PENDING)

    def test_post_form_encoded_creates_application(self):
        resp = self.client.post(self.url, {'name': 'Sam'}, HTTP_ORIGIN=SITE)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(MemberApplication.objects.count(), 1)

    def test_missing_name_is_rejected(self):
        resp = self.post({'email': 'sam@example.com'})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('Full name is required.', resp.json()['errors'])
        self.assertFalse(MemberApplication.objects.exists())

    def test_invalid_email_and_overlong_field_are_rejected(self):
        resp = self.post({'name': 'Sam', 'email': 'not-an-email', 'phone': '0' * 50})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(len(resp.json()['errors']), 2)
        self.assertFalse(MemberApplication.objects.exists())

    def test_required_waiver_needs_signature(self):
        WaiverTemplate.objects.create(organisation=self.org, name='Consent', file=ContentFile(b'%PDF', name='c.pdf'))
        self.assertEqual(self.post({'name': 'Sam'}).status_code, 400)
        self.assertEqual(self.post({'name': 'Sam', 'signature_data': 'data:image/png;base64,AAAA'}).status_code, 201)

    def test_other_origin_is_refused(self):
        resp = self.post({'name': 'Sam'}, origin='https://evil.example')
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(MemberApplication.objects.exists())

    def test_request_without_origin_is_allowed(self):
        resp = self.post({'name': 'Sam'}, origin=None)
        self.assertEqual(resp.status_code, 201)
        self.assertNotIn('Access-Control-Allow-Origin', resp)

    def test_preflight(self):
        resp = self.client.options(self.url, HTTP_ORIGIN=SITE, HTTP_ACCESS_CONTROL_REQUEST_METHOD='POST')
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(resp['Access-Control-Allow-Origin'], SITE)
        self.assertIn('POST', resp['Access-Control-Allow-Methods'])

    def test_honeypot_pretends_success_but_saves_nothing(self):
        resp = self.post({'name': 'Bot', 'website': 'http://spam.example'})
        self.assertEqual(resp.status_code, 201)
        self.assertFalse(MemberApplication.objects.exists())

    def test_rate_limited_per_ip(self):
        for _ in range(3):
            self.assertEqual(self.post({'email': 'x@example.com'}).status_code, 400)
        resp = self.post({'name': 'Sam'})
        self.assertEqual(resp.status_code, 429)
        self.assertFalse(MemberApplication.objects.exists())

    def test_invalid_json(self):
        resp = self.client.post(self.url, '{nope', content_type='application/json', HTTP_ORIGIN=SITE)
        self.assertEqual(resp.status_code, 400)

    def test_unknown_org(self):
        url = reverse('member_signup_api', kwargs={'org_slug': 'nope'})
        resp = self.client.post(url, json.dumps({'name': 'Sam'}), content_type='application/json', HTTP_ORIGIN=SITE)
        self.assertEqual(resp.status_code, 404)


class SignupPageTests(TestCase):
    """The HTML /join/ page shares create_application with the API."""

    def setUp(self):
        self.org = Organisation.objects.create(name='Club', slug='club')
        self.url = reverse('member_signup', kwargs={'org_slug': 'club'})

    def test_page_still_creates_application(self):
        resp = self.client.post(self.url, {'name': 'Sam', 'email': 'sam@example.com'})
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, 'members/signup_done.html')
        self.assertEqual(MemberApplication.objects.get().name, 'Sam')

    def test_page_shows_errors(self):
        resp = self.client.post(self.url, {'name': '', 'email': 'bad'})
        self.assertTemplateUsed(resp, 'members/signup.html')
        self.assertContains(resp, 'Full name is required.')
        self.assertFalse(MemberApplication.objects.exists())
