"""The public timetable: the website widget, the club's finder page and "Try a class"."""
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from billing.models import BillingPolicy, PolicyDiscount
from classes import timetable
from classes.embed_views import parse_domains
from classes.models import Class, ClassMember, Session, SessionAbsence, SessionBooking
from members.models import Member, MemberApplication
from network.models import MemberAccount
from organisations.features import FEATURES_BY_KEY
from organisations.models import Organisation, OrganisationMember

ALL_ON = {key: True for key in FEATURES_BY_KEY}
EVERY_DAY_AT_SIX = [{'day': d, 'time': '18:00', 'end': '19:00'} for d in range(7)]


def at(day, hour, minute=0):
    return timezone.make_aware(datetime.combine(day, time(hour, minute)))


class TimetableTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organisation.objects.create(
            name='Riverside Judo', slug='riverside', features=dict(ALL_ON),
            settings={'public_timetable': True, 'embed_domains': ['https://www.riversidejudo.co.uk']},
            address_line1='1 River St', city='Cardiff', postcode='CF10 1AA',
            latitude=Decimal('51.48'), longitude=Decimal('-3.18'), disciplines=['judo'],
        )
        cls.juniors = Class.objects.create(organisation=cls.org, name='Juniors', is_active=True,
                                           schedule=EVERY_DAY_AT_SIX, min_age=7, max_age=11)
        cls.private = Class.objects.create(organisation=cls.org, name='Squad only', is_active=False,
                                           schedule=EVERY_DAY_AT_SIX)
        cls.member = Member.objects.create(organisation=cls.org, name='Alice Able', email='alice@example.com')
        cls.today = timezone.localdate()

    def build(self, **kwargs):
        kwargs.setdefault('now', at(self.today, 0, 1))
        return timetable.build(self.org, self.today, **kwargs)

    def entries(self, days):
        return [(e.date, e.cls.name) for day in days for e in day.entries]


class TimetableDataTests(TimetableTestBase):
    def test_active_classes_are_projected_from_their_schedule_from_today(self):
        days = self.build(weeks=2)
        self.assertEqual([d.date for d in days], [self.today + timedelta(days=n) for n in range(14)])
        self.assertNotIn('Squad only', {name for _, name in self.entries(days)})
        entry = days[0].entries[0]
        self.assertEqual((entry.time_text, entry.who, entry.cancelled), ('18:00–19:00', 'Ages 7 to 11', False))

    def test_generated_sessions_win_and_gaps_in_them_are_left_alone(self):
        tomorrow = self.today + timedelta(days=1)
        Session.objects.create(assigned_class=self.juniors, date=tomorrow, is_cancelled=True)
        # The club generated registers up to three days on and deliberately left out the day after tomorrow.
        Session.objects.create(assigned_class=self.juniors, date=self.today + timedelta(days=3))
        dates = {e.date: e for day in self.build(weeks=2) for e in day.entries}
        self.assertTrue(dates[tomorrow].cancelled)
        self.assertNotIn(self.today + timedelta(days=2), dates)
        self.assertIn(self.today + timedelta(days=3), dates)
        self.assertIn(self.today + timedelta(days=4), dates)   # after the last generated session: from the schedule
        self.assertNotIn(self.today, dates)                     # before it, only generated sessions count

    def test_sessions_that_have_started_today_are_left_out(self):
        days = timetable.build(self.org, self.today, weeks=1, now=at(self.today, 18, 30))
        self.assertNotIn(self.today, [d.date for d in days])

    def test_places_left_only_when_the_club_chooses_and_the_class_has_a_capacity(self):
        self.juniors.max_capacity = 3
        self.juniors.save()
        self.assertIsNone(self.build()[0].entries[0].places_left)

        other = Member.objects.create(organisation=self.org, name='Bob Best')
        third = Member.objects.create(organisation=self.org, name='Cat Cole')
        ClassMember.objects.create(assigned_class=self.juniors, member=self.member, auto_enrol=True)
        ClassMember.objects.create(assigned_class=self.juniors, member=other, auto_enrol=True)
        session = Session.objects.create(assigned_class=self.juniors, date=self.today + timedelta(days=1))
        SessionAbsence.objects.create(session=session, member=other)
        SessionBooking.objects.create(session=session, member=third)
        SessionBooking.objects.create(session=session, member=Member.objects.create(organisation=self.org, name='Dan'))
        days = self.build(show_places=True, weeks=2)
        places = {e.date: e.places_left for day in days for e in day.entries}
        self.assertEqual(places[self.today + timedelta(days=1)], 0)   # Alice + two bookings, Bob can't make it
        self.assertEqual(places[self.today + timedelta(days=2)], 1)   # only the two enrolled
        self.assertTrue([e for day in days for e in day.entries if e.date == self.today + timedelta(days=1)][0].full)

    def test_one_class_only(self):
        Class.objects.create(organisation=self.org, name='Adults', is_active=True, schedule=EVERY_DAY_AT_SIX)
        names = {name for _, name in self.entries(self.build(only_class=self.juniors.pk))}
        self.assertEqual(names, {'Juniors'})

    def test_start_dates_are_kept_within_the_weeks_people_can_page_through(self):
        self.assertEqual(timetable.clamp_start(None, self.today), self.today)
        self.assertEqual(timetable.clamp_start(self.today - timedelta(weeks=5), self.today), self.today)
        far = timetable.clamp_start(self.today + timedelta(weeks=52), self.today)
        self.assertEqual(far, self.today + timedelta(weeks=timetable.WEEKS_AHEAD - 1))


class EmbedViewTests(TimetableTestBase):
    def url(self, **params):
        base = reverse('embed_timetable', kwargs={'org_slug': self.org.slug})
        return base + ('?' + '&'.join(f'{k}={v}' for k, v in params.items()) if params else '')

    def test_hidden_until_the_club_makes_its_timetable_public(self):
        self.org.settings = {**self.org.settings, 'public_timetable': False}
        self.org.save()
        self.assertEqual(self.client.get(self.url()).status_code, 404)

    def test_only_the_clubs_websites_and_dojo_can_frame_it(self):
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Security-Policy'], "frame-ancestors 'self' https://www.riversidejudo.co.uk")
        self.assertNotIn('X-Frame-Options', response)
        self.assertIn('max-age', response['Cache-Control'])

    def test_shows_the_classes_and_no_member_details(self):
        ClassMember.objects.create(assigned_class=self.juniors, member=self.member, auto_enrol=True)
        response = self.client.get(self.url())
        self.assertContains(response, 'Juniors')
        self.assertContains(response, 'Ages 7 to 11')
        self.assertNotContains(response, 'Squad only')
        self.assertNotContains(response, 'Alice')
        self.assertNotContains(response, 'alice@example.com')

    def test_try_a_class_goes_to_the_join_form_with_the_class_and_intro_offer(self):
        plan = BillingPolicy.objects.create(organisation=self.org, name='Monthly', billing_cycle='monthly', amount=30)
        PolicyDiscount.objects.create(policy=plan, name='Welcome', discount_type='percentage', value=Decimal('100'),
                                      code='WELCOME', show_publicly=True, duration_months=1)
        response = self.client.get(self.url())
        join = reverse('member_signup', kwargs={'org_slug': self.org.slug})
        self.assertContains(response, f'href="{join}?source=widget&amp;code=WELCOME&amp;class={self.juniors.pk}"')
        self.assertContains(response, 'target="_blank"')
        self.assertContains(response, reverse('portal_login_request', kwargs={'org_slug': self.org.slug}))

    def test_no_try_buttons_when_the_club_takes_no_applications(self):
        self.org.set_features({**ALL_ON, 'applications': False})
        self.org.save()
        response = self.client.get(self.url())
        self.assertContains(response, 'Juniors')
        self.assertNotContains(response, 'Try a class')

    def test_paging_through_weeks(self):
        response = self.client.get(self.url())
        self.assertEqual(response.context['prev_url'], '')
        next_week = self.today + timedelta(weeks=1)
        self.assertEqual(response.context['next_url'], f'?start={next_week.isoformat()}')
        response = self.client.get(self.url(start=next_week.isoformat(), weeks=2, theme='dark'))
        self.assertEqual(response.context['days'][0].date, next_week)
        self.assertEqual(response.context['theme'], 'dark')
        self.assertIn('weeks=2', response.context['prev_url'])

    def test_a_class_from_another_club_is_ignored(self):
        other = Organisation.objects.create(name='Other', slug='other', features=dict(ALL_ON))
        theirs = Class.objects.create(organisation=other, name='Theirs', is_active=True, schedule=EVERY_DAY_AT_SIX)
        response = self.client.get(self.url(**{'class': theirs.pk}))
        self.assertContains(response, 'Juniors')
        self.assertNotContains(response, 'Theirs')

    def test_the_loader_script(self):
        response = self.client.get(reverse('embed_script'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response['Content-Type'].startswith('text/javascript'))
        self.assertContains(response, "dojoEmbed === 'height'")
        self.assertContains(response, '/embed/')


class DomainTests(TestCase):
    def test_addresses_are_reduced_to_origins(self):
        origins, errors = parse_domains('www.riversidejudo.co.uk/classes\nhttps://*.wixsite.com\n'
                                        'http://localhost:8000, HTTPS://Club.com https://club.com')
        self.assertEqual(origins, ['https://www.riversidejudo.co.uk', 'https://*.wixsite.com',
                                   'http://localhost:8000', 'https://club.com'])
        self.assertEqual(errors, [])

    def test_anything_that_could_bend_the_header_is_refused(self):
        origins, errors = parse_domains("ftp://club.com\nclub.com;script-src\n'self'\nhttps://club.com/ 'unsafe-inline'")
        self.assertEqual(origins, ['https://club.com'])
        self.assertEqual(len(errors), 4)

    def test_at_most_ten(self):
        origins, errors = parse_domains('\n'.join(f'site{n}.com' for n in range(12)))
        self.assertEqual(len(origins), 10)
        self.assertEqual(len(errors), 1)


class TryAClassTests(TimetableTestBase):
    def join(self):
        return reverse('member_signup', kwargs={'org_slug': self.org.slug})

    def test_the_join_form_says_which_class_and_records_it(self):
        response = self.client.get(f'{self.join()}?source=widget&class={self.juniors.pk}')
        self.assertContains(response, 'try <strong>Juniors</strong>')
        self.assertContains(response, f'name="trial_class" value="{self.juniors.pk}"')
        self.assertContains(response, 'name="source" value="widget"')
        self.client.post(self.join(), {'first_name': 'Ivy', 'surname': 'Ince', 'email': 'ivy@example.com',
                                       'trial_class': self.juniors.pk, 'source': 'widget'})
        application = MemberApplication.objects.get()
        self.assertEqual((application.trial_class, application.source), (self.juniors, 'widget'))

    def test_other_clubs_classes_hidden_classes_and_made_up_sources_are_ignored(self):
        other = Organisation.objects.create(name='Other', slug='other', features=dict(ALL_ON))
        theirs = Class.objects.create(organisation=other, name='Theirs', is_active=True)
        for class_id in (theirs.pk, self.private.pk, 'abc'):
            response = self.client.get(f'{self.join()}?source=spam&class={class_id}')
            self.assertNotContains(response, 'name="trial_class"')
            self.assertNotContains(response, 'name="source"')
        self.client.post(self.join(), {'first_name': 'Ivy', 'surname': 'Ince', 'trial_class': theirs.pk, 'source': 'spam'})
        application = MemberApplication.objects.get()
        self.assertEqual((application.trial_class, application.source), (None, ''))

    def test_staff_see_the_class_and_where_it_came_from(self):
        admin = User.objects.create_user('admin', password='pw')
        OrganisationMember.objects.create(user=admin, organisation=self.org, role='org_admin')
        MemberApplication.objects.create(organisation=self.org, name='Ivy Ince', trial_class=self.juniors,
                                         source=MemberApplication.Source.WIDGET)
        self.client.force_login(admin)
        response = self.client.get(reverse('application_list', kwargs={'org_slug': self.org.slug}))
        self.assertContains(response, 'Wants to try Juniors')
        self.assertContains(response, 'From your website timetable')


class WebsiteSettingsTests(TimetableTestBase):
    def setUp(self):
        self.admin = User.objects.create_user('admin', password='pw')
        OrganisationMember.objects.create(user=self.admin, organisation=self.org, role='org_admin')
        self.client.force_login(self.admin)
        self.url = reverse('org_settings', kwargs={'org_slug': self.org.slug})

    def test_saving_the_website_settings(self):
        response = self.client.post(self.url, {
            'action': 'website', 'public_timetable': '1', 'timetable_places': '1',
            'embed_domains': 'riversidejudo.co.uk\nwww.riversidejudo.co.uk/classes\nnot a site!',
        }, follow=True)
        self.org.refresh_from_db()
        self.assertTrue(self.org.public_timetable and self.org.timetable_shows_places)
        self.assertEqual(self.org.embed_domains, ['https://riversidejudo.co.uk', 'https://www.riversidejudo.co.uk'])
        self.assertContains(response, 'isn&#x27;t a website address')

    def test_switching_it_off(self):
        self.client.post(self.url, {'action': 'website', 'embed_domains': ''})
        self.org.refresh_from_db()
        self.assertFalse(self.org.public_timetable)
        self.assertEqual(self.org.embed_domains, [])

    def test_the_snippet_and_preview(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'data-club="riverside"')
        self.assertContains(response, reverse('embed_script'))
        self.assertContains(response, f'src="{reverse("embed_timetable", kwargs={"org_slug": "riverside"})}"')


class ClubPageTimetableTests(TimetableTestBase):
    def club_page(self):
        return self.client.get(reverse('network_club', kwargs={'slug': self.org.slug}))

    def test_the_club_page_shows_the_timetable_with_try_links(self):
        response = self.club_page()
        self.assertContains(response, 'id="timetable"')
        join = reverse('network_join', kwargs={'slug': self.org.slug})
        self.assertContains(response, f'href="{join}?source=finder&amp;class={self.juniors.pk}"')

    def test_not_shown_until_public(self):
        self.org.settings = {**self.org.settings, 'public_timetable': False}
        self.org.save()
        self.assertNotContains(self.club_page(), 'id="timetable"')

    def test_signing_in_to_join_keeps_the_class_and_offer(self):
        join = reverse('network_join', kwargs={'slug': self.org.slug})
        response = self.client.get(f'{join}?source=finder&class={self.juniors.pk}&code=WELCOME')
        self.assertEqual(response.status_code, 302)
        self.assertIn('next=%2Fclubs%2Friverside%2Fjoin%2F%3Fsource%3Dfinder%26class%3D', response['Location'])

        user = User.objects.create_user('sam@example.com', email='sam@example.com', password='pw')
        MemberAccount.objects.create(user=user)
        self.client.force_login(user)
        response = self.client.get(f'{join}?source=finder&class={self.juniors.pk}&code=WELCOME')
        signup = reverse('member_signup', kwargs={'org_slug': self.org.slug})
        self.assertRedirects(response, f'{signup}?class={self.juniors.pk}&source=finder&code=WELCOME',
                             fetch_redirect_response=False)


class BoundaryTests(TimetableTestBase):
    """The QA hard rules for the new routes: who's let in and refused, and no personal data out."""
    MEDICAL = 'SENTINEL-MEDICAL-4471'
    GUARDIAN_PHONE = '07700 900471'

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from members.models import Guardian
        child = Member.objects.create(organisation=cls.org, name='Kit Child', medical_info=cls.MEDICAL,
                                      email='kit@example.com')
        Guardian.objects.create(member=child, name='Pat Parent', phone=cls.GUARDIAN_PHONE)
        ClassMember.objects.create(assigned_class=cls.juniors, member=child, auto_enrol=True)
        Session.objects.create(assigned_class=cls.juniors, date=cls.today + timedelta(days=1),
                               notes='Kit Child collected early')
        cls.other = Organisation.objects.create(name='Other Club', slug='other-club', features=dict(ALL_ON))
        cls.admin = User.objects.create_user('admin', password='pw')
        OrganisationMember.objects.create(user=cls.admin, organisation=cls.org, role='org_admin')
        cls.coach = User.objects.create_user('coach', password='pw')
        OrganisationMember.objects.create(user=cls.coach, organisation=cls.org, role='coach')
        cls.other_admin = User.objects.create_user('other-admin', password='pw')
        OrganisationMember.objects.create(user=cls.other_admin, organisation=cls.other, role='org_admin')

    def test_no_personal_data_on_any_public_page(self):
        self.org.settings = {**self.org.settings, 'timetable_places': True}
        self.juniors.max_capacity = 10
        self.juniors.save()
        self.org.save()
        pages = [
            reverse('embed_timetable', kwargs={'org_slug': self.org.slug}),
            reverse('embed_timetable', kwargs={'org_slug': self.org.slug}) + '?weeks=4',
            reverse('network_club', kwargs={'slug': self.org.slug}),
            reverse('embed_script'),
        ]
        for url in pages:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            for secret in (self.MEDICAL, self.GUARDIAN_PHONE, 'Kit Child', 'Pat Parent', 'kit@example.com',
                           'collected early', 'Alice', 'alice@example.com'):
                self.assertNotContains(response, secret, msg_prefix=url)

    def test_the_widget_is_only_for_clubs_that_switched_it_on(self):
        self.assertEqual(self.client.get(reverse('embed_timetable', kwargs={'org_slug': self.org.slug})).status_code, 200)
        self.assertEqual(self.client.get(reverse('embed_timetable', kwargs={'org_slug': 'other-club'})).status_code, 404)
        self.assertEqual(self.client.get(reverse('embed_timetable', kwargs={'org_slug': 'no-such-club'})).status_code, 404)

    def test_only_the_clubs_admins_change_its_website_settings(self):
        url = reverse('org_settings', kwargs={'org_slug': self.org.slug})
        data = {'action': 'website', 'embed_domains': 'https://evil.example'}
        response = self.client.post(url, data)                          # anonymous
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('login'), response['Location'])
        for user in (self.coach, self.other_admin):
            self.client.force_login(user)
            self.assertIn(self.client.post(url, data).status_code, (403, 404))
        self.org.refresh_from_db()
        self.assertEqual(self.org.embed_domains, ['https://www.riversidejudo.co.uk'])
        self.client.force_login(self.admin)                              # control
        self.client.post(url, {**data, 'public_timetable': '1'})
        self.org.refresh_from_db()
        self.assertEqual(self.org.embed_domains, ['https://evil.example'])


class WeeklySummaryTests(TestCase):
    def test_days_with_the_same_times_are_grouped(self):
        summary = timetable.weekly_summary({
            0: [('17:30', '18:30'), ('19:00', '20:30')], 2: [('19:00', '20:30')], 3: [('17:30', '18:30'), ('19:00', '20:30')],
        })
        self.assertEqual(summary, 'Mon & Thu 17:30–18:30, 19:00–20:30 · Wed 19:00–20:30')

    def test_runs_of_days_and_long_lists(self):
        weekdays = {d: [('18:00', '19:00')] for d in range(5)}
        self.assertEqual(timetable.weekly_summary({**weekdays, 5: [('10:00', '11:00')]}),
                         'Mon–Fri 18:00–19:00 · Sat 10:00–11:00')
        busy = {1: [('09:00', ''), ('10:00', '11:00'), ('12:00', '13:00'), ('18:00', '19:00')]}
        self.assertEqual(timetable.weekly_summary(busy), 'Tue 09:00, 10:00–11:00, +2 more')
        self.assertEqual(timetable.weekly_summary({6: [('', '')]}), 'Sun')
        self.assertEqual(timetable.weekly_summary({}), '')

    def test_slots_come_from_active_classes_only_and_skip_bad_entries(self):
        org = Organisation.objects.create(name='Club', slug='club', features=dict(ALL_ON))
        Class.objects.create(organisation=org, name='On', is_active=True, schedule=[
            {'day': 3, 'time': '19:00', 'end': '20:00'}, {'day': 3, 'time': '17:00', 'end': '18:00'},
            {'day': 9, 'time': '10:00'}, {'day': 'Mon'}, 'junk'])
        Class.objects.create(organisation=org, name='Off', is_active=False, schedule=[{'day': 0, 'time': '10:00'}])
        self.assertEqual(timetable.weekly_slots([org.pk]), {org.pk: {3: [('17:00', '18:00'), ('19:00', '20:00')]}})


class FinderDayTests(TimetableTestBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.juniors.schedule = [{'day': 0, 'time': '17:30', 'end': '18:30'}, {'day': 3, 'time': '17:30', 'end': '18:30'}]
        cls.juniors.save()
        place = {'address_line1': '2 Road', 'city': 'Cardiff', 'postcode': 'CF10 2BB',
                 'latitude': Decimal('51.49'), 'longitude': Decimal('-3.17'), 'disciplines': ['judo']}
        cls.weekend = Organisation.objects.create(name='Weekend Karate', slug='weekend', features=dict(ALL_ON),
                                                  settings={'public_timetable': True}, **place)
        Class.objects.create(organisation=cls.weekend, name='Saturday', is_active=True,
                             schedule=[{'day': 5, 'time': '10:00', 'end': '11:30'}])
        cls.quiet = Organisation.objects.create(name='Quiet Club', slug='quiet', features=dict(ALL_ON), **place)
        Class.objects.create(organisation=cls.quiet, name='Monday', is_active=True,
                             schedule=[{'day': 0, 'time': '19:00', 'end': '20:00'}])

    def search(self, **params):
        data = self.client.get(reverse('network_search'), params).json()
        return {club['slug']: club for club in data['clubs']}

    def test_each_club_shows_its_usual_week_if_its_timetable_is_public(self):
        clubs = self.search()
        self.assertEqual(clubs['riverside']['classes'], 'Mon & Thu 17:30–18:30')
        self.assertEqual(clubs['riverside']['days'], [0, 3])
        self.assertEqual(clubs['weekend']['classes'], 'Sat 10:00–11:30')
        self.assertEqual((clubs['quiet']['classes'], clubs['quiet']['days']), ('', []))   # not public: nothing shown

    def test_filtering_by_day(self):
        self.assertEqual(set(self.search(days='0')), {'riverside'})
        self.assertEqual(set(self.search(days='5')), {'weekend'})
        self.assertEqual(set(self.search(days='0,5')), {'riverside', 'weekend'})
        self.assertEqual(set(self.search(days='2')), set())
        self.assertEqual(set(self.search()), {'riverside', 'weekend', 'quiet'})
        self.assertEqual(set(self.search(days='9,x,')), {'riverside', 'weekend', 'quiet'})   # nonsense is ignored

    def test_works_with_distance_and_discipline(self):
        clubs = self.search(days='0', lat='51.48', lng='-3.18', radius='5', discipline='judo')
        self.assertEqual(set(clubs), {'riverside'})
        self.assertIsNotNone(clubs['riverside']['miles'])

    def test_the_day_filter_shows_once_a_club_publishes_its_timetable(self):
        self.assertContains(self.client.get(reverse('network_finder')), 'id="n-days"')
        Organisation.objects.update(settings={})
        self.assertNotContains(self.client.get(reverse('network_finder')), 'id="n-days"')


class ClubPageLayoutTests(TimetableTestBase):
    def test_timetable_under_about_and_apply_to_join_between_the_map_and_reviews(self):
        from django.test import override_settings
        self.org.about = 'We are a friendly club.'
        self.org.save()
        with override_settings(GOOGLE_MAPS_API_KEY='test-key'):
            page = self.client.get(reverse('network_club', kwargs={'slug': self.org.slug})).content.decode()
        # Left column: About, then the timetable. Right column: the map, Apply to join, then the reviews.
        spots = [page.index(marker) for marker in ('id="about"', 'id="timetable"', 'id="n-club-map"', 'id="join"',
                                                    'Apply to join', 'id="reviews"')]
        self.assertEqual(spots, sorted(spots))
