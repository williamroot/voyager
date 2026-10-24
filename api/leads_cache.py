"""Cache pré-computado da listagem de leads (`GET /api/v1/leads/`).

## Por que existe

A listagem roda sob `LEADS_SQL_TIMEOUT_SECONDS` (20 s) porque está no caminho
da requisição (regra nº 7). Em 13/10/2026, nos 13 dias anteriores, o pull das
08:00 do Juriscope falhou com 503 em **10 de 13 dias no TRF3** e **12 de 13 no
TJSP**. O plano é o certo (`proc_leads_api_idx`), o que custa é o QUE vem na
cabeça dele: leads já consumidos que ganharam movimentação recente continuam no
topo da ordenação `(-ultima_movimentacao_em, -classificacao_score, -id)` e o
`NOT IN` do `LeadConsumption` os descarta um a um. Medido no TRF3 nível 2,
limit 1.800: **151.060 linhas removidas pelo filtro, 73.141 heap fetches, 9,2 s
quente** — e às 08:00, frio, passa dos 20 s. A `tribunals_process` tem 130 M
linhas e só ~50% all-visible: o custo cresce todo dia e VACUUM não resolve.

## O que faz

Um job RQ fora da requisição (sem teto de HTTP; teto PRÓPRIO de 120 s por
combinação) roda a MESMA consulta da listagem e guarda no Redis a lista
ordenada de ids pendentes (até 10.000) por cliente x combinação
`(nivel, tribunal, min_score, devedor_publico)`. A listagem serve desse cache
quando ele é recente (< 2 h): tira os consumidos DEPOIS do cálculo (uma consulta
barata no índice `(cliente_id, processo_id)`), re-confere classificação, score,
tribunal e devedor nas linhas escolhidas, mantém a ordem e devolve até `limit`.
Sem cache recente, cai na consulta ao vivo de sempre (com o 503 de sempre).

Quais combinações: as que o cliente PEDIU recentemente (cada chamada da API
registra a combinação num hash do Redis) mais as sementes do Juriscope
(`LEADS_CACHE_SEMENTES_*`), para que o primeiro pull depois do deploy já ache
cache. Quais clientes: `ApiClient` ativo que consumiu nos últimos 30 dias — o
cliente 1 (`juriscope`) não consome desde maio e não gasta banco.

## Teto é alerta, nunca corte mudo

O cache guarda no máximo `TETO_IDS`. Se ele bateu o teto (`completo=False`) e,
depois de tirar os consumidos, sobrou menos que o `limit` pedido, a resposta diz
`cache.esgotado=true, cache.completo=false` e o log sai em WARNING: pode haver
lead além do que foi guardado. Esgotar com `completo=True` é só "não há mais
leads" — também dito na resposta.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import timedelta

import django_rq
from django.conf import settings
from django.core.cache import cache
from django.db import OperationalError, connection, transaction
from django.db.models import Q
from django.utils import timezone
from django_rq import job

from tribunals.classificador import CLASSES_FAZENDA_PUBLICA
from tribunals.models import ApiClient, LeadConsumption, PrecatorioFilaOficial, Process

logger = logging.getLogger('voyager.api.leads_cache')

#: Teto de ids guardados por combinação. É o mesmo teto do `limit` da API: um
#: pedido nunca precisa de mais que isto de uma vez.
TETO_IDS = 10_000

_VERSAO = 'v1'
_CACHE_KEY = 'leads:cache:' + _VERSAO + ':%s:%s'      # cliente_id, combo
_COMBOS_KEY = 'leads:combos:' + _VERSAO + ':%s'       # cliente_id -> {combo: epoch}
_ULTIMA_EXECUCAO_KEY = 'leads:cache:' + _VERSAO + ':ultima_execucao'
_LOCK_KEY = 'lock:leads_cache_precomputo'
#: Kill switch sem deploy: com esta chave no Redis, a listagem ignora o cache.
_DESLIGADO_KEY = 'leads:cache:' + _VERSAO + ':desligado'

#: Combinação esquecida depois disto sai do hash (não é mais pré-computada).
_COMBO_ESQUECER_DIAS = 30

#: Campos que a resposta usa — `.only()` evita trazer o JSONB do processo.
_CAMPOS_RESPOSTA = (
    'id', 'numero_cnj', 'tribunal_id', 'classificacao', 'classificacao_score',
    'classe_nome', 'classe_codigo', 'ano_cnj', 'classificacao_em',
    'classificacao_versao',
)


# ---------------------------------------------------------------------------
# Combinação (a chave do cache)
# ---------------------------------------------------------------------------
def chave_combo(nivel: str, tribunal: str, min_score: float,
                devedor_publico: bool) -> str:
    """`'PRECATORIO|TRF3|0.7000|0'` — normalizada para 0.7 e 0.70 caírem juntas."""
    return (f'{(nivel or "").upper()}|{(tribunal or "").upper() or "*"}|'
            f'{float(min_score):.4f}|{1 if devedor_publico else 0}')


def ler_combo(chave: str) -> tuple[str, str, float, bool]:
    nivel, tribunal, score, dp = chave.split('|')
    return nivel, ('' if tribunal == '*' else tribunal), float(score), dp == '1'


def _sementes() -> list[str]:
    """Combinações do pull diário do Juriscope (`datamodel/tasks.py` deles)."""
    out = []
    for trib in getattr(settings, 'LEADS_CACHE_SEMENTES_TRIBUNAIS', ()):
        out.append(chave_combo('PRECATORIO', trib,
                               settings.LEADS_CACHE_SEMENTE_SCORE_N1, False))
        out.append(chave_combo('PRE_PRECATORIO', trib,
                               settings.LEADS_CACHE_SEMENTE_SCORE_N2, False))
    for trib in getattr(settings, 'LEADS_CACHE_SEMENTES_DC_TRIBUNAIS', ()):
        out.append(chave_combo('DIREITO_CREDITORIO', trib,
                               settings.LEADS_CACHE_SEMENTE_SCORE_N3, True))
    return out


# ---------------------------------------------------------------------------
# A consulta (uma só, usada pelo ao vivo e pelo pré-cálculo)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Trava do precatório de 2º grau das filas oficiais (TJMT, TJPA)
# ---------------------------------------------------------------------------
# O JURISCOPE não lê o PJe de 2º grau do TJMT nem o do TJPA (as contas de
# mineração não têm cadastro lá: SSO "usuário ou senha inválido" no client
# `pje-tjmt-2g`/`pje-tjpa-2g`, medido em 23/10/2026). O precatório de 2º grau
# que a fila oficial promove a PRECATORIO continua PRECATORIO (contagem, tela,
# stats e gate o veem) e só NÃO sai no pull — senão queimaria a cota de
# consulta do tribunal tentando um processo que o leitor não abre.
#
# Uma lista, por tribunal: `FILA_OFICIAL_2G_PULL_LIBERADO` (padrão vazio = não
# serve o 2º grau de nenhuma fila oficial). Tribunal com fila oficial de 2º grau
# entra em `SUFIXO_PRECATORIO_2G` já travado; liberar = pôr a sigla na lista
# quando o JURISCOPE tiver o leitor. `TJMT_PRECATORIO_2G_NO_PULL=True` continua
# valendo como "TJMT liberado" (compatibilidade com a flag do PR do TJMT).
#: Sufixo do CNJ do precatório autuado no PJe 2º grau (foro 0000) por tribunal.
SUFIXO_PRECATORIO_2G = {'TJMT': '.8.11.0000', 'TJPA': '.8.14.0000'}
SUFIXO_TJMT_2G = SUFIXO_PRECATORIO_2G['TJMT']


def tribunais_2g_liberados() -> set[str]:
    lib = {str(t).strip().upper() for t in
           getattr(settings, 'FILA_OFICIAL_2G_PULL_LIBERADO', []) or [] if str(t).strip()}
    if getattr(settings, 'TJMT_PRECATORIO_2G_NO_PULL', False):
        lib.add('TJMT')
    return lib


def tribunais_2g_travados(tribunal: str) -> list[str]:
    """Tribunais cujo 2º grau da fila oficial fica fora DESTE pedido: todos os
    travados para `tribunal` vazio; só ele (se travado) para um tribunal."""
    liberados = tribunais_2g_liberados()
    trav = [t for t in SUFIXO_PRECATORIO_2G if t not in liberados]
    return [t for t in trav if t == tribunal] if tribunal else trav


def trava_tjmt_2g_ativa(tribunal: str) -> bool:
    """Compatibilidade: a trava do TJMT vale para o pedido?"""
    return 'TJMT' in tribunais_2g_travados(tribunal)


def _ids_2g_da_fila(travados: list[str]):
    """Processos de 2º grau que a fila oficial dos tribunais travados conhece.
    Subconsulta pelo índice de `tribunal_id`."""
    q = Q()
    for t in travados:
        q |= Q(tribunal_id=t, chave__endswith=SUFIXO_PRECATORIO_2G[t])
    return (PrecatorioFilaOficial.objects.filter(q, processo__isnull=False)
            .values('processo_id'))


def montar_queryset(cliente_id: int, nivel: str, tribunal: str, min_score: float,
                    devedor_publico: bool, incluir_consumidos: bool = False):
    """O recorte da listagem. Ao vivo e pré-cálculo TÊM de usar este, senão o
    cache serve uma ordem/um filtro diferente do que a API promete."""
    qs = (
        Process.objects.filter(classificacao=nivel,
                               classificacao_score__gte=min_score)
        .select_related('tribunal')
        .order_by('-ultima_movimentacao_em', '-classificacao_score', '-id')
    )
    if tribunal:
        qs = qs.filter(tribunal_id=tribunal)
    if devedor_publico:
        qs = qs.filter(
            Q(classe_codigo__in=CLASSES_FAZENDA_PUBLICA)
            | Q(classe_nome__icontains='fazenda públ')
            | Q(classe_nome__icontains='fazenda pub')
        )
    if not incluir_consumidos:
        # Qualquer registro em LeadConsumption para esse cliente x processo conta.
        consumidos = LeadConsumption.objects.filter(
            cliente_id=cliente_id).values('processo_id')
        qs = qs.exclude(pk__in=consumidos)
    travados = tribunais_2g_travados(tribunal)
    if travados:
        # O JURISCOPE não lê o PJe 2º grau destes tribunais: entregar estes
        # processos queimaria a cota de consulta. Continuam PRECATORIO
        # (contagem, tela, stats e gate os veem); só não saem no pull.
        qs = qs.exclude(pk__in=_ids_2g_da_fila(travados))
    return qs


def e_devedor_publico(classe_codigo: str | None, classe_nome: str | None) -> bool:
    """O mesmo predicado de `montar_queryset(devedor_publico=True)`, em Python
    (o `icontains` do Postgres é `UPPER(x) LIKE UPPER('%..%')`)."""
    if (classe_codigo or '') in CLASSES_FAZENDA_PUBLICA:
        return True
    nome = (classe_nome or '').lower()
    return 'fazenda públ' in nome or 'fazenda pub' in nome


def executar_com_teto(fn, segundos: int):
    """Roda `fn()` sob `statement_timeout` de verdade.

    `SET LOCAL` dentro de `transaction.atomic()` porque o pgbouncer roda em
    transaction-mode: um `SET` solto vale para uma conexão e a query seguinte
    sai por outra. Padrão da casa (`djen/jobs.py::_leitura_com_teto`).
    """
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute('SET LOCAL statement_timeout = %s', [int(segundos * 1000)])
        return fn()


# ---------------------------------------------------------------------------
# Registro dos pedidos
# ---------------------------------------------------------------------------
def _conn():
    return django_rq.get_connection('default')


def registrar_pedido(cliente_id: int, combo: str) -> None:
    """Anota que o cliente pediu esta combinação agora. Best-effort: o registro
    não pode derrubar a listagem."""
    try:
        _conn().hset(_COMBOS_KEY % cliente_id, combo, int(time.time()))
    except Exception:
        logger.warning('leads_cache: falha ao registrar pedido',
                       extra={'cliente_id': cliente_id, 'combo': combo})


def combos_do_cliente(cliente_id: int) -> list[str]:
    """Pedidas nos últimos `LEADS_CACHE_COMBO_DIAS` + sementes, sem repetição.

    As pedidas vêm primeiro (as mais recentes antes): se o orçamento de tempo
    do job acabar, o que fica de fora é o que ninguém pediu.
    """
    agora = time.time()
    janela = settings.LEADS_CACHE_COMBO_DIAS * 86400
    esquecer = _COMBO_ESQUECER_DIAS * 86400
    pedidas: list[tuple[float, str]] = []
    try:
        conn = _conn()
        cru = conn.hgetall(_COMBOS_KEY % cliente_id) or {}
        velhas = []
        for k, v in cru.items():
            combo = k.decode() if isinstance(k, bytes) else k
            try:
                quando = float(v)
            except (TypeError, ValueError):
                continue
            if agora - quando > esquecer:
                velhas.append(combo)
            elif agora - quando <= janela:
                pedidas.append((quando, combo))
        if velhas:
            conn.hdel(_COMBOS_KEY % cliente_id, *velhas)
    except Exception:
        logger.warning('leads_cache: falha ao ler combinações pedidas',
                       extra={'cliente_id': cliente_id})
    vistos: set[str] = set()
    out: list[str] = []
    for _, combo in sorted(pedidas, reverse=True):
        if combo not in vistos:
            vistos.add(combo)
            out.append(combo)
    for combo in _sementes():
        if combo not in vistos:
            vistos.add(combo)
            out.append(combo)
    return out


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------
def servir_cache_ligado() -> bool:
    """Setting `LEADS_CACHE_ENABLED` E a chave do Redis (efeito em segundos)."""
    if not settings.LEADS_CACHE_ENABLED:
        return False
    try:
        return not _conn().exists(_DESLIGADO_KEY)
    except Exception:
        return True   # sem Redis o cache também não é lido; a leitura já cai


def desligar_cache(desligar: bool = True) -> None:
    """`desligar_cache()` → listagem 100% ao vivo; `desligar_cache(False)` volta."""
    conn = _conn()
    if desligar:
        conn.set(_DESLIGADO_KEY, '1')
    else:
        conn.delete(_DESLIGADO_KEY)


# ---------------------------------------------------------------------------
# Leitura do cache (caminho da requisição)
# ---------------------------------------------------------------------------
def ler_cache(cliente_id: int, combo: str) -> dict | None:
    try:
        cru = _conn().get(_CACHE_KEY % (cliente_id, combo))
    except Exception:
        logger.warning('leads_cache: Redis indisponível na leitura',
                       extra={'cliente_id': cliente_id, 'combo': combo})
        return None
    if not cru:
        return None
    try:
        return json.loads(cru)
    except (TypeError, ValueError):
        return None


def servir_do_cache(cliente_id: int, nivel: str, tribunal: str, min_score: float,
                    devedor_publico: bool, limit: int, teto_s: int):
    """`(linhas, meta)` servidas do cache, ou `None` se não há cache recente.

    `OperationalError` (teto estourado) sobe para quem chama — é o mesmo 503
    da consulta ao vivo.
    """
    combo = chave_combo(nivel, tribunal, min_score, devedor_publico)
    dado = ler_cache(cliente_id, combo)
    if not dado:
        return None
    idade = time.time() - float(dado.get('epoch') or 0)
    if idade < 0 or idade > settings.LEADS_CACHE_MAX_IDADE_S:
        return None
    ids: list[int] = [int(i) for i in dado.get('ids') or []]

    def _consultar():
        consumidos = set(
            LeadConsumption.objects.filter(cliente_id=cliente_id, processo_id__in=ids)
            .values_list('processo_id', flat=True)) if ids else set()
        restantes = [i for i in ids if i not in consumidos]
        trav_2g = tribunais_2g_travados(tribunal)
        if trav_2g and restantes:
            # Cache calculado com a trava aberta não fura a trava fechada.
            travados = set(_ids_2g_da_fila(trav_2g).filter(processo_id__in=restantes)
                           .values_list('processo_id', flat=True))
            restantes = [i for i in restantes if i not in travados]
        linhas: list = []
        reclassificados = 0
        # Busca em fatias na ordem do cache: um `limit` pequeno não precisa
        # trazer 10 mil linhas; um processo que mudou de nível só é trocado
        # pelo próximo da fila.
        fatia = max(limit, 500)
        pos = 0
        while len(linhas) < limit and pos < len(restantes):
            pedaco = restantes[pos:pos + fatia]
            pos += fatia
            por_id = {p.pk: p for p in
                      Process.objects.filter(pk__in=pedaco).only(*_CAMPOS_RESPOSTA)}
            for pid in pedaco:
                p = por_id.get(pid)
                if p is None or not _ainda_vale(p, nivel, tribunal, min_score,
                                                devedor_publico):
                    reclassificados += 1
                    continue
                linhas.append(p)
                if len(linhas) >= limit:
                    break
        return linhas, len(consumidos), reclassificados

    linhas, n_consumidos, n_reclass = executar_com_teto(_consultar, teto_s)
    completo = bool(dado.get('completo'))
    esgotado = len(linhas) < limit
    meta = {
        'gerado_em': dado.get('gerado_em'),
        'idade_s': int(idade),
        'ids_no_cache': len(ids),
        'completo': completo,
        'descartados_consumidos': n_consumidos,
        'descartados_reclassificados': n_reclass,
        'esgotado': esgotado,
    }
    if esgotado and not completo:
        # Teto é alerta (regra nº 2): o cache guardou TETO_IDS e acabou antes
        # do `limit` — pode haver lead além do que foi guardado.
        logger.warning(
            'listar_leads: cache esgotou no teto (cliente=%s combo=%s limit=%s '
            'servidos=%s ids_no_cache=%s consumidos_desde=%s reclassificados=%s) '
            '— pode haver leads além dos %s guardados',
            cliente_id, combo, limit, len(linhas), len(ids), n_consumidos,
            n_reclass, TETO_IDS)
    elif esgotado:
        logger.info(
            'listar_leads: cache esgotou — não há mais leads (cliente=%s combo=%s '
            'limit=%s servidos=%s)', cliente_id, combo, limit, len(linhas))
    return linhas, meta


def _ainda_vale(p, nivel: str, tribunal: str, min_score: float,
                devedor_publico: bool) -> bool:
    """Re-confere, na linha ATUAL, o filtro com que ela entrou no cache."""
    if p.classificacao != nivel:
        return False
    if (p.classificacao_score or 0) < min_score:
        return False
    if tribunal and p.tribunal_id != tribunal:
        return False
    return not devedor_publico or e_devedor_publico(p.classe_codigo, p.classe_nome)


# ---------------------------------------------------------------------------
# Pré-cálculo (job)
# ---------------------------------------------------------------------------
def clientes_elegiveis() -> list[int]:
    """`ApiClient` ativo que consumiu nos últimos `LEADS_CACHE_CLIENTE_DIAS`."""
    desde = timezone.now() - timedelta(days=settings.LEADS_CACHE_CLIENTE_DIAS)
    recentes = (LeadConsumption.objects.filter(consumido_em__gte=desde)
                .values('cliente_id'))
    return list(ApiClient.objects.filter(ativo=True, pk__in=recentes)
                .order_by('pk').values_list('pk', flat=True))


def precomputar_combo(cliente_id: int, combo: str) -> dict:
    """Calcula e grava UMA combinação. Levanta em erro — quem chama isola."""
    nivel, tribunal, min_score, devedor_publico = ler_combo(combo)
    if nivel not in dict(Process.CLASSIF_CHOICES):
        raise ValueError(f'nivel inválido na combinação: {combo}')
    qs = montar_queryset(cliente_id, nivel, tribunal, min_score, devedor_publico)
    t0 = time.monotonic()
    ids = executar_com_teto(
        lambda: list(qs.values_list('id', flat=True)[:TETO_IDS]),
        settings.LEADS_CACHE_SQL_TIMEOUT_SECONDS)
    levou = round(time.monotonic() - t0, 2)
    agora = timezone.now()
    dado = {
        'versao': _VERSAO,
        'cliente_id': cliente_id,
        'combo': combo,
        'gerado_em': agora.isoformat(),
        'epoch': time.time(),
        'ids': ids,
        'completo': len(ids) < TETO_IDS,
        'levou_s': levou,
    }
    _conn().set(_CACHE_KEY % (cliente_id, combo), json.dumps(dado),
                ex=settings.LEADS_CACHE_TTL_S)
    return {'ids': len(ids), 'completo': dado['completo'], 'levou_s': levou}


@job('leads_consumo', timeout=3600)
def precomputar_leads_cache() -> dict:
    """Pré-calcula a listagem de leads de cada cliente ativo x combinação pedida.

    Sequencial de propósito: uma consulta pesada por vez no banco. Cada
    combinação tem teto PRÓPRIO (`LEADS_CACHE_SQL_TIMEOUT_SECONDS`) e uma que
    falha não derruba as outras — o cache anterior dela continua lá e, quando
    passar de `LEADS_CACHE_MAX_IDADE_S`, a API volta para a consulta ao vivo.
    """
    if not cache.add(_LOCK_KEY, '1', timeout=3600):
        logger.info('precomputar_leads_cache: skip (outra execução em curso)')
        return {'skip': 'lock'}
    inicio = time.monotonic()
    relatorio: dict = {'inicio': timezone.now().isoformat(), 'combos': {},
                       'falhas': 0, 'ok': 0, 'fora_do_orcamento': 0}
    try:
        clientes = clientes_elegiveis()
        relatorio['clientes'] = clientes
        fila: list[tuple[int, str]] = [
            (cid, combo) for cid in clientes for combo in combos_do_cliente(cid)]
        teto_combos = settings.LEADS_CACHE_MAX_COMBOS
        if len(fila) > teto_combos:
            logger.error(
                'precomputar_leads_cache: %d combinações pedidas, teto %d — as '
                '%d excedentes NÃO serão pré-calculadas (caem na consulta ao vivo)',
                len(fila), teto_combos, len(fila) - teto_combos)
            relatorio['acima_do_teto'] = len(fila) - teto_combos
            fila = fila[:teto_combos]
        orcamento = settings.LEADS_CACHE_ORCAMENTO_S
        for cid, combo in fila:
            chave = f'{cid}:{combo}'
            if time.monotonic() - inicio > orcamento:
                relatorio['combos'][chave] = 'fora do orçamento de tempo'
                relatorio['fora_do_orcamento'] += 1
                continue
            try:
                r = precomputar_combo(cid, combo)
                relatorio['combos'][chave] = r
                relatorio['ok'] += 1
                if not r['completo']:
                    logger.info('precomputar_leads_cache: %s bateu o teto de %d ids '
                                '(levou %ss)', chave, TETO_IDS, r['levou_s'])
            except OperationalError as exc:
                relatorio['combos'][chave] = f'teto SQL: {str(exc)[:160]}'
                relatorio['falhas'] += 1
                logger.error('precomputar_leads_cache: %s estourou %ss: %s', chave,
                             settings.LEADS_CACHE_SQL_TIMEOUT_SECONDS, exc)
            except Exception as exc:  # uma combinação não derruba as outras
                relatorio['combos'][chave] = f'erro: {type(exc).__name__}: {str(exc)[:160]}'
                relatorio['falhas'] += 1
                logger.exception('precomputar_leads_cache: %s falhou', chave)
        if relatorio['fora_do_orcamento']:
            logger.error(
                'precomputar_leads_cache: orçamento de %ss acabou — %d combinações '
                'ficaram sem recálculo nesta passada', orcamento,
                relatorio['fora_do_orcamento'])
        relatorio['levou_s'] = round(time.monotonic() - inicio, 1)
        try:
            _conn().set(_ULTIMA_EXECUCAO_KEY, json.dumps(relatorio, default=str),
                        ex=7 * 86400)
        except Exception:
            logger.warning('precomputar_leads_cache: falha ao gravar relatório')
        logger.info('precomputar_leads_cache: ok=%d falhas=%d fora_do_orcamento=%d '
                    'em %ss', relatorio['ok'], relatorio['falhas'],
                    relatorio['fora_do_orcamento'], relatorio['levou_s'])
        return relatorio
    finally:
        cache.delete(_LOCK_KEY)


def ultima_execucao() -> dict | None:
    """Relatório da última passada do pré-cálculo (para runbook/diagnóstico)."""
    try:
        cru = _conn().get(_ULTIMA_EXECUCAO_KEY)
    except Exception:
        return None
    return json.loads(cru) if cru else None
