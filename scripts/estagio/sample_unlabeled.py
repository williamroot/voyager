"""Amostra processos SEM rótulo (fora do conjunto de treino) pra predição cega.

Gera um jsonl com pseudo-rótulos (`classe=DESCONHECIDO`) no mesmo schema da
Fase 1, pra reusar o `build_features.py` sem mudança. Usado na amostra de
validação humana (ex.: 5 EMITIDO novos + 5 DC novos no TJSP).

Uso (host voyager, dentro do container web):
  DATABASE_URL=… python sample_unlabeled.py --tribunal TJSP --n 4000 \
      --excluir estagio_labels.jsonl.gz --out unlabeled_tjsp.jsonl
"""
from __future__ import annotations

import argparse
import gzip
import json
import os


def main() -> None:
    import psycopg  # noqa: PLC0415

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--tribunal', required=True)
    ap.add_argument('--n', type=int, default=4000)
    ap.add_argument('--min-movs', type=int, default=5)
    ap.add_argument('--classes', default='12078,156,15160,15215,12079',
                    help='classes TPU alvo (CSV); use ex. "7" p/ conhecimento (candidatos DC)')
    ap.add_argument('--excluir', help='labels jsonl.gz da Fase 1 (CNJs a excluir)')
    ap.add_argument('--out', required=True)
    args = ap.parse_args()
    classes = [c.strip() for c in args.classes.split(',') if c.strip()]

    excluir: set[str] = set()
    if args.excluir:
        opener = gzip.open if args.excluir.endswith('.gz') else open
        with opener(args.excluir, 'rt') as f:
            for line in f:
                excluir.add(json.loads(line)['numero_cnj'])

    dsn = os.environ['DATABASE_URL'].replace('postgres://', 'postgresql://', 1)
    rows: list[str] = []
    with psycopg.connect(dsn, connect_timeout=20) as conn, conn.cursor() as cur:
        cur.execute('SET statement_timeout = 300000')
        # TABLESAMPLE evita full scan; classe de Cumprimento contra Fazenda =
        # população de interesse do estágio (onde precatório nasce)
        cur.execute("""
            SELECT numero_cnj FROM tribunals_process TABLESAMPLE SYSTEM (2)
            WHERE tribunal_id = %s
              AND total_movimentacoes >= %s
              AND classe_codigo = ANY(%s)
            LIMIT %s
        """, (args.tribunal, args.min_movs, classes, args.n * 3))
        for (cnj,) in cur.fetchall():
            if cnj not in excluir:
                rows.append(cnj)
            if len(rows) >= args.n:
                break

    with open(args.out, 'w') as f:
        for cnj in rows:
            f.write(json.dumps({
                'numero_cnj': cnj, 'tribunal': args.tribunal,
                'classe': 'DESCONHECIDO', 'subtipo': None, 'flags': {},
                'evidencias': [], 'fonte': 'amostra_cega', 'label_ev_dt': None,
            }) + '\n')
    print(f'{len(rows)} CNJs sem rótulo → {args.out}')


if __name__ == '__main__':
    main()
