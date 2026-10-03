from django.db import migrations


def flatten_names(apps, schema_editor):
    """Collapse line breaks in existing names, which make email headers invalid."""
    Organisation = apps.get_model('organisations', 'Organisation')
    for org in Organisation.objects.all():
        flat = ' '.join(org.name.split())
        if flat != org.name:
            org.name = flat
            org.save(update_fields=['name'])


class Migration(migrations.Migration):

    dependencies = [
        ('organisations', '0009_organisationmember_calendar_colour'),
    ]

    operations = [
        migrations.RunPython(flatten_names, migrations.RunPython.noop),
    ]
