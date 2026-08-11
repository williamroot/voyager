"""API de Busca v1 — endpoints REST sobre o Elasticsearch (`search/busca_api.py`).

Contrato completo (params, shapes, exemplos) no docstring de `search/busca_api.py`
e em `.ia/API.md`. Auth igual aos demais endpoints externos (HasAPIKeyOrBearer).

Erros: 400 param inválido, 404 processo não encontrado, 503 ES fora — nunca 500 cru.
"""
import logging

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from search import busca_api
from search.busca_api import BuscaParamError

from .permissions import HasAPIKeyOrBearer

logger = logging.getLogger('voyager.api.busca')


def _erro_400(msg):
    return Response({'erro': msg}, status=status.HTTP_400_BAD_REQUEST)


def _erro_503(exc):
    logger.error('Busca ES indisponivel: %s', exc)
    return Response(
        {'erro': 'elasticsearch_indisponivel', 'detail': str(exc)},
        status=status.HTTP_503_SERVICE_UNAVAILABLE,
    )


@api_view(['GET'])
@permission_classes([HasAPIKeyOrBearer])
def busca_processo(request, cnj):
    """GET /api/v1/busca/processos/<cnj>/ — doc completo por CNJ exato."""
    try:
        doc = busca_api.get_processo(cnj)
    except BuscaParamError as e:
        return _erro_400(str(e))
    except Exception as e:
        return _erro_503(e)
    if doc is None:
        return Response({'erro': 'processo_nao_encontrado'},
                        status=status.HTTP_404_NOT_FOUND)
    cnj_norm = doc.get('proc') or busca_api.normalizar_cnj(cnj)
    return Response({
        'versao': busca_api.VERSAO,
        'processo': doc,
        'movimentacoes_url': f'/api/v1/busca/processos/{cnj_norm}/movimentacoes/',
    })


@api_view(['GET'])
@permission_classes([HasAPIKeyOrBearer])
def busca_processo_movimentacoes(request, cnj):
    """GET /api/v1/busca/processos/<cnj>/movimentacoes/?size=&cursor="""
    try:
        pagina = busca_api.movimentacoes_por_cnj(
            cnj,
            size=request.GET.get('size'),
            cursor=request.GET.get('cursor'),
        )
        # 404 limpo se o processo não existe (só na 1ª página — com cursor o
        # cliente já provou que existe; poupa 1 roundtrip por página).
        if not pagina['results'] and not request.GET.get('cursor'):
            if busca_api.get_processo(cnj) is None:
                return Response({'erro': 'processo_nao_encontrado'},
                                status=status.HTTP_404_NOT_FOUND)
    except BuscaParamError as e:
        return _erro_400(str(e))
    except Exception as e:
        return _erro_503(e)
    return Response(pagina)


@api_view(['GET'])
@permission_classes([HasAPIKeyOrBearer])
def busca_processos(request):
    """GET /api/v1/busca/processos/?parte=&advogado=&<filtros>&ordenar=&size=&cursor="""
    g = request.GET.get
    try:
        pagina = busca_api.buscar_processos(
            parte=(g('parte') or '').strip() or None,
            advogado=(g('advogado') or '').strip() or None,
            filtros=busca_api.parse_filtros_processos(request.GET),
            ordenar=g('ordenar'),
            size=g('size'),
            cursor=g('cursor'),
        )
    except BuscaParamError as e:
        return _erro_400(str(e))
    except Exception as e:
        return _erro_503(e)
    return Response(pagina)


@api_view(['GET'])
@permission_classes([HasAPIKeyOrBearer])
def busca_movimentacoes(request):
    """GET /api/v1/busca/movimentacoes/?q=&<filtros>&ordenar=&size=&cursor="""
    g = request.GET.get
    try:
        pagina = busca_api.buscar_movimentacoes(
            q=g('q'),
            filtros=busca_api.parse_filtros_movs(request.GET),
            ordenar=g('ordenar'),
            size=g('size'),
            cursor=g('cursor'),
        )
    except BuscaParamError as e:
        return _erro_400(str(e))
    except Exception as e:
        return _erro_503(e)
    return Response(pagina)
