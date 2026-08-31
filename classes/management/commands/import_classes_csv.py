"""
Bulk-import classes and their sessions from CSV exports.

Classes CSV columns (name required): name, description, schedule, max_capacity
  - schedule is a free-text string like "Tue 18:00-18:45" or "Tue 18:00-18:45, Thu 19:00-20:00"
    — parsed into the Class.schedule JSON format ([{"day": 1, "time": "18:00", "end": "18:45"}, ...]).
  - Existing classes (matched by organisation + name) are updated in place, so this is safe to re-run
    if you tweak the source CSV.

Sessions CSV columns (class_name and date required): class_name, date, is_cancelled
  - class_name is matched against the "name" column in the classes CSV / existing classes.
  - Existing sessions (matched by class + date) are left alone unless --update-sessions is passed.

Run with:
    python manage.py import_classes_csv Classes.csv --sessions Sessions.csv --org <org-slug> --dry-run
    python manage.py import_classes_csv Classes.csv --sessions Sessions.csv --org <org-slug>
"""
import csv
import re
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

DAY_ALIASES = {
    'mon': 0, 'monday': 0,
    'tue': 1, 'tues': 1, 'tuesday': 1,
    'wed': 2, 'weds': 2, 'wednesday': 2,
    'thu': 3, 'thur': 3, 'thurs': 3, 'thursday': 3,
    'fri': 4, 'friday': 4,
    'sat': 5, 'saturday': 5,
    'sun': 6, 'sunday': 6,
}

SCHEDULE_ENTRY_RE = re.compile(
    r'^\s*([A-Za-z]+)\s+(\d{1,2}[:.]\d{2})\s*(?:-\s*(\d{1,2}[:.]\d{2}))?\s*$'
)

DATE_FORMATS = ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%d-%b-%Y', '%d %b %Y')


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


def parse_schedule(raw):
    """Turn 'Tue 18:00-18:45, Thu 19:00-20:00' into Class.schedule JSON. Returns (schedule, warnings)."""
    raw = (raw or '').strip()
    if not raw:
        return [], []
    schedule, warnings = [], []
    for chunk in re.split(r'[;,]', raw):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = SCHEDULE_ENTRY_RE.match(chunk)
        if not m:
            warnings.append(f'could not parse schedule entry "{chunk}"')
            continue
        day_raw, start, end = m.groups()
        day = DAY_ALIASES.get(day_raw.strip().lower())
        if day is None:
            warnings.append(f'unrecognised day "{day_raw}" in schedule entry "{chunk}"')
            continue
        entry = {'day': day, 'time': start.replace('.', ':')}
        if end:
            entry['end'] = end.replace('.', ':')
        schedule.append(entry)
    return schedule, warnings


class Command(BaseCommand):
    help = 'Bulk-import classes (with parsed schedule) and their sessions from CSV exports.'

    def add_arguments(self, parser):
        parser.add_argument('classes_csv', nargs='?', default='Classes.csv',
                             help='Path to the classes CSV (default: Classes.csv in the project root)')
        parser.add_argument('--sessions', dest='sessions_csv', default='Sessions.csv',
                             help='Path to the sessions CSV (default: Sessions.csv in the project root). Pass "" to skip sessions.')
        parser.add_argument('--org', dest='org_slug',
                             help='Organisation slug to import into (required if more than one organisation exists)')
        parser.add_argument('--dry-run', action='store_true',
                             help='Parse and validate only — nothing is written to the database')
        parser.add_argument('--update-sessions', action='store_true',
                             help='If a session for a class+date already exists, update its is_cancelled flag instead of leaving it alone')

    def handle(self, *args, **options):
        from organisations.models import Organisation
        from classes.models import Class, Session

        classes_path = options['classes_csv']
        sessions_path = options['sessions_csv']
        dry_run = options['dry_run']

        classes_rows = self._read_csv(classes_path, required=['name'])

        sessions_rows = []
        if sessions_path:
            try:
                sessions_rows = self._read_csv(sessions_path, required=['class_name', 'date'])
            except CommandError as e:
                self.stdout.write(self.style.WARNING(f'Skipping sessions: {e}'))

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

        warnings = []
        classes_created, classes_updated = 0, 0
        class_lookup = {}  # lower(name) -> Class instance

        with transaction.atomic():
            for i, row in enumerate(classes_rows, start=2):
                name = (row.get('name') or '').strip()
                if not name:
                    warnings.append(f'Classes row {i}: missing name, skipped.')
                    continue

                schedule, sched_warnings = parse_schedule(row.get('schedule'))
                for w in sched_warnings:
                    warnings.append(f'Classes row {i} ({name}): {w}')

                max_capacity_raw = (row.get('max_capacity') or '').strip()
                max_capacity = None
                if max_capacity_raw:
                    try:
                        max_capacity = int(max_capacity_raw)
                    except ValueError:
                        warnings.append(f'Classes row {i} ({name}): invalid max_capacity "{max_capacity_raw}" — left blank.')

                defaults = dict(
                    description=(row.get('description') or '').strip(),
                    schedule=schedule,
                    max_capacity=max_capacity,
                )

                existing = Class.objects.filter(organisation=org, name=name).first()
                if existing:
                    if not dry_run:
                        for field, value in defaults.items():
                            setattr(existing, field, value)
                        existing.save()
                    classes_updated += 1
                    class_lookup[name.lower()] = existing
                else:
                    if not dry_run:
                        obj = Class.objects.create(organisation=org, name=name, **defaults)
                    else:
                        obj = Class(organisation=org, name=name, **defaults)
                    classes_created += 1
                    class_lookup[name.lower()] = obj

            sessions_created, sessions_updated, sessions_skipped = 0, 0, 0
            for i, row in enumerate(sessions_rows, start=2):
                class_name = (row.get('class_name') or row.get('class') or '').strip()
                if not class_name:
                    warnings.append(f'Sessions row {i}: missing class_name, skipped.')
                    continue

                cls = class_lookup.get(class_name.lower())
                if not cls:
                    cls = Class.objects.filter(organisation=org, name=class_name).first()
                if not cls:
                    warnings.append(f'Sessions row {i}: no class named "{class_name}" — session skipped.')
                    sessions_skipped += 1
                    continue

                sess_date, date_err = parse_date(row.get('date'))
                if date_err or not sess_date:
                    warnings.append(f'Sessions row {i} ({class_name}): {date_err or "missing date"} — session skipped.')
                    sessions_skipped += 1
                    continue

                is_cancelled_raw = (row.get('is_cancelled') or '').strip().lower()
                is_cancelled = is_cancelled_raw in ('yes', 'true', '1')

                if dry_run:
                    exists = Session.objects.filter(assigned_class=cls, date=sess_date).exists() if cls.pk else False
                    if exists:
                        sessions_updated += 1
                    else:
                        sessions_created += 1
                    continue

                session, created = Session.objects.get_or_create(
                    assigned_class=cls, date=sess_date,
                    defaults={'is_cancelled': is_cancelled},
                )
                if created:
                    sessions_created += 1
                elif options['update_sessions'] and session.is_cancelled != is_cancelled:
                    session.is_cancelled = is_cancelled
                    session.save(update_fields=['is_cancelled'])
                    sessions_updated += 1

            if dry_run:
                transaction.set_rollback(True)

        self.stdout.write(self.style.SUCCESS(
            f'{"Would create" if dry_run else "Created"} {classes_created} class{"es" if classes_created != 1 else ""}, '
            f'{"would update" if dry_run else "updated"} {classes_updated} existing class{"es" if classes_updated != 1 else ""}.'
        ))
        if sessions_rows:
            self.stdout.write(self.style.SUCCESS(
                f'{"Would create" if dry_run else "Created"} {sessions_created} session{"s" if sessions_created != 1 else ""}'
                + (f', updated {sessions_updated}' if sessions_updated else '')
                + (f', skipped {sessions_skipped}' if sessions_skipped else '')
                + '.'
            ))

        if warnings:
            self.stdout.write(self.style.WARNING(f'{len(warnings)} warning(s):'))
            for w in warnings:
                self.stdout.write(f'  - {w}')

    def _read_csv(self, path, required):
        try:
            fh = open(path, encoding='utf-8-sig')
        except OSError as e:
            raise CommandError(f'Could not open {path}: {e}')
        with fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames:
                raise CommandError(f'Could not parse headers in {path}.')
            reader.fieldnames = [h.strip().lower().replace(' ', '_') for h in reader.fieldnames]
            missing = [c for c in required if c not in reader.fieldnames]
            if missing:
                raise CommandError(f'{path} must have column(s): {", ".join(missing)}')
            return list(reader)
