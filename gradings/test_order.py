"""The Gradings list runs in date order: upcoming soonest first, then past most recent first, then unscheduled."""
from datetime import date, timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from organisations.models import Organisation, OrganisationMember
from organisations.test_features import ALL_ON

from .models import Grading, GradingSession
from .views import in_date_order


class GradingOrderTests(TestCase):
    def setUp(self):
        self.org = Organisation.objects.create(name='Test Club', slug='test-club', features=dict(ALL_ON))
        admin = User.objects.create_user('admin', password='pw')
        OrganisationMember.objects.create(user=admin, organisation=self.org, role='org_admin')
        self.client.force_login(admin)
        self.today = date.today()

        def grading(name, *days, cancelled=()):
            g = Grading.objects.create(organisation=self.org, name=name)
            for d in days:
                GradingSession.objects.create(grading=g, date=self.today + timedelta(days=d))
            for d in cancelled:
                GradingSession.objects.create(grading=g, date=self.today + timedelta(days=d), is_cancelled=True)
            return g
        grading('Autumn', -60)
        grading('Adults', 30, -100)              # next session in a month
        grading('Spring', -10)                   # most recent past
        grading('Juniors', 7, 60)                # next session in a week
        grading('Black belt', cancelled=[3])     # only a cancelled session
        grading('Today', 0)

    def test_upcoming_soonest_first_then_past_most_recent_first_then_none(self):
        expected = ['Today', 'Juniors', 'Adults', 'Spring', 'Autumn', 'Black belt']
        self.assertEqual([g.name for g in in_date_order(Grading.objects.all(), self.today)], expected)
        page = self.client.get(reverse('grading_list', kwargs={'org_slug': self.org.slug})).content.decode()
        self.assertEqual(sorted(expected, key=lambda n: page.index(f'>{n}</a>')), expected)
        self.assertIn('Next session', page)
        self.assertIn('No sessions yet', page)
        self.assertIn('Last held', page)


class SessionOrderTests(TestCase):
    def setUp(self):
        self.org = Organisation.objects.create(name='Test Club', slug='test-club', features=dict(ALL_ON))
        admin = User.objects.create_user('admin', password='pw')
        OrganisationMember.objects.create(user=admin, organisation=self.org, role='org_admin')
        self.client.force_login(admin)
        self.grading = Grading.objects.create(organisation=self.org, name='Winter grading')
        day = date(2026, 12, 5)
        for name, offset in (('Seniors', 1), ('kyu grades', 0), ('Adults', 1), ('Juniors', 0), ('Dan grades', 7)):
            GradingSession.objects.create(grading=self.grading, name=name, date=day + timedelta(days=offset))

    def page(self):
        return self.client.get(reverse('grading_detail', kwargs={'org_slug': self.org.slug, 'pk': self.grading.pk})).content.decode()

    def test_sessions_run_oldest_to_newest_then_a_to_z(self):
        expected = ['Juniors', 'kyu grades', 'Adults', 'Seniors', 'Dan grades']
        page = self.page()
        self.assertEqual(sorted(expected, key=page.index), expected)

    def test_with_many_sessions_the_15_most_recent_still_read_oldest_first(self):
        for i in range(15):
            GradingSession.objects.create(grading=self.grading, name=f'Extra {i:02d}', date=date(2026, 1, 1) + timedelta(days=i))
        sessions = self.client.get(reverse('grading_detail', kwargs={'org_slug': self.org.slug, 'pk': self.grading.pk})).context['sessions']
        self.assertEqual(len(sessions), 15)
        self.assertEqual(sessions[-1].name, 'Dan grades')
        self.assertEqual([s.date for s in sessions], sorted(s.date for s in sessions))
        self.assertNotIn('Extra 00', [s.name for s in sessions])
