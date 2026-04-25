from django.contrib.postgres.search import SearchQuery, SearchRank
from django_filters import rest_framework as filters

from tribunals.models import IngestionRun, Movimentacao, Process

MIN_SEARCH_LENGTH = 3


class ProcessFilter(filters.FilterSet):
    tribunal = filters.CharFilter(field_name='tribunal_id')
    tribunal__in = filters.BaseInFilter(field_name='tribunal_id')
    numero_cnj = filters.CharFilter(lookup_expr='exact')
    inserido_em__gte = filters.DateTimeFilter(field_name='inserido_em', lookup_expr='gte')
    inserido_em__lte = filters.DateTimeFilter(field_name='inserido_em', lookup_expr='lte')
    ultima_movimentacao_em__gte = filters.DateTimeFilter(field_name='ultima_movimentacao_em', lookup_expr='gte')
    ultima_movimentacao_em__lte = filters.DateTimeFilter(field_name='ultima_movimentacao_em', lookup_expr='lte')
    sem_movimentacoes = filters.BooleanFilter(method='filter_sem_movs')

    class Meta:
        model = Process
        fields = []

    def filter_sem_movs(self, qs, name, value):
        return qs.filter(total_movimentacoes=0) if value else qs.exclude(total_movimentacoes=0)


class MovimentacaoFilter(filters.FilterSet):
    tribunal = filters.CharFilter(field_name='tribunal_id')
    tribunal__in = filters.BaseInFilter(field_name='tribunal_id')
    processo = filters.NumberFilter(field_name='processo_id')
    numero_cnj = filters.CharFilter(field_name='processo__numero_cnj')
    data_disponibilizacao__gte = filters.DateTimeFilter(field_name='data_disponibilizacao', lookup_expr='gte')
    data_disponibilizacao__lte = filters.DateTimeFilter(field_name='data_disponibilizacao', lookup_expr='lte')
    inserido_em__gte = filters.DateTimeFilter(field_name='inserido_em', lookup_expr='gte')
    inserido_em__lte = filters.DateTimeFilter(field_name='inserido_em', lookup_expr='lte')
    tipo_comunicacao = filters.CharFilter(lookup_expr='iexact')
    nome_classe = filters.CharFilter(lookup_expr='iexact')
    codigo_classe = filters.CharFilter()
    q = filters.CharFilter(method='filter_search')

    class Meta:
        model = Movimentacao
        fields = []

    def filter_search(self, qs, name, value):
        value = (value or '').strip()
        if len(value) < MIN_SEARCH_LENGTH:
            return qs
        if len(value.split()) >= 3:
            query = SearchQuery(value, config='portuguese', search_type='websearch')
            return (
                qs.filter(search_vector=query)
                .annotate(rank=SearchRank('search_vector', query))
                .order_by('-rank', '-data_disponibilizacao')
            )
        # Para termos curtos, ILIKE %x% usa o índice GIN trigram (gin_trgm_ops).
        return qs.filter(texto__icontains=value).order_by('-data_disponibilizacao')


class IngestionRunFilter(filters.FilterSet):
    tribunal = filters.CharFilter(field_name='tribunal_id')
    status = filters.CharFilter()
    started_at__gte = filters.DateTimeFilter(field_name='started_at', lookup_expr='gte')

    class Meta:
        model = IngestionRun
        fields = []
