"""
Middleware to capture LTI locale and apply it to the session.
Works specifically with the coursera-lti tenant to avoid affecting main site sessions.
"""
import logging

logger = logging.getLogger(__name__)


class LtiLocaleMiddleware:
    """
    Captures launch_presentation_locale from LTI POST requests
    and sets the language preference for the LTI session.
    Uses tenant-specific cookie names to avoid conflicts with main site.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Solo procesar LTI launches (POST con oauth_consumer_key)
        if request.method == "POST" and "oauth_consumer_key" in request.POST:
            launch_locale = request.POST.get("launch_presentation_locale")
            
            if launch_locale:
                # Convertir "en-US" a formato lowercase "en-us"
                language_code = launch_locale.lower()
                base_language = language_code.split("-")[0]
                
                # Establecer en la sesión
                request.session["django_language"] = base_language
                
                # Sobrescribir cookie en el request para que los otros middlewares la vean
                from django.conf import settings
                cookie_name = getattr(settings, 'LANGUAGE_COOKIE_NAME', 'openedx-language-preference')
                request.COOKIES[cookie_name] = language_code
                
                logger.info(f"[LTI] Language set to: {language_code} (base: {base_language})")
            else:
                logger.warning("[LTI] No launch_presentation_locale found in LTI POST")
                
        response = self.get_response(request)
        
        # Establecer la cookie en la respuesta para requests subsiguientes
        if request.method == "POST" and "oauth_consumer_key" in request.POST:
            launch_locale = request.POST.get("launch_presentation_locale")
            if launch_locale:
                language_code = launch_locale.lower()
                from django.conf import settings
                cookie_name = getattr(settings, 'LANGUAGE_COOKIE_NAME', 'openedx-language-preference')
                response.set_cookie(
                    cookie_name, 
                    language_code,
                    domain=getattr(settings, 'SESSION_COOKIE_DOMAIN', None),
                    max_age=None,  # Cookie de sesión, no permanente
                    samesite='None',
                    secure=True
                )
                logger.info(f"[LTI] Cookie {cookie_name} set to: {language_code}")
        
        return response
