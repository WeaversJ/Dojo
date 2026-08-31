from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('organisations', '0008_organisationmember_emergency_contact_2'),
    ]

    operations = [
        migrations.AddField(
            model_name='organisationmember',
            name='calendar_colour',
            field=models.CharField(
                blank=True, max_length=7,
                help_text="Hex colour used for this staff member's sessions on the org calendar, e.g. #2563EB.",
            ),
        ),
    ]
