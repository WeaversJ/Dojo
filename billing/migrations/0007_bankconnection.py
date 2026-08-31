# The bank-account-balance integration this migration originally created
# (BankConnection) was rolled back before release. Left as a no-op rather
# than deleted — the sandbox this was built in can't unlink files on the
# mounted filesystem — so the migration numbering stays intact and nothing
# downstream breaks if this was ever applied.
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0006_invoiceitem'),
        ('organisations', '0007_staffholiday'),
    ]

    operations = []
