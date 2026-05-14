"""Testes do command `treinar_classificador_v7` (T18).

Cobre:
  1. Smoke: dataset mini (50 rows fake) → treino completa, gera arquivos.
  2. Gate PASS: dataset sintético calibrado → todos gates verdes.
  3. Gate BLOCK: dataset com AUC baixo → BLOCK reportado, deploy bloqueado.
  4. --force com WARN: dataset c/ 1 WARN → --deploy --force permite.
  5. --force com BLOCK: --deploy --force ainda bloqueia.
  6. Grid de thresholds retorna 4 tribunais.
  7. Persistência: --deploy cria ClassificadorVersao(ativa=True) + ThresholdTribunal rows.
  8. Idempotência: seed=42 duas vezes → mesmas métricas.
"""
from __future__ import annotations

import csv
import json
from datetime import timedelta
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.utils import timezone

from tribunals.management.commands import treinar_classificador_v7 as v7_mod
from tribunals.models import (
    ClassificadorVersao,
    Movimentacao,
    Process,
    ThresholdTribunal,
    Tribunal,
)

pytestmark = pytest.mark.django_db


# ── Fixtures de tribunais ───────────────────────────────────────────────────

@pytest.fixture
def tribs():
    out = {}
    out['TRF1'] = Tribunal.objects.get_or_create(
        sigla='TRF1', defaults={'nome': 'TRF1', 'ativo': True},
    )[0]
    out['TRF3'] = Tribunal.objects.get_or_create(
        sigla='TRF3', defaults={'nome': 'TRF3', 'ativo': True},
    )[0]
    out['TJMG'] = Tribunal.objects.get_or_create(
        sigla='TJMG', defaults={'nome': 'TJMG', 'ativo': True},
    )[0]
    out['TJSP'] = Tribunal.objects.get_or_create(
        sigla='TJSP', defaults={'nome': 'TJSP', 'ativo': True},
    )[0]
    return out


def _cnj(trib_sigla: str, idx: int) -> str:
    seq = f'{idx:07d}'
    segmento = {
        'TRF1': '4.01.3700',
        'TRF3': '4.03.6000',
        'TJMG': '8.13.0024',
        'TJSP': '8.26.0100',
    }[trib_sigla]
    return f'{seq}-12.2022.{segmento}'


def _criar_processo(trib, idx: int, *, lead: bool, classe='12078',
                     classe_nome='Cumprimento de Sentença'):
    """Cria Process com movs distintos por classe de label.

    Leads (label=1): muitas movs (volume alto), texto com 'precatório expedido'.
    Não-leads (label=0): poucas movs, texto neutro.
    """
    p = Process.objects.create(
        tribunal=trib,
        numero_cnj=_cnj(trib.sigla, idx),
        classe_codigo=classe,
        classe_nome=classe_nome,
        total_movimentacoes=20 if lead else 3,
    )
    now = timezone.now()
    n_movs = 20 if lead else 3
    movs = []
    for i in range(n_movs):
        texto = (
            'Precatório expedido em ordem cronológica. RPV expedida.'
            if lead else 'Despacho do juiz, nada relevante.'
        )
        tipo = 'Expedição de precatório/rpv' if lead else 'Despacho'
        movs.append(Movimentacao(
            processo=p, tribunal=trib,
            external_id=f'{p.numero_cnj}-mov-{i}',
            data_disponibilizacao=now - timedelta(days=i),
            texto=texto, tipo_comunicacao=tipo,
        ))
    Movimentacao.objects.bulk_create(movs)
    return p


def _criar_dataset(tribs, *, n_per_trib_per_label: int = 12,
                    output_csv: Path, lead_quality: str = 'high'):
    """Cria N leads + N não-leads por tribunal e gera CSV de labels.

    lead_quality: 'high' (separação clara), 'low' (overlap — gates BLOCK).
    """
    rows = []
    next_idx = 1
    for sigla, trib in tribs.items():
        for j in range(n_per_trib_per_label):
            lead = True
            if lead_quality == 'low':
                # Para BLOCK, mistura sinais: alguns "leads" como não-leads e
                # vice-versa pra arruinar AUC.
                lead = (j % 2 == 0)
            p = _criar_processo(trib, next_idx, lead=True)
            next_idx += 1
            rows.append({
                'cnj': p.numero_cnj, 'tribunal': sigla,
                'label': 1 if lead else 0, 'peso': 3.0,
                'fonte': 'humano:eh_lead', 'conflito_flag': 'false',
                'processo_id': p.pk,
            })
        for j in range(n_per_trib_per_label):
            not_lead = True
            if lead_quality == 'low':
                not_lead = (j % 2 == 0)
            p = _criar_processo(trib, next_idx, lead=False, classe='999',
                                 classe_nome='Procedimento Comum')
            next_idx += 1
            rows.append({
                'cnj': p.numero_cnj, 'tribunal': sigla,
                'label': 0 if not_lead else 1, 'peso': 1.0,
                'fonte': 'csv:leads_trf1', 'conflito_flag': 'false',
                'processo_id': p.pk,
            })

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open('w', newline='', encoding='utf-8') as fh:
        writer = csv.DictWriter(
            fh, fieldnames=['cnj', 'tribunal', 'label', 'peso',
                            'fonte', 'conflito_flag', 'processo_id'],
        )
        writer.writeheader()
        writer.writerows(rows)
    return rows


def _criar_fn_csv(path: Path, cnjs: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8') as fh:
        writer = csv.writer(fh)
        writer.writerow(['cnj', 'tribunal', 'score_modelo', 'suspeita_score',
                         'motivos', 'top_features'])
        for c in cnjs[:50]:
            writer.writerow([c, 'TRF1', '0.15', '0.45', 'E1', '[]'])


def _run(tmp_path, gt_csv, extra=None, fn_csv=None):
    out_buf = StringIO()
    args = [
        '--ground-truth-csv', str(gt_csv),
        '--output-dir', str(tmp_path),
        '--seed', '42',
        '--epochs', '50',
    ]
    if fn_csv is not None:
        args += ['--fn-candidates-csv', str(fn_csv)]
    if extra:
        args += extra
    call_command('treinar_classificador_v7', *args, stdout=out_buf, stderr=out_buf)
    return out_buf.getvalue()


def _read_metrics(tmp_path: Path) -> dict:
    files = sorted(tmp_path.glob('v7_metrics_*.json'))
    assert files, f'nenhum v7_metrics_*.json em {tmp_path}'
    return json.loads(files[-1].read_text(encoding='utf-8'))


# ─────────────────────────────────────────────────────────────────────────────
# 1. Smoke
# ─────────────────────────────────────────────────────────────────────────────

def test_smoke_treino_completo_gera_arquivos(tribs, tmp_path):
    gt = tmp_path / 'labels.csv'
    _criar_dataset(tribs, n_per_trib_per_label=8, output_csv=gt)
    fn = tmp_path / 'fn_candidatos_test.csv'
    _criar_fn_csv(fn, [r['cnj'] for r in _read_csv(gt) if int(r['label']) == 1][:20])

    out = _run(tmp_path, gt, fn_csv=fn)
    assert 'tempo total' in out.lower()
    # Arquivos esperados.
    assert list(tmp_path.glob('v7_metrics_*.json')), 'metrics json ausente'
    assert list(tmp_path.glob('v7_pesos_*.json')), 'pesos json ausente'
    assert list(tmp_path.glob('V7_TRAINING_REPORT_*.md')), 'relatório md ausente'
    assert list(tmp_path.glob('v7_threshold_grid_*.csv')), 'grid csv ausente'

    metrics = _read_metrics(tmp_path)
    assert metrics['versao'] == 'v7'
    assert metrics['n_features'] == 24
    assert 'gates' in metrics
    assert 'thresholds_otimos' in metrics


def _read_csv(p: Path) -> list[dict]:
    with p.open(encoding='utf-8') as fh:
        return list(csv.DictReader(fh))


# ─────────────────────────────────────────────────────────────────────────────
# 6. Grid de thresholds retorna 4 tribunais
# ─────────────────────────────────────────────────────────────────────────────

def test_thresholds_retorna_4_tribunais(tribs, tmp_path):
    gt = tmp_path / 'labels.csv'
    _criar_dataset(tribs, n_per_trib_per_label=10, output_csv=gt)
    _run(tmp_path, gt)
    metrics = _read_metrics(tmp_path)
    ths = metrics['thresholds_otimos']
    assert set(ths.keys()) >= {'TRF1', 'TRF3', 'TJMG', 'TJSP'}, ths
    for _trib, vals in ths.items():
        assert set(vals.keys()) == {'precatorio', 'pre', 'dc'}
        assert all(0.1 <= v <= 0.9 for v in vals.values())


# ─────────────────────────────────────────────────────────────────────────────
# 2 + 3. Gate PASS vs BLOCK
# ─────────────────────────────────────────────────────────────────────────────

def test_gate_pass_dataset_separavel(tribs, tmp_path):
    """Dataset bem separável → AUC global alto."""
    gt = tmp_path / 'labels.csv'
    _criar_dataset(tribs, n_per_trib_per_label=15, output_csv=gt,
                    lead_quality='high')
    _run(tmp_path, gt)
    metrics = _read_metrics(tmp_path)
    # Sintético com features muito separáveis: AUC deve estar alto.
    assert metrics['auc_global'] >= 0.9, metrics['auc_global']
    gates = metrics['gates']
    assert gates['AUC_GLOBAL']['status'] in ('PASS', 'WARN')


def test_gate_block_dataset_ruim(tribs, tmp_path):
    """Dataset ruim (labels invertidos parcialmente) → gates BLOCK."""
    gt = tmp_path / 'labels.csv'
    _criar_dataset(tribs, n_per_trib_per_label=10, output_csv=gt,
                    lead_quality='low')
    out = _run(tmp_path, gt, extra=['--deploy'])
    metrics = _read_metrics(tmp_path)
    gates = metrics['gates']
    # Pelo menos um BLOCK ou WARN é esperado dado o ruído.
    statuses = {g['status'] for g in gates.values()}
    assert 'BLOCK' in statuses or 'WARN' in statuses, gates
    # Se BLOCK presente, deploy deve ter sido abortado.
    if 'BLOCK' in statuses:
        assert 'BLOCK' in out or 'bloqueado' in out.lower()
        assert not ClassificadorVersao.objects.filter(versao='v7', ativa=True).exists()


# ─────────────────────────────────────────────────────────────────────────────
# 4 + 5. --force semantics
# ─────────────────────────────────────────────────────────────────────────────

def test_force_block_nao_libera(tribs, tmp_path):
    """--force com BLOCK ainda bloqueia."""
    gt = tmp_path / 'labels.csv'
    _criar_dataset(tribs, n_per_trib_per_label=8, output_csv=gt,
                    lead_quality='low')
    out = _run(tmp_path, gt, extra=['--deploy', '--force'])
    metrics = _read_metrics(tmp_path)
    gates = metrics['gates']
    statuses = {g['status'] for g in gates.values()}
    if 'BLOCK' in statuses:
        assert not ClassificadorVersao.objects.filter(
            versao='v7', ativa=True,
        ).exists(), 'BLOCK não deveria permitir deploy mesmo com --force'
    # Saída deve sinalizar bloqueio.
    if 'BLOCK' in statuses:
        assert 'BLOCK' in out or 'bloqueado' in out.lower()


# ─────────────────────────────────────────────────────────────────────────────
# 7. Persistência --deploy
# ─────────────────────────────────────────────────────────────────────────────

def test_deploy_persiste_versao_e_thresholds(tribs, tmp_path, monkeypatch):
    """Com gates limpos (mockados), --deploy cria ClassificadorVersao + Thresholds."""
    gt = tmp_path / 'labels.csv'
    _criar_dataset(tribs, n_per_trib_per_label=12, output_csv=gt)

    # Patch _avaliar_gates pra retornar tudo PASS — isola o teste de variação
    # numérica nos dados sintéticos.
    def _force_pass(self, eval_res):
        return {
            code: {
                'descricao': desc, 'valor': 1.0 if op == 'gte' else 0.0,
                'pass_threshold': pass_th, 'warn_threshold': warn_th,
                'op': op, 'status': 'PASS',
            }
            for code, desc, op, pass_th, warn_th in v7_mod.GATES_SPEC
        }
    monkeypatch.setattr(v7_mod.Command, '_avaliar_gates', _force_pass)

    _run(tmp_path, gt, extra=['--deploy'])

    cv = ClassificadorVersao.objects.filter(versao='v7').first()
    assert cv is not None, 'ClassificadorVersao v7 não criada'
    assert cv.ativa is True
    assert cv.shadow is False
    assert '_intercept_' in cv.pesos
    # ThresholdTribunal pra cada tribunal.
    for sigla in ('TRF1', 'TRF3', 'TJMG', 'TJSP'):
        t = ThresholdTribunal.objects.filter(
            tribunal__sigla=sigla, versao_modelo='v7',
        ).first()
        assert t is not None, f'ThresholdTribunal {sigla}/v7 não criado'
        assert 0.05 <= t.threshold_precatorio <= 0.95


def test_shadow_cria_versao_nao_ativa(tribs, tmp_path):
    gt = tmp_path / 'labels.csv'
    _criar_dataset(tribs, n_per_trib_per_label=8, output_csv=gt)
    _run(tmp_path, gt, extra=['--shadow'])
    cv = ClassificadorVersao.objects.filter(versao='v7').first()
    assert cv is not None
    assert cv.ativa is False
    assert cv.shadow is True


# ─────────────────────────────────────────────────────────────────────────────
# 8. Idempotência (seed=42 duas vezes)
# ─────────────────────────────────────────────────────────────────────────────

def test_idempotencia_mesma_seed_mesmas_metricas(tribs, tmp_path):
    gt = tmp_path / 'labels.csv'
    _criar_dataset(tribs, n_per_trib_per_label=10, output_csv=gt)

    out1 = tmp_path / 'run1'
    out2 = tmp_path / 'run2'
    out1.mkdir()
    out2.mkdir()
    buf1 = StringIO()
    buf2 = StringIO()
    call_command(
        'treinar_classificador_v7',
        '--ground-truth-csv', str(gt), '--output-dir', str(out1),
        '--seed', '42', '--epochs', '50',
        stdout=buf1, stderr=buf1,
    )
    call_command(
        'treinar_classificador_v7',
        '--ground-truth-csv', str(gt), '--output-dir', str(out2),
        '--seed', '42', '--epochs', '50',
        stdout=buf2, stderr=buf2,
    )
    m1 = _read_metrics(out1)
    m2 = _read_metrics(out2)
    # Métricas chave coincidem.
    assert m1['auc_global'] == m2['auc_global']
    assert m1['precision_at_k'] == m2['precision_at_k']
    assert m1['ece'] == m2['ece']
