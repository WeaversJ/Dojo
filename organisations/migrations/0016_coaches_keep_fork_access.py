# This fork let every coach open billing, all-class attendance, gradings, stock,
# generate sessions and enrol members. DojoUK makes each of those a permission an
# admin grants on the Staff page, so coaches who already exist are given them
# here and lose nothing on upgrade. New coaches start with none; an admin ticks
# what they need on the Staff page.

from django.db import migrations

FORK_COACH_ACCESS = (
    'can_manage_billing', 'can_view_all_attendance', 'can_manage_gradings',
    'can_generate_sessions', 'can_manage_class_members', 'can_view_stock',
)


def grant_existing_coaches(apps, schema_editor):
    OrganisationMember = apps.get_model('organisations', 'OrganisationMember')
    OrganisationMember.objects.filter(role='coach').update(**{name: True for name in FORK_COACH_ACCESS})


class Migration(migrations.Migration):

    dependencies = [
        ('organisations', '0015_organisationmember_dark_mode_accent_theme'),
    ]

    operations = [
        migrations.RunPython(grant_existing_coaches, migrations.RunPython.noop),
    ]
