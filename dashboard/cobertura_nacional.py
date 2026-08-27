"""Cobertura do acervo nacional — o número que define se o produto existe.

O Voyager tem um princípio nº 1 (COMPLETUDE) e, até esta tela, nenhum lugar
mostrava o progresso dele. A frase "só tínhamos 13% do acervo nacional" custou
horas de sonda em agosto/2026 e vivia numa nota de acompanhamento; o número de
hoje vivia num terminal. Card sem série é foto; o que ensina é a curva.

  numerador   `tribunals_process` — um processo nosso, único por (tribunal, CNJ)
  denominador CNJs DISTINTOS do `voyager-acervo`, o esqueleto do Datajud

**O denominador NÃO é o total de docs do índice.** São 342.046.902 documentos
para 289.277.192 CNJs distintos: o Datajud emite um doc por (tribunal, grau) e
24,3% dos processos têm dois ou mais graus. Dividir por 342 M subestima a
cobertura em ~5 pontos (29,2% contra 34,5%) — e o erro tem a cara de rigor, que
é o pior tipo.

Tudo aqui roda em JOB DE AQUECIMENTO, nunca no caminho da requisição (regra
nº 7). A cardinalidade sozinha leva 36 s. A tela lê o cache e, se não houver,
diz que não mediu — nunca segura a página.
"""
import datetime
import logging

from django.core.cache import cache
from django.db import connection, transaction
from django.utils import timezone

logger = logging.getLogger('voyager.dashboard.cobertura')

CHAVE = 'cobertura_nacional:v1'
TTL = 60 * 60 * 30          # 30 h: o warm roda de 6 em 6 h; o TTL só evita eternizar

#: dias da série. 45 cobre a recuperação nacional inteira (o pico de 18/08 tem
#: 6,7 M num dia só) com folga para o "último mês" que a tela promete.
DIAS_SERIE = 45

#: quantos tribunais aparecem na barra. Os 12 maiores já são ~70% do país.
TOP_TRIBUNAIS = 12


def _cardinalidade_cnjs(es) -> int | None:
    """CNJs distintos no esqueleto nacional. `None` quando não deu pra medir.

    HyperLogLog com `precision_threshold` alto: no volume de 342 M o erro fica
    em torno de 1%, e é por isso que a tela mostra "≈". Contar exato exigiria
    varrer o índice inteiro — e um número exato que ninguém consegue recalcular
    é pior que um aproximado que se sabe aproximado.
    """
    try:
        r = es.search(index='voyager-acervo', body={
            'size': 0,
            'aggs': {'cnjs': {'cardinality': {'field': 'proc_digits',
                                              'precision_threshold': 40000}}},
        }, request_timeout=120)
        return int(r['aggregations']['cnjs']['value'])
    except Exception:
        logger.warning('cobertura: cardinalidade dos CNJs falhou', exc_info=True)
        return None


def _por_tribunal(es, indice: str) -> dict:
    try:
        r = es.search(index=indice, body={
            'size': 0, 'aggs': {'t': {'terms': {'field': 'tribunal', 'size': 100}}},
        }, request_timeout=120)
        return {b['key']: b['doc_count'] for b in r['aggregations']['t']['buckets']}
    except Exception:
        logger.warning('cobertura: agg por tribunal falhou em %s', indice, exc_info=True)
        return {}


def _serie_ingestao(total_hoje: int) -> list[dict]:
    """Série diária REGRESSIVA: quantos processos existiam ao fim de cada dia.

    Reconstruída de trás pra frente a partir do total de hoje e do que entrou em
    cada dia (`inserido_em`). É a única série honesta disponível — ninguém
    guardou snapshot diário —, e ela tem uma propriedade boa: o ponto de hoje é
    exato por construção, então o erro (se houver) fica no passado distante, não
    no número que o usuário vai citar.
    """
    with transaction.atomic(), connection.cursor() as c:
        c.execute("SET LOCAL statement_timeout = '240s'")
        c.execute("""SELECT inserido_em::date AS d, count(*)
                       FROM tribunals_process
                      WHERE inserido_em >= now() - make_interval(days => %s)
                      GROUP BY 1 ORDER BY 1""", [DIAS_SERIE])
        por_dia = {d: n for d, n in c.fetchall()}

    hoje = timezone.localdate()
    dias = [hoje - datetime.timedelta(days=i) for i in range(DIAS_SERIE, -1, -1)]
    # acumulado do fim pro começo: acum[hoje] = total; acum[d-1] = acum[d] - novos[d]
    acum, saida = total_hoje, []
    for d in reversed(dias):
        saida.append({'d': d.isoformat(), 'acum': acum, 'novos': por_dia.get(d, 0)})
        acum -= por_dia.get(d, 0)
    saida.reverse()
    return saida


def calcular() -> dict | None:
    """Mede tudo e devolve o payload. `None` se faltar peça essencial.

    Abster > chutar: sem denominador não há cobertura, e uma tela que inventa
    denominador mente com ar de precisão.
    """
    from search.client import get_es
    es = get_es()

    try:
        acervo_docs = es.count(index='voyager-acervo', request_timeout=60)['count']
        nosso_indice = es.count(index='voyager-processos', request_timeout=60)['count']
    except Exception:
        logger.error('cobertura: não consegui contar os índices', exc_info=True)
        return None

    cnjs = _cardinalidade_cnjs(es)
    if not cnjs:
        return None

    with transaction.atomic(), connection.cursor() as c:
        c.execute("SET LOCAL statement_timeout = '20s'")
        # `reltuples` do planner: estimativa, e a tela DIZ que é estimativa.
        # `count(*)` exato em 103 M sob a carga de backfill é minutos de I/O
        # para mudar a terceira casa decimal.
        c.execute("SELECT reltuples::bigint FROM pg_class WHERE relname = 'tribunals_process'")
        linha = c.fetchone()
        total_pg = int(linha[0]) if linha and linha[0] else 0

    ac = _por_tribunal(es, 'voyager-acervo')
    nos = _por_tribunal(es, 'voyager-processos')
    tribunais = []
    for t, n in sorted(ac.items(), key=lambda kv: -kv[1])[:TOP_TRIBUNAIS]:
        x = nos.get(t, 0)
        tribunais.append({'t': t, 'datajud': n, 'nosso': x,
                          'cob': round(100.0 * x / n, 1) if n else 0.0})

    serie_raw = _serie_ingestao(total_pg)
    serie = [{'d': p['d'], 'acum': p['acum'], 'novos': p['novos'],
              'cob': round(100.0 * p['acum'] / cnjs, 2)} for p in serie_raw]

    hoje = serie[-1]['cob'] if serie else 0.0
    ha30 = next((p['cob'] for p in serie if p['d'] ==
                 (timezone.localdate() - datetime.timedelta(days=30)).isoformat()), None)
    if ha30 is None and serie:
        ha30 = serie[0]['cob']

    # data do retrato do esqueleto: ele NÃO é atualizado, e a tela precisa dizer
    # isso — usar um mapa de 13 dias atrás para afirmar que um processo "não
    # existe" erraria em tudo que foi distribuído desde então.
    varrido = None
    try:
        r = es.search(index='voyager-acervo', body={
            'size': 1, 'sort': [{'varrido_em': {'order': 'desc'}}],
            '_source': ['varrido_em']}, request_timeout=30)
        hits = r['hits']['hits']
        if hits:
            varrido = (hits[0]['_source'].get('varrido_em') or '')[:10]
    except Exception:
        pass

    return {
        'medido_em': timezone.now().isoformat(),
        'acervo_docs': acervo_docs,
        'cnjs_distintos': cnjs,
        'docs_por_cnj': round(acervo_docs / cnjs, 2) if cnjs else None,
        'acervo_varrido_em': varrido,
        'total_pg': total_pg,
        'nosso_indice': nosso_indice,
        'cobertura': hoje,
        'cobertura_ha_30d': ha30,
        'ganho_pp': round(hoje - ha30, 2) if ha30 is not None else None,
        'novos_janela': sum(p['novos'] for p in serie),
        'faltam': max(cnjs - total_pg, 0),
        'serie': serie,
        'tribunais': tribunais,
        'dias_serie': DIAS_SERIE,
    }


def aquecer() -> dict | None:
    """Job de aquecimento — chamado pelo scheduler. Nunca levanta."""
    try:
        payload = calcular()
    except Exception:
        logger.error('cobertura: aquecimento falhou', exc_info=True)
        return None
    if payload:
        cache.set(CHAVE, payload, TTL)
        logger.info('cobertura: %.2f%% (%s de ≈%s CNJs), +%s pp em 30 dias',
                    payload['cobertura'], f"{payload['total_pg']:,}",
                    f"{payload['cnjs_distintos']:,}", payload['ganho_pp'])
    return payload


def ler():
    """O que a TELA usa. Só cache — nunca calcula no caminho da requisição."""
    return cache.get(CHAVE)
