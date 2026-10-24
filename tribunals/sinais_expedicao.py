"""Sinais de expedição em lote: QUANDO o requisitório apareceu no diário.

Consumido pelo vigia de expedição do JURISCOPE (`POST /api/v1/leads/sinais/`):
o processo já foi coletado lá, o ofício ainda não existia, e a pergunta é
"saiu alguma coisa depois disso?". O classificador responde "tem sinal" (uma
feature 0/1); aqui a resposta é a DATA da última movimentação que casa o sinal,
qual sinal foi, um trecho dela e os vetos com a data de cada um.

## Mesma regra do classificador, sem cópia

Os padrões vêm das constantes de `tribunals/classificador.py` — as mesmas que
montam o `_MOVS_AGG_SQL`. Mexeu numa regex lá, mudou aqui. Os vetos usam as
mesmas funções (`f24_pago_pos_exped`, `f30_extinto_neg`, `f32_convertido`).

## Qual sinal vale em cada tribunal

Só os sinais de EVENTO de expedição: F2 (tipo de comunicação "Expedição de
precatório/rpv" / "Precatório"), F7 ("Enviada ao Tribunal" / "Preparada para
Envio") e F20 (particípio: "precatório expedido", "requisição ... enviada").
O F14 ("ofício requisitório" no texto) fica de fora em todo tribunal: no TJMG
83% dele é ORDEM ("expeça-se") ou ambíguo — ver `PRECATORIO_SINAL_FEATURES`.

O TJRS usa só o F31, como na regra de promoção do classificador: lá o F20 casa
a ordem ("determinada expedição", 0/4 com ofício) e a RPV da SEFAZ.

## Custo

Uma passagem pelas movimentações dos processos pedidos, entrando pelo índice
`(processo_id, data_disponibilizacao DESC)` — nunca pela tabela inteira. Cada
regex roda uma vez por movimentação (CTE materializada); o trecho é lido
depois, por pk, só das movimentações escolhidas.
"""

from __future__ import annotations

import html
import re
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date, datetime

from django.db import connection, transaction
from django.utils import timezone

from .classificador import (
    CONVERSAO_RPV_TJRS_SQL,
    EXTINCAO_NEGATIVA_SQL,
    EXTINCAO_SATISFEITA_SQL,
    EXTINCAO_SATISFEITA_VETO_SQL,
    F19_CANCELAMENTO_SQL,
    F20_EXPEDICAO_SQL,
    INTIMACAO_EXPEDICAO_PRECATORIO_TJRS_SQL,
    PAGAMENTO_SQL,
    PRECATORIO_SINAL_FEATURES,
    SATISFACAO_SINAL_TRIBUNAIS,
    SINAL_COORDENADORIA_TRIBUNAIS,
    SINAL_EXPEDICAO_PRECATORIO_TJRS_SQL,
    SINAL_PRECATORIO_TJPA_SQL,
    TIPOS_F2_EXPEDICAO,
    TIPOS_F7_ENVIO,
    VETO_CONVERSAO_RPV_TJRS_SQL,
    f24_pago_pos_exped,
    f30_extinto_neg,
    f32_convertido,
    f39_extinto_satisfeito,
)
from .cnj import partes
from .models import Process

LIMITE_CNJS = 5000
# Processos por consulta de movimentações (ver `calcular_sinais`).
LOTE_SQL = 250
TRECHO_MAX = 160

F2 = 'F2_precat_tc'
F7 = 'F7_envTrib_tc'
F20 = 'F20_exp_juriscope'
F31 = 'F31_exp_precatorio_tjrs'

SINAIS_PADRAO = (F2, F7, F20)
# O TJRS herda a lista da regra de promoção do classificador (só F31).
SINAIS_POR_TRIBUNAL = {'TJRS': PRECATORIO_SINAL_FEATURES['TJRS']}

# Desempate quando a mesma movimentação casa mais de um sinal.
_ORDEM_SINAIS = (F2, F7, F20, F31)

# Coluna da CTE para cada feature de sinal.
_COLUNA = {F2: 'f2', F7: 'f7', F20: 'f20', F31: 'f31'}

# Chars antes do casamento que entram no trecho (o resto vem depois dele).
_ANTES_DO_CASAMENTO = 50
# Lê mais que o trecho final: as entidades HTML do TJRS encolhem no unescape.
_LER_DO_TEXTO = 400


def sinais_do_tribunal(tribunal_id: str) -> tuple[str, ...]:
    return SINAIS_POR_TRIBUNAL.get(tribunal_id, SINAIS_PADRAO)


def _sql_in(valores: tuple[str, ...]) -> str:
    return '(' + ','.join(f"'{v}'" for v in valores) + ')'


# Uma linha por processo. `f31_tjrs`, `f32` e `satisf` têm o mesmo recorte de
# tribunal que o `_MOVS_AGG_SQL` (`f31_max_dt`/`f32_max_dt`/`satisf_max_dt`).
_AGG_SQL = """
    WITH m AS MATERIALIZED (
        SELECT processo_id, id, tribunal_id, inserido_em,
               data_disponibilizacao AS dt,
               tipo_comunicacao IN __TIPOS_F2__ AS f2,
               tipo_comunicacao IN __TIPOS_F7__ AS f7,
               texto ~* %(f20)s AS f20,
               (tribunal_id = 'TJRS' AND texto ~* %(f31)s) AS f31,
               (tribunal_id = 'TJRS' AND texto ~* %(f33)s) AS f33,
               texto ~* %(f19)s AS f19,
               texto ~* %(pago)s AS pago,
               texto ~* %(extneg)s AS extneg,
               (tribunal_id = 'TJRS' AND texto ~* %(conv)s
                AND texto !~* %(veto)s) AS f32,
               (tribunal_id = ANY(%(tribs_satisf)s) AND texto ~* %(satisf)s
                AND texto !~* %(satisf_veto)s) AS satisf
        FROM tribunals_movimentacao
        WHERE processo_id = ANY(%(pids)s)
    )
    SELECT processo_id,
           MAX(dt) AS ult_mov_dt,
           MAX(dt) FILTER (WHERE f19) AS f19_dt,
           MAX(dt) FILTER (WHERE pago) AS pago_dt,
           MAX(dt) FILTER (WHERE f20) AS exped_dt,
           MAX(dt) FILTER (WHERE extneg) AS extneg_dt,
           MAX(dt) FILTER (WHERE f31) AS f31_tjrs_dt,
           -- Referência do F32 no classificador: envio OU minuta (F31 ou F33).
           MAX(dt) FILTER (WHERE f31 OR f33) AS exped_tjrs_dt,
           MAX(dt) FILTER (WHERE f32) AS f32_dt,
           MAX(dt) FILTER (WHERE satisf) AS satisf_dt,
           COALESCE(json_agg(json_build_object(
               'id', id, 'dt', dt, 'ins', inserido_em,
               'f2', f2, 'f7', f7, 'f20', f20, 'f31', f31)
           ) FILTER (WHERE f2 OR f7 OR f20 OR f31), '[]') AS candidatos
    FROM m
    GROUP BY processo_id
""".replace('__TIPOS_F2__', _sql_in(TIPOS_F2_EXPEDICAO)).replace('__TIPOS_F7__', _sql_in(TIPOS_F7_ENVIO))

_PARAMS_REGEX = {
    'f20': F20_EXPEDICAO_SQL,
    'f31': SINAL_EXPEDICAO_PRECATORIO_TJRS_SQL,
    'f33': INTIMACAO_EXPEDICAO_PRECATORIO_TJRS_SQL,
    'f19': F19_CANCELAMENTO_SQL,
    'pago': PAGAMENTO_SQL,
    'extneg': EXTINCAO_NEGATIVA_SQL,
    'conv': CONVERSAO_RPV_TJRS_SQL,
    'veto': VETO_CONVERSAO_RPV_TJRS_SQL,
    'satisf': EXTINCAO_SATISFEITA_SQL,
    'satisf_veto': EXTINCAO_SATISFEITA_VETO_SQL,
    'tribs_satisf': sorted(SATISFACAO_SINAL_TRIBUNAIS),
}

# Últimas duas transições (o log só grava quando a categoria muda).
_LOG_SQL = """
    SELECT p.id, l.classificacao, l.criada_em
    FROM unnest(%s::bigint[]) AS p(id)
    CROSS JOIN LATERAL (
        SELECT id, classificacao, criada_em
        FROM tribunals_classificacaolog
        WHERE processo_id = p.id
        ORDER BY criada_em DESC, id DESC
        LIMIT 2
    ) l
    ORDER BY p.id, l.criada_em DESC, l.id DESC
"""

# Trecho centrado no casamento; `ini` > 1 diz que o começo do texto ficou de
# fora. Sem regex (F2/F7 são tipo de comunicação), o começo do texto.
_TRECHO_SQL = """
    SELECT id, ini, substr(texto, ini, __LER__)
    FROM (
        SELECT id, texto, GREATEST(
                   COALESCE(NULLIF(regexp_instr(texto, %(re)s, 1, 1, 0, 'i'), 0), 1)
                   - __ANTES__, 1) AS ini
        FROM tribunals_movimentacao
        WHERE id = ANY(%(ids)s)
    ) t
""".replace('__ANTES__', str(_ANTES_DO_CASAMENTO)).replace('__LER__', str(_LER_DO_TEXTO))

_TRECHO_SEM_REGEX_SQL = """
    SELECT id, 1, substr(texto, 1, __LER__)
    FROM tribunals_movimentacao
    WHERE id = ANY(%(ids)s)
""".replace('__LER__', str(_LER_DO_TEXTO))

_RE_TAG = re.compile(r'<[^>]+>')
_RE_ESPACO = re.compile(r'\s+')


def normalizar_cnj(cnj) -> str | None:
    """CNJ com máscara (`NNNNNNN-DD.AAAA.J.TR.OOOO`), como o banco guarda."""
    p = partes(str(cnj or ''))
    if not p:
        return None
    return f"{p['sequencial']}-{p['dv']}.{p['ano']}.{p['segmento']}." f"{p['tribunal']}.{p['origem']}"


def _dia(dt: datetime | None) -> str | None:
    """Data de São Paulo. O DJEN grava a disponibilização às 00:00 -03."""
    return timezone.localtime(dt).date().isoformat() if dt else None


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _depois_de(dt: datetime | None, desde: date | None) -> bool | None:
    if desde is None or dt is None:
        return None
    return timezone.localtime(dt).date() >= desde


def _limpar_trecho(bruto: str, cortado_no_inicio: bool) -> str:
    """Texto legível de até `TRECHO_MAX` chars: sem tags, entidades resolvidas."""
    bruto = bruto or ''
    if cortado_no_inicio:
        # A primeira palavra pode ser meia entidade ("cedil;") ou meia tag.
        bruto = bruto.split(None, 1)[1] if len(bruto.split(None, 1)) > 1 else ''
    texto = _RE_ESPACO.sub(' ', html.unescape(_RE_TAG.sub(' ', bruto))).strip()
    prefixo = '…' if cortado_no_inicio and texto else ''
    cabe = TRECHO_MAX - len(prefixo)
    if len(texto) > cabe:
        texto = texto[: cabe - 1].rstrip() + '…'
    return prefixo + texto


def _escolher_sinal(tribunal_id: str, candidatos: list[dict]) -> tuple[dict, str] | None:
    """A movimentação mais recente que casa um sinal válido no tribunal."""
    validos = sinais_do_tribunal(tribunal_id)
    melhor = None
    for c in candidatos:
        casados = [f for f in _ORDEM_SINAIS if f in validos and c[_COLUNA[f]]]
        if not casados:
            continue
        chave = (c['dt'], c['id'])
        if melhor is None or chave > melhor[0]:
            melhor = (chave, c, casados[0])
    if melhor is None:
        return None
    return melhor[1], melhor[2]


def _ler_trechos(escolhas: dict[int, str]) -> dict[int, str]:
    """{mov_id: feature} → {mov_id: trecho}. Uma consulta por pk e por regex."""
    regex_da_feature = {F20: F20_EXPEDICAO_SQL, F31: SINAL_EXPEDICAO_PRECATORIO_TJRS_SQL}
    por_regex: dict[str | None, list[int]] = {}
    for mov_id, feat in escolhas.items():
        por_regex.setdefault(regex_da_feature.get(feat), []).append(mov_id)
    trechos = {}
    with connection.cursor() as cur:
        for regex, ids in por_regex.items():
            if regex is None:
                cur.execute(_TRECHO_SEM_REGEX_SQL, {'ids': ids})
            else:
                cur.execute(_TRECHO_SQL, {'re': regex, 'ids': ids})
            for mov_id, ini, bruto in cur.fetchall():
                trechos[mov_id] = _limpar_trecho(bruto, ini > 1)
    return trechos


def _ler_fatia(cur, fatia: list[int]) -> tuple[list[dict], list[tuple]]:
    cur.execute(_AGG_SQL, {**_PARAMS_REGEX, 'pids': fatia})
    cols = [c[0] for c in cur.description]
    linhas = [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
    cur.execute(_LOG_SQL, [fatia])
    return linhas, cur.fetchall()


def _desligar_jit(cur) -> None:
    # O planner estima milhões de linhas para o `= ANY` e liga o JIT, que não
    # se paga aqui (medido em prod: 1,85 s sem contra 2,1-2,3 s com, por fatia
    # de 500). `SET LOCAL` só vale dentro de uma transação.
    if connection.in_atomic_block:
        cur.execute('SET LOCAL jit = off')


def _ler_fatia_em_thread(fatia: list[int], teto_s: float | None):
    """Fatia lida numa thread: conexão própria, fechada no fim."""
    try:
        with transaction.atomic(), connection.cursor() as cur:
            if teto_s:
                cur.execute('SET LOCAL statement_timeout = %s', [int(teto_s * 1000)])
            _desligar_jit(cur)
            return _ler_fatia(cur, fatia)
    finally:
        connection.close()


def _ler_fatias(fatias: list[list[int]], orcamento_s: float | None, teto_s: float | None, paralelismo: int):
    """Lê as fatias até acabar ou estourar o orçamento de tempo.

    Teto de tempo é alerta, nunca corte mudo: a fatia que não cabe no que
    sobrou (estimada pela mais lenta até aqui) não começa, e os CNJs dela
    voltam em `nao_processados` para o cliente reenviar. O custo é a regex
    sobre o texto, proporcional ao volume de movimentações de cada processo.
    """
    agg, logs, processados = {}, {}, set()
    inicio = time.monotonic()
    mais_lenta = 0.0

    def guardar(fatia, linhas, log_rows):
        for linha in linhas:
            agg[linha['processo_id']] = linha
        for pid, nivel, em in log_rows:
            logs.setdefault(pid, []).append((nivel, em))
        processados.update(fatia)

    def cabe() -> bool:
        return orcamento_s is None or time.monotonic() - inicio + mais_lenta <= orcamento_s

    if paralelismo <= 1 or len(fatias) <= 1:
        with connection.cursor() as cur:
            _desligar_jit(cur)
            for fatia in fatias:
                if processados and not cabe():
                    break
                t0 = time.monotonic()
                guardar(fatia, *_ler_fatia(cur, fatia))
                mais_lenta = max(mais_lenta, time.monotonic() - t0)
        return agg, logs, processados

    pendentes = list(reversed(fatias))
    with ThreadPoolExecutor(max_workers=paralelismo) as pool:
        em_voo = {}
        while pendentes or em_voo:
            while pendentes and len(em_voo) < paralelismo and (not processados or cabe()):
                fatia = pendentes.pop()
                em_voo[pool.submit(_ler_fatia_em_thread, fatia, teto_s)] = (fatia, time.monotonic())
            if not em_voo:
                break
            prontos, _ = wait(em_voo, return_when=FIRST_COMPLETED)
            for fut in prontos:
                fatia, t0 = em_voo.pop(fut)
                guardar(fatia, *fut.result())
                mais_lenta = max(mais_lenta, time.monotonic() - t0)
    return agg, logs, processados


def _fatiar(pids_por_cnj: dict[str, list[int]]) -> list[list[int]]:
    """Fatias de ~`LOTE_SQL` processos; um CNJ em dois tribunais não se divide."""
    fatias, atual = [], []
    for pids_cnj in pids_por_cnj.values():
        atual += pids_cnj
        if len(atual) >= LOTE_SQL:
            fatias.append(atual)
            atual = []
    if atual:
        fatias.append(atual)
    return fatias


def _ultima_transicao(historico: list[tuple], desde: date | None) -> dict | None:
    if not historico:
        return None
    nivel, em = historico[0]
    return {
        'nivel': nivel,
        'nivel_anterior': historico[1][0] if len(historico) > 1 else None,
        'em': _iso(em),
        'depois_de_desde': _depois_de(em, desde),
    }


def _vetos(linha: dict) -> dict:
    """Data da última ocorrência e o valor da feature, como o classificador."""
    exped_dt = linha.get('exped_dt')
    return {
        'F19_cancelado_ANTI': {'ultima_em': _dia(linha.get('f19_dt')), 'ativo': bool(linha.get('f19_dt'))},
        'F24_pago_pos_exped_ANTI': {
            'ultima_em': _dia(linha.get('pago_dt')),
            'ativo': bool(f24_pago_pos_exped(linha.get('pago_dt'), exped_dt)),
        },
        'F30_extinto_neg_ANTI': {
            'ultima_em': _dia(linha.get('extneg_dt')),
            'ativo': bool(f30_extinto_neg(linha.get('extneg_dt'), exped_dt)),
        },
        'F32_convertido_rpv_tjrs': {
            'ultima_em': _dia(linha.get('f32_dt')),
            'ativo': bool(f32_convertido(linha.get('exped_tjrs_dt'), linha.get('f32_dt'))),
        },
        'F39_extinto_satisfeito_ANTI': {
            'ultima_em': _dia(linha.get('satisf_dt')),
            'ativo': bool(f39_extinto_satisfeito(linha.get('satisf_dt'), exped_dt)),
        },
    }


def _item(p: dict, linha: dict, historico: list[tuple], escolha, trechos: dict, desde: date | None) -> dict:
    feats = sinais_do_tribunal(p['tribunal_id'])
    cand = linha.get('candidatos') or []
    por_feature = {}
    for f in feats:
        datas = [c['dt'] for c in cand if c[_COLUNA[f]]]
        por_feature[f] = _dia(max(datas)) if datas else None
    mov, feat = escolha if escolha else (None, None)
    score = p['classificacao_score']
    return {
        'cnj': p['numero_cnj'],
        'tribunal': p['tribunal_id'],
        'nivel': p['classificacao'],
        'score': round(score, 4) if score is not None else None,
        'classificacao_em': _iso(p['classificacao_em']),
        'ultima_transicao': _ultima_transicao(historico, desde),
        'sinal_expedicao_em': _dia(mov['dt']) if mov else None,
        'sinal_inserido_em': _iso(mov['ins']) if mov else None,
        'sinal_tipo': feat,
        'sinal_trecho': trechos.get(mov['id']) if mov else None,
        'sinal_mov_id': mov['id'] if mov else None,
        'sinal_depois_de_desde': _depois_de(mov['dt'], desde) if mov else None,
        'sinais_considerados': list(feats),
        'sinais_por_feature': por_feature,
        'vetos': _vetos(linha),
        'movimentacao_mais_recente_em': _dia(linha.get('ult_mov_dt')),
    }


def calcular_sinais(
    cnjs, desde: date | None = None, orcamento_s: float | None = None, teto_s: float | None = None, paralelismo: int = 1
) -> dict:
    """Sinais de expedição, transição de nível e vetos para um lote de CNJs.

    Devolve `results`, `nao_encontrados`, `invalidos`, `nao_processados` e
    `completo`. Um CNJ que existe em dois tribunais (TRF1 e TRF6, o número
    antigo herdado) gera duas linhas em `results`.

    `orcamento_s`: passado esse tempo, as fatias seguintes não rodam e os CNJs
    delas voltam em `nao_processados` (a primeira leva roda sempre).
    `paralelismo` > 1 lê as fatias em threads, cada uma na sua conexão e
    transação, com `statement_timeout` = `teto_s`; com 1, tudo roda na conexão
    (e na transação) de quem chama.
    """
    invalidos, normalizados = [], {}
    for bruto in cnjs:
        cnj = normalizar_cnj(bruto)
        if cnj is None:
            invalidos.append(str(bruto))
        else:
            normalizados.setdefault(cnj, bruto)
    if len(normalizados) > LIMITE_CNJS:
        raise ValueError(f'mais de {LIMITE_CNJS} CNJs')

    processos = list(
        Process.objects.filter(numero_cnj__in=list(normalizados))
        .order_by('numero_cnj', 'tribunal_id')
        .values('id', 'numero_cnj', 'tribunal_id', 'classificacao', 'classificacao_score', 'classificacao_em')
    )
    pids_por_cnj: dict[str, list[int]] = {}
    for p in processos:
        pids_por_cnj.setdefault(p['numero_cnj'], []).append(p['id'])
    nao_encontrados = [c for c in normalizados if c not in pids_por_cnj]

    agg, logs, processados = _ler_fatias(_fatiar(pids_por_cnj), orcamento_s, teto_s, paralelismo)
    nao_processados = [c for c, ids in pids_por_cnj.items() if ids[0] not in processados]

    escolhidos = {}
    trib_por_pid = {p['id']: p['tribunal_id'] for p in processos}
    for pid, linha in agg.items():
        for c in linha['candidatos']:
            c['dt'] = datetime.fromisoformat(c['dt'])
            c['ins'] = datetime.fromisoformat(c['ins']) if c['ins'] else None
        escolha = _escolher_sinal(trib_por_pid[pid], linha['candidatos'])
        if escolha:
            escolhidos[pid] = escolha
    trechos = _ler_trechos({c['id']: feat for c, feat in escolhidos.values()}) if escolhidos else {}

    results = [
        _item(p, agg.get(p['id']) or {}, logs.get(p['id']) or [], escolhidos.get(p['id']), trechos, desde)
        for p in processos
        if p['id'] in processados
    ]
    return {
        'results': results,
        'nao_encontrados': nao_encontrados,
        'invalidos': invalidos,
        'nao_processados': nao_processados,
        'completo': not nao_processados,
    }


# ===========================================================================
# Contrato do vigia (F1): `{"itens": [{"cnj", "desde"}]}` → `{"sinais": [...]}`
# ===========================================================================
# A pergunta do vigia é mais estreita que a de `calcular_sinais`: "DEPOIS do
# meu download (`desde`, por CNJ), saiu publicação que diz que o ofício foi
# expedido?". Por isso a leitura entra no índice `(processo_id,
# data_disponibilizacao DESC)` já com o recorte `> desde` — só as publicações
# novas passam pela regex, não o histórico inteiro do processo.
#
# Sinais: os mesmos de `sinais_do_tribunal` (F2/F7/F20; TJRS só F31) e, no
# TJPA, também o F41 (texto de envio/devolução à Coordenadoria de Precatórios).
# RPV conta: o vigia procura ofício requisitório de qualquer tipo.
#
# `pago` é o F24 recortado na janela: há marcador de pagamento (`PAGAMENTO_SQL`)
# depois de `desde` e ele é posterior (ou igual) ao último sinal de expedição
# da janela — ou não há sinal na janela. Mesma função `f24_pago_pos_exped`; a
# diferença para o classificador é a referência de expedição, que aqui é
# qualquer sinal válido do tribunal (não só o F20) e só dentro da janela.

LIMITE_ITENS_VIGIA = 500

F41 = 'F41_precatorio_coord_tjpa'
SINAIS_EXTRA_VIGIA = {'TJPA': (F41,)}

_ORDEM_SINAIS_VIGIA = (F2, F7, F20, F31, F41)
_COLUNA_VIGIA = {**_COLUNA, F41: 'f41'}

TIPO_OFICIO = 'oficio'
TIPO_PRECATORIO = 'precatorio'
TIPO_RPV = 'rpv'
TIPO_ALVARA_PAGO = 'alvara_pago'


def sinais_vigia_do_tribunal(tribunal_id: str) -> tuple[str, ...]:
    return sinais_do_tribunal(tribunal_id) + SINAIS_EXTRA_VIGIA.get(tribunal_id, ())


# Uma linha por processo que tenha publicação na janela. O `LATERAL` força a
# entrada pelo índice com o recorte de data de cada processo.
_VIGIA_SQL = """
    WITH alvo AS (
        SELECT * FROM unnest(%(pids)s::bigint[], %(desdes)s::timestamptz[]) AS a(pid, desde)
    ),
    m AS MATERIALIZED (
        SELECT a.pid AS processo_id, mv.id, mv.data_disponibilizacao AS dt,
               mv.tipo_comunicacao AS tc,
               mv.tipo_comunicacao IN __TIPOS_F2__ AS f2,
               mv.tipo_comunicacao IN __TIPOS_F7__ AS f7,
               mv.texto ~* %(f20)s AS f20,
               (mv.tribunal_id = 'TJRS' AND mv.texto ~* %(f31)s) AS f31,
               (mv.tribunal_id = ANY(%(tribs_f41)s) AND mv.texto ~* %(f41)s) AS f41,
               mv.texto ~* %(pago)s AS pago
        FROM alvo a
        CROSS JOIN LATERAL (
            SELECT id, data_disponibilizacao, tribunal_id, tipo_comunicacao, texto
            FROM tribunals_movimentacao
            WHERE processo_id = a.pid AND data_disponibilizacao > a.desde
        ) mv
    )
    SELECT processo_id,
           COUNT(*) AS publicacoes,
           MAX(dt) FILTER (WHERE pago) AS pago_dt,
           COALESCE(json_agg(json_build_object(
               'id', id, 'dt', dt, 'tc', tc,
               'f2', f2, 'f7', f7, 'f20', f20, 'f31', f31, 'f41', f41)
           ) FILTER (WHERE f2 OR f7 OR f20 OR f31 OR f41), '[]') AS candidatos
    FROM m
    GROUP BY processo_id
""".replace('__TIPOS_F2__', _sql_in(TIPOS_F2_EXPEDICAO)).replace('__TIPOS_F7__', _sql_in(TIPOS_F7_ENVIO))

_PARAMS_VIGIA = {
    'f20': F20_EXPEDICAO_SQL,
    'f31': SINAL_EXPEDICAO_PRECATORIO_TJRS_SQL,
    'f41': SINAL_PRECATORIO_TJPA_SQL,
    'tribs_f41': sorted(SINAL_COORDENADORIA_TRIBUNAIS),
    'pago': PAGAMENTO_SQL,
}

# O trecho do F20 que casou diz o tipo ("precatório expedido", "rpv expedida",
# "ofício requisitório expedido"). Lido por pk, só das movimentações escolhidas.
_CASAMENTO_F20_SQL = """
    SELECT id, regexp_substr(texto, %(re)s, 1, 1, 'i')
    FROM tribunals_movimentacao
    WHERE id = ANY(%(ids)s)
"""

_RE_TIPO_RPV = re.compile(r'rpv|pequeno\s+valor', re.I)
_RE_TIPO_PRECATORIO = re.compile(r'precat[óo]rio', re.I)


def tipo_do_texto(texto: str | None) -> str:
    """precatorio / rpv quando o texto diz um só; senão `oficio` (não chuta)."""
    texto = texto or ''
    rpv = bool(_RE_TIPO_RPV.search(texto))
    precat = bool(_RE_TIPO_PRECATORIO.search(texto))
    if precat and not rpv:
        return TIPO_PRECATORIO
    if rpv and not precat:
        return TIPO_RPV
    return TIPO_OFICIO


def _tipo_do_sinal(feat: str, cand: dict, casamento_f20: str | None) -> str:
    if feat in (F31, F41):
        return TIPO_PRECATORIO
    if feat == F20:
        return tipo_do_texto(casamento_f20)
    if feat == F2:
        # 'Precatório' é inequívoco; 'Expedição de precatório/rpv' é um só
        # movimento TPU para os dois.
        return tipo_do_texto(cand.get('tc'))
    return TIPO_OFICIO  # F7: "Enviada ao Tribunal" não diz qual requisição


def _escolher_sinal_vigia(tribunal_id: str, candidatos: list[dict]) -> tuple[dict, str] | None:
    validos = sinais_vigia_do_tribunal(tribunal_id)
    melhor = None
    for c in candidatos:
        casados = [f for f in _ORDEM_SINAIS_VIGIA if f in validos and c[_COLUNA_VIGIA[f]]]
        if not casados:
            continue
        chave = (c['dt'], c['id'])
        if melhor is None or chave > melhor[0]:
            melhor = (chave, c, casados[0])
    if melhor is None:
        return None
    return melhor[1], melhor[2]


def _casamentos_f20(ids: list[int]) -> dict[int, str]:
    if not ids:
        return {}
    with connection.cursor() as cur:
        cur.execute(_CASAMENTO_F20_SQL, {'re': F20_EXPEDICAO_SQL, 'ids': ids})
        return dict(cur.fetchall())


def _item_vazio(cnj: str) -> dict:
    return {
        'cnj': cnj, 'tribunal': None, 'conhecido': False, 'publicacoes_desde': 0,
        'sinal_expedicao': False, 'ultimo_sinal_em': None, 'tipo': None,
        'classificacao': None, 'pago': False,
    }


def _normalizar_itens(itens) -> tuple[dict[str, datetime], list[str]]:
    """{cnj mascarado: desde} (o mais antigo, se repetido) e os inválidos."""
    invalidos, desde_por_cnj = [], {}
    for bruto, desde in itens:
        cnj = normalizar_cnj(bruto)
        if cnj is None:
            invalidos.append(str(bruto))
            continue
        atual = desde_por_cnj.get(cnj)
        desde_por_cnj[cnj] = desde if atual is None else min(atual, desde)
    return desde_por_cnj, invalidos


def _ler_janela(cur, processos: list[dict], desde_por_cnj: dict[str, datetime]) -> list[dict]:
    cur.execute(_VIGIA_SQL, {
        **_PARAMS_VIGIA,
        'pids': [p['id'] for p in processos],
        'desdes': [desde_por_cnj[p['numero_cnj']] for p in processos],
    })
    cols = [c[0] for c in cur.description]
    linhas = [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
    for linha in linhas:
        for c in linha['candidatos']:
            c['dt'] = datetime.fromisoformat(c['dt'])
    return linhas


def _ler_janela_em_thread(processos, desde_por_cnj, teto_s: float | None) -> list[dict]:
    """Fatia lida numa thread: conexão e transação próprias, fechada no fim."""
    try:
        with transaction.atomic(), connection.cursor() as cur:
            if teto_s:
                cur.execute('SET LOCAL statement_timeout = %s', [int(teto_s * 1000)])
            _desligar_jit(cur)
            return _ler_janela(cur, processos, desde_por_cnj)
    finally:
        connection.close()


def _ler_janelas(processos: list[dict], desde_por_cnj: dict[str, datetime],
                 teto_s: float | None, paralelismo: int) -> dict[int, dict]:
    """{processo_id: linha} das publicações na janela de cada processo.

    O custo é ler o `texto` (TOAST, frio em disco) e passar a regex nele — medido
    em prod (24/10/2026, TJSP): 13 mil publicações / 17 MB num lote de 500 levam
    2,2 s quente e até 16 s frio numa conexão só. Com `paralelismo` > 1 as
    fatias vão em threads, cada uma com `statement_timeout` = `teto_s`.
    """
    if not processos:
        return {}
    if paralelismo <= 1 or len(processos) == 1:
        with connection.cursor() as cur:
            _desligar_jit(cur)
            linhas = _ler_janela(cur, processos, desde_por_cnj)
    else:
        n = -(-len(processos) // paralelismo)
        fatias = [processos[i:i + n] for i in range(0, len(processos), n)]
        with ThreadPoolExecutor(max_workers=len(fatias)) as pool:
            futuros = [pool.submit(_ler_janela_em_thread, f, desde_por_cnj, teto_s) for f in fatias]
            linhas = [linha for fut in futuros for linha in fut.result()]
    return {linha['processo_id']: linha for linha in linhas}


def _linha_vigia(p: dict, linha: dict, escolha, casamentos: dict[int, str]) -> dict:
    sinal_dt = escolha[0]['dt'] if escolha else None
    pago_dt = linha.get('pago_dt')
    pago = bool(f24_pago_pos_exped(pago_dt, sinal_dt))
    if pago:
        tipo, ultimo = TIPO_ALVARA_PAGO, pago_dt
    elif escolha:
        cand, feat = escolha
        tipo, ultimo = _tipo_do_sinal(feat, cand, casamentos.get(cand['id'])), sinal_dt
    else:
        tipo, ultimo = None, None
    return {
        'cnj': p['numero_cnj'],
        'tribunal': p['tribunal_id'],
        'conhecido': True,
        'publicacoes_desde': linha.get('publicacoes') or 0,
        'sinal_expedicao': escolha is not None,
        'ultimo_sinal_em': _iso(ultimo),
        'tipo': tipo,
        'classificacao': p['classificacao'],
        'pago': pago,
    }


def calcular_sinais_vigia(itens: list[tuple[str, datetime]], teto_s: float | None = None,
                          paralelismo: int = 1) -> dict:
    """Sinais de expedição POSTERIORES a `desde`, por CNJ.

    `itens`: [(cnj, desde aware)]. CNJ inválido vai para `invalidos`; CNJ
    repetido usa o `desde` mais antigo. Um CNJ em dois tribunais (TRF1/TRF6)
    gera uma linha por processo; CNJ desconhecido, uma linha `conhecido=false`.
    Com `paralelismo` 1 roda na conexão (e na transação) de quem chama — o
    teto é dela; com mais, as fatias vão em threads com `teto_s` cada.
    """
    desde_por_cnj, invalidos = _normalizar_itens(itens)
    if len(desde_por_cnj) > LIMITE_ITENS_VIGIA:
        raise ValueError(f'mais de {LIMITE_ITENS_VIGIA} CNJs')

    processos = list(
        Process.objects.filter(numero_cnj__in=list(desde_por_cnj))
        .order_by('numero_cnj', 'tribunal_id')
        .values('id', 'numero_cnj', 'tribunal_id', 'classificacao')
    )
    agg = _ler_janelas(processos, desde_por_cnj, teto_s, paralelismo)
    escolhas = {}
    for p in processos:
        if p['id'] in agg:
            escolha = _escolher_sinal_vigia(p['tribunal_id'], agg[p['id']]['candidatos'])
            if escolha:
                escolhas[p['id']] = escolha
    casamentos = _casamentos_f20([c['id'] for c, feat in escolhas.values() if feat == F20])

    por_cnj: dict[str, list[dict]] = {}
    for p in processos:
        por_cnj.setdefault(p['numero_cnj'], []).append(
            _linha_vigia(p, agg.get(p['id']) or {}, escolhas.get(p['id']), casamentos))
    sinais = []
    for cnj in desde_por_cnj:
        sinais.extend(por_cnj.get(cnj) or [_item_vazio(cnj)])
    return {'sinais': sinais, 'invalidos': invalidos}
