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


def _recuperacao() -> tuple[list, dict, list, dict, dict]:
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
    fila de trabalho de verdade — 156 dias em 24/08, sendo 121 do TJRS; **1 em
    01/09**, o TJRS 2025-09-19.

    ── a coluna `recuperado`, e por que ela precisou existir (01/09/2026) ──

    A tela publicava `recuperável` — uma CONSTANTE de 18/08 — ao lado de
    `nunca_refeito = 0`. Lendo a linha do TJSP hoje, "64.895.691" ao lado de
    "0 dias" só pode ser lido como *faltam 64,9 milhões*. Não faltam: já
    voltaram. Era o mesmo defeito do gráfico de Estoque, número congelado do
    passado ao lado de número vivo sem dizer qual é qual.

    `recuperado` é o conserto, e é MEDIÇÃO, não estimativa: para cada dia que
    **já tinha sido coletado antes do corte**, soma as `movimentacoes_novas` dos
    runs `success` posteriores a ele. Isto é, o que a re-coleta trouxe de novo
    num dia que a gente já dava por coletado — exatamente a publicação que o
    teto por UF decapitava.

    Medido em 02/09/2026 nos 28 tribunais que sangram: **115.359.154**
    publicações recuperadas em 4.246 dias, contra 212.308.169 estimados em
    18/08. E a estimativa erra para os dois lados (TRF4 devolveu 2,0× o
    estimado), então `estimado − recuperado` NÃO é publicado como saldo.

    O que resta é publicado em DIAS, que é o que se mede.

    ── FASE 3: a Fase 2 acabou, a recuperação NÃO (02/09/2026) ──

    A mesma régua nos 19 tribunais que sangram e estão fora da `FASE_2`:

        Fase 2 ...  3.999 dias-alvo ·     1 nunca refeito ·  99,97%
        Fase 3 ...  8.404 dias-alvo · 7.167 nunca refeitos ·  14,7%
        nacional . 12.403 dias-alvo · 7.168 nunca refeitos

    Publicar só a Fase 2 fazia a tela dizer 100% e quem lesse concluía que
    acabou. **Acabou 1/3.** Por isso a Fase 3 tem tabela própria, com as mesmas
    colunas — e o nacional honesto sai junto.

    ⚠️ O TJPR (1.152 dias nunca refeitos, 38.807.963 estimados = 43% do que
    resta) está em `FORA_DO_ALVO`: aparece na tabela marcado, e **fora das
    somas**. Ele não é buraco nosso, é decisão comercial; somá-lo faria a Fase 3
    parecer o dobro do problema que ela é para nós.
    """
    from tribunals.models import IngestionRun as R

    siglas = list(dict.fromkeys(list(M.FASE_2) + list(M.RECUPERAVEL_POR_TRIBUNAL)))
    def _zero():
        return {'alvo': 0, 'flat': 0, 'pos_corte': 0, 'recuperavel': 0,
                'nunca_refeito': 0, 'falso_pos': 0, 'recuperado': 0,
                'fora_alvo': 0, 'fora_dias': 0, 'fora_nunca': 0,
                'fora_estimado': 0, 'fora_recuperado': 0}

    linhas, tot = [], _zero()
    fase3, tot3 = [], _zero()
    nac = {'recuperado': 0, 'dias': 0, 'estimado': 0, 'alvo': 0,
           'nunca_refeito': 0, 'alvo_da_casa': 0, 'nunca_da_casa': 0}

    for sigla in siglas:
        ult = {}                      # dia -> (started_at, razão, status)
        por_dia = {}                  # dia -> [(started_at, status, novas)]
        qs = (R.objects.filter(fonte='djen', tribunal__sigla=sigla)
              .only('janela_inicio', 'janela_fim', 'paginas_lidas', 'status',
                    'movimentacoes_novas', 'movimentacoes_duplicadas', 'started_at'))
        for r in qs.iterator(chunk_size=3000):
            if r.janela_inicio != r.janela_fim:
                continue
            novas = r.movimentacoes_novas or 0
            por_dia.setdefault(r.janela_inicio, []).append(
                (r.started_at, r.status, novas))
            if not r.paginas_lidas:
                continue
            itens = novas + (r.movimentacoes_duplicadas or 0)
            if itens < M.MIN_ITENS_DIA_GRANDE:
                continue
            ant = ult.get(r.janela_inicio)
            if ant is None or r.started_at > ant[0]:
                ult[r.janela_inicio] = (r.started_at, itens / r.paginas_lidas, r.status)

        def _refeito(v):
            return v[0].replace(tzinfo=None) >= M.CORTE_FLAT and v[2] == 'success'

        # O que a RE-coleta trouxe: só conta o dia que já tinha run ANTES do
        # corte. Sem esse filtro, a coleta normal do dia de ontem entraria como
        # "recuperação" e o número viraria propaganda.
        recuperado = dias_ref = 0
        for runs in por_dia.values():
            pre = [x for x in runs if x[0].replace(tzinfo=None) < M.CORTE_FLAT]
            pos = [x for x in runs
                   if x[0].replace(tzinfo=None) >= M.CORTE_FLAT and x[1] == 'success']
            if pre and pos:
                recuperado += sum(x[2] for x in pos)
                dias_ref += 1

        est = M.RECUPERAVEL_POR_TRIBUNAL.get(sigla, 0)
        alvo = len(ult)
        flat = sum(1 for v in ult.values() if v[1] >= M.RAZAO_CAMINHO_FLAT)
        pos_corte = sum(1 for v in ult.values() if _refeito(v))
        # A célula que importa: razão baixa E sem run novo. As outras três
        # combinações têm explicação conhecida (ver docstring).
        nunca = sum(1 for v in ult.values()
                    if v[1] < M.RAZAO_CAMINHO_FLAT and not _refeito(v))
        falso_pos = (alvo - flat) - nunca
        linha = {
            'sigla': sigla, 'alvo': alvo, 'flat': flat, 'pos_corte': pos_corte,
            'falta': alvo - flat, 'recuperavel': est,
            'recuperado': recuperado, 'dias_refeitos': dias_ref,
            'nunca_refeito': nunca, 'falso_pos': falso_pos,
            'fora_do_alvo': M.FORA_DO_ALVO.get(sigla),
            'pct_flat': (100.0 * flat / alvo) if alvo else 0,
            'pct_corte': (100.0 * pos_corte / alvo) if alvo else 0,
            'pct_honesto': (100.0 * (alvo - nunca) / alvo) if alvo else 0,
        }
        alvo_da_casa = sigla not in M.FORA_DO_ALVO

        nac['recuperado'] += recuperado
        nac['dias'] += dias_ref
        nac['estimado'] += est
        nac['alvo'] += alvo
        nac['nunca_refeito'] += nunca
        if alvo_da_casa:
            nac['alvo_da_casa'] += alvo
            nac['nunca_da_casa'] += nunca

        destino, soma = ((linhas, tot) if sigla in M.FASE_2 else (fase3, tot3))
        destino.append(linha)
        # O TJPR entra na TABELA (some da tela seria pior) e fica FORA das
        # somas de "o que falta": ele não é buraco nosso, é decisão comercial.
        if alvo_da_casa:
            soma['alvo'] += alvo; soma['flat'] += flat
            soma['pos_corte'] += pos_corte; soma['recuperavel'] += est
            soma['nunca_refeito'] += nunca
            soma['falso_pos'] += falso_pos
            soma['recuperado'] += recuperado
        else:
            soma['fora_alvo'] += 1
            soma['fora_dias'] += alvo
            soma['fora_nunca'] += nunca
            soma['fora_estimado'] += est
            soma['fora_recuperado'] += recuperado

    linhas.sort(key=lambda x: M.FASE_2.index(x['sigla']))
    # Fase 3 ordenada pelo que RESTA, não pelo volume: a fila de trabalho é
    # `nunca_refeito`, e é ela que a tela existe para tornar acionável.
    fase3.sort(key=lambda x: (-x['nunca_refeito'], -x['recuperavel']))
    for soma in (tot, tot3):
        soma['pct_flat'] = (100.0 * soma['flat'] / soma['alvo']) if soma['alvo'] else 0
        soma['pct_corte'] = (100.0 * soma['pos_corte'] / soma['alvo']) if soma['alvo'] else 0
        soma['pct_honesto'] = ((100.0 * (soma['alvo'] - soma['nunca_refeito']) / soma['alvo'])
                               if soma['alvo'] else 0)
        soma['falta_razao'] = soma['alvo'] - soma['flat']
        soma['refeitos'] = soma['alvo'] - soma['nunca_refeito']
        # a MESMA régua contando quem está fora do alvo, para quem quiser a
        # leitura do país inteiro em vez da leitura do que é buraco nosso
        soma['alvo_com_fora'] = soma['alvo'] + soma['fora_dias']
        soma['nunca_com_fora'] = soma['nunca_refeito'] + soma['fora_nunca']
        soma['pct_com_fora'] = (
            (100.0 * (soma['alvo_com_fora'] - soma['nunca_com_fora'])
             / soma['alvo_com_fora']) if soma['alvo_com_fora'] else 0)
    nac['estimado_em'] = M.RECUPERAVEL_MEDIDO_EM
    nac['pct'] = (100.0 * nac['recuperado'] / nac['estimado']) if nac['estimado'] else None
    nac['pct_honesto'] = ((100.0 * (nac['alvo_da_casa'] - nac['nunca_da_casa'])
                           / nac['alvo_da_casa']) if nac['alvo_da_casa'] else 0)
    nac['refeitos_da_casa'] = nac['alvo_da_casa'] - nac['nunca_da_casa']
    return linhas, tot, fase3, tot3, nac


def _vazao_recuperacao() -> list:
    """Runs de UM DIA que fecharam `success`, por dia, desde o corte.

    **Esta série vale mais que a porcentagem.** A recuperação não é um número
    que só sobe: é um processo, e processo para. Medido em 02/09/2026:

        18/08 293 · 19/08 1.131 · 20/08 208 · 21/08 774 · 22/08 270
        23/08 1.641 · 24/08 371 · 25/08 206 · 26/08 359 · 27/08 80
        28/08 59 · 29/08 59 · 30/08 59 · 31/08 61 · 01/09 184

    De 27/08 em diante são ~59/dia, que é **exatamente a coleta diária dos 59
    tribunais e mais nada**: o mutirão da Fase 2 acabou e ninguém religou. Uma
    vazão que cai ao piso e ninguém vê é a assinatura do problema que este
    projeto inteiro persegue — run verde, log limpo, número redondo.

    Por isso a tela publica `piso` (o nº de tribunais ativos = a coleta do dia)
    junto com a série: sem ele, 59 parece produção.
    """
    try:
        from django.db.models import Count, F
        from django.db.models.functions import TruncDate

        from tribunals.models import IngestionRun as R, Tribunal
        # CORTE_FLAT é naive e o resto do módulo o compara contra
        # `started_at.replace(tzinfo=None)`, que é UTC. Passar o naive pro ORM
        # faria o Django interpretá-lo no TIME_ZONE do projeto (-03) e mover o
        # corte em 3 h — a armadilha de fuso que já contaminou medição aqui.
        corte = M.CORTE_FLAT.replace(tzinfo=datetime.timezone.utc)
        qs = (R.objects.filter(fonte='djen', status='success',
                               started_at__gte=corte,
                               janela_inicio=F('janela_fim'))
              .annotate(d=TruncDate('started_at')).values('d')
              .annotate(n=Count('id')).order_by('d'))
        serie = [{'dia': r['d'], 'n': r['n']} for r in qs if r['d']]
        piso = Tribunal.objects.filter(ativo=True).count()
        pico = max((p['n'] for p in serie), default=0)
        return {'serie': serie[-30:], 'piso': piso, 'pico': pico,
                'ultimo': serie[-1]['n'] if serie else None}
    except Exception:  # noqa: BLE001
        logger.warning('completude: falhou medir a vazão', exc_info=True)
        return None


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


def _confronto_datajud() -> dict | None:
    """Par `(declarado, nosso)` do Datajud, agregado. `None` = nada medido.

    **Não vai na rede.** Quem fala com o CNJ é o `warm_completude_datajud`, job
    separado com orçamento próprio — a API do CNJ leva ~46 s por tribunal
    quando a cota `varredura` está disputada, e no mesmo job isso atrasaria a
    parte barata, que é a que a tela lê. Aqui só se AGENDA a rodada (quando há
    medição velha) e se agrega o que já está medido.
    """
    try:
        from django.core.cache import cache as _c

        from . import completude_datajud as DJ
        DJ.agendar_rodada()
        return DJ.agregar(_c.get(DJ.CHAVE))
    except Exception:  # noqa: BLE001
        logger.warning('completude: falhou agregar o confronto do datajud', exc_info=True)
        return None


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
        (dados['recuperacao'], dados['resumo_recup'], dados['fase3'],
         dados['resumo_fase3'], dados['recup_nacional']) = _recuperacao()
        dados['vazao'] = _vazao_recuperacao()
        dados['diarios'] = _diarios()
        dados['datajud'] = _confronto_datajud()
        cache.set(CACHE_KEY, dados, timeout=TTL)
        dt = (datetime.datetime.now() - t0).total_seconds()
        logger.info('completude medida em %.0fs', dt)
    except Exception:  # noqa: BLE001 — cron não pode morrer e sumir
        logger.exception('completude: falhou medir')
    return {'ok': True}
