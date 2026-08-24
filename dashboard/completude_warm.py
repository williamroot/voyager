"""Mede a completude do acervo fora do caminho da requisição.

Roda em cron e grava um dicionário pronto no cache. NADA aqui pode ir pro hot
path: são contagens no Elasticsearch de 1,39 bilhão de docs e agregações no
Postgres — a página só LÊ o resultado.

Cada consulta tem TETO DE ESPERA explícito. É a regra nº 7 do CLAUDE.md, e ela
existe porque uma medição de rodapé sem `request_timeout` derrubou o site.
"""
import datetime
import logging

from django.core.cache import cache
from django_rq import job

from . import completude_medicoes as M
from .completude_views import CACHE_KEY

logger = logging.getLogger('voyager.completude')

TTL = 60 * 90          # 90 min: a medição é cara e o acervo não muda em minutos
ES_TIMEOUT = 120


def _contar_es(indice: str, corpo: dict | None = None) -> int | None:
    """Contagem no ES com teto de espera. `None` = não deu pra medir.

    Devolver None e a tela dizer "não medido" é melhor que devolver 0 e a tela
    dizer que o acervo está vazio.
    """
    try:
        from search.client import get_es, index_name
        es = get_es()
        r = es.count(index=index_name(indice), body=corpo, request_timeout=ES_TIMEOUT)
        return int(r['count'])
    except Exception:  # noqa: BLE001
        logger.warning('completude: falhou contar %s', indice, exc_info=True)
        return None


def _contar_diarios() -> int | None:
    """Publicações que entraram pela terceira porta. `None` = app não migrado."""
    try:
        from django.db.models import Sum

        from diarios.models import EdicaoDiario as E
        n = E.objects.aggregate(n=Sum('itens_gravados'))['n']
        return int(n or 0)
    except Exception:  # noqa: BLE001 — app pode não estar migrado ainda
        return None


def _recuperacao_por_tribunal() -> tuple[list, dict]:
    """Quanto da recuperação do DJEN já foi refeito, POR TRIBUNAL.

    Mostra as DUAS réguas de propósito, porque nenhuma delas sozinha é honesta:

      · razão itens/página >= 700  → dia veio pelo caminho flat. Falso positivo
        conhecido: o downshift de 5xx reduz o page size e derruba a razão de um
        dia que saiu flat (medido: TJDFT 2025-11-17, razão 197, era flat).
      · run posterior ao corte     → saiu pelo caminho novo, sem ambiguidade.
        Subestima: um dia que já era bom antes nunca precisou ser refeito.

    A verdade está entre as duas. Mostrar uma só seria escolher a que soa melhor.

    ── por que `falta` (razão) NÃO chega a zero, medido em 24/08/2026 ──

    Cruzando as duas réguas nos 3.945 dias-alvo da Fase 2:

                          refeito pós-corte   NÃO refeito
        razão >= 700            3.328              320
        razão <  700              141              156

    Os **141** da célula (razão baixa, JÁ refeito) são falso positivo puro: o
    conserto do OOM (24/08) tornou o `itensPorPagina` DINÂMICO — num tribunal de
    publicação pesada a página cai pra 100-300 itens por orçamento de BYTES.
    Exemplos do próprio dia: TJGO 2026-08-24 razão 207 (62.612 itens em 302
    páginas), TRF4 2026-08-24 razão 100, TRF2 2026-08-24 razão 439 — todos
    coletados pelo caminho flat, hoje. **A razão passou a medir o peso da
    publicação, não o caminho da coleta.**

    Por isso a tela ganhou uma TERCEIRA coluna, `nunca_refeito` (razão baixa E
    sem `success` pós-corte): é a única das três que pode chegar a zero, e é a
    fila de trabalho de verdade — 156 dias, sendo 121 do TJRS.
    """
    from tribunals.models import IngestionRun as R

    linhas, tot = [], {'alvo': 0, 'flat': 0, 'pos_corte': 0, 'recuperavel': 0,
                       'nunca_refeito': 0, 'falso_pos': 0}
    for sigla in M.FASE_2:
        ult = {}
        qs = (R.objects.filter(fonte='djen', tribunal__sigla=sigla)
              .only('janela_inicio', 'janela_fim', 'paginas_lidas', 'status',
                    'movimentacoes_novas', 'movimentacoes_duplicadas', 'started_at'))
        for r in qs.iterator(chunk_size=3000):
            if r.janela_inicio != r.janela_fim or not r.paginas_lidas:
                continue
            itens = (r.movimentacoes_novas or 0) + (r.movimentacoes_duplicadas or 0)
            if itens < M.MIN_ITENS_DIA_GRANDE:
                continue
            ant = ult.get(r.janela_inicio)
            if ant is None or r.started_at > ant[0]:
                ult[r.janela_inicio] = (r.started_at, itens / r.paginas_lidas, r.status)

        alvo = len(ult)
        flat = sum(1 for v in ult.values() if v[1] >= M.RAZAO_CAMINHO_FLAT)

        def _refeito(v):
            return v[0].replace(tzinfo=None) >= M.CORTE_FLAT and v[2] == 'success'

        pos = sum(1 for v in ult.values() if _refeito(v))
        # A célula que importa: razão baixa E sem run novo. As outras três
        # combinações têm explicação conhecida (ver docstring).
        nunca = sum(1 for v in ult.values()
                    if v[1] < M.RAZAO_CAMINHO_FLAT and not _refeito(v))
        falso_pos = (alvo - flat) - nunca
        rec = M.RECUPERAVEL_POR_TRIBUNAL.get(sigla, 0)
        linhas.append({
            'sigla': sigla, 'alvo': alvo, 'flat': flat, 'pos_corte': pos,
            'falta': alvo - flat, 'recuperavel': rec,
            'nunca_refeito': nunca, 'falso_pos': falso_pos,
            'pct_flat': (100.0 * flat / alvo) if alvo else 0,
            'pct_corte': (100.0 * pos / alvo) if alvo else 0,
            'pct_honesto': (100.0 * (alvo - nunca) / alvo) if alvo else 0,
        })
        tot['alvo'] += alvo; tot['flat'] += flat
        tot['pos_corte'] += pos; tot['recuperavel'] += rec
        tot['nunca_refeito'] += nunca
        tot['falso_pos'] += falso_pos

    tot['pct_flat'] = (100.0 * tot['flat'] / tot['alvo']) if tot['alvo'] else 0
    tot['pct_corte'] = (100.0 * tot['pos_corte'] / tot['alvo']) if tot['alvo'] else 0
    tot['pct_honesto'] = ((100.0 * (tot['alvo'] - tot['nunca_refeito']) / tot['alvo'])
                          if tot['alvo'] else 0)
    tot['falta_razao'] = tot['alvo'] - tot['flat']
    return linhas, tot


def _diarios() -> list:
    """Estado das edições de diário, por fonte.

    `EdicaoDiario` distingue LACUNA de AUSÊNCIA — feriado forense é
    `inexistente` (nunca mais tentar) e é diferente de `pendente` (ainda não
    fomos lá). Sem essa distinção a tela contaria recesso como buraco.
    """
    try:
        from django.db.models import Count, Max, Min

        from diarios.models import EdicaoDiario as E
    except Exception:  # noqa: BLE001 — app pode não estar migrado ainda
        return []

    fontes = []
    # `.order_by()` NUA antes do distinct, e não é estilo: `EdicaoDiario.Meta`
    # tem `ordering = ['-data', 'chave']`, e o Django injeta as colunas do
    # ORDER BY no SELECT DISTINCT. Sem isto o DISTINCT vale para a TRIPLA
    # (fonte, data, chave) e devolve uma linha por edição — a tela imprimiu 8
    # cartões `tjsp-dje` idênticos, cada um repetindo o agregado "8 pendentes
    # de 8", como se houvesse 64 pendências.
    for slug in E.objects.order_by().values_list('fonte', flat=True).distinct():
        qs = E.objects.filter(fonte=slug)
        por = dict(qs.values_list('status').annotate(n=Count('id')))
        faixa = qs.aggregate(de=Min('data'), ate=Max('data'))
        total = sum(por.values())
        # "resolvida" = já sabemos a resposta: coletada, vazia, inexistente ou
        # sem dado aproveitável. Pendente e falha é que são buraco de verdade.
        resolvidas = sum(por.get(s, 0) for s in
                         (E.OK, E.VAZIA, E.INEXISTENTE, E.SEM_APROVEITAMENTO, E.FORA_DA_JANELA))
        fontes.append({
            'slug': slug, 'total': total, 'por_status': por,
            'resolvidas': resolvidas, 'pendentes': por.get(E.PENDENTE, 0),
            'falhas': por.get(E.FALHA, 0),
            'pct': (100.0 * resolvidas / total) if total else 0,
            'de': faixa['de'], 'ate': faixa['ate'],
        })
    return sorted(fontes, key=lambda f: -f['total'])


@job('default', timeout=1800)
def warm_completude() -> dict:
    """Cron: mede os dois lados e deixa pronto pra tela. Nunca propaga erro."""
    t0 = datetime.datetime.now()
    dados = {'portas': {}, 'medido_em': t0}
    try:
        dados['portas']['djen'] = {'temos': _contar_es('movimentacoes')}
        dados['portas']['datajud'] = {'temos': _contar_es('acervo')}
        # Diários NÃO se conta pelo ES: `periodico_diario_slug` está preenchido
        # em TODAS as 1,4 bilhão de publicações (o doc builder usa a sigla do
        # tribunal como fallback), então filtrar por ele contaria o acervo
        # inteiro. A fonte de verdade é o próprio coletor — `itens_gravados` do
        # EdicaoDiario, que é "quantas linhas desta unidade estão no banco",
        # semântica escolhida de propósito para não zerar ao reprocessar.
        dados['portas']['diarios'] = {'temos': _contar_diarios()}
        dados['recuperacao'], dados['resumo_recup'] = _recuperacao_por_tribunal()
        dados['diarios'] = _diarios()
        cache.set(CACHE_KEY, dados, timeout=TTL)
        dt = (datetime.datetime.now() - t0).total_seconds()
        logger.info('completude medida em %.0fs', dt)
    except Exception:  # noqa: BLE001 — cron não pode morrer e sumir
        logger.exception('completude: falhou medir')
    return {'ok': True}
