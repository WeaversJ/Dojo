"""
Export session IDs and coach (user) IDs to CSV, so a SessionCoach-attendance
CSV can be built referencing them (columns: session_id, coach_id, present).

Writes two files into the project root (or wherever you point --out-dir):
  sessions_lookup.csv — id, class_name, date, is_cancelled, is_extra
  coaches_lookup.csv  — id, username, full_name, role, coaching_licence

Run with:
    python manage.py export_session_coach_lookup --org <org-slug>
    python manage.py export_session_coach_lookup --org <org-slug> --out-dir some/folder
"""
import csv
import os

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Export session IDs and coach/user IDs to CSV for building a SessionCoach import.'

    def add_arguments(self, parser):
        parser.add_argument('--org', dest='org_slug',
                             help='Organisation slug (required if more than one organisation exists)')
        parser.add_argument('--out-dir', dest='out_dir', default='.',
                             help='Directory to write the CSVs into (default: project root)')

    def handle(self, *args, **options):
        from organisations.models import Organisation, OrganisationMember
        from classes.models import Session

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

        out_dir = options['out_dir']
        os.makedirs(out_dir, exist_ok=True)

        # ── Sessions ──────────────────────────────────────────────────────────
        sessions_path = os.path.join(out_dir, 'sessions_lookup.csv')
        sessions = (
            Session.objects.filter(assigned_class__organisation=org)
            .select_related('assigned_class')
            .order_by('assigned_class__name', 'date')
        )
        with open(sessions_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['id', 'class_name', 'date', 'is_cancelled', 'is_extra'])
            count = 0
            for s in sessions:
                writer.writerow([s.pk, s.assigned_class.name, s.date.isoformat(), 'Yes' if s.is_cancelled else 'No', 'Yes' if s.is_extra else 'No'])
                count += 1
        self.stdout.write(self.style.SUCCESS(f'Wrote {count} session{"s" if count != 1 else ""} to {sessions_path}'))

        # ── Coaches ───────────────────────────────────────────────────────────
        coaches_path = os.path.join(out_dir, 'coaches_lookup.csv')
        members = (
            OrganisationMember.objects.filter(organisation=org)
            .select_related('user')
            .order_by('-role', 'user__first_name', 'user__last_name')
        )
        with open(coaches_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['id', 'username', 'full_name', 'role', 'coaching_licence'])
            count = 0
            for m in members:
                full_name = m.user.get_full_name() or m.user.username
                writer.writerow([m.user.pk, m.user.username, full_name, m.get_role_display(), m.coaching_licence])
                count += 1
        self.stdout.write(self.style.SUCCESS(f'Wrote {count} organisation member{"s" if count != 1 else ""} (coaches + admins) to {coaches_path}'))

        self.stdout.write(
            'Note: "id" in coaches_lookup.csv is the Django auth User id — that\'s what SessionCoach.coach '
            'points to (not the OrganisationMember id).'
        )
