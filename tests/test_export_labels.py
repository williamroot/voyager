"""Testes do service de export de labels para retreino v7.

Cobre:
  1. CSV legado simples (leads_trf1.csv) → label=1, peso=1.0
  2. Divergência humano vs Juriscope → humano ganha, conflito_flag=True
  3. ProcessoValidacao com label_final preenchido → usa label_final
  4. incerto/skip/precisa_enriquecer excluídos
  5. `min_data` filtra LeadConsumption e ProcessoValidacao
  6. Idempotência — 2 execuções consecutivas produzem mesmo conteúdo
  7. CNJ inexistente no DB → processo_id vazio no CSV
  8. `estatisticas_labels` retorna dict correto
"""
from __future__ import annotations

import csv
from datetime import timedelta
from pathlib import Path

import pytest
from django.conf import settings as dj_settings
from django.contrib.auth import get_user_model
from django.utils import timezone

from tribunals.models import (
    ApiClient,
    LeadConsumption,
    Process,
    ProcessoValidacao,
    Tribunal,
)
from tribunals.services.export_labels import (
    PESO_CSV_BASE,
    PESO_HUMANO,
    estatisticas_labels,
    exportar_labels_retreino,
)

pytestmark = pytest.mark.django_db

User = get_user_model()

CNJ_A = '0000001-10.2017.4.01.3820'
CNJ_B = '0000001-11.2018.4.01.3000'
CNJ_C = '0000040-43.2016.4.03.6000'  # TRF3
CNJ_DESCONHECIDO = '9999999-99.2099.4.01.9999'


# ─── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def trf1(db):
    t, _ = Tribunal.objects.get_or_create(sigla='TRF1', defaults={'nome': 'TRF1', 'ativo': True})
    return t


@pytest.fixture
def trf3(db):
    t, _ = Tribunal.objects.get_or_create(sigla='TRF3', defaults={'nome': 'TRF3', 'ativo': True})
    return t


@pytest.fixture
def cliente_juriscope(db):
    return ApiClient.objects.create(nome='juriscope-test', api_key='key-test-123')


@pytest.fixture
def user_alice(db):
    return User.objects.create_user(username='alice', password='x')


@pytest.fixture
def user_bob(db):
    return User.objects.create_user(username='bob', password='x')


@pytest.fixture
def tmp_output(tmp_path) -> Path:
    return tmp_path / 'labels.csv'


@pytest.fixture
def base_dir_vazio(tmp_path, monkeypatch) -> Path:
    """Aponta `settings.BASE_DIR` pra um tmp vazio (sem CSVs legados).

    Permite testes isolados focados em fontes Django.

    Retorna o subdiretório `data_ground_truth/` (onde os CSVs legados moram
    desde 2026-05-12). Apontar `BASE_DIR` pro tmp_path raiz garante que o
    código de produção encontre o subdir.
    """
    monkeypatch.setattr(dj_settings, 'BASE_DIR', tmp_path, raising=False)
    ground_truth = tmp_path / 'data_ground_truth'
    ground_truth.mkdir(parents=True, exist_ok=True)
    return ground_truth


def _ler_csv(path: Path) -> list[dict]:
    with path.open('r', encoding='utf-8', newline='') as fh:
        return list(csv.DictReader(fh))


# ─── Test 1: CSV legado simples ──────────────────────────────────────────────

def test_csv_legado_simples_leads_trf1(base_dir_vazio, tmp_output):
    """leads_trf1.csv com 2 CNJs → label=1, peso=1.0, fonte csv:leads_trf1."""
    csv_path = base_dir_vazio / 'leads_trf1.csv'
    csv_path.write_text(f'numero_processo\n{CNJ_A}\n{CNJ_B}\n', encoding='utf-8')

    out = exportar_labels_retreino(
        output_path=tmp_output,
        incluir_humano=False,
        incluir_juriscope=False,
        incluir_csvs_legados=True,
    )
    rows = _ler_csv(out)

    cnjs = {r['cnj']: r for r in rows}
    assert set(cnjs) == {CNJ_A, CNJ_B}
    for r in rows:
        assert int(r['label']) == 1
        assert float(r['peso']) == PESO_CSV_BASE
        assert r['fonte'] == 'csv:leads_trf1'
        assert r['conflito_flag'] == 'false'
        assert r['tribunal'] == 'TRF1'


# ─── Test 2: Divergência humano vs Juriscope ─────────────────────────────────

def test_divergencia_humano_vs_juriscope(
    base_dir_vazio, tmp_output, trf1, cliente_juriscope, user_alice,
):
    """Humano (peso 3) deve vencer Juriscope (peso 2) com labels divergentes."""
    proc = Process.objects.create(tribunal=trf1, numero_cnj=CNJ_A)

    # Juriscope diz label=0 (sem expedição)
    LeadConsumption.objects.create(
        processo=proc, cliente=cliente_juriscope,
        resultado=LeadConsumption.RESULTADO_SEM_EXPEDICAO,
    )
    # Humano diz label=1 (é lead)
    ProcessoValidacao.objects.create(
        processo=proc, usuario=user_alice,
        resultado=ProcessoValidacao.RESULTADO_EH_LEAD,
        versao_modelo='v6',
        classificacao_no_momento='PRECATORIO',
        score_no_momento=0.9,
    )

    out = exportar_labels_retreino(
        output_path=tmp_output,
        incluir_csvs_legados=False,
    )
    rows = _ler_csv(out)

    assert len(rows) == 1
    r = rows[0]
    assert r['cnj'] == CNJ_A
    assert int(r['label']) == 1                    # humano venceu
    assert float(r['peso']) == PESO_HUMANO
    assert r['fonte'].startswith('humano:')
    assert r['conflito_flag'] == 'true'


# ─── Test 3: label_final preenchido tem precedência ─────────────────────────

def test_processo_validacao_usa_label_final(
    base_dir_vazio, tmp_output, trf1, user_alice,
):
    proc = Process.objects.create(tribunal=trf1, numero_cnj=CNJ_A)

    pv = ProcessoValidacao.objects.create(
        processo=proc, usuario=user_alice,
        resultado=ProcessoValidacao.RESULTADO_EH_LEAD,   # original = lead
        label_final=ProcessoValidacao.RESULTADO_NAO_LEAD,  # revisor → não-lead
        versao_modelo='v6',
        classificacao_no_momento='PRECATORIO',
        score_no_momento=0.7,
    )
    assert pv.label_final == 'nao_lead'

    out = exportar_labels_retreino(
        output_path=tmp_output,
        incluir_csvs_legados=False,
        incluir_juriscope=False,
    )
    rows = _ler_csv(out)

    assert len(rows) == 1
    r = rows[0]
    assert int(r['label']) == 0
    assert 'final' in r['fonte']


# ─── Test 4: incerto/skip/precisa_enriquecer excluídos ───────────────────────

def test_excluir_incerto_skip_precisa_enriquecer(
    base_dir_vazio, tmp_output, trf1, user_alice, user_bob,
):
    procs = [
        Process.objects.create(tribunal=trf1, numero_cnj='0000010-10.2020.4.01.0000'),
        Process.objects.create(tribunal=trf1, numero_cnj='0000020-20.2020.4.01.0000'),
        Process.objects.create(tribunal=trf1, numero_cnj='0000030-30.2020.4.01.0000'),
        Process.objects.create(tribunal=trf1, numero_cnj='0000040-40.2020.4.01.0000'),
    ]
    # excluídos
    ProcessoValidacao.objects.create(
        processo=procs[0], usuario=user_alice,
        resultado=ProcessoValidacao.RESULTADO_INCERTO,
        versao_modelo='v6', classificacao_no_momento='NAO_LEAD', score_no_momento=0.1,
    )
    ProcessoValidacao.objects.create(
        processo=procs[1], usuario=user_alice,
        resultado=ProcessoValidacao.RESULTADO_SKIP,
        versao_modelo='v6', classificacao_no_momento='NAO_LEAD', score_no_momento=0.1,
    )
    ProcessoValidacao.objects.create(
        processo=procs[2], usuario=user_alice,
        resultado=ProcessoValidacao.RESULTADO_PRECISA_ENRIQUECER,
        versao_modelo='v6', classificacao_no_momento='NAO_LEAD', score_no_momento=0.1,
    )
    # incluído
    ProcessoValidacao.objects.create(
        processo=procs[3], usuario=user_alice,
        resultado=ProcessoValidacao.RESULTADO_EH_LEAD,
        versao_modelo='v6', classificacao_no_momento='PRECATORIO', score_no_momento=0.9,
    )

    out = exportar_labels_retreino(
        output_path=tmp_output,
        incluir_csvs_legados=False,
        incluir_juriscope=False,
    )
    rows = _ler_csv(out)

    assert len(rows) == 1
    assert rows[0]['cnj'] == '0000040-40.2020.4.01.0000'
    assert int(rows[0]['label']) == 1


# ─── Test 5: min_data filtra corretamente ────────────────────────────────────

def test_min_data_filtra_juriscope_e_humano(
    base_dir_vazio, tmp_output, trf1, cliente_juriscope, user_alice, user_bob,
):
    proc_velho = Process.objects.create(tribunal=trf1, numero_cnj='0000010-10.2020.4.01.0000')
    proc_novo = Process.objects.create(tribunal=trf1, numero_cnj='0000020-20.2020.4.01.0000')

    # LeadConsumption antigo
    lc_velho = LeadConsumption.objects.create(
        processo=proc_velho, cliente=cliente_juriscope,
        resultado=LeadConsumption.RESULTADO_VALIDADO,
    )
    LeadConsumption.objects.filter(pk=lc_velho.pk).update(
        consumido_em=timezone.now() - timedelta(days=365),
    )

    # LeadConsumption recente
    LeadConsumption.objects.create(
        processo=proc_novo, cliente=cliente_juriscope,
        resultado=LeadConsumption.RESULTADO_VALIDADO,
    )

    # ProcessoValidacao antiga
    pv_velha = ProcessoValidacao.objects.create(
        processo=proc_velho, usuario=user_alice,
        resultado=ProcessoValidacao.RESULTADO_NAO_LEAD,
        versao_modelo='v6', classificacao_no_momento='NAO_LEAD', score_no_momento=0.1,
    )
    ProcessoValidacao.objects.filter(pk=pv_velha.pk).update(
        criada_em=timezone.now() - timedelta(days=365),
    )

    # ProcessoValidacao nova
    ProcessoValidacao.objects.create(
        processo=proc_novo, usuario=user_bob,
        resultado=ProcessoValidacao.RESULTADO_EH_LEAD,
        versao_modelo='v6', classificacao_no_momento='PRECATORIO', score_no_momento=0.95,
    )

    cutoff = timezone.now() - timedelta(days=30)
    out = exportar_labels_retreino(
        output_path=tmp_output,
        min_data=cutoff,
        incluir_csvs_legados=False,
    )
    rows = _ler_csv(out)

    cnjs = {r['cnj'] for r in rows}
    assert cnjs == {'0000020-20.2020.4.01.0000'}  # só o recente


# ─── Test 6: Idempotência ────────────────────────────────────────────────────

def test_idempotencia_duas_execucoes_iguais(
    base_dir_vazio, tmp_path, trf1, user_alice, cliente_juriscope,
):
    proc = Process.objects.create(tribunal=trf1, numero_cnj=CNJ_A)
    proc2 = Process.objects.create(tribunal=trf1, numero_cnj=CNJ_B)

    LeadConsumption.objects.create(
        processo=proc, cliente=cliente_juriscope,
        resultado=LeadConsumption.RESULTADO_VALIDADO,
    )
    ProcessoValidacao.objects.create(
        processo=proc2, usuario=user_alice,
        resultado=ProcessoValidacao.RESULTADO_EH_PRECATORIO,
        versao_modelo='v6', classificacao_no_momento='PRECATORIO', score_no_momento=0.99,
    )
    # CSV legado também
    (base_dir_vazio / 'leads_trf1.csv').write_text(
        f'numero_processo\n{CNJ_A}\n{CNJ_B}\n', encoding='utf-8',
    )

    out1 = tmp_path / 'out1.csv'
    out2 = tmp_path / 'out2.csv'
    exportar_labels_retreino(output_path=out1)
    exportar_labels_retreino(output_path=out2)

    assert out1.read_text(encoding='utf-8') == out2.read_text(encoding='utf-8')


# ─── Test 7: CNJ não existe no DB → processo_id vazio ────────────────────────

def test_cnj_inexistente_no_db_tem_processo_id_vazio(base_dir_vazio, tmp_output):
    (base_dir_vazio / 'leads_trf1.csv').write_text(
        f'numero_processo\n{CNJ_DESCONHECIDO}\n', encoding='utf-8',
    )

    out = exportar_labels_retreino(
        output_path=tmp_output,
        incluir_humano=False, incluir_juriscope=False,
    )
    rows = _ler_csv(out)

    assert len(rows) == 1
    assert rows[0]['cnj'] == CNJ_DESCONHECIDO
    assert rows[0]['processo_id'] == ''


# ─── Test 8: estatisticas_labels ─────────────────────────────────────────────

def test_estatisticas_labels_csv_simples(
    base_dir_vazio, tmp_output, trf1, trf3, cliente_juriscope,
):
    """Mix de fontes com 1 conflito + 1 lead-only — sanity de stats."""
    proc1 = Process.objects.create(tribunal=trf1, numero_cnj=CNJ_A)
    proc3 = Process.objects.create(tribunal=trf3, numero_cnj=CNJ_C)

    # Juriscope: A negativo, C positivo
    LeadConsumption.objects.create(
        processo=proc1, cliente=cliente_juriscope,
        resultado=LeadConsumption.RESULTADO_SEM_EXPEDICAO,
    )
    LeadConsumption.objects.create(
        processo=proc3, cliente=cliente_juriscope,
        resultado=LeadConsumption.RESULTADO_VALIDADO,
    )
    # CSV legado: A positivo (conflito), B positivo (sem juriscope)
    (base_dir_vazio / 'leads_trf1.csv').write_text(
        f'numero_processo\n{CNJ_A}\n{CNJ_B}\n', encoding='utf-8',
    )

    out = exportar_labels_retreino(
        output_path=tmp_output,
        incluir_humano=False,
    )
    stats = estatisticas_labels(out)

    assert stats['total'] == 3                   # A, B, C
    # CNJ_A: juriscope negativo venceu (peso 2 > 1) → label=0
    # CNJ_B: csv-only → label=1
    # CNJ_C: juriscope positivo → label=1
    assert stats['label_counts'].get(0, 0) == 1
    assert stats['label_counts'].get(1, 0) == 2
    assert stats['conflitos'] == 1               # só CNJ_A
    assert 'TRF1' in stats['tribunais']
    assert 'TRF3' in stats['tribunais']
    assert stats['peso_medio'] > 0
    # CNJ_B não está no DB → 1 row sem processo_id
    assert stats['sem_processo_id'] >= 1
