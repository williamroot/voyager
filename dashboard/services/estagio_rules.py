"""Motor de REGRAS determinísticas — Estágio do Crédito por CNJ.

Classifica um processo em um dos estágios do ciclo de vida do crédito
judicial, usando SÓ as movimentações públicas (DJEN + Datajud) já
persistidas em `tribunals.Movimentacao`:

    DC        — Direito Creditório: sentença/trânsito, execução ainda não
                instaurada (ou não visível).
    PRE       — Pré-precatório: cumprimento de sentença / homologação de
                cálculos, SEM expedição de ofício requisitório.
    EMITIDO   — Precatório/RPV expedido (ofício requisitório nas movs).
    MORTO     — crédito encerrado. Sub-selos:
                  SATISFEITO   — extinção pelo pagamento/cumprimento da
                                 obrigação (desfecho POSITIVO: crédito pago).
                  IMPROCEDENTE — improcedência / prescrição / indeferimento
                                 da inicial (crédito não existe).
    INDEFINIDO — nenhum sinal reconhecido (honestidade > palpite).

Nuances de extinção (decisão de produto 2026-07-30):
  * extinção pelo pagamento           → MORTO/SATISFEITO (selo especial)
  * extinção SEM resolução de mérito  → NÃO rebaixa; vira badge informativo
  * improcedência/prescrição          → MORTO/IMPROCEDENTE
  * extinção de embargos/incidente    → ignorada (não é o processo principal)
  * extinção de natureza incerta      → badge, classe mantida

Cada veredito vem ANCORADO: lista de {mov_id, data, trecho, sinal, label}
apontando a movimentação exata que sustenta a classificação.

Interface plugável (`ESTAGIO_ENGINE` em settings: 'rules' | 'gbm' | 'hybrid'):
o GBM (`tribunals/estagio.py`, mantido por outro fluxo) pode substituir ou
compor com este motor via `predict_estagio()` — nunca importamos o módulo
GBM em import-time, só dentro do dispatcher, com fallback pra regras.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Optional

from django.conf import settings

logger = logging.getLogger('voyager.dashboard.estagio_rules')

ENGINE_VERSION = 'regras v1'

# ---------------------------------------------------------------------------
# Estágios / selos
# ---------------------------------------------------------------------------
ESTAGIO_DC = 'DC'
ESTAGIO_PRE = 'PRE'
ESTAGIO_EMITIDO = 'EMITIDO'
ESTAGIO_MORTO = 'MORTO'
ESTAGIO_INDEFINIDO = 'INDEFINIDO'

SELO_SATISFEITO = 'SATISFEITO'
SELO_IMPROCEDENTE = 'IMPROCEDENTE'

ESTAGIO_LABELS = {
    ESTAGIO_DC: 'Direito Creditório',
    ESTAGIO_PRE: 'Pré-precatório',
    ESTAGIO_EMITIDO: 'Precatório emitido',
    ESTAGIO_MORTO: 'Crédito encerrado',
    ESTAGIO_INDEFINIDO: 'Estágio indefinido',
}

# ---------------------------------------------------------------------------
# Sinais (regex sobre texto público das movimentações)
# Ordem de força: cada sinal tem (code, estagio, label, peso) — peso só
# alimenta a confiança, a hierarquia é determinística.
# ---------------------------------------------------------------------------
_RX = lambda p: re.compile(p, re.IGNORECASE)  # noqa: E731

# --- EMITIDO -----------------------------------------------------------------
RX_EXPED_FORTE = _RX(
    r'precat[óo]rio\s+expedido|rpv\s+expedid|'
    r'of[íi]cio\s+requisit[óo]rio\s+expedido|'
    r'expedi[çc][ãa]o\s+de\s+(precat[óo]rio|rpv|requisi[çc][ãa]o\s+de\s+pagamento)|'
    r'requisi[çc][ãa]o\s+de\s+pagamento\s+de\s+(pequeno\s+valor|precat[óo]rio)\s+(enviada|expedida)|'
    r'determin(o|ada?)\s+(a\s+)?expedi[çc][ãa]o\s+de\s+(precat[óo]rio|rpv|of[íi]cio\s+requisit[óo]rio)|'
    r'expe[çc]a(m)?-se\s+(o\s+)?(precat[óo]rio|of[íi]cio\s+requisit[óo]rio|a?\s*rpv)'
)
RX_OFICIO_REQ = _RX(r'of[íi]cio\s+requisit[óo]rio')
RX_REQ_PAGAMENTO = _RX(r'requisi[çc][ãa]o\s+de\s+pagamento')
# tipo_comunicacao explícito da DJEN
TIPOS_EXPEDICAO = {'expedição de precatório/rpv', 'precatório'}

# --- PRE ---------------------------------------------------------------------
RX_CUMPRIMENTO = _RX(r'cumprimento\s+de\s+senten[çc]a')
RX_HOMOLOGACAO = _RX(
    r'homolog\w+\s+(os?\s+|as?\s+)?(c[áa]lculos?|contas?|valor(es)?)|'
    r'c[áa]lculos?\s+homologad|homologa[çc][ãa]o\s+d[eo]s?\s+c[áa]lculos?'
)
# Classes TPU de cumprimento (mesma lista do classificador de leads — mantida
# local pra este módulo não depender do pipeline ML).
CLASSES_CUMPRIMENTO = {'12078', '156', '15160', '15215', '12079'}

# --- DC ----------------------------------------------------------------------
RX_TRANSITO = _RX(r'tr[âa]nsit(o|ad[oa]|ou)\s+em\s+julgado')
RX_SENTENCA_PROC = _RX(
    r'julgo\s+(parcialmente\s+)?procedente|'
    r'proced[êe]ncia\s+(parcial\s+)?d[oa]s?\s+pedido|'
    r'\bcondeno\b'
)

# --- Extinções (subtipadas) --------------------------------------------------
RX_EXT_SATISFEITO = _RX(
    r'extin[çt]\w*[^.;]{0,140}(pagamento|satisfa[çc][ãa]o|cumprimento\s+da\s+obriga[çc][ãa]o|quita[çc])|'
    r'(pagamento|satisfa[çc][ãa]o\s+da\s+obriga[çc][ãa]o|quita[çc][ãa]o)[^.;]{0,140}extin[çt]|'
    r'satisfa[çc][ãa]o\s+da\s+obriga[çc][ãa]o|obriga[çc][ãa]o\s+satisfeita|'
    r'art\.?\s*924\s*,?\s*(inciso\s+)?(ii|2)\b'
)
RX_IMPROCEDENTE = _RX(
    r'julgo\s+improcedente|improceden(te|tes|cia)|'
    r'indef(iro|erimento)\s+(d?a\s+)?(peti[çc][ãa]o\s+)?(inicial|exordial)|'
    r'denego\s+seguimento|'
    r'(decreto|reconhe[çc]o|pronuncio)\s+a\s+prescri|'
    r'prescri[çc][ãa]o[^.;]{0,60}(decretada|reconhecida|consumada|pronunciada)'
)
RX_EXT_SEM_MERITO = _RX(
    r'sem\s+resolu[çc][ãa]o\s+d[eo]\s+m[ée]rito|art\.?\s*485\b'
)
RX_EXT_INCIDENTE = _RX(
    r'extin[çt]\w*[^.;]{0,100}(embargos|incidente|impugna[çc][ãa]o)|'
    r'(embargos|incidente|impugna[çc][ãa]o)[^.;]{0,100}extin[çt]'
)
RX_EXT_GENERICA = _RX(
    r'julgo\s+extint|extin[çc][ãa]o\s+d[ao]\s+(processo|execu[çc][ãa]o|cumprimento)'
)

# --- Flags -------------------------------------------------------------------
RX_RPV = _RX(r'\brpv\b|requisi[çc][ãa]o\s+de\s+pequeno\s+valor')
RX_PAGAMENTO = _RX(
    r'alvar[áa]\s+(judicial|de\s+levantamento)|mandado\s+de\s+levantamento|'
    r'expe[çc]am?-se\s+(o\s+)?alvar[áa]|levantamento\s+d[eo]s?\s*(valores|quantia|dep[óo]sito)'
)
RX_PAG_PARCIAL = _RX(
    r'pagamento\s+parcial|parcial(mente)?\s+(pago|quitad)|levantamento\s+parcial'
)

# --- Valor homologado (best-effort, NULLABLE) --------------------------------
RX_VALOR_BRL = re.compile(r'R\$\s*([\d]{1,3}(?:\.\d{3})*(?:,\d{2})|[\d]+(?:,\d{2}))')
RX_VALOR_CONTEXTO = _RX(
    r'homolog|valor\s+(da\s+)?(execu[çc][ãa]o|d[ée]bito|cr[ée]dito|devido)|'
    r'quantia\s+de|import[âa]ncia\s+de|montante\s+de|cr[ée]dito\s+de'
)


@dataclass
class Ancora:
    """Uma movimentação-âncora: o elo entre o veredito e o dado público."""
    sinal: str
    label: str
    estagio: str            # estágio (ou 'FLAG'/'BADGE') que o sinal sustenta
    mov_id: Optional[int]
    data: Optional[str]     # ISO — None quando sinal vem do cadastro (classe)
    trecho: str             # janela de contexto ao redor do match
    tipo: str = ''          # tipo_comunicacao da mov
    meio: str = ''          # 'D' (DJEN) / 'datajud'
    extra: dict = field(default_factory=dict)


def _trecho(texto: str, match: re.Match, janela: int = 140) -> str:
    """Extrai janela de contexto ao redor do match, com reticências."""
    ini = max(0, match.start() - janela)
    fim = min(len(texto), match.end() + janela)
    pedaco = re.sub(r'\s+', ' ', texto[ini:fim]).strip()
    pre = '…' if ini > 0 else ''
    pos = '…' if fim < len(texto) else ''
    return f'{pre}{pedaco}{pos}'


def _norm(s: str) -> str:
    return (s or '').strip()


class _Scan:
    """Acumulador de sinais durante a varredura das movimentações."""

    def __init__(self):
        self.ancoras: list[Ancora] = []
        # última data (ISO) vista por código de sinal — pra regras temporais
        self.ultima_data: dict[str, str] = {}
        self.contagem: dict[str, int] = {}

    def add(self, sinal: str, label: str, estagio: str, mov, match: Optional[re.Match],
            texto: str, max_por_sinal: int = 2):
        data_iso = mov['data'].isoformat() if mov.get('data') else None
        if data_iso and (sinal not in self.ultima_data or data_iso > self.ultima_data[sinal]):
            self.ultima_data[sinal] = data_iso
        self.contagem[sinal] = self.contagem.get(sinal, 0) + 1
        # guarda primeira e última ocorrência (substitui a última se exceder)
        existentes = [a for a in self.ancoras if a.sinal == sinal]
        anc = Ancora(
            sinal=sinal, label=label, estagio=estagio,
            mov_id=mov.get('id'), data=data_iso,
            trecho=_trecho(texto, match) if match else re.sub(r'\s+', ' ', texto)[:280],
            tipo=_norm(mov.get('tipo')), meio=_norm(mov.get('meio')),
        )
        if len(existentes) < max_por_sinal:
            self.ancoras.append(anc)
        else:
            # mantém primeira, substitui a mais recente
            idx = self.ancoras.index(existentes[-1])
            self.ancoras[idx] = anc

    def tem(self, sinal: str) -> bool:
        return sinal in self.contagem


def analisar_movs(movs: Iterable[dict], classe_codigo: str = '',
                  classe_nome: str = '') -> dict:
    """Núcleo puro do motor de regras — opera sobre uma lista de dicts
    {id, data(datetime), tipo, texto, meio}. Sem ORM: testável isolado.

    Retorna o veredito estruturado (ver docstring do módulo).
    """
    movs = sorted(
        (m for m in movs if m.get('data')),
        key=lambda m: m['data'],
    )
    scan = _Scan()
    valor_candidatos: list[tuple[str, float, str, Optional[int]]] = []  # (data, valor, trecho, mov_id)

    for m in movs:
        texto = _norm(m.get('texto'))
        tipo = _norm(m.get('tipo')).lower()
        blob = f'{m.get("tipo") or ""}. {texto}'

        # ---- EMITIDO
        if tipo in TIPOS_EXPEDICAO:
            scan.add('tipo_expedicao', 'Comunicação de expedição de precatório/RPV',
                     ESTAGIO_EMITIDO, m, None, blob)
        mt = RX_EXPED_FORTE.search(blob)
        if mt:
            scan.add('exped_confirmada', 'Expedição de precatório/RPV confirmada',
                     ESTAGIO_EMITIDO, m, mt, blob)
        else:
            mt = RX_OFICIO_REQ.search(blob)
            if mt:
                scan.add('oficio_requisitorio', 'Menção a ofício requisitório',
                         ESTAGIO_EMITIDO, m, mt, blob)
            mt = RX_REQ_PAGAMENTO.search(blob)
            if mt:
                scan.add('requisicao_pagamento', 'Requisição de pagamento',
                         ESTAGIO_EMITIDO, m, mt, blob)

        # ---- PRE
        mt = RX_CUMPRIMENTO.search(blob)
        if mt:
            scan.add('cumprimento_text', 'Cumprimento de sentença em curso',
                     ESTAGIO_PRE, m, mt, blob)
        mt = RX_HOMOLOGACAO.search(blob)
        if mt:
            scan.add('homologacao', 'Homologação de cálculos',
                     ESTAGIO_PRE, m, mt, blob)

        # ---- DC
        mt = RX_TRANSITO.search(blob)
        if mt:
            scan.add('transito', 'Trânsito em julgado',
                     ESTAGIO_DC, m, mt, blob)
        mt = RX_SENTENCA_PROC.search(blob)
        if mt:
            scan.add('sentenca_procedente', 'Sentença de procedência/condenação',
                     ESTAGIO_DC, m, mt, blob)

        # ---- Extinções (subtipadas; incidente é avaliado primeiro e ignora o resto)
        eh_incidente = RX_EXT_INCIDENTE.search(blob)
        mt_sat = RX_EXT_SATISFEITO.search(blob)
        mt_imp = RX_IMPROCEDENTE.search(blob)
        mt_sem = RX_EXT_SEM_MERITO.search(blob)
        mt_gen = RX_EXT_GENERICA.search(blob)
        if eh_incidente and not mt_sat:
            # extinção de embargos/incidente/impugnação → não é o processo
            # principal; registra como badge informativo de baixo peso.
            scan.add('ext_incidente', 'Extinção de incidente/embargos (ignorada)',
                     'BADGE', m, eh_incidente, blob, max_por_sinal=1)
        else:
            if mt_sat:
                scan.add('ext_satisfeito', 'Extinção pelo pagamento — obrigação satisfeita',
                         ESTAGIO_MORTO, m, mt_sat, blob)
            if mt_imp:
                scan.add('improcedencia', 'Improcedência / prescrição / indeferimento',
                         ESTAGIO_MORTO, m, mt_imp, blob)
            if mt_sem:
                scan.add('ext_sem_merito', 'Extinção sem resolução de mérito',
                         'BADGE', m, mt_sem, blob)
            if mt_gen and not (mt_sat or mt_imp or mt_sem):
                scan.add('ext_ambigua', 'Extinção de natureza incerta',
                         'BADGE', m, mt_gen, blob)

        # ---- Flags
        mt = RX_RPV.search(blob)
        if mt:
            scan.add('rpv', 'RPV (requisição de pequeno valor)', 'FLAG', m, mt, blob)
        mt = RX_PAGAMENTO.search(blob)
        if mt:
            scan.add('pagamento', 'Pagamento/levantamento detectado', 'FLAG', m, mt, blob)
        mt = RX_PAG_PARCIAL.search(blob)
        if mt:
            scan.add('pag_parcial', 'Pagamento parcial', 'FLAG', m, mt, blob)

        # ---- Valor homologado (best-effort)
        if RX_VALOR_CONTEXTO.search(texto):
            for vm in RX_VALOR_BRL.finditer(texto):
                # só considera valores a até 200 chars de um contexto de valor
                ctx_ini = max(0, vm.start() - 200)
                if RX_VALOR_CONTEXTO.search(texto[ctx_ini:vm.start()]):
                    try:
                        num = float(vm.group(1).replace('.', '').replace(',', '.'))
                    except ValueError:
                        continue
                    if num < 100:  # ruído (custas, taxas mínimas)
                        continue
                    valor_candidatos.append((
                        m['data'].isoformat(), num,
                        _trecho(texto, vm, 100), m.get('id'),
                    ))

    # ---- sinal de cadastro: classe TPU de cumprimento -----------------------
    if classe_codigo and classe_codigo in CLASSES_CUMPRIMENTO:
        scan.ancoras.append(Ancora(
            sinal='cumprimento_classe',
            label=f'Classe judicial: {classe_nome or "Cumprimento de sentença"}',
            estagio=ESTAGIO_PRE, mov_id=None, data=None,
            trecho=f'Classe TPU {classe_codigo} — {classe_nome or "Cumprimento de sentença"} '
                   '(dado do cadastro público do processo)',
        ))
        scan.contagem['cumprimento_classe'] = 1

    return _decidir(scan, movs, valor_candidatos)


def _decidir(scan: _Scan, movs: list[dict], valor_candidatos: list) -> dict:
    """Aplica a hierarquia determinística e monta o veredito."""
    ud = scan.ultima_data
    emit_forte = scan.tem('exped_confirmada') or scan.tem('tipo_expedicao')
    emit_fraco = scan.tem('oficio_requisitorio') or scan.tem('requisicao_pagamento')
    emit_dt = max((ud.get(s, '') for s in ('exped_confirmada', 'tipo_expedicao')), default='')
    cumprimento = (scan.tem('cumprimento_text') or scan.tem('cumprimento_classe'))
    pre_sinal = cumprimento or scan.tem('homologacao')
    dc_sinal = scan.tem('transito') or scan.tem('sentenca_procedente')

    classe = ESTAGIO_INDEFINIDO
    selo = None
    conf = 0.30
    motivo = 'Nenhum marco processual reconhecido nas movimentações públicas.'

    sat_dt = ud.get('ext_satisfeito', '')
    imp_dt = ud.get('improcedencia', '')

    if scan.tem('ext_satisfeito'):
        classe, selo = ESTAGIO_MORTO, SELO_SATISFEITO
        conf = 0.85 + (0.05 if scan.tem('pagamento') else 0.0)
        motivo = ('Extinção pelo pagamento/cumprimento da obrigação — o crédito '
                  'foi SATISFEITO (desfecho positivo: já pago, não há mais o que comprar).')
    elif scan.tem('improcedencia') and (not emit_forte or imp_dt >= emit_dt):
        classe, selo = ESTAGIO_MORTO, SELO_IMPROCEDENTE
        conf = 0.80
        motivo = ('Desfecho terminal negativo (improcedência / prescrição / '
                  'indeferimento) sem expedição posterior — o crédito não se confirmou.')
    elif emit_forte:
        classe = ESTAGIO_EMITIDO
        conf = 0.90 if scan.tem('exped_confirmada') and scan.tem('tipo_expedicao') else 0.85
        motivo = 'Expedição de precatório/RPV registrada nas movimentações públicas.'
    elif emit_fraco and pre_sinal:
        classe = ESTAGIO_EMITIDO
        conf = 0.70
        motivo = ('Menção a ofício requisitório/requisição de pagamento em processo '
                  'de cumprimento — expedição provável, sem confirmação explícita.')
    elif pre_sinal:
        classe = ESTAGIO_PRE
        conf = 0.60
        if cumprimento and scan.tem('homologacao'):
            conf = 0.75
        if scan.tem('transito'):
            conf = min(0.80, conf + 0.05)
        motivo = ('Cumprimento de sentença/homologação de cálculos em curso, '
                  'sem expedição de requisitório visível — fase pré-precatório.')
    elif dc_sinal:
        classe = ESTAGIO_DC
        conf = 0.65 if scan.tem('transito') else 0.55
        motivo = ('Sentença/trânsito em julgado detectado, sem cumprimento de '
                  'sentença visível — direito creditório em formação.')

    # improcedência com expedição posterior: crédito sobreviveu ao revés
    badges = []
    if scan.tem('improcedencia') and classe not in (ESTAGIO_MORTO,):
        badges.append({'code': 'improc_superada',
                       'label': 'Decisão negativa anterior superada por expedição posterior'})
    if scan.tem('ext_sem_merito'):
        badges.append({'code': 'ext_sem_merito',
                       'label': 'Extinção sem resolução de mérito detectada (não altera o estágio)'})
    if scan.tem('ext_ambigua') and classe != ESTAGIO_MORTO:
        badges.append({'code': 'ext_ambigua',
                       'label': 'Extinção de natureza incerta detectada (classe mantida)'})
    if scan.tem('ext_incidente'):
        badges.append({'code': 'ext_incidente',
                       'label': 'Extinção de incidente/embargos ignorada (não encerra o principal)'})

    flags = []
    if scan.tem('rpv'):
        flags.append({'code': 'rpv', 'label': 'RPV'})
    if scan.tem('pagamento'):
        flags.append({'code': 'pagamento', 'label': 'Pagamento/levantamento'})
    if scan.tem('pag_parcial'):
        flags.append({'code': 'pag_parcial', 'label': 'Pagamento parcial'})

    # valor homologado: candidato mais recente (best-effort, NULLABLE)
    valor = None
    if valor_candidatos:
        valor_candidatos.sort(key=lambda c: c[0])  # por data
        v_data, v_num, v_trecho, v_mov = valor_candidatos[-1]
        valor = {'valor': v_num, 'data': v_data, 'trecho': v_trecho, 'mov_id': v_mov}

    ancoras = [a.__dict__ for a in sorted(
        scan.ancoras, key=lambda a: (a.data or ''),
    )]

    return {
        'classe': classe,
        'selo': selo or classe,
        'label': ESTAGIO_LABELS.get(classe, classe),
        'confianca': round(min(conf, 0.95), 2),
        'motivo': motivo,
        'flags': flags,
        'badges': badges,
        'ancoras': ancoras,
        'valor_homologado': valor,
        'total_movs': len(movs),
        'engine': ENGINE_VERSION,
    }


# ---------------------------------------------------------------------------
# Camada ORM — busca as movs do processo (query SARGável: processo_id + índice
# (processo, -data_disponibilizacao)) e delega ao núcleo puro.
# ---------------------------------------------------------------------------

def analisar_processo(processo) -> dict:
    """Roda o motor de regras sobre um `tribunals.Process` persistido."""
    from tribunals.models import Movimentacao

    movs_qs = (
        Movimentacao.objects
        .filter(processo_id=processo.pk, ativo=True)
        .order_by('data_disponibilizacao')
        .values('id', 'data_disponibilizacao', 'tipo_comunicacao', 'texto', 'meio')
    )
    movs = [
        {'id': r['id'], 'data': r['data_disponibilizacao'],
         'tipo': r['tipo_comunicacao'], 'texto': r['texto'], 'meio': r['meio']}
        for r in movs_qs
    ]
    resultado = analisar_movs(
        movs,
        classe_codigo=processo.classe_codigo or '',
        classe_nome=processo.classe_nome or '',
    )
    resultado['tribunal'] = processo.tribunal_id
    resultado['cnj'] = processo.numero_cnj
    fontes = sorted({('DJEN' if m['meio'] != 'datajud' else 'Datajud') for m in movs})
    resultado['fontes'] = fontes
    return resultado


# ---------------------------------------------------------------------------
# Dispatcher plugável — regras | gbm | hybrid
# ---------------------------------------------------------------------------

def predict_estagio(processo) -> dict:
    """Ponto de entrada oficial. Respeita `settings.ESTAGIO_ENGINE`:

      'rules'  (default) — só o motor determinístico deste módulo
      'gbm'    — usa `tribunals.estagio.predict_estagio` (lib GBM); fallback regras
      'hybrid' — roda ambos; GBM concordante reforça a confiança, discordante
                 vira badge (o veredito ancorado das regras prevalece — o
                 investidor precisa ver os marcos, não um score opaco)
    """
    engine = getattr(settings, 'ESTAGIO_ENGINE', 'rules')
    resultado = analisar_processo(processo)

    if engine in ('gbm', 'hybrid'):
        try:
            from tribunals.estagio import predict_estagio as gbm_predict  # noqa: PLC0415
            gbm = gbm_predict(processo.numero_cnj)
            if engine == 'gbm' and gbm and gbm.get('classe'):
                gbm.setdefault('engine', 'gbm')
                gbm.setdefault('ancoras', resultado['ancoras'])
                gbm['total_movs'] = resultado['total_movs']
                return gbm
            if engine == 'hybrid' and gbm and gbm.get('classe'):
                if gbm['classe'] == resultado['classe']:
                    resultado['confianca'] = round(
                        min(0.98, resultado['confianca'] + 0.08), 2)
                    resultado['engine'] = 'regras v1 + gbm (concordantes)'
                else:
                    resultado['badges'].append({
                        'code': 'gbm_divergente',
                        'label': f'Modelo GBM sugere {gbm["classe"]} '
                                 f'({gbm.get("confianca", "?")}) — regras prevalecem',
                    })
                    resultado['engine'] = 'regras v1 (gbm divergente)'
        except Exception as exc:  # GBM indisponível nunca derruba a página
            logger.info('estagio: engine %s indisponível (%s); usando regras', engine, exc)

    return resultado


# ---------------------------------------------------------------------------
# Utilidades de CNJ (validação + inferência de tribunal)
# ---------------------------------------------------------------------------

CNJ_RE = re.compile(r'^(\d{7})-?(\d{2})\.?(\d{4})\.?(\d)\.?(\d{2})\.?(\d{4})$')

# TR estadual (J=8) → UF, ordem alfabética CNJ Res. 65/2008
_TR_ESTADUAL = {
    '01': 'AC', '02': 'AL', '03': 'AP', '04': 'AM', '05': 'BA', '06': 'CE',
    '07': 'DFT', '08': 'ES', '09': 'GO', '10': 'MA', '11': 'MT', '12': 'MS',
    '13': 'MG', '14': 'PA', '15': 'PB', '16': 'PR', '17': 'PE', '18': 'PI',
    '19': 'RJ', '20': 'RN', '21': 'RS', '22': 'RO', '23': 'RR', '24': 'SC',
    '25': 'SE', '26': 'SP', '27': 'TO',
}


def normalizar_cnj(raw: str) -> Optional[str]:
    """Aceita CNJ com/sem máscara e devolve o formato canônico
    NNNNNNN-DD.AAAA.J.TR.OOOO — ou None se inválido."""
    limpo = re.sub(r'[\s./-]', '', unicodedata.normalize('NFKC', raw or ''))
    if not limpo.isdigit() or len(limpo) != 20:
        return None
    return (f'{limpo[0:7]}-{limpo[7:9]}.{limpo[9:13]}.'
            f'{limpo[13]}.{limpo[14:16]}.{limpo[16:20]}')


def tribunal_sigla_do_cnj(cnj: str) -> Optional[str]:
    """Infere a sigla do tribunal a partir dos segmentos J.TR do CNJ."""
    m = CNJ_RE.match(cnj or '')
    if not m:
        return None
    j, tr = m.group(4), m.group(5)
    if j == '4':
        return f'TRF{int(tr)}'
    if j == '5':
        return f'TRT{int(tr)}'
    if j == '8':
        uf = _TR_ESTADUAL.get(tr)
        return f'TJ{uf}' if uf else None
    if j == '3':
        return 'STJ'
    if j == '1':
        return 'STF'
    return None
