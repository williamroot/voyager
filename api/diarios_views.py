"""API Jusbrasil/Digesto-compat: endpoints de Diários Oficiais."""
import logging

from django.http import JsonResponse
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from tribunals.models import FonteDiario, Movimentacao, Tribunal
from tribunals.tipos_norm import TIPOS_NORM

from .permissions import HasAPIKeyOrBearer

logger = logging.getLogger('voyager.api.diarios')

MAX_SIZE = 10000
MAX_FROM = 10000


@api_view(['POST'])
@permission_classes([HasAPIKeyOrBearer])
def diario_buscar(request):
    """POST /api/v1/diarios-oficiais/doc/buscar
    Body = query DSL Elasticsearch 7.14 (passthrough pro cluster).
    Response = envelope ES nativo.
    """
    try:
        body = request.data if hasattr(request, 'data') else {}
    except Exception:
        return Response({'error': 'invalid_json'}, status=status.HTTP_400_BAD_REQUEST)

    if not isinstance(body, dict):
        return Response({'error': 'body_deve_ser_objeto'}, status=status.HTTP_400_BAD_REQUEST)

    frm = body.get('from', 0)
    size = body.get('size', 10)
    query = body.get('query')

    if query is None:
        return Response({'error': 'query_obrigatorio'}, status=status.HTTP_400_BAD_REQUEST)
    if not isinstance(frm, int) or frm < 0:
        return Response({'error': 'from_invalido'}, status=status.HTTP_400_BAD_REQUEST)
    if not isinstance(size, int) or size < 1:
        return Response({'error': 'size_invalido'}, status=status.HTTP_400_BAD_REQUEST)
    if frm + size > MAX_FROM:
        return Response({'error': 'from_plus_size_excede_10000'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        from search.client import get_es, index_name
        es = get_es()
        # Garante que a resposta tenha _type pra compat Jusbrasil.
        es_body = dict(body)
        es_body.setdefault('from', frm)
        es_body.setdefault('size', size)
        result = es.search(index=index_name('movimentacoes'), body=es_body)

        # Converte pra dict puro (ES 8 retorna AttrDict que JsonResponse não serializa).
        if hasattr(result, 'body'):
            result = dict(result.body)
        elif not isinstance(result, dict):
            result = dict(result)

        # Injeta _type em cada hit (ES 8 não retorna _type).
        for hit in result.get('hits', {}).get('hits', []):
            hit.setdefault('_type', '_doc')
            hit.setdefault('_index', index_name('movimentacoes'))

        return JsonResponse(result)
    except Exception as e:
        logger.error('Erro na busca ES: %s', e)
        return Response(
            {'error': 'elasticsearch_unavailable', 'detail': str(e)},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )


@api_view(['GET'])
@permission_classes([HasAPIKeyOrBearer])
def diario_get(request, doc_id: int):
    """GET /api/v1/diarios-oficiais/doc/get/<id>
    Retorna envelope {found, _source} compatível com Jusbrasil.
    found=false (não 404) quando o doc não existe.
    """
    try:
        from search.client import get_es, index_name
        es = get_es()
        result = es.get(index=index_name('movimentacoes'), id=doc_id, ignore=[404])
    except Exception as e:
        logger.error('Erro no get ES: %s', e)
        return Response(
            {'error': 'elasticsearch_unavailable', 'detail': str(e)},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    # Converte pra dict puro (ES 8 retorna AttrDict).
    if hasattr(result, 'body'):
        result = dict(result.body)
    elif not isinstance(result, dict):
        result = dict(result)

    if result.get('found', False):
        source = result['_source']
        # Adiciona cached_docurl se PDF disponível.
        try:
            mov = Movimentacao.objects.get(pk=doc_id)
            from pdf_storage.cached_docurl import cached_docurl_for
            cached = cached_docurl_for(mov)
            if cached:
                source['cached_docurl'] = cached
        except Movimentacao.DoesNotExist:
            pass
        return JsonResponse({
            '_index': result.get('_index', index_name('movimentacoes')),
            '_type': '_doc',
            '_id': str(doc_id),
            '_version': result.get('_version', 1),
            'found': True,
            '_source': source,
        })
    return JsonResponse({
        '_index': index_name('movimentacoes'),
        '_type': '_doc',
        '_id': str(doc_id),
        'found': False,
    })


@api_view(['GET'])
@permission_classes([HasAPIKeyOrBearer])
def fontes_recortes(request):
    """GET /api/v1/diarios-oficiais/fontes_recortes
    Retorna dict {source_id: nome, ...} das fontes ativas.
    """
    fontes = FonteDiario.objects.select_related('tribunal').all()
    return JsonResponse({str(f.source_id): f.nome for f in fontes})


@api_view(['GET'])
@permission_classes([HasAPIKeyOrBearer])
def tipos_norm(request):
    """GET /api/v1/monitoramento/proc/tipos_norm_andamentos_movs
    Retorna lista [[tipo, subtipo, "nome"], ...] da fixture estática.
    """
    return JsonResponse([[t, s, nome] for t, s, nome in TIPOS_NORM], safe=False)


@api_view(['GET'])
@permission_classes([HasAPIKeyOrBearer])
def status_cobertura(request):
    """GET /api/base-judicial/tribproc/status_cobertura?area=<area>
    Retorna matriz de cobertura por área.
    """
    area = request.GET.get('area', '')
    validas = {'estadual', 'trabalhista', 'federal', 'superior'}
    if area not in validas:
        return Response(
            {'error': 'area_invalida', 'validas': list(validas)},
            status=status.HTTP_400_BAD_REQUEST,
        )

    tribunais = Tribunal.objects.filter(ativo=True).order_by('sigla')
    values = [['Tribunal', 'Estado(s)', 'Sistema', 'Instância', 'Distribuição',
               'Anexos', 'Andamentos', 'Publicações', 'Info']]
    for t in tribunais:
        tem_fonte = FonteDiario.objects.filter(tribunal=t).exists()
        values.append([
            t.sigla, '', 'DJEN', '', 'Não', 'Não', 'Sim',
            'Sim' if tem_fonte else 'Parcial', '',
        ])

    return JsonResponse({
        'majorDimension': 'ROWS',
        'range': f'{area.capitalize()}!A1:J{len(values)}',
        'values': values,
    })