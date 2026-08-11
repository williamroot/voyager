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
- valor_relativo= valor do bucket / maior valor entre os buckets (0..1)
- score_foco    = densidade × valor_relativo × (1 − cobertura)
                  prioriza estado precatório-rico, com dinheiro, ainda pouco tocado

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
      "cobertura_pct": 42.5,   // float 0..100 — % validado (cache; null se pending)
      "densidade": 0.034,      // float 0..1
      "score_foco": 0.0191     // float ≥ 0
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

3) GET /dashboard/api/comercial/top?metric=score&n=10
   → {
       "metric": "score",              // score | volume | valor | potencial | confirmado
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
METRICAS_RANKING = {'score', 'volume', 'valor', 'potencial', 'confirmado'}

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
def calcular_score_foco(volume: int, valor: float, potencial: int,
                        cobertura_pct, valor_max: float) -> tuple:
    """densidade × valor_relativo × (1 − cobertura). Retorna (densidade, score_foco).

    - cobertura_pct None (sem dado no cache) ⇒ trata como 0 (fator (1−0)=1),
      ou seja, "não tocamos" — o que INFLA o score, coerente com "ataque primeiro".
    - valor 0/None no BUCKET ⇒ fator valor NEUTRO (1.0). valor_causa é ESPARSO
      (medido 11/08: só SP tem no ES — federal/DJEN não traz) — desconhecido não é
      "sem dinheiro"; multiplicar por ~0 mataria o ranking inteiro por falta de
      dado. O front sinaliza "R$ desconhecido". Quando o valor real (Falcon)
      entrar, o fator volta a discriminar.
    """
    densidade = (potencial / volume) if volume else 0.0
    if not valor:
        valor_relativo = 1.0            # desconhecido ≠ zero: neutro
    elif valor_max:
        valor_relativo = valor / valor_max
    else:
        valor_relativo = 1.0
    cob = 0.0 if cobertura_pct is None else max(0.0, min(cobertura_pct, 100.0)) / 100.0
    score = densidade * valor_relativo * (1.0 - cob)
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
    }


def _enriquecer_buckets(buckets: list, campo: str, cobertura_map: dict) -> list:
    """Junta cobertura + calcula densidade/score_foco. Ordena por score desc."""
    valor_max = max((b['valor'] for b in buckets), default=0.0)
    for b in buckets:
        chave = b[campo]
        cob = cobertura_map.get(chave)
        b['cobertura_pct'] = cob
        dens, score = calcular_score_foco(
            b['volume'], b['valor'], b['potencial'] or 0, cob, valor_max)
        b['densidade'] = dens
        b['score_foco'] = score
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

    resp = _run(build_body_por_campo('uf', filtros))
    total_resp = _run(build_body_total(filtros))

    buckets = _parse_buckets(resp, 'uf')
    buckets = _enriquecer_buckets(buckets, 'uf', _cobertura_por_uf())

    payload = {
        'ufs': buckets,
        'total': _parse_total(total_resp),
        'filtros': filtros,
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

    resp = _run(build_body_por_campo('tribunal', filtros))
    total_resp = _run(build_body_total(filtros))

    buckets = _parse_buckets(resp, 'tribunal')
    buckets = _enriquecer_buckets(buckets, 'tribunal', _cobertura_por_tribunal())

    payload = {
        'uf': uf,
        'tribunais': buckets,
        'total': _parse_total(total_resp),
        'filtros': filtros,
        'gerado_em': _agora_iso(),
    }
    cache.set(cache_key, payload, ttl)
    return payload


def ranking_top(metric: str = 'score', n: int = 10, filtros: dict | None = None) -> dict:
    """Ranking Top-N por UF numa métrica. Reusa `agg_por_uf` (mesma agg ES).

    metric ∈ {score, volume, valor, potencial, confirmado}. n clampado 1..100.
    """
    filtros = filtros or {}
    metric = (metric or 'score').strip().lower()
    if metric not in METRICAS_RANKING:
        metric = 'score'
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 10
    n = max(1, min(n, 100))

    base = agg_por_uf(filtros)
    chave_ordem = 'score_foco' if metric == 'score' else metric
    ranking = sorted(base['ufs'], key=lambda x: x.get(chave_ordem, 0), reverse=True)[:n]

    return {
        'metric': metric,
        'n': n,
        'ranking': ranking,
        'filtros': filtros,
        'gerado_em': base.get('gerado_em') or _agora_iso(),
    }


def _agora_iso() -> str:
    from django.utils import timezone
    return timezone.now().isoformat()
