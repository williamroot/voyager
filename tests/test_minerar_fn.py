"""Testes do command `minerar_fn` (Task #16).

Cobre:
- E2 (novos regex): processos com texto "precatório expedido" ativam E2.
- E3 (F1 órfão): Cumprimento com poucas movs ativa E3.
- composite score limitado a [0, 1].
- --dry-run não escreve CSV.
- --upsert-lote é no-op quando tribunals.sampling.criar_lote não existe.
"""
from __future__ import annotations

import os
import sys
from datetime import timedelta
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.utils import timezone

from tribunals.models import Movimentacao, Process, Tribunal

pytestmark = pytest.mark.django_db


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def trf1():
    t, _ = Tribunal.objects.get_or_create(
        sigla='TRF1', defaults={'nome': 'TRF1', 'ativo': True},
    )
    return t


def _cnj(i: int) -> str:
    seq = f'{i:07d}'
    return f'{seq}-12.2025.4.01.3700'


def _criar_processo(trib, idx: int, *, classe='12078', total_movs=10,
                    classificacao=Process.CLASSIF_NAO_LEAD, score=0.05):
    p = Process.objects.create(
        tribunal=trib,
        numero_cnj=_cnj(idx),
        classe_codigo=classe,
        classe_nome='Cumprimento de Sentença contra a Fazenda Pública',
        total_movimentacoes=total_movs,
        classificacao=classificacao,
        classificacao_score=score,
        classificacao_versao='v6',
    )
    return p


def _criar_movs(processo, trib, *, n: int, texto: str = '', tipo: str = ''):
    now = timezone.now()
    movs = []
    for i in range(n):
        movs.append(Movimentacao(
            processo=processo, tribunal=trib,
            external_id=f'{processo.numero_cnj}-mov-{i}',
            data_disponibilizacao=now - timedelta(days=i),
            texto=texto, tipo_comunicacao=tipo,
        ))
    Movimentacao.objects.bulk_create(movs)


# ── Teste 1: E2 ativa em processos com "precatório expedido" ───────────────

def test_e2_regex_precatorio_expedido(trf1, tmp_path):
    """100 NAO_LEAD; 5 com texto 'precatório expedido' devem ativar E2."""
    # 95 processos sem o texto chave (classe NÃO-Cumprimento pra não disparar E3)
    for i in range(95):
        p = _criar_processo(trf1, i + 1000, classe='999', total_movs=20)
        _criar_movs(p, trf1, n=3, texto='despacho do juiz', tipo='Despacho')

    # 5 com "precatório expedido" (também sem classe Cumprimento pra isolar E2)
    targets = []
    for i in range(5):
        p = _criar_processo(trf1, i + 9000, classe='999', total_movs=20)
        _criar_movs(p, trf1, n=2,
                    texto='Precatório expedido em ordem cronológica',
                    tipo='Comunicação')
        targets.append(p.numero_cnj)

    output = tmp_path / 'fn.csv'
    out_buf = StringIO()
    call_command(
        'minerar_fn',
        '--tribunal', 'TRF1',
        '--limit', '5000',
        '--output', str(output),
        '--limite-universo', '500',
        stdout=out_buf,
    )

    assert output.exists()
    content = output.read_text(encoding='utf-8')
    # Os 5 alvos devem aparecer no CSV (linha contém o CNJ e motivos com E2)
    for cnj in targets:
        assert cnj in content, f'{cnj} ausente do CSV'
    # Pelo menos uma linha tem 'E2' em motivos
    assert 'E2' in content


# ── Teste 2: E3 ativa em F1 órfão ───────────────────────────────────────────

def test_e3_f1_orfao(trf1, tmp_path):
    """Processo com classe Cumprimento e ≤5 movs ativa E3."""
    # 1 candidato F1 órfão (classe 12078, 3 movs)
    p = _criar_processo(trf1, 7777, classe='12078', total_movs=3)
    _criar_movs(p, trf1, n=3, texto='movimentação genérica')

    # mais alguns sem F1 pra ter universo
    for i in range(20):
        ni = _criar_processo(trf1, 8000 + i, classe='999', total_movs=50)
        _criar_movs(ni, trf1, n=5, texto='despacho')

    output = tmp_path / 'fn.csv'
    out_buf = StringIO()
    call_command(
        'minerar_fn',
        '--tribunal', 'TRF1',
        '--limit', '100',
        '--output', str(output),
        '--limite-universo', '500',
        stdout=out_buf,
    )

    content = output.read_text(encoding='utf-8')
    assert p.numero_cnj in content
    # A linha correspondente deve mencionar E3
    target_line = next(ln for ln in content.splitlines() if p.numero_cnj in ln)
    assert 'E3' in target_line


# ── Teste 3: composite suspeita_score nunca excede 1.0 ──────────────────────

def test_suspeita_score_max_1(trf1, tmp_path):
    """Processo que dispara várias estratégias deve ser clamped em 1.0."""
    # Cria processo "jackpot": classe 12078 + poucas movs + texto com vários
    # regex novos + score na banda [0.10, 0.20]
    p = _criar_processo(trf1, 12345, classe='12078', total_movs=3, score=0.15)
    _criar_movs(
        p, trf1, n=3,
        texto=('Precatório expedido. RPV expedida em ordem cronológica. '
               'Ofício requisitório expedido. Pagamento administrativo '
               'efetuado. Transitado em julgado.'),
        tipo='Comunicação',
    )

    output = tmp_path / 'fn.csv'
    out_buf = StringIO()
    call_command(
        'minerar_fn',
        '--tribunal', 'TRF1',
        '--limit', '10',
        '--output', str(output),
        '--limite-universo', '100',
        stdout=out_buf,
    )

    content = output.read_text(encoding='utf-8')
    # Encontra linha desse CNJ; coluna 4 é suspeita_score
    target = next(ln for ln in content.splitlines() if p.numero_cnj in ln)
    fields = target.split(',')
    suspeita = float(fields[3])
    assert 0.0 <= suspeita <= 1.0


# ── Teste 4: --dry-run não escreve CSV ──────────────────────────────────────

def test_dry_run_nao_escreve(trf1, tmp_path):
    p = _criar_processo(trf1, 22222, classe='12078', total_movs=3)
    _criar_movs(p, trf1, n=2, texto='precatório expedido')

    output = tmp_path / 'nao_existe.csv'
    out_buf = StringIO()
    call_command(
        'minerar_fn',
        '--tribunal', 'TRF1',
        '--limit', '50',
        '--output', str(output),
        '--limite-universo', '100',
        '--dry-run',
        stdout=out_buf,
    )

    assert not output.exists(), 'CSV não deveria ser escrito em --dry-run'
    assert 'dry-run' in out_buf.getvalue().lower()


# ── Teste 5: --upsert-lote sem sampling — no-op com warning ────────────────

def test_upsert_lote_sem_sampling(trf1, tmp_path, monkeypatch):
    """Quando tribunals.sampling não existe, --upsert-lote deve apenas avisar."""
    # Garante que import falhe — remove módulo do cache se acaso existir.
    monkeypatch.delitem(sys.modules, 'tribunals.sampling', raising=False)

    # Bloqueia o import direto colocando um finder/loader que rejeita
    # esse path específico — usa um wrapper no __import__.
    import builtins
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == 'tribunals.sampling' or (
            name == 'tribunals' and fromlist and 'sampling' in fromlist
        ):
            raise ImportError('forçado pelo teste — sampling indisponível')
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, '__import__', fake_import)

    # Cria um candidato pra ter algo no top-N
    p = _criar_processo(trf1, 33333, classe='12078', total_movs=3)
    _criar_movs(p, trf1, n=2, texto='precatório expedido')

    output = tmp_path / 'fn.csv'
    out_buf = StringIO()
    call_command(
        'minerar_fn',
        '--tribunal', 'TRF1',
        '--limit', '10',
        '--output', str(output),
        '--limite-universo', '100',
        '--upsert-lote',
        stdout=out_buf,
    )

    out = out_buf.getvalue()
    assert 'upsert-lote' in out.lower() or 'sampling' in out.lower()
    # CSV foi escrito normalmente
    assert output.exists()
