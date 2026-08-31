from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('organisations', '0007_staffholiday'),
    ]

    operations = [
        migrations.AddField(
            model_name='organisationmember',
            name='emergency_contact_2_name',
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name='organisationmember',
            name='emergency_contact_2_phone',
            field=models.CharField(blank=True, max_length=30),
        ),
    ]
