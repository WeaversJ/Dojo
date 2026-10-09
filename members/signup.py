"""Shared logic for public membership applications.

Used by both the HTML signup page (/join/<org>/) and the JSON signup API
(/api/join/<org>/) so the two can never disagree about what a valid
application is.
"""
from datetime import date

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from .models import MemberApplication

TEXT_FIELDS = (
    'email', 'phone', 'address_line1', 'address_line2', 'city', 'county', 'postcode',
    'guardian_name', 'guardian_email', 'guardian_phone', 'medical_info', 'notes',
)
EMAIL_FIELDS = ('email', 'guardian_email')
# A drawn signature is a base64 PNG; anything far beyond this is not one.
MAX_SIGNATURE_LENGTH = 2_000_000


def client_ip(request):
    ip = request.META.get('HTTP_X_FORWARDED_FOR', request.META.get('REMOTE_ADDR', ''))
    if ',' in ip:
        ip = ip.split(',')[0].strip()
    return ip


def active_waivers(org):
    return org.waiver_templates.filter(is_active=True)


def create_application(org, data):
    """Validate submitted fields and create a MemberApplication.

    `data` is any mapping of field name -> value (request.POST or parsed JSON).
    Returns (application, errors); exactly one of them is empty.
    """
    def value(key):
        v = data.get(key, '')
        return v.strip() if isinstance(v, str) else ''

    errors = []
    name = value('name')
    if not name:
        errors.append('Full name is required.')

    signature_data = value('signature_data')
    if active_waivers(org).filter(is_required=True).exists() and not signature_data:
        errors.append('Please sign the document before submitting.')
    if len(signature_data) > MAX_SIGNATURE_LENGTH:
        errors.append('Signature is too large.')

    fields = {key: value(key) for key in TEXT_FIELDS}
    for key in ('name',) + TEXT_FIELDS:
        max_length = MemberApplication._meta.get_field(key).max_length
        current = name if key == 'name' else fields[key]
        if max_length and len(current) > max_length:
            label = MemberApplication._meta.get_field(key).verbose_name
            errors.append(f'{label.capitalize()} must be at most {max_length} characters.')
    for key in EMAIL_FIELDS:
        if fields[key]:
            try:
                validate_email(fields[key])
            except ValidationError:
                label = MemberApplication._meta.get_field(key).verbose_name
                errors.append(f'Enter a valid {label}.')

    if errors:
        return None, errors

    dob = None
    dob_raw = value('date_of_birth')
    if dob_raw:
        try:
            dob = date.fromisoformat(dob_raw)
        except ValueError:
            pass

    application = MemberApplication.objects.create(
        organisation=org,
        name=name,
        date_of_birth=dob,
        signature_data=signature_data,
        **fields,
    )
    return application, []
