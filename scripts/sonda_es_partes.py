#!/usr/bin/env python
"""Sonda ES x PG do índice `voyager-processos` — a régua de PARTES e de PRESENÇA.

Roda dentro do container web (`docker exec voyager-web-1 python scripts/sonda_es_partes.py`).

O que ela mede, e por que cada escolha:

1. **PRESENÇA** — quantos processos do Postgres NÃO estão no índice. Por `_mget`
   (realtime GET por `_id`, resposta exata por documento). Nunca `_count`, nunca
   `_cat/indices` (que conta objeto `nested` como doc e já mostrou +16,9 M
   "sobrando" enquanto faltavam 12 M — erra para o lado que ENCERRA a
   investigação).

2. **PARTES, por CONTEÚDO** — `exists` do ES conta string vazia como valor
   presente (regra nº 4 do CLAUDE.md); foi assim que `partes`/`advs` foram
   servidos como 100% valendo 20%. Aqui o `_source` do campo `partes` é LIDO e
   medido pelo tamanho da string.

3. **O CRUZAMENTO que separa dois buracos diferentes** — para cada processo:
   tem `ProcessoParte` no Postgres? tem `partes` no doc? A tabela 2x2 separa
   "o PG não tem parte" (buraco de DADO — é o alvo do E1) de "o PG tem e o doc
   não" (buraco de ÍNDICE — é meu).

Amostra por CONGLOMERADO, igual à de `scripts/auditoria_campos.py`: âncoras
uniformes no espaço de pk e um bloco de pks consecutivos a partir de cada uma
(seeks de índice, não scan). Semente e tamanho DECLARADOS e impressos.
`list(set(x))[:N]` pegaria os menores pks e mediria a parte do acervo que nunca
teve problema — já produziu alarme falso neste projeto.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections import defaultdict

import django

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from django.db import connection, transaction  # noqa: E402

from search import gate  # noqa: E402

#: Semente da rodada. Trocar = outra amostra.
SEMENTE = 20260825
#: Teto de espera do lado do Postgres — nada sem teto (regra nº 7).
PG_TIMEOUT = '120s'


def _controle_positivo() -> None:
    """Verificação com input vazio reporta SUCESSO. A sonda prova que sabe os dois lados."""
    assert _tem_partes({'partes': 'FULANO DE TAL, INSS'}), 'sonda recusou doc COM partes'
    assert not _tem_partes({'partes': ''}), 'sonda aceitou string vazia como parte'
    assert not _tem_partes({}), 'sonda aceitou campo AUSENTE como parte'
    assert not _tem_partes({'partes': '   '}), 'sonda aceitou espaço em branco'


def _tem_partes(src: dict) -> bool:
    return bool((src.get('partes') or '').strip())


def _tem(src: dict, campo: str) -> bool:
    v = src.get(campo)
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    return True


def amostra_pks(n_ancoras: int, bloco: int, seed: int) -> list[tuple[int, str, bool, dict]]:
    """(id, tribunal_id, tem_parte_no_pg, campos_do_pg) da amostra."""
    rng = random.Random(seed)
    with transaction.atomic(), connection.cursor() as cur:
        cur.execute('SET LOCAL statement_timeout = %s', [PG_TIMEOUT])
        cur.execute('SELECT min(id), max(id) FROM tribunals_process')
        lo, hi = cur.fetchone()
    ancoras = sorted(rng.randrange(lo, hi - bloco) for _ in range(n_ancoras))
    linhas: list[tuple] = []
    with transaction.atomic(), connection.cursor() as cur:
        cur.execute('SET LOCAL statement_timeout = %s', [PG_TIMEOUT])
        cur.execute(
            'WITH ancoras(a) AS (SELECT unnest(%s::bigint[])) '
            'SELECT p.id, p.tribunal_id, p.data_autuacao IS NOT NULL, '
            "       coalesce(p.juizo,'') <> '', coalesce(p.classe_nome,'') <> '', "
            "       coalesce(p.assunto_nome,'') <> '', coalesce(p.orgao_julgador_nome,'') <> '', "
            '       p.valor_causa IS NOT NULL, p.segredo_justica '
            'FROM ancoras CROSS JOIN LATERAL ('
            '  SELECT id, tribunal_id, data_autuacao, juizo, classe_nome, assunto_nome, '
            '         orgao_julgador_nome, valor_causa, segredo_justica '
            '  FROM tribunals_process WHERE id >= ancoras.a ORDER BY id LIMIT %s) p',
            [ancoras, bloco])
        linhas = cur.fetchall()
    ids = sorted({r[0] for r in linhas})
    com_parte = _quem_tem_parte(ids)
    return [(r[0], r[1], r[0] in com_parte,
             {'data_autuacao': r[2], 'juizo': r[3], 'classe_nome': r[4],
              'assunto': r[5], 'orgao_julgador': r[6], 'valor_causa': r[7],
              'segredo_justica': r[8]})
            for r in linhas]


def _quem_tem_parte(ids: list[int], chunk: int = 5_000) -> set[int]:
    """Quais destes processos têm ao menos uma linha em `tribunals_processoparte`."""
    achados: set[int] = set()
    for i in range(0, len(ids), chunk):
        fatia = ids[i:i + chunk]
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute('SET LOCAL statement_timeout = %s', [PG_TIMEOUT])
            cur.execute('SELECT DISTINCT processo_id FROM tribunals_processoparte '
                        'WHERE processo_id = ANY(%s)', [fatia])
            achados.update(r[0] for r in cur.fetchall())
    return achados


CAMPOS_ES = ['partes', 'advs', 'data_autuacao', 'juizo', 'classe_nome', 'assunto',
             'orgao_julgador', 'valor_causa', 'segredo_justica', 'grau']


def perguntar_es(ids: list[int]) -> dict[int, dict | None]:
    """`_mget` por id. `None` = doc ausente. Propaga erro do ES de propósito."""
    es = gate._es()
    idx = gate.indice_processos()
    saida: dict[int, dict | None] = {}
    for i in range(0, len(ids), gate.BLOCO_MGET):
        fatia = ids[i:i + gate.BLOCO_MGET]
        r = es.mget(index=idx, ids=[str(x) for x in fatia], source=CAMPOS_ES,
                    request_timeout=gate.ES_TIMEOUT)
        for d in r['docs']:
            saida[int(d['_id'])] = d.get('_source', {}) if d.get('found') else None
    return saida


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--n-ancoras', type=int, default=600)
    ap.add_argument('--bloco', type=int, default=20)
    ap.add_argument('--seed', type=int, default=SEMENTE)
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args()

    _controle_positivo()
    t0 = time.monotonic()
    linhas = amostra_pks(a.n_ancoras, a.bloco, a.seed)
    t_pg = time.monotonic() - t0
    ids = [r[0] for r in linhas]
    t1 = time.monotonic()
    docs = perguntar_es(ids)
    t_es = time.monotonic() - t1

    tot = len(linhas)
    fora = [r for r in linhas if docs.get(r[0]) is None]
    dentro = [r for r in linhas if docs.get(r[0]) is not None]
    # 2x2: PG tem parte? x doc tem parte?
    m = defaultdict(int)
    por_trib: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    campos_perdidos = defaultdict(int)
    for pk, trib, pg_parte, campos in linhas:
        src = docs.get(pk)
        por_trib[trib]['n'] += 1
        por_trib[trib]['pg_parte'] += int(pg_parte)
        if src is None:
            por_trib[trib]['fora'] += 1
            m[('fora', pg_parte)] += 1
            continue
        doc_parte = _tem_partes(src)
        por_trib[trib]['doc_parte'] += int(doc_parte)
        m[(pg_parte, doc_parte)] += 1
        for c in ('data_autuacao', 'juizo', 'classe_nome', 'assunto',
                  'orgao_julgador', 'valor_causa'):
            if campos[c] and not _tem(src, c):
                campos_perdidos[c] += 1

    r = {
        'seed': a.seed, 'n_ancoras': a.n_ancoras, 'bloco': a.bloco,
        'amostrados': tot, 'segundos_pg': round(t_pg, 1), 'segundos_es': round(t_es, 1),
        'fora_do_indice': len(fora),
        'fora_pct': round(100.0 * len(fora) / tot, 2) if tot else None,
        'no_indice': len(dentro),
        'pg_com_parte': sum(1 for r_ in linhas if r_[2]),
        'doc_sem_partes': m[(True, False)] + m[(False, False)],
        'doc_sem_partes_pct': (round(100.0 * (m[(True, False)] + m[(False, False)])
                                     / len(dentro), 2) if dentro else None),
        'matriz': {'pg_sim_doc_sim': m[(True, True)], 'pg_sim_doc_nao': m[(True, False)],
                   'pg_nao_doc_sim': m[(False, True)], 'pg_nao_doc_nao': m[(False, False)],
                   'fora_pg_sim': m[('fora', True)], 'fora_pg_nao': m[('fora', False)]},
        'campos_no_pg_e_nao_no_doc': dict(campos_perdidos),
        'por_tribunal': {t: dict(v) for t, v in sorted(
            por_trib.items(), key=lambda kv: -kv[1]['n'])},
    }
    if a.json:
        print(json.dumps(r, default=str))
        return
    print(f'amostra seed={a.seed} · {a.n_ancoras} âncoras x {a.bloco} pks = '
          f'{tot} processos reais · PG {t_pg:.1f}s · ES {t_es:.1f}s')
    print(f'FORA do índice ....... {len(fora):>7,} / {tot:,} = {r["fora_pct"]}%')
    print(f'no índice ............ {len(dentro):>7,}')
    print(f'  doc SEM partes ..... {r["doc_sem_partes"]:>7,} = {r["doc_sem_partes_pct"]}% '
          f'(medido por CONTEÚDO do campo, não por `exists`)')
    print('\nmatriz PG x doc (só os que estão no índice):')
    print(f'  PG tem parte  & doc tem  {m[(True, True)]:>7,}')
    print(f'  PG tem parte  & doc NAO  {m[(True, False)]:>7,}   <- buraco de ÍNDICE')
    print(f'  PG NAO tem    & doc tem  {m[(False, True)]:>7,}   <- doc velho/parte apagada')
    print(f'  PG NAO tem    & doc NAO  {m[(False, False)]:>7,}   <- buraco de DADO')
    print(f'  fora do índice, PG tem parte  {m[("fora", True)]:>7,}')
    print(f'  fora do índice, PG sem parte  {m[("fora", False)]:>7,}')
    print('\nPG tem o campo e o doc NÃO:')
    for c, k in sorted(campos_perdidos.items(), key=lambda kv: -kv[1]):
        print(f'  {c:<18} {k:>7,}')
    print(f'\n{"tribunal":<10} {"n":>7} {"fora":>7} {"fora%":>7} {"pg_parte%":>10} {"doc_parte%":>11}')
    for t, v in sorted(por_trib.items(), key=lambda kv: -kv[1]['n'])[:30]:
        n = v['n']
        dentro_t = n - v['fora']
        print(f'{t:<10} {n:>7,} {v["fora"]:>7,} {100.0 * v["fora"] / n:>6.1f}% '
              f'{100.0 * v["pg_parte"] / n:>9.1f}% '
              f'{(100.0 * v["doc_parte"] / dentro_t) if dentro_t else 0:>10.1f}%')


if __name__ == '__main__':
    sys.exit(main())
