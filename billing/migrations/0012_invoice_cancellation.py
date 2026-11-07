import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('billing', '0011_merge_policy_validators_and_expense'),
    ]

    operations = [
        migrations.AlterField(
            model_name='invoice',
            name='status',
            field=models.CharField(choices=[('unpaid', 'Unpaid'), ('paid', 'Paid'), ('overdue', 'Overdue'), ('cancelled', 'Cancelled')], default='unpaid', max_length=10),
        ),
        migrations.AddField(
            model_name='invoice',
            name='cancelled_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='invoice',
            name='cancelled_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='invoice',
            name='cancel_reason',
            field=models.TextField(blank=True, help_text='Why this invoice was cancelled (e.g. issued in error).'),
        ),
    ]
