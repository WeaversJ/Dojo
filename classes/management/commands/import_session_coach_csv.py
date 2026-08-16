"""
Bulk-import coach attendance (SessionCoach) from a CSV export.

CSV columns (all required): class_name, date, coach_name, present

Matching rules:
  - class_name must match an existing Class (import Classes.csv first).
  - If no Session exists for that class + date, one is created automatically
    (marked is_extra=True) — same behaviour as import_registers_csv, so a
    coach-only register taken on a date outside the official schedule isn't
    dropped.
  - coach_name is matched against this organisation's OrganisationMembers by
    full name (first + last name), falling back to username. Coaches who
    aren't already set up as an org member/user are skipped with a warning —
    this command never creates user accounts.
  - Re-running is safe: SessionCoach rows are upserted per (session, coach).

Run with:
    python manage.py import_session_coach_csv CoachAttendance.csv --org <org-slug> --dry-run
    python manage.py import_session_coach_csv CoachAttendance.csv --org <org-slug>
"""
import csv
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

DATE_FORMATS = ('%d/%m/%Y', '%Y-%m-%d', '%d-%m-%Y', '%d-%b-%Y', '%d %b %Y')


def parse_date(raw):
    raw = (raw or '').strip()
    if not raw:
        return None, None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date(), None
        except ValueError:
            continue
    return None, f'unrecognised date format "{raw}"'


class Command(BaseCommand):
    help = 'Bulk-import coach attendance (class, date, coach, present) into SessionCoach from a CSV export.'

    def add_arguments(self, parser):
        parser.add_argument('csv_path', nargs='?', default='CoachAttendance.csv',
                             help='Path to the coach-attendance CSV (default: CoachAttendance.csv in the project root)')
        parser.add_argument('--org', dest='org_slug',
                             help='Organisation slug to import into (required if more than one organisation exists)')
        parser.add_argument('--dry-run', action='store_true',
                             help='Parse and validate only — nothing is written to the database')

    def handle(self, *args, **options):
        from organisations.models import Organisation, OrganisationMember
        from classes.models import Class, Session, SessionCoach

        path = options['csv_path']
        dry_run = options['dry_run']

        try:
            fh = open(path, encoding='utf-8-sig')
        except OSError as e:
            raise CommandError(f'Could not open {path}: {e}')
        with fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames:
                raise CommandError(f'Could not parse headers in {path}.')
            reader.fieldnames = [h.strip().lower().replace(' ', '_') for h in reader.fieldnames]
            missing = [c for c in ('class_name', 'date', 'coach_name') if c not in reader.fieldnames]
            if missing:
                raise CommandError(f'{path} must have column(s): {", ".join(missing)}')
            rows = list(reader)

        # Resolve organisation
        if options['org_slug']:
            try:
                org = Organisation.objects.get(slug=options['org_slug'])
            except Organisation.DoesNotExist:
                raise CommandError(f'No organisation with slug "{options["org_slug"]}".')
        else:
            orgs = list(Organisation.objects.all())
            if len(orgs) == 1:
                org = orgs[0]
            elif not orgs:
                raise CommandError('No organisations exist yet — set one up first.')
            else:
                options_list = ', '.join(o.slug for o in orgs)
                raise CommandError(f'Multiple organisations exist — specify one with --org <slug>. Options: {options_list}')

        self.stdout.write(f'Importing into organisation: {org.name} ({org.slug}){" [DRY RUN]" if dry_run else ""}')

        classes_by_name = {c.name.strip().lower(): c for c in Class.objects.filter(organisation=org)}

        coaches_by_name = {}
        coaches_by_username = {}
        for m in OrganisationMember.objects.filter(organisation=org).select_related('user'):
            full_name = (m.user.get_full_name() or '').strip().lower()
            if full_name:
                coaches_by_name[full_name] = m.user
            coaches_by_username[m.user.username.strip().lower()] = m.user

        warnings = []
        sessions_created = 0
        recorded, updated, skipped = 0, 0, 0
        session_cache = {}  # (class_pk, date) -> Session

        with transaction.atomic():
            for i, row in enumerate(rows, start=2):
                class_name = (row.get('class_name') or row.get('class') or '').strip()
                coach_name = (row.get('coach_name') or row.get('coach') or '').strip()
                if not class_name or not coach_name:
                    warnings.append(f'Row {i}: missing class_name or coach_name, skipped.')
                    skipped += 1
                    continue

                cls = classes_by_name.get(class_name.lower())
                if not cls:
                    warnings.append(f'Row {i}: no class named "{class_name}" — skipped.')
                    skipped += 1
                    continue

                sess_date, date_err = parse_date(row.get('date'))
                if date_err or not sess_date:
                    warnings.append(f'Row {i} ({class_name}): {date_err or "missing date"} — skipped.')
                    skipped += 1
                    continue

                coach = coaches_by_name.get(coach_name.lower()) or coaches_by_username.get(coach_name.lower())
                if not coach:
                    warnings.append(f'Row {i}: no coach/org member named "{coach_name}" — skipped (set them up as a coach in Staff first if this is expected).')
                    skipped += 1
                    continue

                present_raw = (row.get('present') or '').strip().lower()
                present = present_raw in ('yes', 'true', '1', 'present')

                cache_key = (cls.pk, sess_date)
                session = session_cache.get(cache_key)
                if session is None:
                    session = Session.objects.filter(assigned_class=cls, date=sess_date).first()
                    if not session:
                        if not dry_run:
                            session = Session.objects.create(assigned_class=cls, date=sess_date, is_extra=True)
                        warnings.append(f'Row {i}: no session existed for "{cls.name}" on {sess_date.isoformat()} — created one automatically (marked as an extra session).')
                        sessions_created += 1
                    session_cache[cache_key] = session

                if dry_run:
                    recorded += 1
                    continue

                sc, created = SessionCoach.objects.update_or_create(
                    session=session, coach=coach,
                    defaults={'present': present},
                )
                if created:
                    recorded += 1
                else:
                    updated += 1

            if dry_run:
                transaction.set_rollback(True)

        summary = f'{"Would record" if dry_run else "Recorded"} {recorded} coach attendance row{"s" if recorded != 1 else ""}'
        if updated:
            summary += f', updated {updated} existing'
        if skipped:
            summary += f', skipped {skipped}'
        if sessions_created:
            summary += f' ({sessions_created} session{"s" if sessions_created != 1 else ""} auto-created)'
        self.stdout.write(self.style.SUCCESS(summary + '.'))

        if warnings:
            self.stdout.write(self.style.WARNING(f'{len(warnings)} warning(s):'))
            for w in warnings:
                self.stdout.write(f'  - {w}')
