"""JSON signup API for a club's own website.

    GET  /api/join/<org>/   club name + active waivers, to build the form
    POST /api/join/<org>/   create a MemberApplication (JSON or form-encoded)

Cross-origin access is limited to SIGNUP_API_ORIGINS. Requests carrying any
other Origin are refused, so other sites can't post applications from a
visitor's browser. The endpoint is CSRF-exempt because it uses no cookies or
session; it can't act as anyone, only do what the public /join/ page does.
"""
import json

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from organisations.models import Organisation
from .signup import active_waivers, client_ip, create_application

# Hidden form field real people never fill in; bots that fill every input do.
HONEYPOT_FIELD = 'website'


def _cors(response, origin):
    if origin:
        response['Access-Control-Allow-Origin'] = origin
        response['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response['Access-Control-Allow-Headers'] = 'Content-Type'
        response['Access-Control-Max-Age'] = '86400'
        response['Vary'] = 'Origin'
    return response


@method_decorator(csrf_exempt, name='dispatch')
class SignupApiView(View):
    def dispatch(self, request, *args, **kwargs):
        origin = request.headers.get('Origin')
        if origin and origin not in settings.SIGNUP_API_ORIGINS:
            return JsonResponse({'errors': ['Origin not allowed.']}, status=403)
        return _cors(super().dispatch(request, *args, **kwargs), origin)

    def options(self, request, org_slug):
        return HttpResponse(status=204)

    def get(self, request, org_slug):
        org = get_object_or_404(Organisation, slug=org_slug)
        return JsonResponse({
            'organisation': org.name,
            'waivers': [
                {
                    'name': w.name,
                    'description': w.description,
                    'url': request.build_absolute_uri(w.file.url) if w.file else None,
                    'required': w.is_required,
                }
                for w in active_waivers(org)
            ],
        })

    def post(self, request, org_slug):
        org = get_object_or_404(Organisation, slug=org_slug)

        if request.content_type == 'application/json':
            try:
                data = json.loads(request.body or b'{}')
            except ValueError:
                return JsonResponse({'errors': ['Invalid JSON.']}, status=400)
            if not isinstance(data, dict):
                return JsonResponse({'errors': ['Invalid JSON.']}, status=400)
        else:
            data = request.POST

        if data.get(HONEYPOT_FIELD):
            # Look successful so the bot moves on, but store nothing.
            return JsonResponse({'ok': True}, status=201)

        key = f'signup-api:{org.pk}:{client_ip(request)}'
        attempts = cache.get_or_set(key, 0, settings.SIGNUP_API_RATE_WINDOW)
        if attempts >= settings.SIGNUP_API_RATE_LIMIT:
            return JsonResponse({'errors': ['Too many applications from this address. Please try again later.']}, status=429)
        try:
            cache.incr(key)
        except ValueError:  # window expired between the read and the increment
            cache.set(key, 1, settings.SIGNUP_API_RATE_WINDOW)

        application, errors = create_application(org, data)
        if errors:
            return JsonResponse({'errors': errors}, status=400)
        return JsonResponse({'ok': True}, status=201)
