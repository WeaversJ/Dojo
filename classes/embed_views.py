"""
The timetable widget a club puts on its own website.

    <script src="https://<dojo>/embed.js" data-club="<club-slug>" async></script>

embed.js (EmbedScriptView) adds an iframe showing EmbedTimetableView, and sizes it to fit as the
timetable reports its height. The timetable is public by design (classes/timetable.py) and only
shown once the club switches it on under Settings → Website. Only the club's own websites (and
Dojo itself, for the preview) may frame it: the page sends a Content-Security-Policy
frame-ancestors list built from the club's Settings → Website domains.

Booking isn't done inside the frame. Browsers block cookies in third-party frames, and booking needs
the member's portal, so the buttons open Dojo in a new tab: "Try a class" goes to the join form
(with the class and the club's intro offer filled in), and members are sent to the portal sign-in.
"""
import re
from datetime import timedelta

from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.http import urlencode
from django.views import View
from django.views.decorators.clickjacking import xframe_options_exempt

from organisations.models import Organisation

from . import timetable

HEX = re.compile(r'#[0-9a-fA-F]{6}')
CACHE_SECONDS = 300


HOST = re.compile(r'(\*\.)?([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}|localhost')


def parse_domains(text, limit=Organisation.MAX_EMBED_DOMAINS):
    """
    The websites a club lists under Settings → Website, one per line, as origins for frame-ancestors:
    'www.cardiffjudo.co.uk/classes' becomes 'https://www.cardiffjudo.co.uk'. '*.example.com' allows
    every subdomain. Returns (origins, errors); entries with errors are left out.
    """
    from urllib.parse import urlsplit
    origins, errors = [], []
    for raw in re.split(r'[\s,]+', text or ''):
        entry = raw.strip()
        if not entry:
            continue
        candidate = entry if '://' in entry else f'https://{entry}'
        try:
            parts = urlsplit(candidate)
            host = (parts.hostname or '').lower()
            port = parts.port
        except ValueError:
            host, port, parts = '', None, None
        if parts is None or parts.scheme not in ('http', 'https') or not HOST.fullmatch(host):
            errors.append(f'"{entry}" isn\'t a website address. Use one like https://www.yourclub.co.uk')
            continue
        origin = f'{parts.scheme}://{host}' + (f':{port}' if port else '')
        if origin not in origins:
            origins.append(origin)
    if len(origins) > limit:
        errors.append(f'You can list up to {limit} websites, so only the first {limit} were kept.')
        origins = origins[:limit]
    return origins, errors


def frame_ancestors(org):
    """The Content-Security-Policy value that lets Dojo itself and the club's websites frame the widget."""
    return ' '.join(["frame-ancestors 'self'", *org.embed_domains])


def try_link_base(org, base_url, source):
    """The start of a "Try a class" link, ready for 'class=<id>' to be added; '' if the club takes no applications."""
    if not org.has_feature('applications'):
        return ''
    from billing import promos
    params = {'source': source}
    offers = promos.public_offers(org)
    if offers:
        params['code'] = offers[0].code
    return f'{base_url}?{urlencode(params)}&'


def colours(org):
    """The club's accent colour (Settings → Appearance) and the text colour that reads on it."""
    theme = org.theme()
    accent = theme['accent_color'] if HEX.fullmatch(str(theme['accent_color'])) else '#2563EB'
    hover = theme['accent_hover'] if HEX.fullmatch(str(theme['accent_hover'])) else accent
    return {'accent': accent, 'accent_hover': hover,
            'on_accent': theme['accent_text'] if HEX.fullmatch(str(theme['accent_text'])) else '#ffffff'}


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@method_decorator(xframe_options_exempt, name='dispatch')
class EmbedTimetableView(View):
    def get(self, request, org_slug):
        org = get_object_or_404(Organisation, slug=org_slug)
        if not org.public_timetable:
            raise Http404

        weeks = max(1, min(_int(request.GET.get('weeks'), 1), timetable.MAX_WEEKS))
        only_class = _int(request.GET.get('class'))
        theme = 'dark' if request.GET.get('theme') == 'dark' else 'light'
        today = timezone.localdate()
        start = timetable.clamp_start(timetable.parse_date(request.GET.get('start')), today)
        last = today + timedelta(weeks=timetable.WEEKS_AHEAD - 1)
        end = start + timedelta(days=7 * weeks - 1)

        classes = timetable.public_classes(org)
        if only_class is not None and only_class not in {c.pk for c in classes}:
            only_class = None

        def page(when):
            params = {'start': when.isoformat()}
            for key in ('weeks', 'class', 'theme'):
                if request.GET.get(key):
                    params[key] = request.GET[key]
            return f'?{urlencode(params)}'

        from billing import promos
        offers = promos.public_offers(org)
        response = render(request, 'classes/embed_timetable.html', {
            'org': org,
            'days': timetable.build(org, start, weeks, only_class),
            'start': start, 'end': end, 'theme': theme,
            'colours': colours(org),
            'classes': [] if only_class is not None else classes,
            'prev_url': page(max(start - timedelta(weeks=weeks), today)) if start > today else '',
            'next_url': page(start + timedelta(weeks=weeks)) if start + timedelta(weeks=weeks) <= last else '',
            'try_base': try_link_base(org, reverse('member_signup', kwargs={'org_slug': org.slug}), 'widget'),
            'offer': offers[0] if offers else None,
            'portal_url': (reverse('portal_login_request', kwargs={'org_slug': org.slug})
                           if org.has_feature('member_portal') else ''),
            'dojo_url': (reverse('network_club', kwargs={'slug': org.slug})
                         if org.listed_in_finder and org.has_location else reverse('network_finder')),
        })
        response['Content-Security-Policy'] = frame_ancestors(org)
        response['Cache-Control'] = f'public, max-age={CACHE_SECONDS}'
        return response


class EmbedScriptView(View):
    """The loader clubs paste into their website. The same for every club, so it's cached for an hour."""

    def get(self, request):
        response = render(request, 'classes/embed.js', content_type='text/javascript; charset=utf-8')
        response['Cache-Control'] = 'public, max-age=3600'
        return response
