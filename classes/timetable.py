"""
The public timetable: a club's active classes and their upcoming sessions, for people who aren't
members yet. It's shown on the club's Dojo finder page and in the widget a club embeds on its own
website (classes/embed_views.py), once the club switches it on under Settings → Website.

It carries no member data: class names, times, who each class is for, cancellations and (if the
club chooses) how many places are left.

Sessions come from two places:
- Sessions already generated for the registers, which can be cancelled or added as extras.
- The class's weekly schedule, for dates after the last generated session, so the timetable
  doesn't go blank when a club generates its registers a few weeks at a time. Dates inside the
  generated range with no session are taken as deliberately left out and aren't filled in.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.db.models import Max
from django.utils import timezone

from .models import Class, ClassMember, Session, SessionAbsence, SessionBooking

MAX_WEEKS = 4            # how many weeks a single view can show
WEEKS_AHEAD = 8          # how far ahead people can page


@dataclass
class Entry:
    cls: Class
    date: date
    time: str            # '18:00', or '' if the class has no time set for that day
    end: str             # '19:00', or ''
    cancelled: bool = False
    extra: bool = False
    places_left: int | None = None   # None: not shown, or the class has no capacity

    @property
    def time_text(self):
        return f'{self.time}–{self.end}' if self.time and self.end else self.time

    @property
    def full(self):
        return self.places_left == 0

    @property
    def who(self):
        """Who the class is for, e.g. 'Ages 7 to 11 · Orange belt and above', or ''."""
        return self.cls.restriction_text()


@dataclass
class Day:
    date: date
    entries: list


def _schedule_entry(cls, day):
    """The class's schedule entry for that weekday ({'day', 'time', 'end'}), or None."""
    for entry in cls.schedule or []:
        if entry.get('day') == day.weekday():
            return entry
    return None


def _parse_time(text):
    try:
        hours, minutes = str(text).split(':')[:2]
        return time(int(hours), int(minutes))
    except (ValueError, TypeError):
        return None


def public_classes(org):
    """The classes on the public timetable: those switched on for booking (Class.is_active)."""
    return list(
        Class.objects.filter(organisation=org, is_active=True)
        .select_related('min_stage', 'max_stage').order_by('name')
    )


def clamp_start(requested, today=None):
    """
    A start date people asked for (or None for today), kept between today and the last page
    WEEKS_AHEAD weeks on. Pages are rolling: today and the six days after it, then the seven after
    that, so the first page is always a full week however late in the week it is.
    """
    today = today or timezone.localdate()
    if requested is None:
        return today
    return min(max(requested, today), today + timedelta(weeks=WEEKS_AHEAD - 1))


def build(org, start, weeks=1, only_class=None, show_places=None, now=None):
    """
    The timetable from `start` for `weeks` weeks, as a list of Days that have something on.

    Days before today, and sessions today that have already started, are left out. `only_class`
    (a Class id) narrows it to one class. Places left are shown if the club has chosen to
    (show_places=None) or if asked (True/False).
    """
    now = timezone.localtime(now) if now else timezone.localtime()
    today = now.date()
    weeks = max(1, min(int(weeks or 1), MAX_WEEKS))
    end = start + timedelta(days=7 * weeks - 1)
    first = max(start, today)
    if show_places is None:
        show_places = org.timetable_shows_places

    classes = public_classes(org)
    if only_class is not None:
        classes = [c for c in classes if c.pk == only_class]
    if not classes or first > end:
        return []

    sessions = {
        (s.assigned_class_id, s.date): s
        for s in Session.objects.filter(assigned_class__in=classes, date__gte=first, date__lte=end)
    }
    last_generated = dict(
        Session.objects.filter(assigned_class__in=classes).values('assigned_class')
        .annotate(last=Max('date')).values_list('assigned_class', 'last')
    )

    # Headcounts for places left, in a few queries rather than a few per session.
    capped = {c.pk for c in classes if show_places and c.has_capacity_limit}
    auto, absent, booked = {}, {}, {}
    if capped:
        for class_id, member_id in ClassMember.objects.filter(
                assigned_class_id__in=capped, auto_enrol=True).values_list('assigned_class_id', 'member_id'):
            auto.setdefault(class_id, set()).add(member_id)
        session_ids = [s.pk for s in sessions.values() if s.assigned_class_id in capped]
        for session_id, member_id in SessionAbsence.objects.filter(
                session_id__in=session_ids).values_list('session_id', 'member_id'):
            absent.setdefault(session_id, set()).add(member_id)
        for session_id, member_id in SessionBooking.objects.filter(
                session_id__in=session_ids).values_list('session_id', 'member_id'):
            booked.setdefault(session_id, set()).add(member_id)

    def places(cls, session):
        if cls.pk not in capped:
            return None
        expected = set(auto.get(cls.pk, ()))
        if session is not None:
            expected = (expected - absent.get(session.pk, set())) | booked.get(session.pk, set())
        return max(0, cls.max_capacity - len(expected))

    days = []
    day = first
    while day <= end:
        entries = []
        for cls in classes:
            session = sessions.get((cls.pk, day))
            slot = _schedule_entry(cls, day)
            if session is None:
                last = last_generated.get(cls.pk)
                if slot is None or (last is not None and day <= last):
                    continue
            entry = Entry(
                cls=cls, date=day,
                time=(slot or {}).get('time', '') or '', end=(slot or {}).get('end', '') or '',
                cancelled=bool(session and session.is_cancelled), extra=bool(session and session.is_extra),
            )
            if day == today:
                starts = _parse_time(entry.time)
                if starts is not None and starts <= now.time():
                    continue
            if not entry.cancelled:
                entry.places_left = places(cls, session)
            entries.append(entry)
        if entries:
            entries.sort(key=lambda e: (e.time == '', e.time, e.cls.name))
            days.append(Day(date=day, entries=entries))
        day += timedelta(days=1)
    return days


def parse_date(text):
    try:
        return datetime.strptime(text or '', '%Y-%m-%d').date()
    except ValueError:
        return None
