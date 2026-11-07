# Joins this fork's invoice cancellation (0012_invoice_cancellation, from #16)
# with DojoUK's billing migrations (up to 0012_expense), which were added side
# by side. No database change.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0012_invoice_cancellation'),
        ('billing', '0012_expense'),
    ]

    operations = []
