from rest_framework.pagination import CursorPagination, LimitOffsetPagination


class DefaultPagination(LimitOffsetPagination):
    default_limit = 50
    max_limit = 200


class MovimentacaoCursorPagination(CursorPagination):
    page_size = 50
    max_page_size = 200
    page_size_query_param = 'page_size'
    ordering = ('-data_disponibilizacao', '-id')
