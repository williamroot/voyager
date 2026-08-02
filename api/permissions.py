"""Permission custom: aceita Bearer, Api-Key e X-API-Key."""
from rest_framework.permissions import BasePermission

from tribunals.models import ApiClient


class HasAPIKeyOrBearer(BasePermission):
    """Aceita 3 formas de auth:
    1. Authorization: Bearer <token>  (lookup ApiClient.api_key)
    2. Authorization: Api-Key <key>   (rest_framework_api_key padrão)
    3. X-API-Key: <key>              (legacy leads)
    """

    def has_permission(self, request, view):
        # 1. Bearer
        auth = request.META.get('HTTP_AUTHORIZATION', '')
        if auth.startswith('Bearer '):
            token = auth[7:].strip()
            if ApiClient.objects.filter(api_key=token, ativo=True).exists():
                request.api_client = ApiClient.objects.get(api_key=token, ativo=True)
                return True
            return False

        # 2. X-API-Key (legacy)
        xkey = request.META.get('HTTP_X_API_KEY', '')
        if xkey:
            if ApiClient.objects.filter(api_key=xkey, ativo=True).exists():
                request.api_client = ApiClient.objects.get(api_key=xkey, ativo=True)
                return True
            return False

        # 3. Api-Key (rest_framework_api_key) — delega pro permission padrão
        from rest_framework_api_key.permissions import HasAPIKey
        return HasAPIKey().has_permission(request, view)