"""
Export session and coach data to CSV for recording SessionCoach attendance.

Generates a CSV with session_id, coach_id, coach_name, class_name, session_date.
This can be used to bulk-import coach attendance records.

Run with:
    python manage.py export_session_coach_attendance --org <org-slug> > coach_attendance.csv
    python manage.py export_session_coach_attendance --org <org-slug> --output coach_attendance.csv
"""
import csv
import sys
from datetime import datetime
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Export session and coach data to CSV for recording coach attendance."

    def add_arguments(self, parser):
        parser.add_argument('--org', dest='org_slug', required=True,
                             help='Organisation slug to export from')
        parser.add_argument('--output', dest='output_file', default=None,
                             help='Output file path (default: stdout)')
        parser.add_argument('--from-date', dest='from_date', default=None,
                             help='Only include sessions from this date (YYYY-MM-DD)')
        parser.add_argument('--to-date', dest='to_date', default=None,
                             help='Only include sessions up to this date (YYYY-MM-DD)')

    def handle(self, *args, **options):
        from organisations.models import Organisation
        from classes.models import Session, ClassCoach

        org_slug = options['org_slug']
        output_file = options['output_file']
        from_date_str = options['from_date']
        to_date_str = options['to_date']

        # Resolve organisation
        try:
            org = Organisation.objects.get(slug=org_slug)
        except Organisation.DoesNotExist:
            raise CommandError(f'No organisation with slug "{org_slug}".')

        # Parse date filters
        from_date = None
        to_date = None
        if from_date_str:
            try:
                from_date = datetime.strptime(from_date_str, '%Y-%m-%d').date()
            except ValueError:
                raise CommandError(f'Invalid from_date format: {from_date_str}. Use YYYY-MM-DD.')
        if to_date_str:
            try:
                to_date = datetime.strptime(to_date_str, '%Y-%m-%d').date()
            except ValueError:
                raise CommandError(f'Invalid to_date format: {to_date_str}. Use YYYY-MM-DD.')

        # Fetch all sessions for this org
        sessions = Session.objects.filter(
            assigned_class__organisation=org
        ).select_related('assigned_class').order_by('date')

        if from_date:
            sessions = sessions.filter(date__gte=from_date)
        if to_date:
            sessions = sessions.filter(date__lte=to_date)

        # Fetch all coaches assigned to classes in this org
        class_coaches = ClassCoach.objects.filter(
            assigned_class__organisation=org
        ).select_related('assigned_class', 'user')

        # Build a map: class_id → list of coaches
        class_coach_map = {}
        for cc in class_coaches:
            class_id = cc.assigned_class.id
            if class_id not in class_coach_map:
                class_coach_map[class_id] = []
            class_coach_map[class_id].append({
                'coach_id': cc.user.id,
                'coach_name': cc.user.get_full_name() or cc.user.username,
                'username': cc.user.username,
            })

        # Open output file or use stdout
        if output_file:
            output_fh = open(output_file, 'w', newline='', encoding='utf-8')
        else:
            output_fh = sys.stdout

        try:
            writer = csv.DictWriter(
                output_fh,
                fieldnames=['session_id', 'coach_id', 'coach_name', 'username', 'class_name', 'session_date', 'present']
            )
            writer.writeheader()

            row_count = 0
            for session in sessions:
                class_id = session.assigned_class.id
                coaches = class_coach_map.get(class_id, [])

                for coach in coaches:
                    writer.writerow({
                        'session_id': session.id,
                        'coach_id': coach['coach_id'],
                        'coach_name': coach['coach_name'],
                        'username': coach['username'],
                        'class_name': session.assigned_class.name,
                        'session_date': session.date.strftime('%Y-%m-%d'),
                        'present': '',  # Leave blank for user to fill in
                    })
                    row_count += 1

            if output_file:
                self.stdout.write(self.style.SUCCESS(f'Exported {row_count} session-coach combinations to {output_file}.'))
        finally:
            if output_file:
                output_fh.close()
