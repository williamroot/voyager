"""API da busca POR PARTE ao vivo na consulta pública (`/api/v1/busca/tribunal/`).

Assíncrona por natureza, não por gosto: uma consulta ao e-SAJ vai de 0,2 s a
71 s, um POST no PJe leva ~20 s, e uma busca "em todos" fala com nove fontes.
Responder síncrono obrigaria a cortar no meio e chamar isso de resultado.

    POST /api/v1/busca/tribunal/          cria a busca  -> 202 + run_id
    GET  /api/v1/busca/tribunal/<run_id>/ lê o andamento e os parciais
    GET  /api/v1/busca/tribunal/catalogo/ o que cada fonte aceita, e desde quando

A resposta é desenhada para NÃO deixar ninguém confundir ausência de dado com
ausência de processo: cada tribunal aparece com o próprio estado, e "a fonte não
tem esse critério", "a fonte pediu para refinar", "a fonte estava fora" e
"nenhum processo" são quatro coisas diferentes, ditas com quatro palavras
diferentes.
"""
import logging

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from enrichers.busca.base import ROTULOS
from enrichers.busca.entrada import EntradaInvalida, validar
from enrichers.busca.jobs import iniciar
from enrichers.busca.registry import CATALOGO, catalogo_publico, foi_medido
from tribunals.models import BuscaTribunalRun

from .permissions import HasAPIKeyOrBearer

logger = logging.getLogger('voyager.api.busca_tribunal')

#: Janela em que a MESMA pergunta é respondida com a resposta anterior, em vez
#: de gastar IP do pool compartilhado para chegar ao mesmo lugar. Curta de
#: propósito: processo novo aparece o tempo todo, e a promessa desta API é
#: justamente falar com a fonte.
CACHE_HORAS = 6

#: Buscas por minuto por cliente de API. Cada uma pode virar nove jobs de
#: scraping, então o teto aqui é o que impede uma tela de drenar o pool que o
#: enriquecimento em massa também usa.
RATE_POR_MINUTO = 20


def _limite_ok(request) -> bool:
    """Token-bucket simples por ApiClient, fail-open (mesma política do MCP:
    Redis fora não pode virar 500 numa API de leitura)."""
    cliente = getattr(request, 'api_client', None)
    if not cliente:
        return True
    chave = f'busca_tribunal:rate:{cliente.pk}'
    try:
        usados = cache.get(chave, 0)
        if usados >= RATE_POR_MINUTO:
            return False
        cache.set(chave, usados + 1, timeout=60)
    except Exception as exc:
        logger.warning('rate limit indisponível (fail-open): %s', exc)
    return True


def _tribunais_pedidos(bruto) -> list[str]:
    """Normaliza o `tribunais` do corpo. `todos`/vazio = o catálogo inteiro.

    Os que não aceitam o critério NÃO são filtrados aqui: eles entram e são
    recusados com motivo na resposta. Sumir com eles em silêncio faria a busca
    "em todos" parecer ter olhado onde não olhou.
    """
    if not bruto or bruto in ('todos', ['todos']):
        return sorted(CATALOGO)
    if isinstance(bruto, str):
        bruto = [t.strip() for t in bruto.split(',')]
    return [str(t).strip().upper() for t in bruto if str(t).strip()]


def _resposta_do_run(run: BuscaTribunalRun, em_cache: bool = False) -> dict:
    return {
        'versao': 'busca-tribunal-1',
        'run_id': str(run.pk),
        'status': run.status,
        'criterio': run.criterio,
        'rotulo_criterio': ROTULOS.get(run.criterio, run.criterio),
        'valor': run.valor,
        'tribunais': run.tribunais,
        'por_tribunal': run.por_tribunal,
        'resultados': run.resultados,
        'encontrados': run.encontrados,
        'novos_no_acervo': run.novos_no_acervo,
        'erros': run.erros,
        'avisos': _avisos(run),
        'em_cache': em_cache,
        'criado_em': run.criado_em.isoformat(),
        'finalizado_em': run.finalizado_em.isoformat() if run.finalizado_em else None,
    }


def _avisos(run: BuscaTribunalRun) -> list[dict]:
    """Frases prontas para a tela, uma por armadilha ATIVA nesta busca.

    Esta é a parte da resposta que impede a leitura errada. Sem ela, "0
    resultados no TJPA" e "o TJPA não busca por nome de advogado" chegam na
    tela com a mesma cara.
    """
    avisos = []
    for sigla, bruto in (run.por_tribunal or {}).items():
        estado = bruto or {}
        situacao = estado.get('status')
        if situacao == 'criterio_indisponivel':
            avisos.append({'codigo': 'criterio_indisponivel', 'tribunal': sigla,
                           'mensagem': estado.get('mensagem') or
                           f'{sigla} não oferece este critério na consulta pública'})
        elif situacao == 'refinar':
            avisos.append({'codigo': 'refinar', 'tribunal': sigla,
                           'mensagem': estado.get('mensagem') or
                           f'{sigla} pediu uma busca mais específica'})
        elif situacao == 'fonte_indisponivel':
            avisos.append({'codigo': 'fonte_indisponivel', 'tribunal': sigla,
                           'mensagem': f'{sigla} não respondeu agora — '
                                       f'"nenhum processo" aqui não é resposta da fonte'})
        if estado.get('truncado'):
            avisos.append({
                'codigo': 'truncado', 'tribunal': sigla,
                'mensagem': (f'{sigla}: li {estado.get("encontrados", 0)} '
                             f'processos, mas {estado.get("motivo_truncagem") or "há mais"}'
                             + (f' (a fonte declara {estado["total_declarado"]})'
                                if estado.get('total_declarado') else ''))})
        if estado.get('aviso_fonte'):
            avisos.append({'codigo': 'fonte_inconsistente', 'tribunal': sigla,
                           'mensagem': f'{sigla}: {estado["aviso_fonte"]}'})
        # "Nunca conferimos" é por (tribunal, critério), e não por tribunal: o
        # motor do PJe oferece os quatro critérios em toda instalação, mas no
        # TRF1 só documento e nome foram exercitados ao vivo. Zero numa fonte
        # nunca exercitada não é fato sobre a pessoa buscada.
        if situacao != 'criterio_indisponivel' and not foi_medido(sigla, run.criterio):
            avisos.append({
                'codigo': 'nao_verificado', 'tribunal': sigla,
                'mensagem': (f'{sigla}: a busca por '
                             f'{ROTULOS.get(run.criterio, run.criterio)} nunca '
                             f'foi conferida ao vivo nesta fonte')})
    return avisos


def _run_em_cache(criterio: str, normalizado: str, tribunais: list[str]):
    """Mesma pergunta, mesmo escopo, respondida há pouco."""
    desde = timezone.now() - timezone.timedelta(hours=CACHE_HORAS)
    candidatos = BuscaTribunalRun.objects.filter(
        criterio=criterio, valor_normalizado=normalizado,
        status=BuscaTribunalRun.STATUS_CONCLUIDO, criado_em__gte=desde,
    ).order_by('-criado_em')[:10]
    for run in candidatos:
        if sorted(run.tribunais or []) == sorted(tribunais):
            return run
    return None


@api_view(['POST'])
@permission_classes([HasAPIKeyOrBearer])
def criar_busca(request):
    """`{criterio, valor, tribunais?, forcar?}` -> 202 com o `run_id`."""
    if not _limite_ok(request):
        return Response(
            {'erro': 'rate_limit', 'mensagem':
             f'Máximo de {RATE_POR_MINUTO} buscas por minuto. '
             f'Cada busca fala com até {len(CATALOGO)} tribunais.'},
            status=status.HTTP_429_TOO_MANY_REQUESTS)

    corpo = request.data if isinstance(request.data, dict) else {}
    try:
        entrada = validar(corpo.get('criterio'), corpo.get('valor'))
    except EntradaInvalida as exc:
        return Response({'erro': exc.codigo, 'mensagem': exc.mensagem},
                        status=status.HTTP_400_BAD_REQUEST)

    tribunais = _tribunais_pedidos(corpo.get('tribunais'))
    desconhecidos = [t for t in tribunais if t not in CATALOGO]
    if desconhecidos:
        return Response({
            'erro': 'tribunal_sem_busca',
            'mensagem': (f'Sem busca por parte em: {", ".join(desconhecidos)}. '
                         f'Disponíveis: {", ".join(sorted(CATALOGO))}.'),
        }, status=status.HTTP_400_BAD_REQUEST)

    if not corpo.get('forcar'):
        anterior = _run_em_cache(entrada['criterio'], entrada['normalizado'], tribunais)
        if anterior:
            return Response(_resposta_do_run(anterior, em_cache=True),
                            status=status.HTTP_200_OK)

    run = BuscaTribunalRun.objects.create(
        criterio=entrada['criterio'],
        valor=entrada['valor'],
        valor_normalizado=entrada['normalizado'],
        tribunais=tribunais,
        api_client=getattr(request, 'api_client', None),
    )
    saida = iniciar(run)
    run.refresh_from_db()
    logger.info('busca criada', extra={'run': str(run.pk), 'criterio': run.criterio,
                                       'tribunais': len(tribunais),
                                       'recusados': len(saida['recusados'])})
    return Response(_resposta_do_run(run), status=status.HTTP_202_ACCEPTED)


@api_view(['GET'])
@permission_classes([HasAPIKeyOrBearer])
def ler_busca(request, run_id):
    """Andamento e parciais. Pode ser chamado enquanto os jobs ainda rodam."""
    try:
        run = BuscaTribunalRun.objects.get(pk=run_id)
    except (BuscaTribunalRun.DoesNotExist, ValueError, TypeError):
        return Response({'erro': 'busca_nao_encontrada'},
                        status=status.HTTP_404_NOT_FOUND)
    return Response(_resposta_do_run(run))


@api_view(['GET'])
@permission_classes([HasAPIKeyOrBearer])
def catalogo(request):
    """O que cada fonte aceita, o teto dela, e a data em que foi conferida.

    Quem monta o seletor da tela é o SERVIDOR: `criterios` diz o que oferecer
    por tribunal, e `verificado_em: null` avisa que aquela linha é herança do
    motor, não medição.
    """
    return Response({
        'versao': 'busca-tribunal-1',
        'criterios': [{'id': cid, 'rotulo': rot} for cid, rot in ROTULOS.items()],
        'tribunais': catalogo_publico(),
        'cache_horas': CACHE_HORAS,
        'rate_por_minuto': RATE_POR_MINUTO,
        'debug': settings.DEBUG,
    })
