"""Classes listed in the order they fall in the week (Monday to Sunday, then by time), not by name."""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from organisations.models import Organisation, OrganisationMember
from organisations.test_features import ALL_ON

from .models import Class, in_week_order


class WeekOrderTests(TestCase):
    def setUp(self):
        self.org = Organisation.objects.create(name='Test Club', slug='test-club', features=dict(ALL_ON))
        admin = User.objects.create_user('admin', password='pw')
        OrganisationMember.objects.create(user=admin, organisation=self.org, role='org_admin')
        self.client.force_login(admin)
        make = lambda name, schedule=(): Class.objects.create(organisation=self.org, name=name, schedule=list(schedule))
        self.open_mat = make('Adults open mat')                                     # no schedule: last
        self.sat = make('Beginners', [{'day': 5, 'time': '10:00'}])
        self.wed_late = make('Advanced', [{'day': 2, 'time': '19:30'}, {'day': 0, 'time': '20:00'}])   # Mon 20:00 first
        self.mon_early = make('Tots', [{'day': 0, 'time': '9:30'}])
        self.mon_evening = make('Juniors', [{'day': 0, 'time': '18:00'}, {'day': 3, 'time': '18:00'}])

    def test_classes_sort_by_their_first_day_and_time_in_the_week(self):
        expected = ['Tots', 'Juniors', 'Advanced', 'Beginners', 'Adults open mat']
        self.assertEqual([c.name for c in in_week_order(Class.objects.all())], expected)
        page = self.client.get(reverse('class_list', kwargs={'org_slug': self.org.slug})).content.decode()
        self.assertEqual(sorted(expected, key=page.index), expected)

    def test_a_classes_own_schedule_reads_monday_first(self):
        self.assertEqual(self.wed_late.schedule_display(), 'Monday 20:00, Wednesday 19:30')

    def test_same_time_falls_back_to_name(self):
        Class.objects.create(organisation=self.org, name='Adults', schedule=[{'day': 0, 'time': '18:00'}])
        names = [c.name for c in in_week_order(Class.objects.all())]
        self.assertEqual(names[:3], ['Tots', 'Adults', 'Juniors'])
