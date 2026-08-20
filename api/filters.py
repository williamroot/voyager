import datetime

from django.utils import timezone
from django_filters import rest_framework as filters
from rest_framework import status
from rest_framework.exceptions import APIException

from tribunals.models import IngestionRun, Movimentacao, Process

MIN_SEARCH_LENGTH = 3
JANELA_BUSCA_PADRAO_DIAS = 31


class BuscaIndisponivel(APIException):
    """503, não 500: o índice de texto caiu, não o nosso código.

    Importa para quem está de plantão — 500 entra na fila de "quebramos algo" e
    503 entra na de "dependência fora do ar", que é o que de fato aconteceu.
    """
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = 'Busca por texto indisponível: índice fora do ar.'
    default_code = 'busca_texto_indisponivel'


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
        """Busca no texto das publicações — pelo Elasticsearch, não pelo Postgres.

        MEDIDO em 20/08/2026, e os dois caminhos que estavam aqui eram ruins de
        formas diferentes:

        * `texto__icontains`: a coluna `texto` não tem índice de busca. O
          `mov_texto_trgm` está declarado no model e **ausente do banco** — em
          `tribunals_movimentacao` existem 9 índices e nenhum cobre `texto`.
          Sem recorte, EXPLAIN dá Seq Scan de custo 111.195.298 sobre 1,39
          bilhão de linhas (815 GB), no caminho da requisição.

        * `search_vector` + `SearchRank`: pior, porque respondia. A coluna
          existe, o índice GIN não, **e não há trigger que a preencha**. Por
          amostra: cheia nas linhas até `id≈4.876.372` (13/03/2024), NULL da
          metade da tabela em diante. São 2.753.688 linhas de 1.385.659.648 —
          **0,199% do acervo**. Uma busca de 3+ palavras varria 0,2% do país e
          devolvia "encontrei isto" sem uma palavra sobre os outros 99,8%.

        O índice que serve pra isto é o `voyager-movimentacoes-v2`, e foi
        exatamente pra isto que os 179.490.613 documentos que faltavam entraram
        nele em 18/08. O ES resolve o texto e devolve PKs; o Postgres só hidrata
        por chave primária.
        """
        from search.busca_api import BuscaIndisponivelError, ids_por_texto

        value = (value or '').strip()
        if len(value) < MIN_SEARCH_LENGTH:
            return qs

        tribunais = [t for t in (self.data.get('tribunal'),) if t]
        tribunais += [t for t in (self.data.get('tribunal__in') or '').split(',') if t]
        de = self.data.get('data_disponibilizacao__gte')
        ate = self.data.get('data_disponibilizacao__lte')
        janela_padrao = not de and not ate
        if janela_padrao:
            # Sem janela, a mediana medida no cluster é 8,07 s e o teto de espera
            # estoura com frequência — 1,4 bilhão de documentos não respondem
            # texto livre nacional dentro de uma requisição. A janela padrão é
            # ECOADA na resposta (`busca_janela`): estreitar em silêncio seria
            # entregar um recorte como se fosse o acervo.
            ate = timezone.localdate()
            de = ate - datetime.timedelta(days=JANELA_BUSCA_PADRAO_DIAS)
        try:
            achado = ids_por_texto(value, tribunais=tribunais, de=de, ate=ate)
        except BuscaIndisponivelError as e:
            # Cair pro Postgres seria trocar "não consigo responder" por uma
            # resposta errada — e ainda por cima varrendo 815 GB pra errar.
            raise BuscaIndisponivel from e

        if self.request is not None and janela_padrao:
            self.request.busca_janela = {
                'de': str(de), 'ate': str(ate), 'dias': JANELA_BUSCA_PADRAO_DIAS,
                'motivo': ('busca sem data_disponibilizacao__gte/__lte usa janela '
                           'padrão; passe as datas para ampliar'),
            }
        if self.request is not None and achado['truncado']:
            # Teto é alerta, nunca corte mudo: o viewset devolve isto no corpo.
            self.request.busca_teto = {
                'truncado': True,
                'devolvidas': len(achado['ids']),
                'ao_menos': achado['total'],
                'dica': 'filtre por tribunal ou data para estreitar a busca',
            }
        # a ordenação final é do CursorPagination (-data_disponibilizacao, -id)
        return qs.filter(pk__in=achado['ids'])


class IngestionRunFilter(filters.FilterSet):
    tribunal = filters.CharFilter(field_name='tribunal_id')
    status = filters.CharFilter()
    started_at__gte = filters.DateTimeFilter(field_name='started_at', lookup_expr='gte')

    class Meta:
        model = IngestionRun
        fields = []
