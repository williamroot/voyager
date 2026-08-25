#!/usr/bin/env python3
"""Auditoria de completude do DADO: matriz tribunal x campo, por AMOSTRA.

Responde "dos processos que já temos, extraímos tudo que a fonte dá?" — que é
pergunta diferente de "temos o processo?" (essa é a do `.ia/ACERVO_CNJ.md`).
O laudo da rodada de 24-25/08/2026 está em `.ia/ENRICHMENT.md`
§"Auditoria de completude do DADO".

POR QUE POR AMOSTRA, E NÃO POR `COUNT(*)`
-----------------------------------------
1. `exists` do ES conta string vazia como valor presente (regra nº 4 do
   CLAUDE.md) — foi assim que `partes`/`advs` foram servidos como 100%
   valendo 20%. Campo `text` só se mede lendo o conteúdo.
2. `reltuples` é estimativa e `_cat/indices` conta objeto `nested` como doc.
3. Um `GROUP BY tribunal_id` em 102 M de linhas num Postgres disk-I/O-bound
   é carga que não se joga em produção por curiosidade.

DESENHO DA AMOSTRA (conglomerados)
----------------------------------
`N_ANCORAS` âncoras uniformes no intervalo de pk, e de cada uma um bloco de
`BLOCO` pks consecutivos (`WHERE id >= a ORDER BY id LIMIT n`). São seeks de
índice: 120.000 linhas saíram em 38,7 s no pgbouncer de produção.

O bloco é conglomerado — pks vizinhos entraram na mesma janela de ingestão, do
mesmo tribunal. Isso custa efeito de desenho, e ele foi MEDIDO contra a
contagem exata do acervo: a distribuição de `enriquecimento_status` da amostra
bate com a real dentro de **1 ponto percentual**. Diferença menor que isso
entre tribunais não é achado.

Alternativas descartadas: `ORDER BY random()` (scan completo), `TABLESAMPLE
SYSTEM` (mesmo viés de página, sem controle do recorte por tribunal),
amostragem pelo ES (o índice tem 91,6 M dos 102,3 M — recortar por ele
herdaria o buraco do reindex, que é justamente uma das coisas medidas).

USO
---
    DATABASE_URL=postgres://user:senha@host:6432/voyager \
        python3 scripts/auditoria_campos.py --n-ancoras 3000 --bloco 40

Só lê. Precisa de `psql` no PATH (não usa Django — roda de qualquer máquina
que alcance o pgbouncer).
"""
from __future__ import annotations

import argparse
import csv
import os
import random
import subprocess
import sys
import tempfile
import unicodedata
from collections import Counter, defaultdict
from urllib.parse import urlparse

#: Semente da rodada publicada em `.ia/ENRICHMENT.md`. Trocar = outra amostra;
#: os números do laudo só são reproduzíveis com esta.
SEMENTE = 20260824

#: Total exato do acervo na data da medição — usado só para escalar os
#: percentuais da amostra. É contagem, não `reltuples`.
ACERVO = 102_296_406

CAMPOS = (
    'id,tribunal_id,numero_cnj,ano_cnj,classe_codigo,classe_nome,assunto_codigo,'
    'assunto_nome,data_autuacao,valor_causa,orgao_julgador_codigo,orgao_julgador_nome,'
    'juizo,segredo_justica,enriquecimento_status,enriquecido_em,'
    'data_enriquecimento_tribunal,data_enriquecimento_djen,data_enriquecimento_datajud,'
    'total_movimentacoes,classificacao,tem_sinal_precatorio,inserido_em'
)

CAMPOS_TEXTO = ('classe_codigo', 'classe_nome', 'assunto_codigo', 'assunto_nome',
                'orgao_julgador_codigo', 'orgao_julgador_nome', 'juizo')
CAMPOS_NULOS = ('data_autuacao', 'valor_causa')

#: "Preenchido" não é "não-nulo". Estes valores ocupam a coluna sem informar
#: nada — servi-los como dado preenchido é a "confiança falsa" do princípio nº 1.
PLACEHOLDERS = frozenset({
    '', '-', '--', '---', '.', '..', '/', 'n/a', 'na', 'nao informado',
    'nao informada', 'sem informacao', 'sem informacoes', 'indefinido',
    'indefinida', 'null', 'none', 'nulo', 'desconhecido', 'nao consta',
    'sem classe', 'sem assunto', 'sem orgao julgador', 'nao cadastrado',
    'nao especificado', 'outros', 'x', 'xxx', '?',
    '0', '00', '000', '0000', '00000',
})


def normalizar(valor: str) -> str:
    """minúsculas, sem acento, espaços colapsados — para bater com PLACEHOLDERS."""
    texto = (valor or '').strip().lower()
    texto = ''.join(c for c in unicodedata.normalize('NFD', texto)
                    if unicodedata.category(c) != 'Mn')
    return ' '.join(texto.split())


def preenchido(valor: str) -> bool:
    return normalizar(valor) not in PLACEHOLDERS


def controle_positivo() -> None:
    """Sonda que só sabe dizer "vazio" pode estar quebrada.

    Verificação com input vazio reporta SUCESSO — então antes de rodar contra
    produção a sonda precisa PROVAR que sabe devolver não-vazio e que sabe
    recusar placeholder. Levanta AssertionError se algum lado falhar.
    """
    assert preenchido('Procedimento Comum Cível'), 'sonda recusou valor real'
    assert preenchido('12078'), 'sonda recusou código real'
    for ruim in ('', '  ', '-', 'não informado', 'N/A', '0', 'Nulo'):
        assert not preenchido(ruim), f'sonda aceitou placeholder {ruim!r}'


def env_psql() -> dict:
    """DATABASE_URL → variáveis PG* para o psql, sem imprimir a senha."""
    url = os.environ.get('DATABASE_URL')
    if not url:
        sys.exit('defina DATABASE_URL (postgres://user:senha@host:porta/base)')
    p = urlparse(url)
    env = dict(os.environ)
    env.update({
        'PGHOST': p.hostname or 'localhost',
        'PGPORT': str(p.port or 5432),
        'PGUSER': p.username or '',
        'PGPASSWORD': p.password or '',
        'PGDATABASE': (p.path or '/voyager').lstrip('/'),
    })
    return env


def coletar(destino: str, n_ancoras: int, bloco: int, env: dict) -> None:
    """Roda o \\copy da amostra por conglomerados. Só SELECT."""
    rng = random.Random(SEMENTE)
    sql_max = 'SELECT min(id), max(id) FROM tribunals_process'
    saida = subprocess.run(['psql', '-X', '-A', '-t', '-F', '\t', '-c', sql_max],
                           env=env, capture_output=True, text=True, check=True)
    lo, hi = (int(x) for x in saida.stdout.strip().split('\t'))
    ancoras = sorted(rng.randrange(lo, hi - bloco) for _ in range(n_ancoras))
    valores = ','.join(f'({a})' for a in ancoras)
    sel = ','.join('p.' + c for c in CAMPOS.split(','))
    sql = (f'\\copy (WITH ancoras(a) AS (VALUES {valores}) '
           f'SELECT {sel} FROM ancoras CROSS JOIN LATERAL ('
           f'SELECT {CAMPOS} FROM tribunals_process WHERE id >= ancoras.a '
           f'ORDER BY id LIMIT {bloco}) p) '
           f"TO '{destino}' WITH (FORMAT csv, HEADER true)")
    with tempfile.NamedTemporaryFile('w', suffix='.sql', delete=False) as fh:
        fh.write(sql + '\n')
        caminho = fh.name
    subprocess.run(['psql', '-X', '-f', caminho], env=env, check=True)
    os.unlink(caminho)
    print(f'amostra: pk ∈ [{lo}, {hi}] · {n_ancoras} âncoras x {bloco} = '
          f'{n_ancoras * bloco} linhas · semente {SEMENTE}', file=sys.stderr)


def analisar(caminho: str) -> None:
    with open(caminho) as fh:
        linhas = list(csv.DictReader(fh))
    total = len(linhas)
    por_tribunal: dict[str, list[dict]] = defaultdict(list)
    for linha in linhas:
        por_tribunal[linha['tribunal_id']].append(linha)

    cabecalho = ['tribunal', 'acervo_est', 'n', 'ok%', *CAMPOS_TEXTO, *CAMPOS_NULOS,
                 'valor_zero%', 'datajud%', 'pendente%', 'nao_encontrado%', 'erro%']
    print('\t'.join(cabecalho))
    ordenados = sorted(por_tribunal.items(), key=lambda kv: -len(kv[1]))
    for sigla, rs in ordenados:
        n = len(rs)

        def pct(k: int, _n: int = n) -> str:
            return f'{100 * k / _n:.1f}'

        celulas = [sigla, str(round(ACERVO * n / total)), str(n),
                   pct(sum(1 for r in rs if r['enriquecimento_status'] == 'ok'))]
        celulas += [pct(sum(1 for r in rs if preenchido(r[c]))) for c in CAMPOS_TEXTO]
        celulas += [pct(sum(1 for r in rs if r[c] != '')) for c in CAMPOS_NULOS]
        celulas.append(pct(sum(1 for r in rs if r['valor_causa'] not in ('',)
                              and float(r['valor_causa'] or 0) == 0)))
        celulas.append(pct(sum(1 for r in rs if r['data_enriquecimento_datajud'] != '')))
        for st in ('pendente', 'nao_encontrado', 'erro'):
            celulas.append(pct(sum(1 for r in rs if r['enriquecimento_status'] == st)))
        print('\t'.join(celulas))

    # --- as três causas de campo vazio, que pedem ações opostas -------------
    c: Counter = Counter()
    for r in linhas:
        c['st_' + r['enriquecimento_status']] += 1
        tem = (bool(r['classe_codigo']), bool(r['assunto_codigo']), bool(r['data_autuacao']))
        if not any(tem):
            c['esqueleto_vazio'] += 1
            c['esqueleto_vazio_' + r['enriquecimento_status']] += 1
        if r['enriquecimento_status'] in ('nao_encontrado', 'erro') \
                and not r['data_enriquecimento_datajud']:
            c['sem_segunda_porta'] += 1
    print('\n# agregados nacionais (amostra → escala)', file=sys.stderr)
    for chave in sorted(c):
        print(f'{chave:34} {c[chave]:>7} ({100 * c[chave] / total:5.2f}%) '
              f'≈ {round(ACERVO * c[chave] / total):>12,}'.replace(',', '.'),
              file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--n-ancoras', type=int, default=3000)
    ap.add_argument('--bloco', type=int, default=40)
    ap.add_argument('--csv', default='amostra.csv',
                    help='caminho do CSV da amostra (reusa se já existir)')
    ap.add_argument('--so-analisar', action='store_true',
                    help='pula a coleta e analisa o CSV existente')
    args = ap.parse_args()

    controle_positivo()
    if not args.so_analisar:
        coletar(os.path.abspath(args.csv), args.n_ancoras, args.bloco, env_psql())
    analisar(args.csv)


if __name__ == '__main__':
    main()
