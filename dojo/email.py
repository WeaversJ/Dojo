from email.utils import formataddr, parseaddr

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email


def is_valid_email(value):
    try:
        validate_email(value)
    except ValidationError:
        return False
    return True


def org_sender(org):
    """From/Reply-To kwargs for an email sent on behalf of an organisation.

    Mail always leaves from DEFAULT_FROM_EMAIL's address, because that's the
    only address the instance's SPF/DKIM records vouch for. Sending as the
    organisation's own address (often a Hotmail/Gmail inbox) fails DMARC and
    gets junked or rejected. The organisation's name is used as the display
    name, and replies are routed to its contact email.
    """
    _, address = parseaddr(settings.DEFAULT_FROM_EMAIL)
    # An invalid address would make Django refuse to build the message at all,
    # so drop the Reply-To rather than the email.
    reply_to = org.email if org.email and is_valid_email(org.email) else None
    # A line break in the name would make Django reject every message.
    name = ' '.join(org.name.split())
    return {
        'from_email': formataddr((name, address)),
        'reply_to': [reply_to] if reply_to else None,
    }
