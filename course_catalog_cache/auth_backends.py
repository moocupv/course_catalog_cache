from django.contrib.auth.backends import ModelBackend

class LtiProviderProxyBackend(ModelBackend):
    """
    Import-safe backend: third_party_auth imports AUTHENTICATION_BACKENDS
    during Django app loading, so do not import edx-platform models at import time.
    """
    def authenticate(self, request=None, username=None, lti_user_id=None, lti_consumer=None, **kwargs):
        if not (lti_user_id and lti_consumer):
            return None

        # Lazy import (only when authenticate() is called; apps already loaded)
        from lms.djangoapps.lti_provider.models import LtiUser

        try:
            lti_user = LtiUser.objects.select_related("edx_user").get(
                lti_user_id=lti_user_id,
                lti_consumer=lti_consumer,
            )
        except LtiUser.DoesNotExist:
            return None

        return lti_user.edx_user
