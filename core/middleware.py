import uuid

from core.db_router import use_replica


class ReplicaReadMiddleware:
    """Roteia todas as leituras ORM pra réplica em paths configurados.

    Writes continuam no primário (ReplicaRouter.db_for_write retorna None).
    No-op se 'replica' não estiver em DATABASES.
    """

    REPLICA_PREFIXES = ('/dashboard/',)

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if any(request.path.startswith(p) for p in self.REPLICA_PREFIXES):
            with use_replica():
                return self.get_response(request)
        return self.get_response(request)


class RequestIdMiddleware:
    HEADER = 'HTTP_X_REQUEST_ID'
    RESPONSE_HEADER = 'X-Request-Id'

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = request.META.get(self.HEADER) or uuid.uuid4().hex
        request.request_id = request_id
        response = self.get_response(request)
        response[self.RESPONSE_HEADER] = request_id
        return response
