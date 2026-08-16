"""
Bulk-import attendance registers from a CSV export.

CSV columns (all required): class_name, date, member_name, present

Matching rules:
  - class_name must match an existing Class (import Classes.csv first).
  - If no Session exists for that class + date, one is created automatically
    (marked is_extra=True) so attendance taken on a date outside the official
    schedule isn't silently dropped — this is flagged in the warnings.
  - member_name is matched against existing Members by exact name first. If
    that fails and the name is exactly two words, "Surname Firstname" is also
    tried (some rows in real exports come in reversed) — also flagged in the
    warnings so it can be sanity-checked. Names that still don't match are
    skipped (this command never creates new members).
  - Re-running is safe: attendance is upserted per (session, member), so a
    row just updates the present/absent flag rather than duplicating.

Run with:
    python manage.py import_registers_csv Registers.csv --org <org-slug> --dry-run
    python manage.py import_registers_csv Registers.csv --org <org-slug>
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
    help = 'Bulk-import attendance registers (class, date, member, present) from a CSV export.'

    def add_arguments(self, parser):
        parser.add_argument('registers_csv', nargs='?', default='Registers.csv',
                             help='Path to the registers CSV (default: Registers.csv in the project root)')
        parser.add_argument('--org', dest='org_slug',
                             help='Organisation slug to import into (required if more than one organisation exists)')
        parser.add_argument('--dry-run', action='store_true',
                             help='Parse and validate only — nothing is written to the database')

    def handle(self, *args, **options):
        from organisations.models import Organisation
        from classes.models import Class, Session, Attendance
        from members.models import Member

        path = options['registers_csv']
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
            missing = [c for c in ('class_name', 'date', 'member_name') if c not in reader.fieldnames]
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
        members_by_name = {m.name.strip().lower(): m for m in Member.objects.filter(organisation=org)}

        warnings = []
        sessions_created = 0
        marked, updated, skipped = 0, 0, 0
        session_cache = {}  # (class_pk, date) -> Session

        with transaction.atomic():
            for i, row in enumerate(rows, start=2):
                class_name = (row.get('class_name') or row.get('class') or '').strip()
                member_name = (row.get('member_name') or row.get('member') or '').strip()
                if not class_name or not member_name:
                    warnings.append(f'Row {i}: missing class_name or member_name, skipped.')
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

                member = members_by_name.get(member_name.lower())
                if not member:
                    parts = member_name.split()
                    if len(parts) == 2:
                        reversed_name = f'{parts[1]} {parts[0]}'
                        member = members_by_name.get(reversed_name.lower())
                        if member:
                            warnings.append(f'Row {i}: matched "{member_name}" to existing member "{member.name}" via reversed name order — check this is correct.')
                if not member:
                    warnings.append(f'Row {i}: no member named "{member_name}" — skipped.')
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
                    marked += 1
                    continue

                attendance, created = Attendance.objects.update_or_create(
                    session=session, member=member,
                    defaults={'present': present},
                )
                if created:
                    marked += 1
                else:
                    updated += 1

            if dry_run:
                transaction.set_rollback(True)

        summary = f'{"Would record" if dry_run else "Recorded"} {marked} attendance row{"s" if marked != 1 else ""}'
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
