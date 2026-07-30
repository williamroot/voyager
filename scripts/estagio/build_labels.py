"""FASE 1 — Construtor de rótulos ESTÁGIO DO CRÉDITO (supervisão cruzada autos×público).

Gera rótulos determinísticos e AUDITÁVEIS do estágio de cada processo a partir
das fontes de VERDADE (autos/Falcon) — nunca das movimentações públicas (que são
feature, Fase 2). Cada rótulo carrega a lista de evidências que o gerou.

Classes (hierarquia EMITIDO > PRE > DC; MORTO avaliado por último, só com
evidência forte e sempre SUBTIPADO):
  EMITIDO — precatório/RPV requisitado: doc OFICIO_REQUISITORIO nos autos,
            nº DEPRE/data_oficio/codigo_requisitorio no Falcon, ou
            ALVARA/PAGAMENTO_COMPROVANTE (com sub-flag pagamento_parcial).
  PRE     — cumprimento maduro (CUMPRIMENTO_SENTENCA / homologação de cálculos)
            MAS sem ofício/DEPRE.
  DC      — SENTENCA/ACORDAO/TRANSITO presente, sem cumprimento maduro.
  MORTO   — SÓ com evidência forte, subtipos:
              satisfeito             — extinção pelo pagamento / art. 924 II CPC
                                       (crédito PAGO — desfecho feliz, sinal de
                                       jurimetria "ente pagou")
              improcedente_prescrito — improcedência / prescrição / decadência
                                       COM mérito (morto de verdade)
            Extinção SEM resolução de mérito NÃO é MORTO (pode ser reproposto):
            mantém o estágio + flag extincao_sem_merito. Extinção de INCIDENTE
            (embargos/agravo/impugnação) é ignorada pro estágio do principal.
            Ambiguidade (ex.: falcon.is_extinto sem texto) → NÃO rebaixa; flag
            extincao_natureza_incerta.

Fontes:
  - Zordon acervo (DB 192.168.30.114): Documento.doc_classe (classificação
    determinística dos autos) + MetadadoExtraido.eventos (datas).
  - Falcon/Juriscope (datamodel_process): data_oficio, numero_processo_DEPRE,
    codigo_requisitorio, tipo (PRECATORIO|RPV), is_extinto, sem_expedicao,
    cessao_credito.
  - Scan de texto (subcomando `scan`, roda com Django do Zordon pra decifrar
    chunks): padrões de extinção nos docs SENTENCA/DECISAO/ACORDAO — necessário
    pra subtipar MORTO com evidência textual.

Uso (na máquina zordon, com o venv do zordon):
  set -a; . ~/zordon/.env; set +a
  ~/zordon/.venv/bin/python build_labels.py scan  --out ~/estagio_tmp/extincao_scan.jsonl
  ~/zordon/.venv/bin/python build_labels.py build --scan ~/estagio_tmp/extincao_scan.jsonl \
      --out ~/estagio_tmp/estagio_labels.jsonl.gz

Saída: jsonl.gz com uma linha por processo rotulado:
  {numero_cnj, tribunal, classe, subtipo, flags{rpv, pagamento_parcial,
   extincao_sem_merito, extincao_natureza_incerta, cessao}, evidencias[],
   fonte, label_ev_dt}
O parquet espelho é gerado depois, na máquina de treino (converter_parquet.py).
"""
# ruff: noqa: RUF001, RUF002 — pt-BR usa sinal de multiplicacao
from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('estagio.build_labels')

# ---------------------------------------------------------------------------
# DSNs — mesmos nomes de env do .env do Zordon (nunca hardcodar senha no repo)
# ---------------------------------------------------------------------------

def falcon_dsn() -> str:
    return (
        f"postgres://{os.environ['FALCON_PG_USER']}:{os.environ['FALCON_PG_PASSWORD']}"
        f"@{os.environ['FALCON_PG_HOST']}:{os.environ.get('FALCON_PG_PORT', '5432')}"
        f"/{os.environ['FALCON_PG_NAME']}"
    )


def zordon_dsn() -> str:
    return (
        f"postgres://{os.environ['ZORDON_DB_USER']}:{os.environ['ZORDON_DB_PASSWORD']}"
        f"@{os.environ['ZORDON_DB_HOST']}:{os.environ.get('ZORDON_DB_PORT', '5432')}"
        f"/{os.environ['ZORDON_DB_NAME']}"
    )


# ---------------------------------------------------------------------------
# CNJ helpers
# ---------------------------------------------------------------------------

_TR_ESTADUAL = {
    '01': 'TJAC', '02': 'TJAL', '03': 'TJAP', '04': 'TJAM', '05': 'TJBA',
    '06': 'TJCE', '07': 'TJDFT', '08': 'TJES', '09': 'TJGO', '10': 'TJMA',
    '11': 'TJMT', '12': 'TJMS', '13': 'TJMG', '14': 'TJPA', '15': 'TJPB',
    '16': 'TJPR', '17': 'TJPE', '18': 'TJPI', '19': 'TJRJ', '20': 'TJRN',
    '21': 'TJRS', '22': 'TJRO', '23': 'TJRR', '24': 'TJSC', '25': 'TJSE',
    '26': 'TJSP', '27': 'TJTO',
}


def normalizar_cnj(raw: str) -> str | None:
    """CNJ canônico NNNNNNN-DD.AAAA.J.TR.OOOO a partir de qualquer grafia."""
    d = re.sub(r'\D', '', raw or '')
    if len(d) != 20:
        return None
    return f'{d[0:7]}-{d[7:9]}.{d[9:13]}.{d[13]}.{d[14:16]}.{d[16:20]}'


def tribunal_do_cnj(cnj: str) -> str:
    """Sigla do tribunal derivada do segmento J.TR do CNJ (fonte confiável —
    o campo `tribunal` do acervo tem lixo, ex.: CNJ 8.02 marcado TJSP)."""
    d = re.sub(r'\D', '', cnj)
    j, tr = d[13], d[14:16]
    if j == '4':
        return f'TRF{int(tr)}'
    if j == '8':
        return _TR_ESTADUAL.get(tr, f'TJ{tr}')
    return f'J{j}TR{tr}'


# ---------------------------------------------------------------------------
# Padrões de extinção (scan de texto dos autos) — granularidade obrigatória
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    s = unicodedata.normalize('NFKD', s or '').encode('ascii', 'ignore').decode().lower()
    return re.sub(r'\s+', ' ', s).strip()


# 1. Extinção pelo PAGAMENTO / cumprimento da obrigação (art. 924 II CPC)
#    → MORTO/satisfeito (crédito pago — desfecho feliz).
#    Padrões em ASCII puro: o texto passa por _norm() (remove acentos).
RE_SATISFEITO = re.compile(
    r'extincao pel[oa] pagamento'
    r'|julgo extint[oa][^.]{0,160}(pagamento|satisfacao da obrigacao|cumprimento da obrigacao)'
    r'|extint[oa][^.]{0,80}art[^0-9]{0,8}924[^.]{0,20}\b(ii|2)\b'
    r'|art[^0-9]{0,8}924[^.]{0,15}(inciso )?\b(ii|2)\b'
    r'|satisfacao (integral )?da obrigacao'
    r'|cumprimento (integral )?da obrigacao'
    r'|adimplemento (integral )?da obrigacao')

# 3. Improcedência / prescrição / decadência COM mérito → MORTO/improcedente_prescrito.
RE_IMPROC = re.compile(
    r'julgo (o pedido )?improcedente|improcedencia d[oe] pedido'
    r'|julgo improcedentes'
    r'|(pronuncio|reconheco|decreto|declaro) a prescricao'
    r'|prescricao intercorrente'
    r'|(pronuncio|reconheco|decreto|declaro) a decadencia')

# 2. Extinção SEM resolução de mérito → NÃO é morto (flag extincao_sem_merito).
RE_SEM_MERITO = re.compile(
    r'sem resolucao d[eo] merito|sem julgamento d[eo] merito'
    r'|art[^0-9]{0,8}485\b'
    r'|abandono da causa'
    r'|ausencia de pressupostos'
    r'|ilegitimidade (ativa|passiva|de parte)'
    r'|indefiro a (peticao )?inicial|indeferimento da (peticao )?inicial')

# 4. Incidente/recurso (embargos, agravo, impugnação): extinção do incidente NÃO
#    afeta o estágio do principal — detectado pelo nome/tipo do documento.
RE_INCIDENTE_NOME = re.compile(
    r'embargos|agravo|impugnacao|excecao|exceção|recurso', re.I)

# Classe judicial do cadastro raspado pelo Falcon (data->>'Classe judicial') que
# caracteriza cumprimento/execução contra a Fazenda — base do rótulo PRE quando
# sem_expedicao. Códigos TPU: 12078, 156, 15160, 15215, 12079.
RE_CLASSE_CUMPRIMENTO_AUTOS = re.compile(
    r'CUMPRIMENTO DE SENTEN|EXECU[ÇC][ÃA]O DE T[ÍI]TULO EXTRAJUDICIAL CONTRA A FAZENDA'
    r'|EXECU[ÇC][ÃA]O INVERTIDA', re.I)

_SCAN_DOC_CLASSES = ('SENTENCA', 'ACORDAO', 'DECISAO')
_SCAN_MAX_ORDINAL = 2          # primeiros 3 chunks de cada doc
_SCAN_MAX_DOCS_PER_PROC = 8


def _trecho(texto_norm: str, m: re.Match, largura: int = 90) -> str:
    ini = max(0, m.start() - largura)
    fim = min(len(texto_norm), m.end() + largura)
    return ('…' if ini else '') + texto_norm[ini:fim] + ('…' if fim < len(texto_norm) else '')


# ---------------------------------------------------------------------------
# Subcomando `scan` — decifra chunks (Django do Zordon) e aplica os padrões
# ---------------------------------------------------------------------------

def cmd_scan(args) -> None:  # noqa: PLR0912
    zordon_home = os.path.expanduser(os.environ.get('ZORDON_HOME', '~/zordon'))
    sys.path.insert(0, zordon_home)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'zordon.settings')
    import django  # noqa: PLC0415
    django.setup()
    import psycopg  # noqa: PLC0415
    from acervo.models import Chunk, Documento  # noqa: PLC0415

    # Candidatos: (a) falcon is_extinto; (b) tem ALVARA/PAGAMENTO_COMPROVANTE;
    # (c) tem SENTENCA/ACORDAO/DECISAO mas nenhum OFICIO (DC candidato a morto).
    logger.info('scan: coletando candidatos (falcon is_extinto)…')
    extintos: set[str] = set()
    with psycopg.connect(falcon_dsn(), connect_timeout=20) as fc, fc.cursor(name='cur_ext') as cur:
        cur.execute(
            "SELECT DISTINCT numero_autos FROM datamodel_process "
            "WHERE is_extinto AND numero_autos IS NOT NULL AND numero_autos <> ''")
        for (na,) in cur:
            cnj = normalizar_cnj(na)
            if cnj:
                extintos.add(cnj)
    logger.info('scan: %d CNJs is_extinto no falcon', len(extintos))

    logger.info('scan: coletando flags de docs por processo (acervo)…')
    proc_flags: dict[int, tuple[str, bool, bool, bool]] = {}
    with psycopg.connect(zordon_dsn(), connect_timeout=20) as zc, zc.cursor(name='cur_docs') as cur:
        cur.execute("""
                SELECT p.id, p.numero_cnj,
                       BOOL_OR(d.doc_classe = 'OFICIO_REQUISITORIO'),
                       BOOL_OR(d.doc_classe IN ('ALVARA', 'PAGAMENTO_COMPROVANTE')),
                       BOOL_OR(d.doc_classe IN ('SENTENCA', 'ACORDAO', 'DECISAO'))
                FROM acervo_processo p
                JOIN acervo_documento d ON d.processo_id = p.id
                GROUP BY p.id, p.numero_cnj
            """)
        for pid, cnj_raw, has_oficio, has_pag, has_dec in cur:
            cnj = normalizar_cnj(cnj_raw)
            if cnj and has_dec:
                proc_flags[pid] = (cnj, has_oficio, has_pag, cnj in extintos)

    candidatos = {
        pid: cnj for pid, (cnj, has_oficio, has_pag, is_ext) in proc_flags.items()
        if is_ext or has_pag or not has_oficio
    }
    logger.info('scan: %d processos candidatos (de %d com docs de decisão)',
                len(candidatos), len(proc_flags))

    pids = list(candidatos.keys())
    n_docs = n_hits = 0
    with open(args.out, 'w') as fout:
        for i in range(0, len(pids), 500):
            batch = pids[i:i + 500]
            docs = list(
                Documento.objects
                .filter(processo_id__in=batch, doc_classe__in=_SCAN_DOC_CLASSES)
                .order_by('processo_id', '-id')
                .values_list('id', 'processo_id', 'doc_classe', 'doc_tipo', 'nome_arquivo')
            )
            # cap de docs por processo (mais recentes primeiro — id desc)
            per_proc: dict[int, int] = defaultdict(int)
            keep = []
            for did, pid, dclasse, dtipo, nome in docs:
                if per_proc[pid] < _SCAN_MAX_DOCS_PER_PROC:
                    per_proc[pid] += 1
                    keep.append((did, pid, dclasse, dtipo, nome))
            doc_ids = [k[0] for k in keep]
            meta = {k[0]: k[1:] for k in keep}
            textos: dict[int, list[str]] = defaultdict(list)
            for did, texto in (
                Chunk.objects
                .filter(documento_id__in=doc_ids, ordinal__lte=_SCAN_MAX_ORDINAL)
                .order_by('documento_id', 'ordinal')
                .values_list('documento_id', 'text')
            ):
                if texto:
                    textos[did].append(texto)
            for did, chunks in textos.items():
                n_docs += 1
                pid, dclasse, dtipo, nome = meta[did][0], meta[did][1], meta[did][2], meta[did][3]
                t = _norm(' '.join(chunks))[:12000]
                incidente = bool(RE_INCIDENTE_NOME.search(f'{dtipo} {nome}'))
                for tipo, rx in (('satisfeito', RE_SATISFEITO),
                                 ('improcedente_prescrito', RE_IMPROC),
                                 ('sem_merito', RE_SEM_MERITO)):
                    m = rx.search(t)
                    if m:
                        n_hits += 1
                        fout.write(json.dumps({
                            'numero_cnj': candidatos[pid],
                            'doc_id': did,
                            'doc_classe': dclasse,
                            'doc_nome': (nome or dtipo)[:120],
                            'tipo': tipo,
                            'incidente': incidente,
                            'trecho': _trecho(t, m),
                        }, ensure_ascii=False) + '\n')
            if (i // 500) % 20 == 0:
                logger.info('scan: %d/%d procs, %d docs lidos, %d hits',
                            i, len(pids), n_docs, n_hits)
    logger.info('scan: FIM — %d docs lidos, %d hits → %s', n_docs, n_hits, args.out)


# ---------------------------------------------------------------------------
# Subcomando `build` — agrega fontes e decide o rótulo
# ---------------------------------------------------------------------------

def _carregar_acervo(dsn: str) -> dict[str, Counter]:
    """{cnj: Counter(doc_classe→n)} do acervo."""
    import psycopg  # noqa: PLC0415
    out: dict[str, Counter] = {}
    with psycopg.connect(dsn, connect_timeout=20) as c, c.cursor(name='cur_ac') as cur:
        cur.itersize = 20000
        cur.execute("""
                SELECT p.numero_cnj, d.doc_classe, COUNT(*)
                FROM acervo_processo p
                JOIN acervo_documento d ON d.processo_id = p.id
                WHERE d.doc_classe <> ''
                GROUP BY p.numero_cnj, d.doc_classe
            """)
        for cnj_raw, dclasse, n in cur:
            cnj = normalizar_cnj(cnj_raw)
            if not cnj:
                continue
            out.setdefault(cnj, Counter())[dclasse] = n
    logger.info('acervo: %d processos com doc_classe', len(out))
    return out


def _carregar_eventos(dsn: str) -> dict[str, dict[str, str]]:
    """{cnj: {tipo_evento: data_min}} do MetadadoExtraido."""
    import psycopg  # noqa: PLC0415
    out: dict[str, dict[str, str]] = {}
    with psycopg.connect(dsn, connect_timeout=20) as c, c.cursor(name='cur_ev') as cur:
        cur.itersize = 5000
        cur.execute(
            'SELECT numero_cnj, eventos FROM acervo_metadadoextraido '
            "WHERE eventos IS NOT NULL AND eventos::text <> '[]'")
        for cnj_raw, eventos in cur:
            cnj = normalizar_cnj(cnj_raw)
            if not cnj or not isinstance(eventos, list):
                continue
            d = out.setdefault(cnj, {})
            for ev in eventos:
                tipo = ev.get('tipo')
                data = ev.get('data')
                if not tipo:
                    continue
                if tipo not in d or (data and data < (d[tipo] or '9999')):
                    d[tipo] = data
    logger.info('metadados: %d processos com eventos', len(out))
    return out


def _carregar_falcon(dsn: str) -> dict[str, dict]:
    """Agregado por numero_autos do Falcon (fonte de verdade estruturada)."""
    import psycopg  # noqa: PLC0415
    out: dict[str, dict] = {}
    with psycopg.connect(dsn, connect_timeout=20) as c, c.cursor(name='cur_fa') as cur:
        cur.itersize = 20000
        cur.execute("""
                SELECT numero_autos,
                       BOOL_OR(tipo = 'RPV')                                        AS rpv,
                       BOOL_OR(tipo = 'PRECATORIO')                                 AS prec,
                       MIN(data_oficio)                                             AS data_oficio,
                       MAX(NULLIF("numero_processo_DEPRE", ''))                     AS depre,
                       MAX(NULLIF(codigo_requisitorio, ''))                         AS codreq,
                       BOOL_OR(COALESCE(is_extinto, false))                         AS extinto,
                       BOOL_AND(COALESCE(sem_expedicao, false))                     AS semexp,
                       BOOL_AND(COALESCE(not_found, false))                         AS notfound,
                       BOOL_OR(COALESCE(cessao_credito, false))                     AS cessao,
                       MAX(data->>'Classe judicial')                                AS classe_j
                FROM datamodel_process
                WHERE numero_autos IS NOT NULL AND numero_autos <> ''
                GROUP BY numero_autos
            """)
        for row in cur:
            cnj = normalizar_cnj(row[0])
            if not cnj:
                continue
            out[cnj] = {
                'rpv': row[1], 'prec': row[2],
                'data_oficio': row[3].date().isoformat() if row[3] else None,
                'depre': row[4], 'codreq': row[5],
                'extinto': row[6], 'semexp': row[7],
                'notfound': row[8], 'cessao': row[9],
                'classe_j': row[10] or '',
            }
    logger.info('falcon: %d processos agregados', len(out))
    return out


def _carregar_scan(path: str | None) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    if not path or not os.path.exists(path):
        logger.warning('scan file ausente (%s) — MORTO ficará sub-representado', path)
        return out
    with open(path) as f:
        for line in f:
            row = json.loads(line)
            out[row['numero_cnj']].append(row)
    logger.info('scan: %d processos com hit de extinção', len(out))
    return out


def decidir_rotulo(cnj: str, docs: Counter, eventos: dict[str, str],  # noqa: PLR0912
                   falcon: dict | None, scans: list[dict]) -> dict | None:
    """Regra determinística auditável. Retorna a linha do rótulo ou None."""
    ev: list[str] = []
    flags = {'rpv': False, 'pagamento_parcial': False,
             'extincao_sem_merito': False, 'extincao_natureza_incerta': False,
             'cessao': False}
    datas: list[str] = []

    # --- evidências EMITIDO -------------------------------------------------
    emitido = False
    if docs.get('OFICIO_REQUISITORIO'):
        emitido = True
        ev.append(f"acervo:doc:OFICIO_REQUISITORIO:n={docs['OFICIO_REQUISITORIO']}")
    if falcon:
        if falcon['data_oficio']:
            emitido = True
            ev.append(f"falcon:data_oficio={falcon['data_oficio']}")
            datas.append(falcon['data_oficio'])
        if falcon['depre']:
            emitido = True
            ev.append(f"falcon:numero_processo_DEPRE={falcon['depre']}")
        if falcon['codreq']:
            emitido = True
            ev.append(f"falcon:codigo_requisitorio={falcon['codreq']}")
        if falcon['rpv']:
            flags['rpv'] = True
            ev.append('falcon:tipo=RPV')
        if falcon['cessao']:
            flags['cessao'] = True
    if eventos.get('EXPEDICAO_OFICIO'):
        emitido = True
        ev.append(f"meta:evento:EXPEDICAO_OFICIO@{eventos['EXPEDICAO_OFICIO']}")
        if eventos['EXPEDICAO_OFICIO']:
            datas.append(eventos['EXPEDICAO_OFICIO'])

    tem_pagamento = bool(docs.get('ALVARA') or docs.get('PAGAMENTO_COMPROVANTE')
                         or eventos.get('PAGAMENTO'))
    if tem_pagamento:
        det = []
        if docs.get('ALVARA'):
            det.append(f"ALVARA:n={docs['ALVARA']}")
        if docs.get('PAGAMENTO_COMPROVANTE'):
            det.append(f"PAGAMENTO_COMPROVANTE:n={docs['PAGAMENTO_COMPROVANTE']}")
        if eventos.get('PAGAMENTO'):
            det.append(f"evento:PAGAMENTO@{eventos['PAGAMENTO']}")
        ev.append('acervo:pagamento:' + ','.join(det))
        # pagamento pressupõe requisição emitida
        emitido = True
        flags['pagamento_parcial'] = True

    # --- evidências PRE -----------------------------------------------------
    pre = False
    if docs.get('CUMPRIMENTO_SENTENCA'):
        pre = True
        ev.append(f"acervo:doc:CUMPRIMENTO_SENTENCA:n={docs['CUMPRIMENTO_SENTENCA']}")
    if eventos.get('HOMOLOGACAO_CALCULOS'):
        pre = True
        ev.append(f"meta:evento:HOMOLOGACAO_CALCULOS@{eventos['HOMOLOGACAO_CALCULOS']}")
    if docs.get('PLANILHA_CALCULO') and not emitido:
        pre = True
        ev.append(f"acervo:doc:PLANILHA_CALCULO:n={docs['PLANILHA_CALCULO']}(fraca)")
    # Negativo FORTE do Falcon: a firma abriu os autos e NÃO achou requisitório
    # (sem_expedicao em todas as linhas) num cumprimento/execução contra a
    # Fazenda (classe do CADASTRO RASPADO DOS AUTOS, não do dado público do
    # Voyager) → cumprimento em curso sem ofício = PRE por definição.
    if (not emitido and falcon and falcon['semexp'] and not falcon['extinto']
            and RE_CLASSE_CUMPRIMENTO_AUTOS.search(falcon.get('classe_j') or '')):
        pre = True
        ev.append(f"falcon:sem_expedicao(all)+classe_autos={falcon['classe_j']}")

    # --- evidências DC ------------------------------------------------------
    dc = False
    for k in ('SENTENCA', 'ACORDAO', 'TRANSITO_JULGADO'):
        if docs.get(k):
            dc = True
            ev.append(f'acervo:doc:{k}:n={docs[k]}')
    if eventos.get('TRANSITO_JULGADO'):
        dc = True
        ev.append(f"meta:evento:TRANSITO_JULGADO@{eventos['TRANSITO_JULGADO']}")

    classe = 'EMITIDO' if emitido else 'PRE' if pre else 'DC' if dc else None

    # --- MORTO por último, evidência forte, subtipado -----------------------
    subtipo = None
    scans_principais = [s for s in scans if not s['incidente']]
    tipos_scan = {s['tipo'] for s in scans_principais}

    if 'satisfeito' in tipos_scan:
        s = next(s for s in scans_principais if s['tipo'] == 'satisfeito')
        classe, subtipo = 'MORTO', 'satisfeito'
        ev.append(f"scan:{s['doc_classe']}#{s['doc_id']}:satisfeito:\"{s['trecho']}\"")
    elif falcon and falcon['extinto'] and tem_pagamento and emitido:
        # estruturado: extinto no Falcon + pagamento nos autos = pago/encerrado
        classe, subtipo = 'MORTO', 'satisfeito'
        ev.append('falcon:is_extinto+acervo:pagamento(=extinção pelo pagamento)')
    elif 'improcedente_prescrito' in tipos_scan and not emitido:
        s = next(s for s in scans_principais if s['tipo'] == 'improcedente_prescrito')
        classe, subtipo = 'MORTO', 'improcedente_prescrito'
        ev.append(f"scan:{s['doc_classe']}#{s['doc_id']}:improcedente_prescrito:\"{s['trecho']}\"")
    elif 'improcedente_prescrito' in tipos_scan and emitido:
        # improcedência convivendo com ofício = provável incidente/embargos → incerto
        flags['extincao_natureza_incerta'] = True
        ev.append('scan:improcedencia+oficio(conflito→incerta)')

    if 'sem_merito' in tipos_scan and subtipo is None:
        flags['extincao_sem_merito'] = True
        s = next(s for s in scans_principais if s['tipo'] == 'sem_merito')
        ev.append(f"scan:{s['doc_classe']}#{s['doc_id']}:sem_merito:\"{s['trecho']}\"")

    if falcon and falcon['extinto'] and subtipo is None \
            and not flags['extincao_sem_merito']:
        flags['extincao_natureza_incerta'] = True
        ev.append('falcon:is_extinto(sem evidência textual→incerta)')

    if classe is None:
        return None

    fontes = set()
    for e in ev:
        fontes.add(e.split(':', 1)[0])
    return {
        'numero_cnj': cnj,
        'tribunal': tribunal_do_cnj(cnj),
        'classe': classe,
        'subtipo': subtipo,
        'flags': flags,
        'evidencias': ev,
        'fonte': '+'.join(sorted(fontes)),
        'label_ev_dt': max(datas) if datas else None,
    }


def cmd_build(args) -> None:
    acervo = _carregar_acervo(zordon_dsn())
    eventos = _carregar_eventos(zordon_dsn())
    falcon = _carregar_falcon(falcon_dsn())
    scans = _carregar_scan(args.scan)

    universo = set(acervo) | set(falcon)
    logger.info('universo rotulável: %d CNJs', len(universo))

    dist: Counter = Counter()
    dist_trib: Counter = Counter()
    dist_sub: Counter = Counter()
    n_out = 0
    vazio: Counter = Counter()
    with gzip.open(args.out, 'wt') as fout:
        for cnj in universo:
            fa = falcon.get(cnj)
            # not_found puro e sem mais nada = sem informação
            if fa and fa['notfound'] and cnj not in acervo:
                vazio['falcon_not_found'] += 1
                continue
            row = decidir_rotulo(cnj, acervo.get(cnj, Counter()),
                                 eventos.get(cnj, {}), fa, scans.get(cnj, []))
            if row is None:
                vazio['sem_evidencia'] += 1
                continue
            fout.write(json.dumps(row, ensure_ascii=False) + '\n')
            n_out += 1
            dist[row['classe']] += 1
            dist_trib[(row['classe'], row['tribunal'])] += 1
            if row['subtipo']:
                dist_sub[row['subtipo']] += 1

    logger.info('FIM: %d rótulos → %s', n_out, args.out)
    logger.info('descartados: %s', dict(vazio))
    print('\n=== Distribuição por classe ===')
    for k, v in dist.most_common():
        print(f'  {k:8s} {v:>9,}')
    print('=== Subtipos MORTO ===')
    for k, v in dist_sub.most_common():
        print(f'  {k:24s} {v:>9,}')
    print('=== classe × tribunal (top 40) ===')
    for (cl, tr), v in dist_trib.most_common(40):
        print(f'  {cl:8s} {tr:6s} {v:>9,}')
    with open(args.report, 'w') as f:
        json.dump({
            'total': n_out,
            'classe': dict(dist),
            'subtipos': dict(dist_sub),
            'classe_tribunal': {f'{c}|{t}': v for (c, t), v in dist_trib.items()},
            'descartados': dict(vazio),
        }, f, ensure_ascii=False, indent=2)
    logger.info('report → %s', args.report)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest='cmd', required=True)
    ap_scan = sub.add_parser('scan', help='scan de extinção nos docs (Django Zordon)')
    ap_scan.add_argument('--out', required=True)
    ap_scan.set_defaults(func=cmd_scan)
    ap_build = sub.add_parser('build', help='agrega fontes e grava rótulos')
    ap_build.add_argument('--scan', help='jsonl do subcomando scan')
    ap_build.add_argument('--out', required=True, help='estagio_labels.jsonl.gz')
    ap_build.add_argument('--report', default='estagio_labels_report.json')
    ap_build.set_defaults(func=cmd_build)
    args = ap.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
