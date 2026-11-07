"""
Overpayment credit: when a member pays more than an invoice, the extra is
taken off their next invoice instead of sitting unused.

The credit stays on the invoice that was overpaid (Invoice.available_credit:
payments less the amount, less what has already been used), so nothing new
is invented: applying it just moves that much from one invoice to the next.
"""
import re
from decimal import Decimal

from django.db import transaction


def member_credit(member):
    """Unused overpayment credit across all of a member's invoices."""
    return sum((invoice.available_credit for invoice in member.invoices.prefetch_related('payments')), Decimal('0'))


def apply_member_credit(invoice):
    """
    Take any unused overpayment credit on the member's other invoices off
    this newly created one, oldest invoice first. The amount used is added
    to the invoice's discount and noted on it, and marked as used on the
    invoices it came from so it can't be offered again. An invoice the
    credit covers in full is marked paid. Returns the amount applied.

    Called straight after an invoice is created, from every place that
    creates one, inside the same transaction.
    """
    from .models import Invoice

    with transaction.atomic():
        needed = Decimal(str(invoice.amount))
        invoice.amount = needed
        if needed <= 0:
            return Decimal('0')
        sources = (
            Invoice.objects.select_for_update()
            .filter(member_id=invoice.member_id, organisation_id=invoice.organisation_id)
            .exclude(pk=invoice.pk)
            .order_by('due_date', 'created_at', 'pk')
        )
        applied = Decimal('0')
        for source in sources:
            if needed <= 0:
                break
            take = min(source.available_credit, needed)
            if take <= 0:
                continue
            source.overpayment_credited = Decimal(source.overpayment_credited) + take
            source.save(update_fields=['overpayment_credited'])
            applied += take
            needed -= take

        if applied <= 0:
            return applied
        invoice.discount_amount = Decimal(str(invoice.discount_amount)) + applied
        invoice.amount = needed
        note = f'Includes £{applied:.2f} credit from an earlier overpayment.'
        invoice.notes = f'{invoice.notes}\n{note}' if invoice.notes else note
        fields = ['amount', 'discount_amount', 'notes']
        if invoice.amount <= 0:
            invoice.status = Invoice.Status.PAID
            fields.append('status')
        invoice.save(update_fields=fields)
        return applied


# The note apply_member_credit writes on an invoice that used credit, in its
# current wording and in this fork's earlier one.
CREDIT_NOTE = re.compile(r'Includes £([0-9,]+\.[0-9]{2}) credit (?:from an earlier|applied from a previous) overpayment\.')


def give_back_credit(invoice):
    """
    Put back the overpayment credit a cancelled invoice used, so it comes off
    the member's next invoice instead of being lost. How much it used is in
    the invoice's note; it goes back onto the member's invoices that gave
    credit, newest first. Returns the amount given back.
    """
    from .models import Invoice

    used = sum((Decimal(amount.replace(',', '')) for amount in CREDIT_NOTE.findall(invoice.notes or '')), Decimal('0'))
    if used <= 0:
        return Decimal('0')
    with transaction.atomic():
        remaining = used
        sources = (
            Invoice.objects.select_for_update()
            .filter(member_id=invoice.member_id, organisation_id=invoice.organisation_id, overpayment_credited__gt=0)
            .exclude(pk=invoice.pk)
            .order_by('-due_date', '-created_at', '-pk')
        )
        for source in sources:
            if remaining <= 0:
                break
            back = min(Decimal(source.overpayment_credited), remaining)
            source.overpayment_credited = Decimal(source.overpayment_credited) - back
            source.save(update_fields=['overpayment_credited'])
            remaining -= back
        returned = used - remaining
        if returned > 0:
            note = f'Cancelled: £{returned:.2f} credit given back to the member.'
            invoice.notes = f'{invoice.notes}\n{note}' if invoice.notes else note
            invoice.save(update_fields=['notes'])
        return returned
