import uuid

from core.db_router import use_replica


class ReplicaReadMiddleware:
    """Roteia todas as leituras ORM pra réplica em paths configurados.

    Writes continuam no primário (ReplicaRouter.db_for_write retorna None).
    No-op se 'replica' não estiver em DATABASES.

    Paths em REPLICA_EXCLUSIONS são excluídos mesmo que batam um prefix —
    usados para páginas que mostram dados recém-escritos (detalhe de processo,
    endpoints de enriquecimento/sincronização on-demand).
    """

    REPLICA_PREFIXES = ('/dashboard/',)
    REPLICA_EXCLUSIONS = (
        '/dashboard/processos/',
        '/dashboard/leads/',
        '/dashboard/api/leads/',
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        uses_prefix = any(request.path.startswith(p) for p in self.REPLICA_PREFIXES)
        is_excluded = any(request.path.startswith(p) for p in self.REPLICA_EXCLUSIONS)
        if uses_prefix and not is_excluded:
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
