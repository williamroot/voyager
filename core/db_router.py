"""Database router pra rotear reads de jobs específicos pra replica.

Uso:
    from core.db_router import use_replica
    with use_replica():
        queries.compute_estatisticas_por_tribunal()  # reads vão pra replica

Writes sempre vão pro default — replica é hot standby read-only. Sem context
ativo, leituras também vão pro default (default safe). O router não roteia
nada se 'replica' não estiver em DATABASES.
"""
import contextlib
import threading

from django.conf import settings

_local = threading.local()


class ReplicaRouter:
    """Roteia reads pra 'replica' quando thread-local está ativo.

    Sessions e auth sempre leem do primário — lag na réplica causa
    AttributeError no SessionStore quando _session_cache não é setado.
    """

    _PRIMARY_ONLY_APPS = frozenset({'sessions', 'auth'})

    def db_for_read(self, model, **hints):
        if 'replica' not in settings.DATABASES:
            return None
        if model._meta.app_label in self._PRIMARY_ONLY_APPS:
            return None
        return getattr(_local, 'reads_alias', None)

    def db_for_write(self, model, **hints):
        return None  # writes sempre no default

    def allow_relation(self, *args, **kwargs):
        return True

    def allow_migrate(self, *args, **kwargs):
        return True


@contextlib.contextmanager
def use_replica():
    """Context manager: roteia reads ORM pra 'replica' até sair do bloco."""
    if 'replica' not in settings.DATABASES:
        # Sem replica configurada — vira no-op
        yield
        return
    prev = getattr(_local, 'reads_alias', None)
    _local.reads_alias = 'replica'
    try:
        yield
    finally:
        _local.reads_alias = prev
