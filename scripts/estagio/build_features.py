"""FASE 2 — Features 100% PÚBLICAS pro classificador de ESTÁGIO DO CRÉDITO.

Anti-leakage é o requisito nº 1: TODAS as features saem do dado PÚBLICO do
Voyager (cadastro do tribunal + movimentações DJEN/Datajud + partes do
enricher público). NENHUMA feature vem de autos/Falcon — essas fontes são
exclusivamente rótulo (Fase 1). Campos `Process.classificacao*` (saída do
classificador de leads) também são PROIBIDOS como feature.

Origem de cada feature (auditável):
  Process (cadastro público):
    ano_cnj, classe_codigo/classe_nome, assunto_codigo, data_autuacao,
    total_movimentacoes, primeira/ultima_movimentacao_em, tribunal
  Movimentacao (texto público DJEN/Datajud) — agregado numa única query
    SARGável por processo_id (extensão do _MOVS_AGG_SQL do classificador v6):
    contagens e datas de marcos: expedição de precatório/RPV, trânsito,
    homologação de cálculos, cumprimento, pagamento (alvará/sequestro),
    desfecho negativo (satisfeito × sem mérito × improcedência — mesma
    granularidade dos rótulos, porém lida do TEXTO PÚBLICO)
  ProcessoParte (enricher público): contagem de partes, ente público no polo
    passivo (regex no nome)

Extração best-effort (saída informativa + feature, nullable — ABSTÉM quando
não há):
  valor_homologado: R$ próximo de "homolog" no texto público das movs
  partes_beneficiarias: nomes do polo ativo (papéis autor/exequente/…)

Uso (no host voyager, LAN com o DB; NUNCA rodar de fora da LAN):
  DATABASE_URL=postgres://… python3 build_features.py \
      --labels estagio_labels.jsonl.gz --out estagio_features.csv.gz \
      --snapshot 2026-07-30 --workers 6 --cap-majoritaria 60000

Saída: CSV gz com 1 linha por processo casado no Voyager (rótulo + features +
saídas informativas). Conversão pra parquet acontece na máquina de treino.
"""
# ruff: noqa: RUF002 — pt-BR usa sinal de multiplicacao
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import logging
import os
import queue
import re
import threading
from datetime import date, datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('estagio.build_features')

# --- catálogos públicos (mesmos do classificador v6/v7) ----------------------
CLASSES_CUMPRIMENTO = {'12078', '156', '15160', '15215', '12079'}
CLASSES_FAZENDA_PUBLICA = {'12078', '12079', '15215'}
RE_JUIZADO = re.compile(r'juizado especial|recurso inominado|procedimento comum', re.I)
RE_ENTE_PUBLICO = re.compile(
    r'\b(uniao|união|municipio|município|estado d|fazenda|inss|instituto nacional'
    r'|autarquia|prefeitura|df|distrito federal|funda[çc][ãa]o p[úu]blica)\b', re.I)
RE_VALOR_BRL = re.compile(r'R\$\s*([\d.]{1,15},\d{2})')

# Agregado de movimentações — extensão do _MOVS_AGG_SQL (F1-F30) do
# tribunals/classificador.py. Uma passada por processo, SARGável (índice em
# processo_id). Todos os padrões operam sobre TEXTO PÚBLICO da comunicação.
MOVS_AGG_SQL = """
    SELECT
        COUNT(*) AS total_movs,
        COUNT(DISTINCT CASE WHEN tipo_comunicacao <> '' THEN tipo_comunicacao END) AS distinct_tipos,
        COALESCE(SUM(CASE WHEN tipo_comunicacao IN ('Expedição de precatório/rpv','Precatório')
                          THEN 1 ELSE 0 END), 0) AS exped_tc_n,
        COALESCE(SUM(CASE WHEN texto ~* 'precat[óo]rio'                 THEN 1 ELSE 0 END), 0) AS precat_text_n,
        COALESCE(SUM(CASE WHEN texto ~* '\\mrpv\\M|requisi[çc][ãa]o de pequeno valor'
                          THEN 1 ELSE 0 END), 0) AS rpv_text_n,
        COALESCE(SUM(CASE WHEN texto ~* 'requisi[çc][ãa]o de pagamento' THEN 1 ELSE 0 END), 0) AS reqpag_text_n,
        COALESCE(SUM(CASE WHEN texto ~* 'of[íi]cio requisit[óo]rio'     THEN 1 ELSE 0 END), 0) AS oficio_text_n,
        COALESCE(SUM(CASE WHEN texto ~* 'precat[óo]rio expedido|rpv expedida|of[íi]cio requisit[óo]rio expedido|requisi[çc][ãa]o de pagamento de pequeno valor enviada|requisi[çc][ãa]o de pagamento de precat[óo]rio enviada|determinada expedi[çc][ãa]o de precat[óo]rio|determinada expedi[çc][ãa]o de rpv|expedi[çc][ãa]o de requisi[çc][ãa]o de pagamento'
                          THEN 1 ELSE 0 END), 0) AS exped_forte_n,
        COALESCE(SUM(CASE WHEN texto ~* 'cancelamento de precat[óo]rio|cancelamento de rpv|revoga[çc][ãa]o de precat[óo]rio|revoga[çc][ãa]o de rpv'
                          THEN 1 ELSE 0 END), 0) AS cancel_n,
        COALESCE(SUM(CASE WHEN texto ~* 'tr[âa]nsito em julgado|transitad[oa] em julgado'
                          THEN 1 ELSE 0 END), 0) AS transito_n,
        COALESCE(SUM(CASE WHEN texto ~* 'homologo|homolga|homologa[çc][ãa]o' AND texto ~* 'c[áa]lculo|valor|conta'
                          THEN 1 ELSE 0 END), 0) AS homolog_n,
        COALESCE(SUM(CASE WHEN texto ~* 'cumprimento de senten[çc]a'    THEN 1 ELSE 0 END), 0) AS cumpr_text_n,
        COALESCE(SUM(CASE WHEN texto ~* 'alvar[áa]\\s+(judicial|de\\s+levantamento)|expe[çc]am?-se\\s+(o\\s+)?alvar[áa]|autorizo[^.]{0,150}sequestro|defiro[^.]{0,100}sequestro|sequestro\\s+do\\s+numer[áa]rio|mandado\\s+de\\s+levantamento'
                          THEN 1 ELSE 0 END), 0) AS pago_n,
        COALESCE(SUM(CASE WHEN texto ~* 'extin[çc][ãa]o pel[oa] pagamento|julgo\\s+extint[oa][^.]{0,160}(pagamento|satisfa[çc][ãa]o da obriga[çc][ãa]o|cumprimento da obriga[çc][ãa]o)|art[^0-9]{0,8}924[^.]{0,15}(inciso\\s+)?II'
                          THEN 1 ELSE 0 END), 0) AS ext_satisf_n,
        COALESCE(SUM(CASE WHEN texto ~* 'sem\\s+resolu[çc][ãa]o\\s+d[eo]\\s+m[ée]rito|art[^0-9]{0,8}485|indefer\\w+\\s+a\\s+(peti[çc][ãa]o|inicial|exordial)'
                          THEN 1 ELSE 0 END), 0) AS ext_semmerito_n,
        COALESCE(SUM(CASE WHEN texto ~* 'improceden|(decreto|reconhe[çc]o|pronuncio)\\s+a\\s+prescri|prescri[çc][ãa]o\\s+intercorrente|nego\\s+provimento'
                          THEN 1 ELSE 0 END), 0) AS improc_n,
        MIN(data_disponibilizacao) AS mov_min_dt,
        MAX(data_disponibilizacao) AS mov_max_dt,
        MIN(CASE WHEN texto ~* 'precat[óo]rio expedido|rpv expedida|of[íi]cio requisit[óo]rio expedido|requisi[çc][ãa]o de pagamento de pequeno valor enviada|requisi[çc][ãa]o de pagamento de precat[óo]rio enviada|determinada expedi[çc][ãa]o de precat[óo]rio|determinada expedi[çc][ãa]o de rpv|expedi[çc][ãa]o de requisi[çc][ãa]o de pagamento'
                 OR tipo_comunicacao IN ('Expedição de precatório/rpv','Precatório')
                 THEN data_disponibilizacao END) AS exped_min_dt,
        MAX(CASE WHEN texto ~* 'precat[óo]rio expedido|rpv expedida|of[íi]cio requisit[óo]rio expedido|requisi[çc][ãa]o de pagamento de pequeno valor enviada|requisi[çc][ãa]o de pagamento de precat[óo]rio enviada|determinada expedi[çc][ãa]o de precat[óo]rio|determinada expedi[çc][ãa]o de rpv|expedi[çc][ãa]o de requisi[çc][ãa]o de pagamento'
                 OR tipo_comunicacao IN ('Expedição de precatório/rpv','Precatório')
                 THEN data_disponibilizacao END) AS exped_max_dt,
        MIN(CASE WHEN texto ~* 'tr[âa]nsito em julgado|transitad[oa] em julgado'
                 THEN data_disponibilizacao END) AS transito_min_dt,
        MAX(CASE WHEN texto ~* 'homologo|homologa[çc][ãa]o' AND texto ~* 'c[áa]lculo|valor|conta'
                 THEN data_disponibilizacao END) AS homolog_max_dt,
        MAX(CASE WHEN texto ~* 'alvar[áa]\\s+(judicial|de\\s+levantamento)|expe[çc]am?-se\\s+(o\\s+)?alvar[áa]|autorizo[^.]{0,150}sequestro|defiro[^.]{0,100}sequestro|sequestro\\s+do\\s+numer[áa]rio|mandado\\s+de\\s+levantamento'
                 THEN data_disponibilizacao END) AS pago_max_dt,
        MAX(CASE WHEN texto ~* 'improceden|(decreto|reconhe[çc]o|pronuncio)\\s+a\\s+prescri|sem\\s+resolu[çc][ãa]o\\s+d[eo]\\s+m[ée]rito|julgo\\s+extint'
                 THEN data_disponibilizacao END) AS extneg_max_dt
    FROM tribunals_movimentacao
    WHERE processo_id = %s
"""

VALOR_HOMOLOG_SQL = """
    SELECT texto FROM tribunals_movimentacao
    WHERE processo_id = %s
      AND texto ~* 'homolog'
      AND texto ~* 'R\\$'
    ORDER BY data_disponibilizacao DESC
    LIMIT 1
"""

PARTES_SQL = """
    SELECT pa.nome, pp.polo, pp.papel
    FROM tribunals_processoparte pp
    JOIN tribunals_parte pa ON pa.id = pp.parte_id
    WHERE pp.processo_id = %s AND pp.representa_id IS NULL
    LIMIT 60
"""

CSV_COLS = [
    # identificação + rótulo (passthrough da Fase 1 — NÃO são features)
    'numero_cnj', 'tribunal', 'classe', 'subtipo', 'flag_rpv',
    'flag_pagamento_parcial', 'flag_extincao_sem_merito',
    'flag_extincao_natureza_incerta', 'fonte', 'label_ev_dt',
    # cadastro público
    'ano_cnj', 'classe_codigo', 'is_cumprimento', 'is_fazenda', 'is_juizado_anti',
    'assunto_codigo', 'dias_autuacao',
    # movs agregadas
    'total_movs', 'distinct_tipos', 'exped_tc_n', 'precat_text_n', 'rpv_text_n',
    'reqpag_text_n', 'oficio_text_n', 'exped_forte_n', 'cancel_n', 'transito_n',
    'homolog_n', 'cumpr_text_n', 'pago_n', 'ext_satisf_n', 'ext_semmerito_n',
    'improc_n',
    # temporais derivadas
    'dias_ult_mov', 'duracao_dias', 'movs_por_ano',
    'tem_exped', 'dias_desde_exped', 'dias_transito_a_exped',
    'tem_transito', 'dias_desde_transito', 'tem_homolog', 'dias_desde_homolog',
    'tem_pago', 'dias_desde_pago', 'pago_pos_exped', 'extneg_pos_exped',
    # partes públicas
    'n_partes', 'tem_ente_publico_passivo',
    # best-effort informativos
    'valor_homologado', 'partes_beneficiarias',
]


def normalizar_cnj_digits(cnj: str) -> str:
    return re.sub(r'\D', '', cnj or '')


def _hash_frac(cnj: str) -> float:
    h = hashlib.sha1(cnj.encode()).hexdigest()[:8]
    return int(h, 16) / 0xFFFFFFFF


_CLASSES_CAPADAS = {'EMITIDO', 'PRE'}   # classes majoritárias; DC/MORTO entram inteiras


def carregar_labels(path: str, cap_majoritaria: int) -> list[dict]:
    """Lê rótulos; cap determinístico (hash-CNJ) nas classes majoritárias
    (EMITIDO/PRE) por tribunal — DC e MORTO entram inteiras."""
    rows = []
    opener = gzip.open if path.endswith('.gz') else open
    with opener(path, 'rt') as f:
        for line in f:
            rows.append(json.loads(line))
    if not cap_majoritaria:
        return rows
    from collections import Counter  # noqa: PLC0415
    tot = Counter((r['classe'], r['tribunal']) for r in rows
                  if r['classe'] in _CLASSES_CAPADAS)
    keep = []
    for r in rows:
        chave = (r['classe'], r['tribunal'])
        if r['classe'] not in _CLASSES_CAPADAS or tot[chave] <= cap_majoritaria or _hash_frac(r['numero_cnj']) < cap_majoritaria / tot[chave]:
            keep.append(r)
    logger.info('labels: %d → %d após cap %d/classe-tribunal (EMITIDO/PRE)',
                len(rows), len(keep), cap_majoritaria)
    return keep


def resolver_processos(conn, labels: list[dict]) -> list[tuple]:
    """CNJ → (processo_id, campos de cadastro). Batches de 2000, match por
    (numero_cnj, tribunal) — tribunal do rótulo vem do próprio CNJ."""
    out = []
    por_cnj = {r['numero_cnj']: r for r in labels}
    cnjs = list(por_cnj.keys())
    with conn.cursor() as cur:
        cur.execute('SET statement_timeout = 120000')
        for i in range(0, len(cnjs), 2000):
            batch = cnjs[i:i + 2000]
            cur.execute("""
                SELECT id, numero_cnj, tribunal_id, ano_cnj, classe_codigo,
                       classe_nome, assunto_codigo, data_autuacao,
                       total_movimentacoes
                FROM tribunals_process
                WHERE numero_cnj = ANY(%s)
            """, (batch,))
            for row in cur.fetchall():
                lab = por_cnj.get(row[1])
                if lab and lab['tribunal'] == row[2]:
                    out.append((lab, row))
            if (i // 2000) % 50 == 0:
                logger.info('resolver: %d/%d cnjs, %d matched', i, len(cnjs), len(out))
    logger.info('resolver: %d/%d rótulos casaram com Process no Voyager',
                len(out), len(cnjs))
    return out


def _dias(a, b) -> float | None:
    """(b - a) em dias; aceita date/datetime; None se faltar."""
    if a is None or b is None:
        return None
    if isinstance(a, datetime):
        a = a.date()
    if isinstance(b, datetime):
        b = b.date()
    return (b - a).days


def extrair_um(cur, lab: dict, prow: tuple, snapshot: date) -> dict:
    (pid, cnj, trib, ano_cnj, classe_cod, classe_nome, assunto_cod,
     data_aut, _total) = prow
    cur.execute(MOVS_AGG_SQL, [pid])
    m = cur.fetchone()
    (total_movs, distinct_tipos, exped_tc_n, precat_text_n, rpv_text_n,
     reqpag_text_n, oficio_text_n, exped_forte_n, cancel_n, transito_n,
     homolog_n, cumpr_text_n, pago_n, ext_satisf_n, ext_semmerito_n, improc_n,
     mov_min_dt, mov_max_dt, exped_min_dt, exped_max_dt, transito_min_dt,
     homolog_max_dt, pago_max_dt, extneg_max_dt) = m

    valor_homolog = None
    if total_movs and homolog_n:
        cur.execute(VALOR_HOMOLOG_SQL, [pid])
        r = cur.fetchone()
        if r and r[0]:
            mv = RE_VALOR_BRL.search(r[0])
            if mv:
                try:
                    valor_homolog = float(mv.group(1).replace('.', '').replace(',', '.'))
                except ValueError:
                    valor_homolog = None

    n_partes = 0
    tem_ente = 0
    beneficiarias: list[str] = []
    cur.execute(PARTES_SQL, [pid])
    for nome, polo, papel in cur.fetchall():
        n_partes += 1
        if polo == 'passivo' and RE_ENTE_PUBLICO.search(nome or ''):
            tem_ente = 1
        if polo == 'ativo' and len(beneficiarias) < 5 \
                and not re.search(r'advogad', (papel or ''), re.I):
            beneficiarias.append(nome)

    flags = lab.get('flags') or {}
    row = {
        'numero_cnj': cnj, 'tribunal': trib,
        'classe': lab['classe'], 'subtipo': lab.get('subtipo') or '',
        'flag_rpv': int(bool(flags.get('rpv'))),
        'flag_pagamento_parcial': int(bool(flags.get('pagamento_parcial'))),
        'flag_extincao_sem_merito': int(bool(flags.get('extincao_sem_merito'))),
        'flag_extincao_natureza_incerta': int(bool(flags.get('extincao_natureza_incerta'))),
        'fonte': lab.get('fonte', ''), 'label_ev_dt': lab.get('label_ev_dt') or '',
        'ano_cnj': ano_cnj or 0,
        'classe_codigo': classe_cod or '',
        'is_cumprimento': int((classe_cod or '') in CLASSES_CUMPRIMENTO),
        'is_fazenda': int((classe_cod or '') in CLASSES_FAZENDA_PUBLICA),
        'is_juizado_anti': int(bool(RE_JUIZADO.search(classe_nome or ''))),
        'assunto_codigo': assunto_cod or '',
        'dias_autuacao': _dias(data_aut, snapshot),
        'total_movs': total_movs or 0, 'distinct_tipos': distinct_tipos or 0,
        'exped_tc_n': exped_tc_n, 'precat_text_n': precat_text_n,
        'rpv_text_n': rpv_text_n, 'reqpag_text_n': reqpag_text_n,
        'oficio_text_n': oficio_text_n, 'exped_forte_n': exped_forte_n,
        'cancel_n': cancel_n, 'transito_n': transito_n, 'homolog_n': homolog_n,
        'cumpr_text_n': cumpr_text_n, 'pago_n': pago_n,
        'ext_satisf_n': ext_satisf_n, 'ext_semmerito_n': ext_semmerito_n,
        'improc_n': improc_n,
        'dias_ult_mov': _dias(mov_max_dt, snapshot),
        'duracao_dias': _dias(mov_min_dt, mov_max_dt),
        'movs_por_ano': round((total_movs or 0) / max((_dias(mov_min_dt, mov_max_dt) or 0) / 365.25, 0.1), 3),
        'tem_exped': int(exped_max_dt is not None),
        'dias_desde_exped': _dias(exped_max_dt, snapshot),
        'dias_transito_a_exped': _dias(transito_min_dt, exped_min_dt),
        'tem_transito': int(transito_min_dt is not None),
        'dias_desde_transito': _dias(transito_min_dt, snapshot),
        'tem_homolog': int(homolog_max_dt is not None),
        'dias_desde_homolog': _dias(homolog_max_dt, snapshot),
        'tem_pago': int(pago_max_dt is not None),
        'dias_desde_pago': _dias(pago_max_dt, snapshot),
        'pago_pos_exped': int(bool(pago_max_dt)
                              and (exped_max_dt is None or pago_max_dt >= exped_max_dt)),
        'extneg_pos_exped': int(bool(extneg_max_dt)
                                and (exped_max_dt is None or extneg_max_dt >= exped_max_dt)),
        'n_partes': n_partes, 'tem_ente_publico_passivo': tem_ente,
        'valor_homologado': valor_homolog if valor_homolog is not None else '',
        'partes_beneficiarias': '; '.join(beneficiarias),
    }
    return row  # noqa: RET504 — nome ajuda o debug


def main() -> None:
    import psycopg  # noqa: PLC0415  # noqa: PLC0415

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--labels', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--snapshot', default=date.today().isoformat())
    ap.add_argument('--workers', type=int, default=6)
    ap.add_argument('--cap-majoritaria', type=int, default=60000,
                    help='cap determinístico de EMITIDO/PRE por tribunal (hash-CNJ)')
    args = ap.parse_args()
    dsn = os.environ['DATABASE_URL'].replace('postgres://', 'postgresql://', 1)
    snapshot = date.fromisoformat(args.snapshot)

    labels = carregar_labels(args.labels, args.cap_majoritaria)

    # resume: pula CNJs já no output
    feitos: set[str] = set()
    if os.path.exists(args.out):
        with gzip.open(args.out, 'rt') as f:
            rd = csv.DictReader(f)
            for r in rd:
                feitos.add(r['numero_cnj'])
        logger.info('resume: %d já extraídos', len(feitos))
    labels = [r for r in labels if r['numero_cnj'] not in feitos]

    with psycopg.connect(dsn, connect_timeout=20) as conn:
        pares = resolver_processos(conn, labels)

    q: queue.Queue = queue.Queue()
    for p in pares:
        q.put(p)
    out_lock = threading.Lock()
    stats = {'ok': 0, 'err': 0}
    mode = 'at' if feitos else 'wt'
    fout = gzip.open(args.out, mode)  # noqa: SIM115 — fechado após join dos workers
    writer = csv.DictWriter(fout, fieldnames=CSV_COLS)
    if not feitos:
        writer.writeheader()

    def worker():
        conn = psycopg.connect(dsn, connect_timeout=20)
        cur = conn.cursor()
        cur.execute('SET statement_timeout = 60000')
        conn.commit()
        while True:
            try:
                lab, prow = q.get_nowait()
            except queue.Empty:
                break
            try:
                row = extrair_um(cur, lab, prow, snapshot)
                with out_lock:
                    writer.writerow(row)
                    stats['ok'] += 1
                    if stats['ok'] % 5000 == 0:
                        logger.info('extraídos %d (%d err, fila %d)',
                                    stats['ok'], stats['err'], q.qsize())
                        fout.flush()
            except Exception as e:
                conn.rollback()
                stats['err'] += 1
                if stats['err'] < 30:
                    logger.warning('erro em %s: %s', prow[1], e)
            q.task_done()
        conn.close()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(args.workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    fout.close()
    logger.info('FIM: %d ok, %d err → %s', stats['ok'], stats['err'], args.out)


if __name__ == '__main__':
    main()
