"""
Bulk-import expenses from a CSV export.

CSV columns (description, amount, expense_date required):
    description    - free text, e.g. "June rent" or "Mat replacement"
    amount         - expense amount, e.g. 350.00
    expense_date   - accepts YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, DD-Mon-YYYY, DD Mon YYYY
    category       - optional: rent / utilities / equipment / insurance / salaries /
                     licensing / marketing / maintenance / other (defaults to "other")
    notes          - optional

Existing expenses are matched on organisation + description + expense_date + amount,
and are skipped by default so this is safe to re-run against an updated export
(use --allow-duplicates to import anyway).

Run with:
    python manage.py import_expenses_csv Expenses.csv --org <org-slug> --dry-run
    python manage.py import_expenses_csv Expenses.csv --org <org-slug>
"""
import csv
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

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


def parse_amount(raw):
    raw = (raw or '').strip().replace('£', '').replace(',', '')
    if not raw:
        return None, None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None, f'invalid amount "{raw}"'
    if value <= 0:
        return None, f'amount "{raw}" must be greater than 0'
    return value, None


class Command(BaseCommand):
    help = 'Bulk-import expenses from a CSV export.'

    def add_arguments(self, parser):
        parser.add_argument('csv_path', nargs='?', default='Expenses.csv',
                             help='Path to the CSV file (default: Expenses.csv in the project root)')
        parser.add_argument('--org', dest='org_slug',
                             help='Organisation slug to import into (required if more than one organisation exists)')
        parser.add_argument('--dry-run', action='store_true',
                             help='Parse and validate only — nothing is written to the database')
        parser.add_argument('--allow-duplicates', action='store_true',
                             help='Import even if an identical expense (same description, date, and amount) already exists')

    def handle(self, *args, **options):
        from organisations.models import Organisation
        from billing.models import Expense

        csv_path = options['csv_path']
        dry_run = options['dry_run']
        allow_duplicates = options['allow_duplicates']

        valid_categories = {c for c, _ in Expense.Category.choices}

        try:
            fh = open(csv_path, encoding='utf-8-sig')
        except OSError as e:
            raise CommandError(f'Could not open {csv_path}: {e}')

        with fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames:
                raise CommandError('Could not parse CSV headers.')
            reader.fieldnames = [h.strip().lower().replace(' ', '_') for h in reader.fieldnames]
            required = ['description', 'amount', 'expense_date']
            missing = [c for c in required if c not in reader.fieldnames]
            if missing:
                raise CommandError(f'CSV must have column(s): {", ".join(missing)}')
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

        created, skipped, warnings = 0, 0, []

        with transaction.atomic():
            for i, row in enumerate(rows, start=2):
                description = (row.get('description') or '').strip()
                if not description:
                    warnings.append(f'Row {i}: missing description, skipped.')
                    continue

                amount, amount_err = parse_amount(row.get('amount'))
                if amount_err:
                    warnings.append(f'Row {i} ({description}): {amount_err} — skipped.')
                    skipped += 1
                    continue

                expense_date, date_err = parse_date(row.get('expense_date') or row.get('date'))
                if date_err or not expense_date:
                    warnings.append(f'Row {i} ({description}): {date_err or "missing expense_date"} — skipped.')
                    skipped += 1
                    continue

                category = (row.get('category') or '').strip().lower()
                if category and category not in valid_categories:
                    warnings.append(f'Row {i} ({description}): unrecognised category "{category}" — used "other".')
                    category = ''
                category = category or Expense.Category.OTHER

                notes = (row.get('notes') or '').strip()

                if not allow_duplicates:
                    if Expense.objects.filter(
                        organisation=org, description=description, expense_date=expense_date, amount=amount,
                    ).exists():
                        warnings.append(f'Row {i} ({description}): an identical expense already exists — skipped (use --allow-duplicates to import anyway).')
                        skipped += 1
                        continue

                if not dry_run:
                    Expense.objects.create(
                        organisation=org,
                        description=description,
                        category=category,
                        amount=amount,
                        expense_date=expense_date,
                        notes=notes,
                    )
                created += 1

            if dry_run:
                transaction.set_rollback(True)

        summary = f'{"Would import" if dry_run else "Imported"} {created} expense{"s" if created != 1 else ""}'
        if skipped:
            summary += f', skipped {skipped} row{"s" if skipped != 1 else ""}'
        self.stdout.write(self.style.SUCCESS(summary + '.'))

        if warnings:
            self.stdout.write(self.style.WARNING(f'{len(warnings)} warning(s):'))
            for w in warnings:
                self.stdout.write(f'  - {w}')
