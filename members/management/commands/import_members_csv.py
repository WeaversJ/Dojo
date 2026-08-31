"""
Bulk-import members from a CSV export.

Supports (all optional except name): date_of_birth, email, phone,
emergency_contact_name, emergency_contact_phone, emergency_contact_2_name,
emergency_contact_2_phone, is_active (Yes/No), medical_info, and
billing_policy_id (matched by billing policy NAME, not a numeric id, against
this organisation's existing billing policies).

Run with:
    python manage.py import_members_csv Members.csv --org <org-slug> --dry-run
    python manage.py import_members_csv Members.csv --org <org-slug>
"""
import csv
from datetime import datetime, date as ddate

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Bulk-import members from a CSV export, including emergency contacts, medical info, active status, and billing policy."

    DATE_FORMATS = ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%d-%b-%Y', '%d %b %Y')

    def add_arguments(self, parser):
        parser.add_argument('csv_path', nargs='?', default='Members.csv',
                             help='Path to the CSV file (default: Members.csv in the project root)')
        parser.add_argument('--org', dest='org_slug',
                             help='Organisation slug to import into (required if more than one organisation exists)')
        parser.add_argument('--dry-run', action='store_true',
                             help='Parse and validate only — nothing is written to the database')
        parser.add_argument('--allow-duplicates', action='store_true',
                             help='Import even if a member with the same name and date of birth already exists')

    def parse_date(self, raw):
        raw = (raw or '').strip()
        if not raw:
            return None, None
        for fmt in self.DATE_FORMATS:
            try:
                return datetime.strptime(raw, fmt).date(), None
            except ValueError:
                continue
        return None, f'unrecognised date format "{raw}"'

    def handle(self, *args, **options):
        from organisations.models import Organisation
        from members.models import Member
        from billing.models import BillingPolicy

        csv_path = options['csv_path']
        dry_run = options['dry_run']
        allow_duplicates = options['allow_duplicates']

        try:
            fh = open(csv_path, encoding='utf-8-sig')
        except OSError as e:
            raise CommandError(f'Could not open {csv_path}: {e}')

        with fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames:
                raise CommandError('Could not parse CSV headers.')
            reader.fieldnames = [h.strip().lower().replace(' ', '_') for h in reader.fieldnames]
            if 'name' not in reader.fieldnames:
                raise CommandError('CSV must have a "name" column.')
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

        # Preload this org's billing policies, matched by name (case-insensitive)
        policies = {p.name.strip().lower(): p for p in BillingPolicy.objects.filter(organisation=org)}

        created, skipped, warnings = 0, 0, []

        with transaction.atomic():
            for i, row in enumerate(rows, start=2):
                name = (row.get('name') or '').strip()
                if not name:
                    warnings.append(f'Row {i}: missing name, skipped.')
                    continue

                dob, dob_err = self.parse_date(row.get('date_of_birth') or row.get('dob'))
                if dob_err:
                    warnings.append(f'Row {i} ({name}): {dob_err} — date_of_birth left blank.')

                email_raw = (row.get('email') or '').strip()
                email = email_raw
                if ';' in email_raw:
                    parts = [e.strip() for e in email_raw.split(';') if e.strip()]
                    email = parts[0] if parts else ''
                    if len(parts) > 1:
                        warnings.append(f'Row {i} ({name}): multiple emails found — used "{email}", dropped {parts[1:]}.')

                is_active_raw = (row.get('is_active') or '').strip().lower()
                is_active = is_active_raw in ('', 'yes', 'true', '1')

                policy = None
                policy_raw = (row.get('billing_policy_id') or row.get('billing_policy') or '').strip()
                if policy_raw:
                    policy = policies.get(policy_raw.lower())
                    if not policy:
                        warnings.append(f'Row {i} ({name}): billing policy "{policy_raw}" not found for this organisation — left unset.')

                if not allow_duplicates:
                    if Member.objects.filter(organisation=org, name=name, date_of_birth=dob).exists():
                        warnings.append(f'Row {i} ({name}): a member with this name and date of birth already exists — skipped (use --allow-duplicates to import anyway).')
                        skipped += 1
                        continue

                fields = dict(
                    organisation=org,
                    name=name,
                    date_of_birth=dob,
                    email=email,
                    phone=(row.get('phone') or '').strip(),
                    emergency_contact_name=(row.get('emergency_contact_name') or '').strip(),
                    emergency_contact_phone=(row.get('emergency_contact_phone') or '').strip(),
                    emergency_contact_2_name=(row.get('emergency_contact_2_name') or '').strip(),
                    emergency_contact_2_phone=(row.get('emergency_contact_2_phone') or '').strip(),
                    is_active=is_active,
                    medical_info=(row.get('medical_info') or '').strip(),
                    billing_policy=policy,
                )

                if not dry_run:
                    Member.objects.create(**fields)
                created += 1

            if dry_run:
                transaction.set_rollback(True)

        summary = f'{"Would import" if dry_run else "Imported"} {created} member{"s" if created != 1 else ""}'
        if skipped:
            summary += f', skipped {skipped} likely duplicate{"s" if skipped != 1 else ""}'
        self.stdout.write(self.style.SUCCESS(summary + '.'))

        if warnings:
            self.stdout.write(self.style.WARNING(f'{len(warnings)} warning(s):'))
            for w in warnings:
                self.stdout.write(f'  - {w}')
