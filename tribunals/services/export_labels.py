"""Exporta labels consolidados para o retreino v7 do classificador.

Junta TODAS as fontes de ground truth em 1 CSV único com pesos amostrais
por origem (definidos em `.ia/REGRAS_NEGOCIO_VALIDACAO.md`):

  Peso 3.0 — Validação humana (`ProcessoValidacao`)
  Peso 2.0 — Juriscope (`LeadConsumption`) e CSVs reforçados (precatorio_1336,
             recuperados_1327, falsos_consumidos_1327, trf3 precatorio_500)
  Peso 1.0 — CSV base `leads_trf1.csv`

Política de divergência quando o mesmo CNJ aparece em fontes diferentes:
  1. Maior peso ganha.
  2. Empate de peso: humano > Juriscope > CSV legado (ordem `_ORIGEM_PRIORIDADE`).
  3. `conflito_flag=True` quando há labels divergentes entre fontes.

Output: CSV em `data/labels_retreino_YYYYMMDD_HHMMSS.csv` (path retornado).

Sem libs novas além de Django/stdlib — não usa pandas (não está em
`requirements.txt`).
"""
from __future__ import annotations

import csv
import logging
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from django.conf import settings

from tribunals.models import (
    LeadConsumption,
    Process,
    ProcessoValidacao,
)

logger = logging.getLogger(__name__)


# ── pesos por origem (ver REGRAS_NEGOCIO_VALIDACAO.md) ───────────────────────
PESO_HUMANO = 3.0
PESO_JURISCOPE = 2.0
PESO_CSV_REFORCADO = 2.0
PESO_CSV_BASE = 1.0

# Ordem de prioridade pra desempate (índice menor = mais autoritário).
_ORIGEM_PRIORIDADE = {
    'humano': 0,
    'juriscope': 1,
    'csv_legado': 2,
}

# Resultados de ProcessoValidacao que viram label=1 / label=0.
_RESULTADO_LEAD_POS = {'eh_lead', 'eh_precatorio', 'eh_pre', 'eh_dc'}
_RESULTADO_LEAD_NEG = {'nao_lead'}
# Resultados explicitamente excluídos do dataset de treino.
_RESULTADO_EXCLUIR = {'incerto', 'precisa_enriquecer', 'skip'}

# Mapeia `LeadConsumption.resultado` → label binário (None = ignorar).
_JURISCOPE_LABEL = {
    'validado': 1,
    'pago': 1,
    'sem_expedicao': 0,
}

# Detecta tribunal a partir do CNJ (segmento J.TR após o ano).
_CNJ_RE = re.compile(r'^\d{7}-\d{2}\.\d{4}\.(\d)\.(\d{2})\.\d{4}$')
_CNJ_LOOSE_RE = re.compile(r'^\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}$')
_SEGMENTO_TRIBUNAL = {
    ('4', '01'): 'TRF1',
    ('4', '03'): 'TRF3',
    ('4', '02'): 'TRF2',
    ('4', '04'): 'TRF4',
    ('4', '05'): 'TRF5',
    ('4', '06'): 'TRF6',
}


def _tribunal_do_cnj(cnj: str) -> str:
    m = _CNJ_RE.match(cnj or '')
    if not m:
        return ''
    return _SEGMENTO_TRIBUNAL.get((m.group(1), m.group(2)), '')


def _is_cnj(s: str) -> bool:
    return bool(_CNJ_LOOSE_RE.match((s or '').strip()))


# ── representação interna ─────────────────────────────────────────────────────

@dataclass
class _LabelRow:
    """Linha consolidada (1 por CNJ após resolução de conflitos)."""

    cnj: str
    tribunal: str
    label: int
    peso: float
    fonte: str
    conflito_flag: bool = False
    processo_id: int | None = None


@dataclass
class _Observacao:
    """Uma observação bruta antes da deduplicação por CNJ."""

    cnj: str
    label: int
    peso: float
    fonte: str
    # Categoria de origem para desempate (humano/juriscope/csv_legado).
    origem: str = 'csv_legado'
    # Nota livre de auditoria (não vai pro CSV final).
    nota: str = ''


# ── leitura de CSVs legados ──────────────────────────────────────────────────

def _iter_cnjs_csv(path: Path) -> Iterator[str]:
    """Itera CNJs de um CSV. Tolera com/sem header `numero_processo`.

    Aceita arquivos com 1 coluna (lista pura) ou multi-coluna (primeira coluna
    é o CNJ). Linhas em branco e linhas com CNJ inválido são puladas.
    """
    if not path.exists():
        logger.warning('CSV não encontrado: %s — pulando', path)
        return
    with path.open('r', encoding='utf-8', newline='') as fh:
        reader = csv.reader(fh)
        first = True
        for row in reader:
            if not row:
                continue
            valor = (row[0] or '').strip()
            if first:
                first = False
                # Se for header, pula.
                if not _is_cnj(valor):
                    continue
            if _is_cnj(valor):
                yield valor


def _csv_observacoes(
    path: Path,
    *,
    label: int,
    peso: float,
    fonte: str,
) -> list[_Observacao]:
    return [
        _Observacao(cnj=c, label=label, peso=peso, fonte=fonte, origem='csv_legado')
        for c in _iter_cnjs_csv(path)
    ]


def _coletar_csvs_legados(base_dir: Path) -> list[_Observacao]:
    """Lê todos os CSVs legados conforme a tabela de fontes."""
    obs: list[_Observacao] = []

    # #1 leads_trf1.csv — label=1, peso=1.0
    obs.extend(_csv_observacoes(
        base_dir / 'leads_trf1.csv',
        label=1, peso=PESO_CSV_BASE, fonte='csv:leads_trf1',
    ))

    # #2 leads_trf3.csv UNION leads_trf3_precatorio_500.csv — label=1, peso=2.0
    obs.extend(_csv_observacoes(
        base_dir / 'leads_trf3.csv',
        label=1, peso=PESO_CSV_REFORCADO, fonte='csv:leads_trf3',
    ))
    obs.extend(_csv_observacoes(
        base_dir / 'leads_trf3_precatorio_500.csv',
        label=1, peso=PESO_CSV_REFORCADO, fonte='csv:leads_trf3_precatorio_500',
    ))

    # #3 leads_trf1_falsos_consumidos_1327.csv — label=0, peso=2.0
    obs.extend(_csv_observacoes(
        base_dir / 'leads_trf1_falsos_consumidos_1327.csv',
        label=0, peso=PESO_CSV_REFORCADO, fonte='csv:leads_trf1_falsos_consumidos_1327',
    ))

    # #4 leads_trf1_recuperados_1327.csv — label=1, peso=2.0
    obs.extend(_csv_observacoes(
        base_dir / 'leads_trf1_recuperados_1327.csv',
        label=1, peso=PESO_CSV_REFORCADO, fonte='csv:leads_trf1_recuperados_1327',
    ))

    # #5 leads_trf1_precatorio_1336.csv — label=1 (subtipo N1), peso=2.0
    obs.extend(_csv_observacoes(
        base_dir / 'leads_trf1_precatorio_1336.csv',
        label=1, peso=PESO_CSV_REFORCADO, fonte='csv:leads_trf1_precatorio_1336',
    ))

    return obs


# ── leitura de fontes Django ─────────────────────────────────────────────────

def _coletar_juriscope(min_data: datetime | None) -> list[_Observacao]:
    """LeadConsumption — resultados validado/pago/sem_expedicao."""
    qs = (
        LeadConsumption.objects
        .filter(resultado__in=list(_JURISCOPE_LABEL.keys()))
        .select_related('processo')
        .only('resultado', 'consumido_em', 'processo__numero_cnj')
    )
    if min_data is not None:
        qs = qs.filter(consumido_em__gte=min_data)

    obs: list[_Observacao] = []
    for lc in qs.iterator(chunk_size=2000):
        label = _JURISCOPE_LABEL.get(lc.resultado)
        if label is None:
            continue
        cnj = (lc.processo.numero_cnj or '').strip()
        if not cnj:
            continue
        obs.append(_Observacao(
            cnj=cnj,
            label=label,
            peso=PESO_JURISCOPE,
            fonte=f'juriscope:{lc.resultado}',
            origem='juriscope',
        ))
    return obs


def _coletar_humano(min_data: datetime | None) -> list[_Observacao]:
    """ProcessoValidacao — usa `label_final` se preenchido (divergência
    resolvida); senão usa `resultado` original. Exclui incerto/skip/
    precisa_enriquecer.
    """
    qs = (
        ProcessoValidacao.objects
        .select_related('processo')
        .only(
            'resultado', 'label_final', 'criada_em', 'processo__numero_cnj',
        )
    )
    if min_data is not None:
        qs = qs.filter(criada_em__gte=min_data)

    obs: list[_Observacao] = []
    for pv in qs.iterator(chunk_size=2000):
        resultado_efetivo = pv.label_final or pv.resultado
        if resultado_efetivo in _RESULTADO_EXCLUIR:
            continue
        if resultado_efetivo in _RESULTADO_LEAD_POS:
            label = 1
        elif resultado_efetivo in _RESULTADO_LEAD_NEG:
            label = 0
        else:
            continue
        cnj = (pv.processo.numero_cnj or '').strip()
        if not cnj:
            continue
        usou_final = bool(pv.label_final)
        obs.append(_Observacao(
            cnj=cnj,
            label=label,
            peso=PESO_HUMANO,
            fonte=f'humano:{resultado_efetivo}' + ('+final' if usou_final else ''),
            origem='humano',
        ))
    return obs


# ── consolidação ─────────────────────────────────────────────────────────────

def _consolidar(observacoes: Iterable[_Observacao]) -> tuple[list[_LabelRow], list[dict]]:
    """Agrupa observações por CNJ e resolve divergências.

    Retorna (rows finais, lista de conflitos resolvidos).
    Cada item em `rows` representa 1 CNJ único.
    """
    por_cnj: dict[str, list[_Observacao]] = {}
    for o in observacoes:
        por_cnj.setdefault(o.cnj, []).append(o)

    rows: list[_LabelRow] = []
    conflitos: list[dict] = []

    for cnj, grupo in por_cnj.items():
        labels_distintos = {o.label for o in grupo}
        conflito = len(labels_distintos) > 1

        # Ordena: maior peso primeiro, depois menor prioridade de origem.
        vencedor = max(
            grupo,
            key=lambda o: (o.peso, -_ORIGEM_PRIORIDADE.get(o.origem, 99)),
        )

        rows.append(_LabelRow(
            cnj=cnj,
            tribunal=_tribunal_do_cnj(cnj),
            label=vencedor.label,
            peso=vencedor.peso,
            fonte=vencedor.fonte,
            conflito_flag=conflito,
            processo_id=None,
        ))

        if conflito:
            conflitos.append({
                'cnj': cnj,
                'vencedor': vencedor.fonte,
                'label_final': vencedor.label,
                'observacoes': [
                    {'fonte': o.fonte, 'label': o.label, 'peso': o.peso}
                    for o in grupo
                ],
            })

    rows.sort(key=lambda r: r.cnj)
    conflitos.sort(key=lambda c: c['cnj'])
    return rows, conflitos


def _anexar_processo_ids(rows: list[_LabelRow]) -> None:
    """Preenche `processo_id` consultando o DB em batches.

    CNJs ausentes ficam com processo_id=None.
    """
    cnjs = [r.cnj for r in rows]
    mapa: dict[str, int] = {}
    batch = 5000
    for i in range(0, len(cnjs), batch):
        chunk = cnjs[i:i + batch]
        for pk, cnj in Process.objects.filter(
            numero_cnj__in=chunk,
        ).values_list('id', 'numero_cnj'):
            # Pode haver mesmo CNJ em tribunais diferentes (raríssimo); o
            # mapeamento prefere o primeiro que aparecer — coerente com o
            # tribunal já inferido pelo CNJ.
            mapa.setdefault(cnj, pk)

    for r in rows:
        r.processo_id = mapa.get(r.cnj)


# ── escrita do CSV final ─────────────────────────────────────────────────────

_CSV_COLUMNS = [
    'cnj', 'tribunal', 'label', 'peso', 'fonte', 'conflito_flag', 'processo_id',
]


def _escrever_csv(rows: list[_LabelRow], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open('w', encoding='utf-8', newline='') as fh:
        writer = csv.writer(fh)
        writer.writerow(_CSV_COLUMNS)
        for r in rows:
            writer.writerow([
                r.cnj,
                r.tribunal,
                r.label,
                f'{r.peso:.4f}',
                r.fonte,
                'true' if r.conflito_flag else 'false',
                '' if r.processo_id is None else r.processo_id,
            ])


# ── API pública ──────────────────────────────────────────────────────────────

@dataclass
class _ExportResult:
    """Acompanha o estado do export para logging/inspeção."""

    rows: list[_LabelRow] = field(default_factory=list)
    conflitos: list[dict] = field(default_factory=list)


def exportar_labels_retreino(
    *,
    min_data: datetime | None = None,
    output_path: Path | None = None,
    incluir_humano: bool = True,
    incluir_juriscope: bool = True,
    incluir_csvs_legados: bool = True,
) -> Path:
    """Consolida labels de TODAS as fontes em 1 CSV com pesos amostrais.

    Args:
        min_data: filtra `LeadConsumption.consumido_em >= min_data` e
            `ProcessoValidacao.criada_em >= min_data`. CSVs legados não
            são filtrados (não têm timestamp confiável).
        output_path: caminho de saída. Default
            `<BASE_DIR>/data/labels_retreino_YYYYMMDD_HHMMSS.csv`.
        incluir_humano: incluir `ProcessoValidacao`.
        incluir_juriscope: incluir `LeadConsumption`.
        incluir_csvs_legados: incluir CSVs raiz (leads_trf1, leads_trf3, etc).

    Returns:
        Path do CSV gerado.
    """
    base_dir = Path(settings.BASE_DIR)
    # CSVs de ground truth versionados em /data_ground_truth/ (não em /data/).
    ground_truth_dir = base_dir / 'data_ground_truth'

    if output_path is None:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_path = base_dir / 'data' / f'labels_retreino_{ts}.csv'
    else:
        output_path = Path(output_path)

    observacoes: list[_Observacao] = []
    if incluir_csvs_legados:
        observacoes.extend(_coletar_csvs_legados(ground_truth_dir))
    if incluir_juriscope:
        observacoes.extend(_coletar_juriscope(min_data))
    if incluir_humano:
        observacoes.extend(_coletar_humano(min_data))

    rows, conflitos = _consolidar(observacoes)
    _anexar_processo_ids(rows)

    if conflitos:
        exemplos = conflitos[:20]
        logger.info(
            'export_labels: %d conflitos resolvidos (mostrando até 20). '
            'Primeiros: %s',
            len(conflitos),
            [
                {'cnj': c['cnj'], 'vencedor': c['vencedor'],
                 'label_final': c['label_final']}
                for c in exemplos
            ],
        )

    _escrever_csv(rows, output_path)

    logger.info(
        'export_labels: %d linhas em %s (humano=%s, juriscope=%s, csvs=%s, '
        'conflitos=%d)',
        len(rows), output_path, incluir_humano, incluir_juriscope,
        incluir_csvs_legados, len(conflitos),
    )

    return output_path


def estatisticas_labels(df: list[_LabelRow] | list[dict] | Path | str) -> dict:
    """Estatísticas sumárias do dataset consolidado.

    Aceita:
      - lista de `_LabelRow` (in-memory, retornado por funções internas)
      - lista de dicts no formato do CSV
      - Path/str apontando pro CSV gerado

    Retorna dict com:
      - total: int
      - label_counts: {0: N, 1: M}
      - fonte_counts: {fonte: N, ...}
      - label_por_fonte: {(label, fonte): N, ...} no formato `"label=L|fonte=F": N`
      - conflitos: int
      - peso_medio: float
      - peso_total: float
      - tribunais: {tribunal: N, ...}
      - sem_processo_id: int
    """
    rows = _normalizar_para_dicts(df)

    total = len(rows)
    label_counts: dict[int, int] = {}
    fonte_counts: dict[str, int] = {}
    label_por_fonte: dict[str, int] = {}
    tribunais: dict[str, int] = {}
    conflitos = 0
    peso_total = 0.0
    sem_proc = 0

    for r in rows:
        lbl = int(r['label'])
        label_counts[lbl] = label_counts.get(lbl, 0) + 1
        fonte_counts[r['fonte']] = fonte_counts.get(r['fonte'], 0) + 1
        chave = f'label={lbl}|fonte={r["fonte"]}'
        label_por_fonte[chave] = label_por_fonte.get(chave, 0) + 1
        trib = r.get('tribunal') or ''
        tribunais[trib] = tribunais.get(trib, 0) + 1
        peso_total += float(r['peso'])
        if _is_truthy_conflito(r.get('conflito_flag')):
            conflitos += 1
        pid = r.get('processo_id')
        if pid in (None, '', 'null'):
            sem_proc += 1

    peso_medio = peso_total / total if total else 0.0

    return {
        'total': total,
        'label_counts': label_counts,
        'fonte_counts': fonte_counts,
        'label_por_fonte': label_por_fonte,
        'conflitos': conflitos,
        'peso_medio': peso_medio,
        'peso_total': peso_total,
        'tribunais': tribunais,
        'sem_processo_id': sem_proc,
    }


def _is_truthy_conflito(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in {'true', '1', 'yes', 'sim'}
    return bool(v)


def _normalizar_para_dicts(
    df: list[_LabelRow] | list[dict] | Path | str,
) -> list[dict]:
    """Aceita 3 tipos e devolve lista de dicts."""
    if isinstance(df, (Path, str)):
        path = Path(df)
        with path.open('r', encoding='utf-8', newline='') as fh:
            return list(csv.DictReader(fh))
    if not df:
        return []
    primeiro = df[0]
    if isinstance(primeiro, _LabelRow):
        return [
            {
                'cnj': r.cnj,
                'tribunal': r.tribunal,
                'label': r.label,
                'peso': r.peso,
                'fonte': r.fonte,
                'conflito_flag': r.conflito_flag,
                'processo_id': r.processo_id,
            }
            for r in df  # type: ignore[union-attr]
        ]
    return list(df)  # type: ignore[arg-type]
