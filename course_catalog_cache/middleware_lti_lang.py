"""
Middleware to capture LTI locale and apply it to the session.
"""
import logging

logger = logging.getLogger(__name__)


class LtiLocaleMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Solo procesar LTI launches (POST con oauth_consumer_key)
        if request.method == "POST" and "oauth_consumer_key" in request.POST:
            launch_locale = request.POST.get("launch_presentation_locale")
            
            if launch_locale:
                language_code = launch_locale.lower()  # "en-us"
                base_language = language_code.split("-")[0]  # "en"
                
                # Establecer en la sesión
                request.session["django_language"] = base_language
                
                # Sobrescribir cookie en el request
                from django.conf import settings
                cookie_name = getattr(settings, "LANGUAGE_COOKIE_NAME", "openedx-language-preference")
                request.COOKIES[cookie_name] = language_code
                
                logger.info(f"[LTI] Language set to: {language_code}")
                
        response = self.get_response(request)
        
        # Establecer cookie en la respuesta
        if request.method == "POST" and "oauth_consumer_key" in request.POST:
            launch_locale = request.POST.get("launch_presentation_locale")
            if launch_locale:
                language_code = launch_locale.lower()
                from django.conf import settings
                cookie_name = getattr(settings, "LANGUAGE_COOKIE_NAME", "openedx-language-preference")
                
                # Forzar la cookie (eliminar cualquier otra)
                if cookie_name in response.cookies:
                    del response.cookies[cookie_name]
                
                response.set_cookie(
                    cookie_name, 
                    language_code,
                    domain=getattr(settings, "SESSION_COOKIE_DOMAIN", None),
                    max_age=None,
                    samesite="None",
                    secure=True
                )
                logger.info(f"[LTI] Cookie {cookie_name} set to: {language_code}")
        
        return response
