# Joins this fork's syllabus migrations (0002-0006) with DojoUK's gradings and
# syllabus migrations (0002-0004), which were added side by side. Both sets of
# tables are kept: the fork's syllabus is the one in use, DojoUK's gradings and
# syllabus stay switched off unless a club turns them on under Settings → Features.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('progression', '0006_alter_syllabussubsection_id'),
        ('progression', '0004_membertechnique'),
    ]

    operations = []
