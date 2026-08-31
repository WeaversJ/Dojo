"""
Bulk-import invoices (and, optionally, an initial payment against each one)
from a CSV export.

CSV columns (member_name, amount, period, due_date required):
    member_name       - matched against this organisation's existing members by name
    amount            - invoice amount, e.g. 45.00
    period            - free text, e.g. "January 2026" or "Autumn Term 2025"
    due_date           - accepts YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, DD-Mon-YYYY, DD Mon YYYY
    billing_policy     - optional, matched by this organisation's billing policy NAME
    discount_amount    - optional, defaults to 0
    status              - optional: unpaid / paid / overdue (defaults to unpaid, or "paid"
                          automatically if paid_amount + paid_date are both given)
    notes               - optional

    Optional payment columns — if paid_date is present, a Payment record is created
    against the invoice:
    paid_amount        - defaults to the invoice amount if left blank
    paid_date          - same date formats as due_date
    payment_method     - manual / stripe / bacs / cash (defaults to manual)

Existing invoices are matched on organisation + member + period, and are skipped by
default so this is safe to re-run against an updated export (use --allow-duplicates
to import anyway, e.g. for two separate invoices in the same period).

Run with:
    python manage.py import_invoices_csv Invoices.csv --org <org-slug> --dry-run
    python manage.py import_invoices_csv Invoices.csv --org <org-slug>
"""
import csv
from datetime import datetime, timezone as dt_timezone
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
    if value < 0:
        return None, f'amount "{raw}" can\'t be negative'
    return value, None


class Command(BaseCommand):
    help = 'Bulk-import invoices (and an optional initial payment per invoice) from a CSV export.'

    VALID_STATUSES = {'unpaid', 'paid', 'overdue'}
    VALID_METHODS = {'manual', 'stripe', 'bacs', 'cash'}

    def add_arguments(self, parser):
        parser.add_argument('csv_path', nargs='?', default='Invoices.csv',
                             help='Path to the CSV file (default: Invoices.csv in the project root)')
        parser.add_argument('--org', dest='org_slug',
                             help='Organisation slug to import into (required if more than one organisation exists)')
        parser.add_argument('--dry-run', action='store_true',
                             help='Parse and validate only — nothing is written to the database')
        parser.add_argument('--allow-duplicates', action='store_true',
                             help='Import even if an invoice for the same member and period already exists')

    def handle(self, *args, **options):
        from organisations.models import Organisation
        from members.models import Member
        from billing.models import Invoice, Payment, BillingPolicy

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
            required = ['member_name', 'amount', 'period', 'due_date']
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

        members = {m.name.strip().lower(): m for m in Member.objects.filter(organisation=org)}
        policies = {p.name.strip().lower(): p for p in BillingPolicy.objects.filter(organisation=org)}

        created, payments_created, skipped, warnings = 0, 0, 0, []

        with transaction.atomic():
            for i, row in enumerate(rows, start=2):
                member_name = (row.get('member_name') or row.get('member') or '').strip()
                if not member_name:
                    warnings.append(f'Row {i}: missing member_name, skipped.')
                    continue

                member = members.get(member_name.lower())
                if not member:
                    warnings.append(f'Row {i}: no member named "{member_name}" in this organisation — skipped.')
                    skipped += 1
                    continue

                amount, amount_err = parse_amount(row.get('amount'))
                if amount_err:
                    warnings.append(f'Row {i} ({member_name}): {amount_err} — skipped.')
                    skipped += 1
                    continue

                period = (row.get('period') or '').strip()
                if not period:
                    warnings.append(f'Row {i} ({member_name}): missing period — skipped.')
                    skipped += 1
                    continue

                due_date, due_err = parse_date(row.get('due_date'))
                if due_err or not due_date:
                    warnings.append(f'Row {i} ({member_name}): {due_err or "missing due_date"} — skipped.')
                    skipped += 1
                    continue

                discount_amount, discount_err = parse_amount(row.get('discount_amount'))
                if discount_err:
                    warnings.append(f'Row {i} ({member_name}): {discount_err} — discount left at 0.')
                    discount_amount = None
                discount_amount = discount_amount or Decimal('0')

                policy = None
                policy_raw = (row.get('billing_policy') or '').strip()
                if policy_raw:
                    policy = policies.get(policy_raw.lower())
                    if not policy:
                        warnings.append(f'Row {i} ({member_name}): billing policy "{policy_raw}" not found — left unset.')

                notes = (row.get('notes') or '').strip()

                if not allow_duplicates:
                    if Invoice.objects.filter(organisation=org, member=member, period=period).exists():
                        warnings.append(f'Row {i} ({member_name}): an invoice for "{period}" already exists — skipped (use --allow-duplicates to import anyway).')
                        skipped += 1
                        continue

                # Optional payment columns
                paid_amount, paid_amount_err = parse_amount(row.get('paid_amount'))
                if paid_amount_err:
                    warnings.append(f'Row {i} ({member_name}): {paid_amount_err} — payment skipped for this row.')
                paid_date, paid_date_err = parse_date(row.get('paid_date'))
                if paid_date_err:
                    warnings.append(f'Row {i} ({member_name}): {paid_date_err} — payment skipped for this row.')

                method = (row.get('payment_method') or 'manual').strip().lower()
                if method not in self.VALID_METHODS:
                    warnings.append(f'Row {i} ({member_name}): unrecognised payment_method "{method}" — used "manual".')
                    method = 'manual'

                status_raw = (row.get('status') or '').strip().lower()
                if status_raw and status_raw not in self.VALID_STATUSES:
                    warnings.append(f'Row {i} ({member_name}): unrecognised status "{status_raw}" — left as unpaid.')
                    status_raw = ''

                has_payment = paid_date is not None
                if status_raw:
                    status = status_raw
                elif has_payment:
                    status = Invoice.Status.PAID
                else:
                    status = Invoice.Status.UNPAID

                if not dry_run:
                    invoice = Invoice.objects.create(
                        organisation=org,
                        member=member,
                        billing_policy=policy,
                        amount=amount,
                        discount_amount=discount_amount,
                        period=period,
                        due_date=due_date,
                        status=status,
                        notes=notes,
                    )
                    if has_payment:
                        Payment.objects.create(
                            invoice=invoice,
                            amount=paid_amount if paid_amount is not None else amount,
                            method=method,
                            paid_at=datetime.combine(paid_date, datetime.min.time()).replace(tzinfo=dt_timezone.utc),
                            notes='Imported from CSV',
                        )
                        payments_created += 1
                elif has_payment:
                    payments_created += 1

                created += 1

            if dry_run:
                transaction.set_rollback(True)

        summary = f'{"Would import" if dry_run else "Imported"} {created} invoice{"s" if created != 1 else ""}'
        if payments_created:
            summary += f' ({payments_created} with an initial payment recorded)'
        if skipped:
            summary += f', skipped {skipped} row{"s" if skipped != 1 else ""}'
        self.stdout.write(self.style.SUCCESS(summary + '.'))

        if warnings:
            self.stdout.write(self.style.WARNING(f'{len(warnings)} warning(s):'))
            for w in warnings:
                self.stdout.write(f'  - {w}')
