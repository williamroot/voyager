"""Agregações do Mapa Comercial de Precatórios — 100% Elasticsearch.

Serviço de agregação que alimenta o mapa comercial (choropleth do Brasil +
drill-down por tribunal + rankings). TODA leitura é do índice ES
`voyager-processos` — NUNCA do Postgres (é o gargalo de prod). A única exceção
é a **cobertura** (% validado), que vem do cache já existente
(`dashboard.queries.cobertura_enriquecimento_data`) — não recomputamos.

--------------------------------------------------------------------------------
DEFINIÇÕES DE MÉTRICA (do plano `.ia/COMERCIAL_MAPA.md`)
--------------------------------------------------------------------------------
- volume        = doc_count (nº de processos no bucket)
- valor         = sum(valor_causa)
- potencial     = count(tem_sinal_precatorio=true)   → sinal DJEN, amplo
- confirmado    = count(classificacao='PRECATORIO')  → ML, preciso mas limitado
                  pela cobertura
- cobertura_pct = % validado do bucket (do cache de enriquecimento, 0..100)
- densidade     = potencial / volume
- score_foco    = **a própria densidade** (o campo sobrevive pelo contrato)
                  O ranking responde UMA pergunta: onde é denso em precatório.
                  Valor e cobertura são ATRIBUTOS exibidos ao lado ("já olhamos
                  24%"), não multiplicadores escondidos na nota — ver
                  `calcular_score_foco` para o porquê e os números.

--------------------------------------------------------------------------------
CONTRATO JSON DOS ENDPOINTS (o agente FRONTEND segue isto à risca)
--------------------------------------------------------------------------------
Todos os endpoints exigem sessão logada (`@login_required`) e devolvem
`Content-Type: application/json`. Filtros vêm por querystring (ver `parse_filtros`).

Objeto BUCKET (unidade de resposta, mesma forma pra UF e tribunal):
    {
      "uf": "SP",              // presente na visão por-UF
      "tribunal": "TJSP",      // presente na visão por-tribunal
      "volume": 123456,        // int  — nº de processos
      "valor": 9876543.21,     // float — soma de valor_causa (R$)
      "potencial": 4200,       // int|null — tem_sinal_precatorio=true; NULL se o
                               //   sinal nunca foi computado no bucket (backfill
                               //   Fase 0 não passou) — desconhecido ≠ zero
      "sinal_processado": true,// bool — backfill do sinal já cobriu o bucket?
      "confirmado": 810,       // int  — classificacao=PRECATORIO
      "todos": 4900,           // int  — UNIÃO possível ∪ confirmado (nunca a
                               //   SOMA: confirmado NÃO é subconjunto de
                               //   possível — medido 12/08/2026: dos 47.720
                               //   confirmados só 6.421 têm sinal de texto, e
                               //   41.299 (87%) só aparecem nesta visão)
      "cobertura_pct": 42.5,   // float 0..100 — % validado (cache; null se pending)
      "densidade": 0.034,      // float 0..1   — lente POSSÍVEIS (compat)
      "score_foco": 0.0191,    // float ≥ 0    — lente POSSÍVEIS (compat)
      "densidade_por_lente": { "potencial": 0.034, "confirmado": 0.001, "todos": 0.035 },
      "score_por_lente":     { "potencial": 0.019, "confirmado": 0.000, "todos": 0.020 }
                               //   o mapa tem 3 lentes; o ranking "ataque
                               //   primeiro" reordena pela lente ativa, senão
                               //   mapa e ranking respondem coisas diferentes
                               //   na mesma tela. Custo zero: é aritmética
                               //   sobre números que já vieram no bucket.
    }

1) GET /dashboard/api/comercial/mapa
   → {
       "ufs":   [ <bucket com "uf">, ... ],   // ordenado por score_foco desc
       "total": { "volume", "valor", "potencial", "confirmado" },  // sem uf
       "filtros": { ...ecoa os filtros aplicados... },
       "gerado_em": "2026-08-11T12:00:00+00:00"
     }

2) GET /dashboard/api/comercial/tribunais?uf=SP
   → {
       "uf": "SP",
       "tribunais": [ <bucket com "tribunal">, ... ],  // score_foco desc
       "total": { "volume", "valor", "potencial", "confirmado" },
       "filtros": { ... },
       "gerado_em": "..."
     }
   `uf` é obrigatório aqui (400 se ausente/ inválido).

3) GET /dashboard/api/comercial/top?metric=score&n=10&lente=todos
   → {
       "metric": "score",       // score | volume | valor | potencial | confirmado | todos
       "lente": "todos",        // potencial | confirmado | todos — só afeta metric=score;
                                //   é a MESMA string do `sinalMode` do front, de propósito
                                //   (tradução no meio do caminho é bug silencioso)
       "n": 10,
       "ranking": [ <bucket com "uf">, ... ],   // ordenado pela métrica desc
       "filtros": { ... },
       "gerado_em": "..."
     }

FILTROS (querystring, todos opcionais salvo indicado):
    uf              keyword    ex.: SP  (em /tribunais é OBRIGATÓRIO)
    tribunal        keyword    ex.: TJSP
    classificacao   keyword    PRECATORIO|PRE_PRECATORIO|DIREITO_CREDITORIO|NAO_LEAD
    tipo            enum       potencial|confirmado — atalho de conveniência:
                               `potencial` ⇒ tem_sinal_precatorio=true;
                               `confirmado` ⇒ classificacao=PRECATORIO
    tem_sinal       bool       true|false|1|0 (sobrescrito por tipo=potencial)
    ano_min         int        ano_cnj >= (clampa em [1990, ano_atual])
    ano_max         int        ano_cnj <= (clampa em [1990, ano_atual])
    valor_min       float      valor_causa >= (negativos são ignorados)
    valor_max       float      valor_causa <=
    codigo_classe   keyword    ex.: 1116
    natureza        keyword    reservado (hoje mapeia p/ codigo_classe se dado)
    metric          enum       (só no /top) score|volume|valor|potencial|confirmado
    n               int        (só no /top) 1..100, default 10

Entradas inválidas NUNCA geram 500: ano futuro/absurdo é clampado, valor
negativo é ignorado, enum desconhecido cai no default. O que não dá pra
recuperar (ex.: `uf` ausente em /tribunais) devolve 400 com `{"erro": "..."}`.
--------------------------------------------------------------------------------
"""
import datetime
import hashlib
import json
import logging

from django.core.cache import cache

from search.geo import UF_FEDERAL, uf_do_tribunal

logger = logging.getLogger('voyager.comercial.agg')

#: janela de plausibilidade pro ano_cnj (evita filtros absurdos)
ANO_MIN_PLAUSIVEL = 1990

#: classificação que conta como "precatório confirmado"
CLASSIF_CONFIRMADO = 'PRECATORIO'

#: métricas aceitas pelo ranking /top
METRICAS_RANKING = {'score', 'volume', 'valor', 'potencial', 'confirmado', 'todos'}

# Lente do mapa → campo do bucket que vira NUMERADOR da densidade no score.
# Uma lente por chave; a chave é a mesma string que o front usa em `sinalMode`,
# pra não haver tradução no meio do caminho (fonte de bug silencioso).
SCORE_POR_LENTE = {
    'potencial': 'potencial',
    'confirmado': 'confirmado',
    'todos': 'todos',
}

#: TTL do warm cache dos agregados globais (sem filtro) — 5 min
CACHE_TTL = 300
_CACHE_PREFIX = 'comercial:agg'

#: nº de buckets terms — 27 UFs + FED com folga; tribunais idem
_TERMS_SIZE = 64


# --------------------------------------------------------------------------- #
# Parse / saneamento de filtros
# --------------------------------------------------------------------------- #
def _ano_atual() -> int:
    return datetime.date.today().year


def _to_int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _to_float(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _to_bool(v):
    if isinstance(v, bool):
        return v
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ('true', '1', 'sim', 'yes'):
        return True
    if s in ('false', '0', 'nao', 'não', 'no'):
        return False
    return None


def parse_filtros(qd) -> dict:
    """Extrai e SANEIA os filtros de um dict-like (request.GET ou dict puro).

    Nunca levanta: valores inválidos são clampados/descartados pra evitar 500.
    Retorna um dict só com as chaves efetivamente informadas (limpo).
    """
    g = qd.get
    filtros: dict = {}

    uf = (g('uf') or '').strip().upper()
    if uf:
        filtros['uf'] = uf

    tribunal = (g('tribunal') or '').strip().upper()
    if tribunal:
        filtros['tribunal'] = tribunal

    classificacao = (g('classificacao') or '').strip().upper()
    if classificacao:
        filtros['classificacao'] = classificacao

    codigo_classe = (g('codigo_classe') or '').strip()
    # `natureza` é reservado; hoje, se vier e não houver codigo_classe, usamos como classe.
    natureza = (g('natureza') or '').strip()
    if not codigo_classe and natureza:
        codigo_classe = natureza
    if codigo_classe:
        filtros['codigo_classe'] = codigo_classe

    # PARTE/ENTIDADE — usa o campo TEXTO `partes` (nomes concatenados), que está
    # em 100% dos 71,18M docs. O nested `participacoes` (estruturado, com polo e
    # documento) só existe em 1,9% até o reindex terminar: filtrar por ele daria
    # "o INSS tem 53.577 processos" quando a base tem 4.402.239 — 82× menos.
    # Quando o reindex fechar, dá pra oferecer `parte_polo=` sem quebrar isto.
    parte = ' '.join((g('parte') or '').split())[:120]
    if parte:
        filtros['parte'] = parte

    # ENTIDADE CANÔNICA (autocomplete) — precisa das GRAFIAS, não do nome.
    # O INSS tem 104 grafias no índice; filtrar só pelo nome canônico
    # ("INSTITUTO NACIONAL DO SEGURO SOCIAL") acha 4.402.239, mas há outras
    # grafias que o `match` AND não alcança. `entidade_id` é resolvido em
    # `resolver_entidade` (1 GET por id, cacheado) → OR de match_phrase.
    # Tem precedência sobre `parte`: escolher no autocomplete é mais específico
    # que digitar texto.
    entidade_id = (g('entidade_id') or '').strip()[:120]
    if entidade_id:
        filtros['entidade_id'] = entidade_id

    tipo = (g('tipo') or '').strip().lower()
    if tipo == 'potencial':
        filtros['tem_sinal'] = True
    elif tipo == 'confirmado':
        filtros['classificacao'] = CLASSIF_CONFIRMADO
    else:
        b = _to_bool(g('tem_sinal'))
        if b is not None:
            filtros['tem_sinal'] = b

    ano_atual = _ano_atual()
    ano_min = _to_int(g('ano_min'))
    ano_max = _to_int(g('ano_max'))
    if ano_min is not None:
        filtros['ano_min'] = max(ANO_MIN_PLAUSIVEL, min(ano_min, ano_atual))
    if ano_max is not None:
        filtros['ano_max'] = max(ANO_MIN_PLAUSIVEL, min(ano_max, ano_atual))
    # se inverteram a faixa, ordena (degrada limpo em vez de retornar vazio surpresa)
    if 'ano_min' in filtros and 'ano_max' in filtros and filtros['ano_min'] > filtros['ano_max']:
        filtros['ano_min'], filtros['ano_max'] = filtros['ano_max'], filtros['ano_min']

    valor_min = _to_float(g('valor_min'))
    valor_max = _to_float(g('valor_max'))
    if valor_min is not None and valor_min >= 0:
        filtros['valor_min'] = valor_min
    if valor_max is not None and valor_max >= 0:
        filtros['valor_max'] = valor_max
    if 'valor_min' in filtros and 'valor_max' in filtros and filtros['valor_min'] > filtros['valor_max']:
        filtros['valor_min'], filtros['valor_max'] = filtros['valor_max'], filtros['valor_min']

    return filtros


# --------------------------------------------------------------------------- #
# Montagem da query ES
# --------------------------------------------------------------------------- #
def build_filter_clauses(filtros: dict) -> list:
    """Traduz filtros saneados numa lista de cláusulas `filter` do bool query ES.

    Só usa `filter` (sem scoring) — barato e cacheável no ES.
    """
    clauses: list = []

    if filtros.get('uf'):
        clauses.append({'term': {'uf': filtros['uf']}})
    if filtros.get('tribunal'):
        clauses.append({'term': {'tribunal': filtros['tribunal']}})
    if filtros.get('classificacao'):
        clauses.append({'term': {'classificacao': filtros['classificacao']}})
    if filtros.get('codigo_classe'):
        clauses.append({'term': {'codigo_classe': filtros['codigo_classe']}})
    if 'tem_sinal' in filtros:
        clauses.append({'term': {'tem_sinal_precatorio': filtros['tem_sinal']}})
    variantes = filtros.get('_variantes')
    if variantes:
        # Entidade escolhida no autocomplete: OR das GRAFIAS dela, em
        # `match_phrase`. Não é só refinamento — CORRIGE o texto livre.
        #
        # Medido em 12/08/2026, "União Federal": o `match` AND do texto livre
        # devolve 1.508.785 processos e o OR das grafias 1.232.679. A diferença
        # de 276.106 (18%) NÃO é recall perdido: são processos onde "união" e
        # "federal" aparecem em partes DIFERENTES da mesma lista — tipo
        # "Defensoria Pública da UNIÃO" + "Caixa Econômica FEDERAL". O texto
        # livre inflava; a grafia exata acerta.
        # (INSS: 4.403.363 → 4.402.239, mesma natureza, magnitude irrisória.)
        clauses.append({'bool': {
            'should': [{'match_phrase': {'partes': v}} for v in variantes],
            'minimum_should_match': 1,
        }})
    elif filtros.get('parte'):
        # `match` com AND: "fazenda são paulo" exige as duas palavras, em
        # qualquer ordem — o nome vem em ordens e grafias diferentes por
        # tribunal ("INSS - Instituto Nacional..." vs "Instituto Nacional do
        # Seguro Social (INSS)"). `match_phrase` exigiria a ordem exata e
        # perderia metade; `should` sem AND traria qualquer processo com a
        # palavra "estado". Fica em `filter` (sem score) porque aqui só
        # contamos — relevância é problema da busca, não da agregação.
        clauses.append({'match': {'partes': {'query': filtros['parte'],
                                             'operator': 'and'}}})

    ano_range = {}
    if 'ano_min' in filtros:
        ano_range['gte'] = filtros['ano_min']
    if 'ano_max' in filtros:
        ano_range['lte'] = filtros['ano_max']
    if ano_range:
        clauses.append({'range': {'ano_cnj': ano_range}})

    valor_range = {}
    if 'valor_min' in filtros:
        valor_range['gte'] = filtros['valor_min']
    if 'valor_max' in filtros:
        valor_range['lte'] = filtros['valor_max']
    if valor_range:
        clauses.append({'range': {'valor_causa': valor_range}})

    return clauses


def _metric_subaggs() -> dict:
    """Sub-aggs de métrica reusadas em cada bucket (valor, potencial, confirmado).

    volume já vem do `doc_count` do bucket — não precisa de sub-agg.
    """
    return {
        'valor': {'sum': {'field': 'valor_causa'}},
        'potencial': {'filter': {'term': {'tem_sinal_precatorio': True}}},
        'confirmado': {'filter': {'term': {'classificacao': CLASSIF_CONFIRMADO}}},
        # quantos docs do bucket TÊM o sinal computado (true OU false). 0 ⇒ o
        # backfill Fase 0 não passou por ali ⇒ potencial é DESCONHECIDO (null),
        # não zero — achado do QA: SP parecia "sem precatório" no modo Potencial.
        'sinal_conhecido': {'filter': {'exists': {'field': 'tem_sinal_precatorio'}}},
        # UNIÃO possível ∪ confirmado — a visão "todos". Tem que ser união (bool
        # should), NUNCA soma: medido no ES em 12/08/2026, dos 47.720 confirmados
        # só 6.421 também têm sinal de texto, então somar contaria essa
        # interseção em dobro. E os outros 41.299 confirmados (87%!) NÃO têm
        # sinal de texto — ficavam invisíveis na visão "possíveis". Confirmado
        # não é subconjunto de possível: são dois detectores independentes
        # (texto das publicações × classificação da IA).
        'todos': {'filter': {'bool': {
            'should': [
                {'term': {'tem_sinal_precatorio': True}},
                {'term': {'classificacao': CLASSIF_CONFIRMADO}},
            ],
            'minimum_should_match': 1,
        }}},
    }


def build_body_por_campo(campo: str, filtros: dict) -> dict:
    """Corpo ES: 1 request, terms-agg no `campo` (uf/tribunal) + sub-aggs de métrica.

    `size: 0` (não traz hits), `track_total_hits` desligado — só agregação.
    """
    body = {
        'size': 0,
        'track_total_hits': False,
        'aggs': {
            'buckets': {
                'terms': {'field': campo, 'size': _TERMS_SIZE},
                'aggs': _metric_subaggs(),
            }
        },
    }
    clauses = build_filter_clauses(filtros)
    if clauses:
        body['query'] = {'bool': {'filter': clauses}}
    return body


def build_body_total(filtros: dict) -> dict:
    """Corpo ES pro TOTAL agregado (sem terms) — 1 request, só métricas globais."""
    body = {
        'size': 0,
        'track_total_hits': True,
        'aggs': _metric_subaggs(),
    }
    clauses = build_filter_clauses(filtros)
    if clauses:
        body['query'] = {'bool': {'filter': clauses}}
    return body


# --------------------------------------------------------------------------- #
# Cobertura (do cache existente) — nunca recomputa, nunca toca o Postgres
# --------------------------------------------------------------------------- #
def _cobertura_por_tribunal() -> dict:
    """{sigla: pct(0..100)} do cache de enriquecimento. Miss → {} (cobertura None)."""
    from dashboard.queries import cobertura_enriquecimento_data
    data = cobertura_enriquecimento_data()
    out = {}
    for r in data.get('rows', []):
        sigla = r.get('sigla')
        if sigla:
            out[sigla] = r.get('pct')
    return out


def _cobertura_por_uf() -> dict:
    """Cobertura por UF: média ponderada por `total` dos tribunais da UF.

    Soma cobertos/total por UF (via `uf_do_tribunal`) e devolve o pct — mais
    honesto que média simples (UF com 1 tribunal gigante domina, como deve).
    """
    from dashboard.queries import cobertura_enriquecimento_data
    data = cobertura_enriquecimento_data()
    acc: dict = {}   # uf → [cobertos, total]
    for r in data.get('rows', []):
        sigla = r.get('sigla')
        total = r.get('total') or 0
        cobertos = r.get('cobertos')
        if cobertos is None:
            # deriva de pct se `cobertos` não veio no payload
            pct = r.get('pct') or 0.0
            cobertos = total * pct / 100.0
        uf = uf_do_tribunal(sigla) if sigla else UF_FEDERAL
        a = acc.setdefault(uf, [0.0, 0.0])
        a[0] += cobertos
        a[1] += total
    return {uf: (round(100 * c / t, 1) if t else None) for uf, (c, t) in acc.items()}


# --------------------------------------------------------------------------- #
# Score de foco
# --------------------------------------------------------------------------- #
#: Peso do "estado imaginário" que amortece densidade de amostra minúscula.
#: Com filtro ativo (ex.: parte=INSS) aparecem UFs com 1 ou 2 processos: 1÷1 =
#: 100% de densidade e o estado lidera o ranking com uma amostra de UM. 1.000
#: processos de prior significa: um estado só passa a ser lido pela sua própria
#: densidade quando tem volume dessa ordem; abaixo disso ele é puxado pra média
#: nacional (3,29% em 12/08/2026). Não afeta quem tem volume real — RO, com
#: 1,07M processos, vai de 0,1115 pra 0,1114.
PRIOR_DENSIDADE = 1000


def calcular_score_foco(volume: int, valor: float, potencial: int,
                        cobertura_pct, valor_max: float,
                        densidade_media: float | None = None) -> tuple:
    """DENSIDADE pura: potencial ÷ volume. Retorna (densidade, score_foco).

    Os dois são o mesmo número — `score_foco` sobrevive só porque é o nome no
    contrato do endpoint. A pergunta que o ranking responde é **"onde é denso
    em precatório"**, e só isso.

    Até 12/08/2026 era `densidade × valor_relativo × (1 − cobertura)`, e essa
    multiplicação era o erro: espremia três perguntas diferentes ("é denso?",
    "tem dinheiro?", "já olhamos?") num número só, que ninguém conseguia
    interpretar e que escondia estado bom por motivo errado. Medido: o MT
    (5,59% de densidade, a 6ª do país) aparecia em 21º porque já tínhamos
    olhado 24% dele e porque publicou um valor menor que o de Alagoas.

    `valor` e `cobertura_pct` continuam na assinatura e no bucket, mas como
    ATRIBUTOS exibidos ao lado do estado — "já olhamos 24%" é uma bandeira que
    o usuário lê e decide, não um desconto silencioso na nota.

    `potencial` é o ALVO: `_enriquecer_buckets` chama esta função uma vez por
    lente (possíveis / confirmados / todos) — ver `SCORE_POR_LENTE`.

    `densidade` é a taxa CRUA (é o que a tela mostra: "34,2% dos processos
    citam precatório"). O `score_foco` é a mesma taxa AMORTECIDA por um prior —
    ver `PRIOR_DENSIDADE`. Sem isso, com filtro por parte, um estado com 1
    processo e 1 possível ficava em 1º com "prioridade 100".
    """
    densidade = (potencial / volume) if volume else 0.0
    if densidade_media is None:
        score = densidade
    else:
        score = ((potencial + PRIOR_DENSIDADE * densidade_media)
                 / (volume + PRIOR_DENSIDADE))
    return round(densidade, 4), round(score, 4)


# --------------------------------------------------------------------------- #
# Execução + montagem dos buckets
# --------------------------------------------------------------------------- #
def _parse_buckets(resp: dict, campo: str) -> list:
    """Extrai buckets brutos do envelope ES em dicts {chave, volume, valor, ...}."""
    raw = (resp.get('aggregations', {}).get('buckets', {}).get('buckets', []))
    out = []
    for b in raw:
        sinal_conhecido = b.get('sinal_conhecido', {}).get('doc_count', 0)
        potencial = b.get('potencial', {}).get('doc_count', 0)
        out.append({
            campo: b['key'],
            'volume': b['doc_count'],
            'valor': round(b.get('valor', {}).get('value') or 0.0, 2),
            # sinal nunca computado no bucket ⇒ potencial DESCONHECIDO (null),
            # não "0 precatórios" — o front sinaliza "não processado".
            'potencial': potencial if sinal_conhecido else None,
            'sinal_processado': bool(sinal_conhecido),
            'confirmado': b.get('confirmado', {}).get('doc_count', 0),
            # visão "todos" = união (nunca soma; ver _metric_subaggs). Sempre
            # numérica: mesmo sem o sinal processado, o confirmado da IA já é um
            # dado real do bucket — é justamente o que a visão "possíveis"
            # esconde. Ex. medido: SP sem sinal processado mas com 4.218
            # confirmados ⇒ 'todos' mostra os 4.218 em vez de "não analisado".
            'todos': b.get('todos', {}).get('doc_count', 0),
        })
    return out


def _parse_total(resp: dict) -> dict:
    aggs = resp.get('aggregations', {})
    total = resp.get('hits', {}).get('total', {})
    volume = total.get('value') if isinstance(total, dict) else total
    return {
        'volume': volume or 0,
        'valor': round(aggs.get('valor', {}).get('value') or 0.0, 2),
        'potencial': aggs.get('potencial', {}).get('doc_count', 0),
        'confirmado': aggs.get('confirmado', {}).get('doc_count', 0),
        'todos': aggs.get('todos', {}).get('doc_count', 0),
    }


def _enriquecer_buckets(buckets: list, campo: str, cobertura_map: dict) -> list:
    """Junta cobertura + calcula densidade/score_foco POR LENTE. Ordena por score desc.

    O mapa tem 3 lentes (possíveis / confirmados / todos) e o ranking "ataque
    primeiro" precisa mudar junto: mapa pintando uma métrica e ranking ordenando
    por outra é contradição na mesma tela. Como o score é aritmética sobre
    números que já vieram no bucket, calcular as 3 não custa request nenhuma no
    ES — sai tudo do mesmo payload.

    `densidade`/`score_foco` (sem sufixo) continuam sendo os de POSSÍVEIS, que é
    o default histórico do endpoint — quem consome sem saber de lente não quebra.
    """
    valor_max = max((b['valor'] for b in buckets), default=0.0)
    # média do CONJUNTO exibido (não uma constante): com filtro por parte a
    # base de comparação certa é "a densidade típica deste recorte", não a do
    # país inteiro. É pra ela que amostra pequena é puxada.
    volume_total = sum(b['volume'] or 0 for b in buckets)
    media_por_lente = {
        lente: ((sum(b.get(campo_alvo) or 0 for b in buckets) / volume_total)
                if volume_total else 0.0)
        for lente, campo_alvo in SCORE_POR_LENTE.items()
    }
    for b in buckets:
        chave = b[campo]
        cob = cobertura_map.get(chave)
        b['cobertura_pct'] = cob
        dens_por_lente, score_por_lente = {}, {}
        for lente, campo_alvo in SCORE_POR_LENTE.items():
            dens, score = calcular_score_foco(
                b['volume'], b['valor'], b.get(campo_alvo) or 0, cob, valor_max,
                densidade_media=media_por_lente[lente])
            dens_por_lente[lente] = dens
            score_por_lente[lente] = score
        b['densidade_por_lente'] = dens_por_lente
        b['score_por_lente'] = score_por_lente
        b['densidade'] = dens_por_lente['potencial']
        b['score_foco'] = score_por_lente['potencial']
    buckets.sort(key=lambda x: x['score_foco'], reverse=True)
    return buckets


def get_es():
    """Wrapper lazy do cliente ES (evita importar `elasticsearch` no import do módulo).

    Existe como atributo de módulo pra ser mockável nos testes
    (`@patch('search.agg_comercial.get_es')`) sem exigir o pacote elasticsearch.
    """
    from search.client import get_es as _get_es
    return _get_es()


def index_name(suffix: str) -> str:
    # não delega a search.client (que importa `elasticsearch` no topo) — mantém o
    # módulo importável em ambientes sem o pacote ES (ex.: rodar só os testes).
    from django.conf import settings
    return f'{settings.ELASTICSEARCH_INDEX_PREFIX}-{suffix}'


def _run(body: dict) -> dict:
    es = get_es()
    return es.search(index=index_name('processos'), body=body)


#: TTL do lookup de entidade → grafias. Entidade muda com o rebuild do índice
#: (raro); 1h é folgado e evita 1 GET no ES por request de mapa.
TTL_ENTIDADE = 3600


def _indice_entidades() -> str:
    """Índice de entidades ATIVO. Configurável porque hoje ele ainda é o de
    teste (`voyager-entidades-teste`): o ranking do autocomplete usava um proxy
    ruim de prevalência e o índice não foi promovido. Promover = trocar o
    setting, sem deploy de código.
    """
    from django.conf import settings
    nome = getattr(settings, 'ENTIDADES_INDICE', None) or 'voyager-entidades-teste'
    return nome


def resolver_entidade(filtros: dict) -> dict:
    """Troca `entidade_id` pelas GRAFIAS da entidade (`_variantes`).

    Sem isto, escolher "INSS" no autocomplete filtraria só pelo nome canônico e
    perderia as outras 103 grafias que o índice de entidades unificou.

    Falha silenciosa por decisão: se o índice de entidades estiver fora ou o id
    não existir, devolve os filtros INTACTOS — o mapa continua respondendo (com
    o `parte` textual, se houver), em vez de 503. Perder o refinamento é menos
    grave que derrubar a tela.
    """
    eid = filtros.get('entidade_id')
    if not eid or filtros.get('_variantes'):
        return filtros
    chave = f'{_CACHE_PREFIX}:ent:{hashlib.md5(eid.encode()).hexdigest()}'
    variantes = cache.get(chave)
    if variantes is None:
        try:
            resp = get_es().get(index=_indice_entidades(), id=eid,
                                _source=['variantes_busca', 'nome_canonico'])
            src = resp.get('_source') or {}
            variantes = list(src.get('variantes_busca') or [])
            if not variantes and src.get('nome_canonico'):
                variantes = [src['nome_canonico']]
        except Exception:
            logger.warning('resolver_entidade: lookup falhou', extra={'entidade_id': eid})
            return filtros
        cache.set(chave, variantes, TTL_ENTIDADE)
    if not variantes:
        return filtros
    return {**filtros, '_variantes': variantes}


#: Quantos candidatos o autocomplete devolve pra escolha do MAIOR. 8 era
#: pouco: "prefeitura de sao paulo" trazia a "Prefeitura de São Paulo" (56
#: processos) porque a "PREFEITURA MUNICIPAL DE SÃO PAULO" (7.380) ficava fora
#: da janela — o ranking textual ordena por um proxy fraco de prevalência.
#: 30 é barato (1 query, cacheada 1h) e cobre a cauda.
CANDIDATOS_TEXTO = 30


def _tokens(texto: str) -> set:
    import unicodedata
    t = unicodedata.normalize('NFKD', texto or '').encode('ascii', 'ignore').decode()
    return {p for p in ''.join(c if c.isalnum() else ' ' for c in t).upper().split()
            if len(p) > 2}          # "de", "do", "da" não discriminam nada


def resolver_texto(filtros: dict) -> dict:
    """Texto digitado → entidade canônica, quando dá pra ter certeza.

    O `match` AND contra o campo `partes` casa as palavras em partes
    DIFERENTES da mesma lista: medido em prod, "união federal" devolvia
    1.508.785 em vez de 1.232.679 porque pegava "Defensoria Pública da UNIÃO" +
    "Caixa Econômica FEDERAL" no mesmo processo (+18% de falso positivo).

    Nenhum parâmetro de query resolve isso — medido: `match_phrase` puro
    despenca ("fazenda sao paulo" → 14 processos, porque o nome real é "Fazenda
    Pública do Estado de São Paulo") e `slop` volta a inflar (slop 5 →
    1.422.380 na União).

    A correção é mudar o ALVO do AND: em vez de casar as palavras contra a
    lista de partes (longa, com várias entidades), casamos contra o NOME da
    entidade (curto, canônico). Se todos os tokens do que foi digitado estão no
    nome de uma entidade conhecida, usamos as GRAFIAS dela — que é o filtro
    correto. Senão, cai no texto livre de antes (e o front avisa).

    Devolve `_entidade_resolvida` pro front poder dizer QUEM ele está mostrando
    e oferecer a troca: adivinhar em silêncio é pior que não adivinhar.
    """
    texto = filtros.get('parte')
    if not texto or filtros.get('_variantes') or filtros.get('entidade_id'):
        return filtros
    alvo = _tokens(texto)
    if not alvo:
        return filtros
    chave = f'{_CACHE_PREFIX}:txt:{hashlib.md5(texto.upper().encode()).hexdigest()}'
    achado = cache.get(chave)
    if achado is None:
        try:
            from search import entidades as _ent
            resp = get_es().search(index=_indice_entidades(),
                                   body=_ent.query_autocomplete(texto, tamanho=CANDIDATOS_TEXTO))
            # Entre os candidatos válidos, o MAIOR — não o primeiro do ranking.
            # Medido: buscar "inss" trazia "Gerente Executivo do INSS de São
            # Paulo" (21 processos) na frente do INSS (4,4 milhões), porque o
            # ranking textual usa `n_partes` como proxy de prevalência e ele é
            # fraco. Quem digita "inss" quer o INSS. `n_processos` (contagem
            # real) manda quando existe; `n_partes` é o desempate enquanto ela
            # não é populada em toda a base.
            candidatos = []
            for h in resp.get('hits', {}).get('hits', []):
                src = h.get('_source') or {}
                # Todos os tokens digitados têm que caber em UMA grafia — o
                # nome canônico OU qualquer variante. Testar só o canônico
                # falhava justamente no caso mais comum: o nome canônico do
                # INSS é "INSTITUTO NACIONAL DO SEGURO SOCIAL" (a grafia mais
                # frequente) e NÃO contém a sigla; quem digita "inss" casa pela
                # variante "INSTITUTO NACIONAL DO SEGURO SOCIAL - INSS".
                # UMA grafia, não a união delas: senão "fazenda gado" casaria
                # juntando "fazenda" de uma variante e "gado" de outra.
                grafias = [src.get('nome_canonico') or '']
                grafias += list(src.get('variantes') or src.get('variantes_busca') or [])
                if any(alvo.issubset(_tokens(g)) for g in grafias):
                    candidatos.append((
                        src.get('n_processos') if src.get('n_processos') is not None
                        else -1,
                        src.get('n_partes') or 0,
                        h.get('_id'), src,
                    ))
            achado = {}
            if candidatos:
                _, _, eid_top, src = max(candidatos, key=lambda c: (c[0], c[1]))
                achado = {
                    'entidade_id': eid_top,
                    'nome': src.get('nome_canonico'),
                    'n_variantes': src.get('n_variantes'),
                    'variantes': list(src.get('variantes_busca') or []),
                }
        except Exception:
            logger.warning('resolver_texto: índice de entidades indisponível',
                           extra={'parte': texto})
            return filtros
        cache.set(chave, achado, TTL_ENTIDADE)
    if not achado or not achado.get('variantes'):
        return filtros
    return {**filtros, '_variantes': achado['variantes'],
            '_entidade_resolvida': {k: achado[k] for k in
                                    ('entidade_id', 'nome', 'n_variantes')}}


def _eco_filtros(filtros: dict) -> dict:
    """Filtros pro payload: some com o interno, mantém a entidade resolvida.

    `_variantes` é ruído (dezenas de grafias) e detalhe de implementação.
    `_entidade_resolvida` FICA: o front precisa dizer de quem são os números
    quando adivinhamos a entidade a partir do texto — adivinhar em silêncio é
    pior que não adivinhar.
    """
    eco = {k: v for k, v in filtros.items() if not k.startswith('_')}
    ent = filtros.get('_entidade_resolvida')
    if ent:
        eco['entidade_resolvida'] = ent
    return eco


def _sem_filtro(filtros: dict) -> bool:
    return not filtros


# --------------------------------------------------------------------------- #
# API pública do serviço (consumida pelas views)
# --------------------------------------------------------------------------- #
def agg_por_uf(filtros: dict | None = None) -> dict:
    """Agregação por UF (1 request ES) + total + cobertura + score. Cache 5min se sem filtro.

    Retorna {'ufs': [...], 'total': {...}, 'filtros': {...}, 'gerado_em': iso}.
    """
    filtros = filtros or {}
    # cache POR COMBINAÇÃO de filtros (achado do QA: range em 71M = 2,7-4,9s por
    # request sem cache; o warm de 5min só existia pro agregado sem filtro).
    # Filtrado usa TTL menor (2min) — é exploração interativa, staleness ok.
    if _sem_filtro(filtros):
        cache_key = f'{_CACHE_PREFIX}:uf:v1'
        ttl = CACHE_TTL
    else:
        chave = json.dumps(filtros, sort_keys=True, default=str)
        cache_key = f'{_CACHE_PREFIX}:uf:f:{hashlib.md5(chave.encode()).hexdigest()}'
        ttl = 120
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    # entidade → grafias DEPOIS do cache lookup: com cache quente não paga o
    # GET no índice de entidades.
    filtros = resolver_texto(resolver_entidade(filtros))
    resp = _run(build_body_por_campo('uf', filtros))
    total_resp = _run(build_body_total(filtros))

    buckets = _parse_buckets(resp, 'uf')
    buckets = _enriquecer_buckets(buckets, 'uf', _cobertura_por_uf())

    payload = {
        'ufs': buckets,
        'total': _parse_total(total_resp),
        'filtros': _eco_filtros(filtros),
        'gerado_em': _agora_iso(),
    }
    cache.set(cache_key, payload, ttl)
    return payload


def agg_tribunais(uf: str, filtros: dict | None = None) -> dict:
    """Drill-down: tribunais de uma UF (1 request ES) + total + cobertura + score.

    `uf` é aplicado como filtro adicional. Retorna
    {'uf': .., 'tribunais': [...], 'total': {...}, 'filtros': .., 'gerado_em': ..}.
    """
    filtros = dict(filtros or {})
    uf = (uf or '').strip().upper()
    filtros['uf'] = uf

    # drill-down default (só uf) 5min; filtrado por combinação, 2min (QA: 349ms+)
    if set(filtros.keys()) == {'uf'}:
        cache_key = f'{_CACHE_PREFIX}:trib:{uf}:v1'
        ttl = CACHE_TTL
    else:
        chave = json.dumps(filtros, sort_keys=True, default=str)
        cache_key = f'{_CACHE_PREFIX}:trib:f:{hashlib.md5(chave.encode()).hexdigest()}'
        ttl = 120
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    filtros = resolver_texto(resolver_entidade(filtros))
    resp = _run(build_body_por_campo('tribunal', filtros))
    total_resp = _run(build_body_total(filtros))

    buckets = _parse_buckets(resp, 'tribunal')
    buckets = _enriquecer_buckets(buckets, 'tribunal', _cobertura_por_tribunal())

    payload = {
        'uf': uf,
        'tribunais': buckets,
        'total': _parse_total(total_resp),
        'filtros': _eco_filtros(filtros),
        'gerado_em': _agora_iso(),
    }
    cache.set(cache_key, payload, ttl)
    return payload


def ranking_top(metric: str = 'score', n: int = 10, filtros: dict | None = None,
                lente: str = 'potencial') -> dict:
    """Ranking Top-N por UF numa métrica. Reusa `agg_por_uf` (mesma agg ES).

    metric ∈ {score, volume, valor, potencial, confirmado, todos}. n clampado 1..100.

    `lente` só afeta metric='score': o "ataque primeiro" reordena conforme a
    lente ativa no mapa, senão o mapa pinta confirmados e o ranking continua
    ordenando por possíveis — duas respostas diferentes pra mesma pergunta na
    mesma tela. Lente desconhecida cai em 'potencial' (default histórico).
    """
    filtros = filtros or {}
    metric = (metric or 'score').strip().lower()
    if metric not in METRICAS_RANKING:
        metric = 'score'
    lente = (lente or 'potencial').strip().lower()
    if lente not in SCORE_POR_LENTE:
        lente = 'potencial'
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 10
    n = max(1, min(n, 100))

    base = agg_por_uf(filtros)
    if metric == 'score':
        def ordem(x):
            return (x.get('score_por_lente') or {}).get(lente, x.get('score_foco') or 0)
    else:
        def ordem(x):
            return x.get(metric) or 0        # `potencial` pode vir None (não processado)
    ranking = sorted(base['ufs'], key=ordem, reverse=True)[:n]

    return {
        'metric': metric,
        'lente': lente,
        'n': n,
        'ranking': ranking,
        'filtros': _eco_filtros(filtros),
        'gerado_em': base.get('gerado_em') or _agora_iso(),
    }


def _agora_iso() -> str:
    from django.utils import timezone
    return timezone.now().isoformat()
