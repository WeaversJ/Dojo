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
