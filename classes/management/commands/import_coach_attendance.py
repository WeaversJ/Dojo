"""
Bulk-import SessionCoach attendance records from a CSV export.

Expects columns: session_id, coach_id, present (Yes/No/1/0, or blank)
or: class_name, session_date, coach_name, present

Run with:
    python manage.py import_coach_attendance coach_attendance.csv --org <org-slug> --dry-run
    python manage.py import_coach_attendance coach_attendance.csv --org <org-slug>
"""
import csv
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Bulk-import SessionCoach attendance records from a CSV export."

    def add_arguments(self, parser):
        parser.add_argument('csv_path', nargs='?', default='coach_attendance.csv',
                             help='Path to the CSV file (default: coach_attendance.csv)')
        parser.add_argument('--org', dest='org_slug',
                             help='Organisation slug to import into (required if more than one organisation exists)')
        parser.add_argument('--dry-run', action='store_true',
                             help='Parse and validate only — nothing is written to the database')

    def parse_present(self, raw):
        """Parse presence field: Yes/No/1/0 or blank."""
        raw = (raw or '').strip().lower()
        if raw in ('yes', '1', 'true'):
            return True
        elif raw in ('no', '0', 'false'):
            return False
        elif not raw:
            return None
        else:
            return None  # Invalid, will warn

    def handle(self, *args, **options):
        from organisations.models import Organisation
        from classes.models import Session, SessionCoach
        from django.contrib.auth.models import User

        csv_path = options['csv_path']
        dry_run = options['dry_run']

        try:
            fh = open(csv_path, encoding='utf-8-sig')
        except OSError as e:
            raise CommandError(f'Could not open {csv_path}: {e}')

        with fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames:
                raise CommandError('Could not parse CSV headers.')
            reader.fieldnames = [h.strip().lower().replace(' ', '_') for h in reader.fieldnames]
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

        self.stdout.write(f'Importing coach attendance into organisation: {org.name} ({org.slug}){" [DRY RUN]" if dry_run else ""}')

        # Detect CSV format: if session_id and coach_id present, use direct mode; else use lookup mode
        has_ids = 'session_id' in reader.fieldnames and 'coach_id' in reader.fieldnames
        has_lookup = 'class_name' in reader.fieldnames and 'session_date' in reader.fieldnames and 'coach_name' in reader.fieldnames

        if not has_ids and not has_lookup:
            raise CommandError('CSV must have either (session_id, coach_id) or (class_name, session_date, coach_name) columns.')

        created, skipped, warnings = 0, 0, []

        with transaction.atomic():
            for i, row in enumerate(rows, start=2):
                session = None
                coach = None
                present = self.parse_present(row.get('present', ''))

                if has_ids:
                    # Direct ID lookup
                    try:
                        session_id = int((row.get('session_id') or '').strip())
                        coach_id = int((row.get('coach_id') or '').strip())
                        session = Session.objects.get(id=session_id, assigned_class__organisation=org)
                        coach = User.objects.get(id=coach_id)
                    except (ValueError, Session.DoesNotExist):
                        warnings.append(f'Row {i}: session_id {row.get("session_id")} not found in org {org.slug}.')
                        skipped += 1
                        continue
                    except User.DoesNotExist:
                        warnings.append(f'Row {i}: coach_id {row.get("coach_id")} not found.')
                        skipped += 1
                        continue

                elif has_lookup:
                    # Lookup by class_name, session_date, coach_name
                    class_name = (row.get('class_name') or '').strip()
                    session_date_str = (row.get('session_date') or '').strip()
                    coach_name = (row.get('coach_name') or '').strip()

                    if not class_name or not session_date_str or not coach_name:
                        warnings.append(f'Row {i}: missing class_name, session_date, or coach_name.')
                        skipped += 1
                        continue

                    try:
                        from datetime import datetime
                        session_date = datetime.strptime(session_date_str, '%Y-%m-%d').date()
                        session = Session.objects.get(
                            assigned_class__organisation=org,
                            assigned_class__name=class_name,
                            date=session_date
                        )
                    except ValueError:
                        warnings.append(f'Row {i}: invalid session_date format "{session_date_str}". Use YYYY-MM-DD.')
                        skipped += 1
                        continue
                    except Session.DoesNotExist:
                        warnings.append(f'Row {i}: no session for {class_name} on {session_date_str}.')
                        skipped += 1
                        continue

                    # Match coach by full name or username
                    coach_qs = User.objects.filter(
                        coached_classes__assigned_class__organisation=org,
                        coached_classes__assigned_class__sessions__id=session.id
                    ).distinct()
                    coach = None
                    for candidate in coach_qs:
                        candidate_name = candidate.get_full_name() or candidate.username
                        if candidate_name.lower() == coach_name.lower():
                            coach = candidate
                            break

                    if not coach:
                        warnings.append(f'Row {i}: coach "{coach_name}" not found for {class_name} on {session_date_str}.')
                        skipped += 1
                        continue

                # Create or update SessionCoach record
                if session and coach:
                    sc, created_flag = SessionCoach.objects.get_or_create(
                        session=session,
                        coach=coach,
                        defaults={'present': present if present is not None else False}
                    )
                    if not created_flag and present is not None:
                        # Update existing record if present is explicitly set
                        sc.present = present
                        if not dry_run:
                            sc.save()
                    if not dry_run:
                        pass  # Already saved via get_or_create or manual save
                    created += 1

            if dry_run:
                transaction.set_rollback(True)

        summary = f'{"Would import" if dry_run else "Imported"} {created} coach attendance record{"s" if created != 1 else ""}'
        if skipped:
            summary += f', skipped {skipped}'
        self.stdout.write(self.style.SUCCESS(summary + '.'))

        if warnings:
            self.stdout.write(self.style.WARNING(f'{len(warnings)} warning(s):'))
            for w in warnings:
                self.stdout.write(f'  - {w}')
