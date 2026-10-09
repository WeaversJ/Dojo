from django.shortcuts import get_object_or_404, render
from django.views import View

from organisations.models import Organisation
from .signup import active_waivers, create_application


class SignupView(View):
    def get(self, request, org_slug):
        org = get_object_or_404(Organisation, slug=org_slug)
        return render(request, 'members/signup.html', {'org': org, 'waivers': active_waivers(org)})

    def post(self, request, org_slug):
        org = get_object_or_404(Organisation, slug=org_slug)
        application, errors = create_application(org, request.POST)
        if errors:
            return render(request, 'members/signup.html', {
                'org': org, 'waivers': active_waivers(org), 'errors': errors, 'data': request.POST,
            })
        return render(request, 'members/signup_done.html', {'org': org})
