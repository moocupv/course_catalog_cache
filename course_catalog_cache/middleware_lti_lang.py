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
        # Y verificar que session y user estén disponibles
        if (request.method == "POST" 
            and "oauth_consumer_key" in request.POST 
            and hasattr(request, 'session')
            and hasattr(request, 'user')):
            
            launch_locale = request.POST.get("launch_presentation_locale")
            
            if launch_locale:
                language_code = launch_locale.lower()  # "en-us"
                base_language = language_code.split("-")[0]  # "en"
                
                # Establecer en la sesión
                request.session["django_language"] = base_language
                
                logger.info(f"[LTI] Language set to: {language_code} (base: {base_language})")
                
                # Actualizar preferencia del usuario INMEDIATAMENTE
                if request.user.is_authenticated:
                    try:
                        from openedx.core.djangoapps.user_api.preferences.api import set_user_preference, get_user_preference
                        
                        # Guardar preferencia original si no existe
                        current_lang = get_user_preference(request.user, "pref-lang")
                        if current_lang != base_language:
                            request.session["lti_original_language"] = current_lang
                            logger.info(f"[LTI] Saved original language: {current_lang}")
                        
                        # Establecer idioma del LTI ANTES de que otros middlewares lo lean
                        set_user_preference(request.user, "pref-lang", base_language)
                        logger.info(f"[LTI] User {request.user.username} pref-lang set to: {base_language}")
                    except Exception as e:
                        logger.warning(f"[LTI] Could not set user preference: {e}")
                
        response = self.get_response(request)
        
        # Establecer cookie en la respuesta
        if (request.method == "POST" 
            and "oauth_consumer_key" in request.POST
            and hasattr(request, 'session')):
            
            launch_locale = request.POST.get("launch_presentation_locale")
            if launch_locale:
                language_code = launch_locale.lower()
                from django.conf import settings
                cookie_name = getattr(settings, "LANGUAGE_COOKIE_NAME", "openedx-language-preference")
                
                response.set_cookie(
                    cookie_name, 
                    language_code,
                    domain=getattr(settings, "SESSION_COOKIE_DOMAIN", None),
                    max_age=90 * 24 * 60 * 60,
                    samesite="None",
                    secure=True
                )
                logger.info(f"[LTI] Cookie {cookie_name} set to: {language_code}")
        
        return response
