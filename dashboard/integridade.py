"""Integridade: o índice bate com o banco, e quem vigia as fontes.

Os outros dois cards do Acompanhamento contam o que CRESCEU. Este conta o que
está ÍNTEGRO — que é outra pergunta, e a mais fácil de responder errado.

## Por que medir dos dois lados

Contagem própria não prova nada (regra nº 5). "O índice tem 103,7 M documentos"
é compatível com o índice inteiro estar servindo o valor errado. A única prova
que vale é comparar o mesmo processo nos DOIS lados: sorteia no Elasticsearch,
busca a mesma linha no Postgres, e conta a divergência nos dois sentidos —
`só_no_PG` (o índice perdeu) e `só_no_ES` (o índice inventou).

## O campo de controle, e por que ele manda

Em 30/08/2026 a primeira medição deste card deu `partes 18,4%` e
`classe_codigo 0,0%`. Ia virar manchete de perda de dados. O que estava
quebrado era a RÉGUA: o índice renomeia os campos (`numero_cnj` → `proc`,
`classe_codigo` → `codigo_classe`, invertido), então o nome adivinhado lia
`None` em todo documento e devolvia zero.

Quem denunciou foi o **controle**: `proc` deu 0,0%, e é impossível — todo
processo tem CNJ. Por isso a regra desta tela:

> se o campo de controle não der 100%, o bloco inteiro NÃO é publicado.

Bloco suprimido com o motivo é honesto. Bloco publicado com régua torta é
exatamente a confiança falsa que o produto existe pra não produzir.

## Custo

O sorteio no ES é `random_score`, ~3 s por passada. Nunca no caminho da
requisição (regra nº 7): quem serve a tela lê só o cache.
"""
import logging

from django.core.cache import cache
from django.db import connection, transaction
from django.utils import timezone

logger = logging.getLogger('voyager.dashboard.integridade')

CHAVE = 'integridade:v1'
TTL = 60 * 60 * 30

#: Documentos sorteados por passada e número de passadas. 1.000 no total dá
#: ±1,5 pp a 95% — suficiente pra distinguir "zero divergência" de "o índice
#: perdeu um campo", que é a pergunta que este bloco responde.
AMOSTRA_POR_PASSADA = 125
PASSADAS = 8

#: O campo que TEM que dar 100%. Se não der, a régua está torta e o bloco cai.
CONTROLE = 'proc'

#: `(nome no ES, coluna no Postgres, rótulo)`. Os nomes DIFEREM — ver o
#: docstring. `partes` no ES é string desnormalizada; no banco é a contagem de
#: `tribunals_processoparte`, então a comparação é "tem parte" contra "tem
#: parte", não texto contra texto.
CAMPOS = [
    ('assunto', 'assunto_nome', 'Assunto'),
    ('codigo_classe', 'classe_codigo', 'Classe'),
    ('grau', 'grau', 'Grau'),
]


def _mil(n) -> str:
    """`103707711` → `103.707.711`.

    Formatado aqui e não no template porque o projeto não instala
    `django.contrib.humanize`, e ligar um app inteiro por causa de um separador
    de milhar é custo desproporcional.
    """
    try:
        return f'{int(n):,}'.replace(',', '.')
    except (TypeError, ValueError):
        return str(n)


def _cheio(v) -> bool:
    if isinstance(v, list):
        return bool(v)
    return v not in (None, '', [], 0)


def _amostra_indice_vs_banco() -> dict | None:
    """Divergência ES × Postgres nos mesmos processos, nos DOIS sentidos."""
    from search.client import get_es

    es = get_es()
    total = es.count(index='voyager-processos')['count']

    campos_es = [CONTROLE, 'id', 'partes'] + [c[0] for c in CAMPOS]
    docs = {}
    for semente in range(PASSADAS):
        r = es.search(index='voyager-processos', size=AMOSTRA_POR_PASSADA,
                      _source=campos_es, request_timeout=30,
                      query={'function_score': {
                          'query': {'match_all': {}},
                          'random_score': {'seed': semente, 'field': '_seq_no'}}})
        for h in r['hits']['hits']:
            docs[h['_source']['id']] = h['_source']
    if not docs:
        return None

    # CONTROLE: sem ele o resto não vale. `proc` é o CNJ — 100% ou a régua
    # está lendo o campo errado (ver docstring do módulo).
    com_controle = sum(1 for d in docs.values() if _cheio(d.get(CONTROLE)))
    if com_controle != len(docs):
        logger.error('integridade: controle %s deu %d/%d — régua torta, bloco '
                     'suprimido', CONTROLE, com_controle, len(docs))
        return {'suprimido': f'campo de controle `{CONTROLE}` deu '
                             f'{com_controle}/{len(docs)}, e tem que dar 100% — '
                             f'a régua está lendo o campo errado'}

    ids = list(docs)
    colunas = ', '.join(f'p.{c[1]}' for c in CAMPOS)
    with transaction.atomic(), connection.cursor() as c:
        c.execute('SET LOCAL statement_timeout = %s', ['60s'])
        c.execute(f"""
            SELECT p.id, {colunas},
                   (SELECT count(*) FROM tribunals_processoparte pp
                     WHERE pp.processo_id = p.id)
              FROM tribunals_process p WHERE p.id = ANY(%s)
        """, [ids])
        pg = {r[0]: r[1:] for r in c.fetchall()}
    if not pg:
        return None

    linhas = []
    for i, (nome_es, _col, rotulo) in enumerate(CAMPOS):
        linhas.append(_comparar(docs, pg, rotulo,
                                lambda d: d.get(nome_es),
                                lambda t, i=i: t[i]))
    linhas.append(_comparar(docs, pg, 'Partes',
                            lambda d: d.get('partes'),
                            lambda t: t[len(CAMPOS)]))
    return {
        'docs_indice': _mil(total),
        'amostra': len(pg),
        'linhas': linhas,
        'divergencia': sum(l['so_pg'] + l['so_es'] for l in linhas),
    }


def _comparar(docs, pg, rotulo, le_es, le_pg) -> dict:
    n_pg = n_es = so_pg = so_es = 0
    for pid, src in docs.items():
        if pid not in pg:
            continue
        v_pg, v_es = _cheio(le_pg(pg[pid])), _cheio(le_es(src))
        n_pg += v_pg
        n_es += v_es
        so_pg += (v_pg and not v_es)
        so_es += (v_es and not v_pg)
    return {'rotulo': rotulo, 'pg': n_pg, 'es': n_es,
            'so_pg': so_pg, 'so_es': so_es}


def _fontes() -> dict:
    """Quem está de pé, quem o vigia pausou, e quem um humano pausou."""
    from enrichers.jobs import _ENRICHERS, _auto_pausados, enrich_pausados

    pausados = enrich_pausados()
    automaticos = _auto_pausados()
    todos = sorted(_ENRICHERS)
    return {
        'total': len(todos),
        'ok': [s for s in todos if s not in pausados],
        'pelo_vigia': sorted(pausados & automaticos),
        'por_humano': sorted(pausados - automaticos),
    }


def _perguntas_poupadas() -> dict:
    """Consultas que NÃO fizemos porque a fonte comprovadamente não tem.

    Não é economia de banda: é `nao_encontrado` que deixou de ser gravado. Esse
    status é uma afirmação SOBRE A FONTE, e dizê-lo depois de perguntar ao
    sistema errado produz confiança falsa.
    """
    from enrichers.jobs import censo_fora_do_esaj

    censo = censo_fora_do_esaj() or {}
    por_tribunal = {}
    for chave, n in censo.items():
        sigla = chave.split('|')[0]
        por_tribunal[sigla] = por_tribunal.get(sigla, 0) + n
    n_total = sum(por_tribunal.values())
    return {
        # `n` é o NÚMERO, e é ele que o template usa pra decidir se mostra o
        # bloco. `total` já vem formatado — e `'0'` é uma string VERDADEIRA no
        # `{% if %}` do Django, então guardar por ela mostraria o bloco zerado.
        'n': n_total,
        'total': _mil(n_total),
        'tribunais': [{'sigla': s, 'n': _mil(n)}
                      for s, n in sorted(por_tribunal.items(), key=lambda kv: -kv[1])],
    }


def calcular() -> dict | None:
    blocos, falhas = {}, []
    for nome, fn in (('indice', _amostra_indice_vs_banco),
                     ('fontes', _fontes),
                     ('poupadas', _perguntas_poupadas)):
        try:
            blocos[nome] = fn()
        except Exception:
            logger.warning('integridade: não consegui medir %s', nome, exc_info=True)
            blocos[nome] = None
            falhas.append(nome)
    if not any(blocos.values()):
        return None
    return {'em': timezone.now().isoformat(), 'nao_medidos': falhas, **blocos}


def aquecer() -> dict | None:
    try:
        p = calcular()
    except Exception:
        logger.error('integridade: aquecimento falhou', exc_info=True)
        return None
    if p:
        cache.set(CHAVE, p, TTL)
        ind = p.get('indice') or {}
        logger.info('integridade: divergência=%s em %s docs · %s fontes ok',
                    ind.get('divergencia'), ind.get('amostra'),
                    len((p.get('fontes') or {}).get('ok') or []))
    return p


def ler():
    """O que a TELA usa. Só cache — o sorteio no ES custa ~25 s (regra nº 7)."""
    return cache.get(CHAVE)
