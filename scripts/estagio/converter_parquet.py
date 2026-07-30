"""Converte as saídas das Fases 1/2 (jsonl.gz / csv.gz) pra parquet.

Roda na máquina de treino (venv com pandas+pyarrow). Os datasets ficam FORA
do git (scripts/estagio/out/ é gitignored); o vínculo é o md5 registrado em
`.ia/MODELOS.md`.

Uso:
  python converter_parquet.py --labels out/estagio_labels.jsonl.gz
  python converter_parquet.py --features out/estagio_features.csv.gz
"""
from __future__ import annotations

import argparse
import gzip
import json

import pandas as pd


def labels_para_parquet(path: str) -> str:
    rows = []
    with gzip.open(path, 'rt') as f:
        for line in f:
            r = json.loads(line)
            flags = r.pop('flags', {})
            for k, v in flags.items():
                r[f'flag_{k}'] = bool(v)
            r['evidencias'] = json.dumps(r.get('evidencias', []), ensure_ascii=False)
            rows.append(r)
    df = pd.DataFrame(rows)
    out = path.replace('.jsonl.gz', '.parquet')
    df.to_parquet(out, index=False)
    print(f'{out}: {len(df):,} linhas')
    return out


def features_para_parquet(path: str) -> str:
    df = pd.read_csv(path, dtype={'classe_codigo': str, 'assunto_codigo': str},
                     low_memory=False)
    out = path.replace('.csv.gz', '.parquet')
    df.to_parquet(out, index=False)
    print(f'{out}: {len(df):,} linhas')
    return out


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--labels')
    ap.add_argument('--features')
    args = ap.parse_args()
    if args.labels:
        labels_para_parquet(args.labels)
    if args.features:
        features_para_parquet(args.features)
