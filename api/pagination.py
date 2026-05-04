from django.db import connection as _db_connection
from rest_framework.pagination import CursorPagination, LimitOffsetPagination


class DefaultPagination(LimitOffsetPagination):
    default_limit = 50
    max_limit = 200


class ProcessPagination(LimitOffsetPagination):
    """LimitOffsetPagination que evita SELECT COUNT(*) seq scan em Process.

    Para querysets sem filtros ativos usa reltuples do pg_class como estimativa
    (~ms) em vez de COUNT(*) (~segundos em 500k+ rows).
    """
    default_limit = 50
    max_limit = 200

    def get_count(self, queryset):
        if not queryset.query.where:
            with _db_connection.cursor() as cur:
                cur.execute(
                    "SELECT reltuples::bigint FROM pg_class WHERE relname='tribunals_process'"
                )
                row = cur.fetchone()
                if row and row[0] and int(row[0]) > 0:
                    return int(row[0])
        return super().get_count(queryset)


class MovimentacaoCursorPagination(CursorPagination):
    page_size = 50
    max_page_size = 200
    page_size_query_param = 'page_size'
    ordering = ('-data_disponibilizacao', '-id')
