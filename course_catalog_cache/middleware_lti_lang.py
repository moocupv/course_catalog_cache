"""
Middleware to capture LTI locale and apply it to the session.
"""

class LtiLocaleMiddleware:
    """
    Captures launch_presentation_locale from LTI POST requests
    and sets the language preference.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Solo procesar LTI launches (POST con oauth_consumer_key)
        if request.method == 'POST' and 'oauth_consumer_key' in request.POST:
            launch_locale = request.POST.get('launch_presentation_locale')
            if launch_locale:
                # Convertir "es-ES" a "es"
                language_code = launch_locale.split('-')[0] if '-' in launch_locale else launch_locale
                # Aplicar a la sesión
                request.session['django_language'] = language_code
                
        response = self.get_response(request)
        return response
