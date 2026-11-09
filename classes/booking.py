"""
Member-facing class bookings: enrolling on a whole class (so they're on every
register), booking a single session, and cancelling either. Each action checks
the rules, makes the change and emails the member a confirmation. The portal
views are thin wrappers around these functions.
"""
from datetime import datetime, time, timedelta

from django.db import transaction
from django.utils import timezone

from mailer import blocks, compose

from .models import Class, ClassMember, Session, SessionAbsence, SessionBooking, WaitingList


class BookingError(Exception):
    """A booking action that isn't allowed; the message is shown to the member."""


def session_start(session):
    """When a session starts: its date at the class's scheduled time for that weekday (midnight if none)."""
    start = time(0, 0)
    for entry in session.assigned_class.schedule or []:
        if entry.get('day') == session.date.weekday() and entry.get('time'):
            try:
                hours, minutes = entry['time'].split(':')[:2]
                start = time(int(hours), int(minutes))
            except (ValueError, TypeError):
                pass
            break
    return timezone.make_aware(datetime.combine(session.date, start))


def session_time_text(session):
    """The class's scheduled time for this session's weekday, e.g. '18:00–19:00' ('' if none)."""
    for entry in session.assigned_class.schedule or []:
        if entry.get('day') == session.date.weekday() and entry.get('time'):
            return f"{entry['time']}–{entry['end']}" if entry.get('end') else entry['time']
    return ''


def changes_closed(session, now=None):
    """True once members can no longer book or cancel this session themselves."""
    now = now or timezone.now()
    cutoff = timedelta(hours=session.assigned_class.organisation.booking_cutoff_hours)
    return now >= session_start(session) - cutoff


def bookable_sessions(cls, now=None):
    """Upcoming, uncancelled sessions of a class that still allow changes, soonest first."""
    today = timezone.localdate()
    sessions = (
        Session.objects.filter(assigned_class=cls, is_cancelled=False, date__gte=today)
        .select_related('assigned_class__organisation')
        .order_by('date')
    )
    return [s for s in sessions if not changes_closed(s, now)]


def session_headcount(session):
    """People expected at a session: everyone auto-enrolled, plus single-session bookings from anyone else."""
    auto = set(ClassMember.objects.filter(assigned_class=session.assigned_class, auto_enrol=True)
               .values_list('member_id', flat=True))
    auto -= set(SessionAbsence.objects.filter(session=session).values_list('member_id', flat=True))
    booked = set(SessionBooking.objects.filter(session=session).values_list('member_id', flat=True))
    return len(auto | booked)


def session_is_full(session):
    cls = session.assigned_class
    return cls.has_capacity_limit and session_headcount(session) >= cls.max_capacity


def _notify(member, key, values, box=None):
    """Email the member (or their guardian) a confirmation. A failed email never undoes the booking."""
    from members.emails import _recipient
    recipient = _recipient(member)
    if not recipient:
        return None
    try:
        compose.send(
            member.organisation, key, [recipient],
            {**compose.member_values(member), **values},
            {'booking_box': blocks.booking_box(*box)} if box else None,
        )
    except Exception:
        return None
    return recipient


def _date_text(day):
    return f'{day:%A} {day.day} {day:%B %Y}'


def _require_active(cls, member):
    if cls.organisation_id != member.organisation_id:
        raise BookingError('That class isn\'t available.')
    if not cls.is_active:
        raise BookingError(f'{cls.name} isn\'t open for booking.')
    reason = cls.ineligible_reason(member)
    if reason:
        raise BookingError(f'You can\'t book {cls.name}. {reason}')


def enrol(member, cls):
    """Enrol a member on every register for a class. Emails a confirmation."""
    _require_active(cls, member)
    with transaction.atomic():
        existing = ClassMember.objects.select_for_update().filter(assigned_class=cls, member=member).first()
        if existing and existing.auto_enrol:
            raise BookingError(f'You\'re already enrolled in {cls.name}.')
        if existing is None and cls.is_full:
            raise BookingError(f'{cls.name} is full, so it can\'t take new members right now.')
        if existing:
            existing.auto_enrol = True
            existing.save(update_fields=['auto_enrol'])
        else:
            ClassMember.objects.create(assigned_class=cls, member=member, auto_enrol=True)
        WaitingList.objects.filter(assigned_class=cls, member=member).delete()
        # A whole-class enrolment makes single-session bookings for this class redundant.
        SessionBooking.objects.filter(session__assigned_class=cls, member=member).delete()
        SessionAbsence.objects.filter(session__assigned_class=cls, member=member).delete()
    schedule = cls.schedule_display()
    _notify(member, 'class_enrolled', {'class_name': cls.name, 'class_schedule': schedule}, (cls.name, schedule))


def cancel_enrolment(member, cls):
    """Take a member off a class. Emails a confirmation and offers the place to the waiting list."""
    with transaction.atomic():
        removed, _ = ClassMember.objects.filter(assigned_class=cls, member=member).delete()
        if not removed:
            raise BookingError(f'You\'re not enrolled in {cls.name}.')
        SessionAbsence.objects.filter(session__assigned_class=cls, member=member).delete()
        promoted = promote_from_waiting_list(cls)
    schedule = cls.schedule_display()
    _notify(member, 'class_enrolment_cancelled', {'class_name': cls.name, 'class_schedule': schedule},
            (cls.name, schedule))
    return promoted


def promote_from_waiting_list(cls):
    """Move the first person on a class's waiting list into the class (if there's space). Returns the member."""
    if cls.is_full:
        return None
    next_up = WaitingList.objects.filter(assigned_class=cls).select_related('member').first()
    if next_up is None:
        return None
    ClassMember.objects.get_or_create(assigned_class=cls, member=next_up.member)
    member = next_up.member
    next_up.delete()
    _notify(member, 'waiting_list_place', {'class_name': cls.name})
    return member


def book_session(member, session):
    """Book a member onto one session. Emails a confirmation."""
    cls = session.assigned_class
    _require_active(cls, member)
    if session.is_cancelled:
        raise BookingError('That session has been cancelled.')
    if changes_closed(session):
        raise BookingError('Booking for that session has closed.')
    with transaction.atomic():
        if ClassMember.objects.filter(assigned_class=cls, member=member, auto_enrol=True).exists():
            raise BookingError(f'You\'re enrolled in {cls.name}, so you\'re already on its registers.')
        if SessionBooking.objects.filter(session=session, member=member).exists():
            raise BookingError('You\'re already booked onto that session.')
        if session_is_full(session):
            raise BookingError('That session is full.')
        SessionBooking.objects.create(session=session, member=member)
    day = _date_text(session.date)
    _notify(member, 'session_booked', {'class_name': cls.name, 'session_date': day}, (cls.name, day))


def cancel_session_booking(member, session):
    """Cancel a single-session booking. Emails a confirmation."""
    if changes_closed(session):
        raise BookingError('That session is too close to cancel online: please contact the club.')
    deleted, _ = SessionBooking.objects.filter(session=session, member=member).delete()
    if not deleted:
        raise BookingError('You\'re not booked onto that session.')
    cls = session.assigned_class
    day = _date_text(session.date)
    _notify(member, 'session_booking_cancelled', {'class_name': cls.name, 'session_date': day}, (cls.name, day))


def _enrolled_session(member, session):
    if not ClassMember.objects.filter(assigned_class=session.assigned_class, member=member, auto_enrol=True).exists():
        raise BookingError('You\'re not enrolled in that class.')


def skip_session(member, session):
    """An enrolled member tells the club they can't make one session. Emails a confirmation."""
    _enrolled_session(member, session)
    if session.is_cancelled:
        raise BookingError('That session has been cancelled.')
    if changes_closed(session):
        raise BookingError('That session is too close to cancel online: please contact the club.')
    _, created = SessionAbsence.objects.get_or_create(session=session, member=member)
    if not created:
        raise BookingError('You\'ve already cancelled that session.')
    cls = session.assigned_class
    day = _date_text(session.date)
    _notify(member, 'session_booking_cancelled', {'class_name': cls.name, 'session_date': day}, (cls.name, day))


def rejoin_session(member, session):
    """Undo skip_session, if the session is still open and has space. Emails a confirmation."""
    _enrolled_session(member, session)
    if changes_closed(session):
        raise BookingError('Booking for that session has closed.')
    if not SessionAbsence.objects.filter(session=session, member=member).exists():
        raise BookingError('You\'re already down to attend that session.')
    if session_is_full(session):
        raise BookingError('That session is full.')
    SessionAbsence.objects.filter(session=session, member=member).delete()
    cls = session.assigned_class
    day = _date_text(session.date)
    _notify(member, 'session_booked', {'class_name': cls.name, 'session_date': day}, (cls.name, day))
