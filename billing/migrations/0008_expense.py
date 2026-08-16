from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('organisations', '0007_staffholiday'),
        ('billing', '0007_bankconnection'),
    ]

    operations = [
        migrations.CreateModel(
            name='Expense',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('description', models.CharField(max_length=255)),
                ('category', models.CharField(choices=[
                    ('rent', 'Rent & venue hire'),
                    ('utilities', 'Utilities'),
                    ('equipment', 'Equipment & mats'),
                    ('insurance', 'Insurance'),
                    ('salaries', 'Coach pay & salaries'),
                    ('licensing', 'Licensing & affiliation fees'),
                    ('marketing', 'Marketing'),
                    ('maintenance', 'Maintenance & repairs'),
                    ('other', 'Other'),
                ], default='other', max_length=20)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=10)),
                ('expense_date', models.DateField(default=django.utils.timezone.localdate)),
                ('notes', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('organisation', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='expenses', to='organisations.organisation')),
            ],
            options={
                'ordering': ['-expense_date', '-created_at'],
            },
        ),
    ]
