"""Testes do módulo de amostragem estratificada (T7).

Cobre as 7 estratégias + `criar_lote`, plus:
- Exclusão de processos já em lotes ativos.
- Exclusão de processos anotados recentemente pelo mesmo usuário.
- Reprodutibilidade (mesma seed → mesmo conjunto).
- CSV inexistente → FileNotFoundError claro (não silencioso).
- criar_lote atomic (rollback total se algo falhar).
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model

from tribunals.models import (
    AmostraProcesso,
    AmostraValidacao,
    ClassificadorVersao,
    Process,
    ProcessoValidacao,
    Tribunal,
)
from tribunals import sampling

pytestmark = pytest.mark.django_db

User = get_user_model()


# ---------- fixtures ----------

@pytest.fixture
def trf1():
    t, _ = Tribunal.objects.get_or_create(
        sigla='TRF1', defaults={'nome': 'TRF1', 'sigla_djen': 'TRF1', 'ativo': True},
    )
    return t


@pytest.fixture
def trf3():
    t, _ = Tribunal.objects.get_or_create(
        sigla='TRF3', defaults={'nome': 'TRF3', 'sigla_djen': 'TRF3', 'ativo': True},
    )
    return t


@pytest.fixture
def user_alice(db):
    return User.objects.create_user(username='alice', password='x')


@pytest.fixture
def versao_ativa(db):
    # Garante exatamente 1 ativa (data migration 0026 já cria v6 ativa, mas
    # data migrations não rodam em pytest-django por default — daí o
    # get_or_create defensivo).
    ClassificadorVersao.objects.filter(ativa=True).update(ativa=False)
    versao, _ = ClassificadorVersao.objects.update_or_create(
        versao='v6',
        defaults={'pesos': {'_intercept_': 0.0}, 'ativa': True},
    )
    return versao


@pytest.fixture
def processos(trf1, trf3):
    """Cria 200 processos distribuídos por tribunal e classificação."""
    objs = []
    # TRF1: 100 procs, distribuição variada de score/classificacao
    for i in range(100):
        if i < 25:
            classificacao = Process.CLASSIF_PRECATORIO
            score = 0.85 + (i % 10) / 100
        elif i < 50:
            classificacao = Process.CLASSIF_PRE_PRECATORIO
            score = 0.50 + (i % 10) / 100
        elif i < 75:
            classificacao = Process.CLASSIF_DIREITO_CREDITORIO
            score = 0.30 + (i % 10) / 100
        else:
            classificacao = Process.CLASSIF_NAO_LEAD
            score = 0.05 + (i % 20) / 100  # alguns acima de 0.20 (mas todos <0.30)
        objs.append(Process(
            tribunal=trf1,
            numero_cnj=f'{i:07d}-00.2025.4.01.0001',
            classificacao=classificacao,
            classificacao_score=score,
            classificacao_versao='v6',
        ))
    # TRF3: 100 procs, distribuição similar
    for i in range(100):
        if i < 25:
            classificacao = Process.CLASSIF_PRECATORIO
            score = 0.80 + (i % 10) / 100
        elif i < 50:
            classificacao = Process.CLASSIF_PRE_PRECATORIO
            score = 0.45 + (i % 10) / 100
        elif i < 75:
            classificacao = Process.CLASSIF_DIREITO_CREDITORIO
            score = 0.30 + (i % 10) / 100
        else:
            classificacao = Process.CLASSIF_NAO_LEAD
            score = 0.10 + (i % 20) / 100
        objs.append(Process(
            tribunal=trf3,
            numero_cnj=f'{i:07d}-00.2025.4.03.0001',
            classificacao=classificacao,
            classificacao_score=score,
            classificacao_versao='v6',
        ))
    Process.objects.bulk_create(objs)
    return Process.objects.all()


@pytest.fixture
def csv_recuperados_tmp(tmp_path, processos, trf1):
    """CSV com header e CNJs reais do banco de teste."""
    path = tmp_path / 'recuperados.csv'
    cnjs = list(
        Process.objects.filter(tribunal=trf1).values_list('numero_cnj', flat=True)[:10]
    )
    with path.open('w', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(['numero_processo'])
        for c in cnjs:
            w.writerow([c])
    return path


# ---------- testes por função ----------

def test_sample_borderline_estratificado(processos, trf1, trf3):
    qs = sampling.sample_borderline(faixa=(0.30, 0.50), limit=40, seed=42)
    pks = list(qs.values_list('pk', flat=True))
    assert len(pks) > 0
    # Estratificou entre tribunais ativos.
    siglas = set(qs.values_list('tribunal_id', flat=True))
    assert siglas == {'TRF1', 'TRF3'}


def test_sample_borderline_tribunal_especifico(processos, trf1):
    qs = sampling.sample_borderline(faixa=(0.30, 0.50), tribunal=trf1, limit=20, seed=7)
    assert set(qs.values_list('tribunal_id', flat=True)) == {'TRF1'}
    for score in qs.values_list('classificacao_score', flat=True):
        assert 0.30 <= score < 0.50


def test_sample_n1_alto(processos, trf1):
    qs = sampling.sample_n1_alto(min_score=0.85, tribunal=trf1, limit=10, seed=1)
    rows = list(qs.values('classificacao', 'classificacao_score'))
    assert len(rows) > 0
    for r in rows:
        assert r['classificacao'] == Process.CLASSIF_PRECATORIO
        assert r['classificacao_score'] >= 0.85


def test_sample_nao_lead_top(processos, trf1):
    qs = sampling.sample_nao_lead_top(min_score=0.10, tribunal=trf1, limit=10, seed=1)
    scores = list(qs.values_list('classificacao_score', flat=True))
    classes = set(qs.values_list('classificacao', flat=True))
    assert classes == {Process.CLASSIF_NAO_LEAD}
    # Ordenado desc por score.
    assert scores == sorted(scores, reverse=True)


def test_sample_random_tribunal_estratifica_por_classe(processos, trf1):
    qs = sampling.sample_random_tribunal(tribunal=trf1, limit=40, seed=99)
    classes = set(qs.values_list('classificacao', flat=True))
    # Tem todas as 4 classes representadas (quota=10 cada, há ≥10 por classe).
    assert classes == {
        Process.CLASSIF_PRECATORIO,
        Process.CLASSIF_PRE_PRECATORIO,
        Process.CLASSIF_DIREITO_CREDITORIO,
        Process.CLASSIF_NAO_LEAD,
    }


def test_sample_random_tribunal_sem_tribunal_levanta(processos):
    with pytest.raises(ValueError, match='exige tribunal'):
        sampling.sample_random_tribunal(tribunal=None)


def test_sample_recuperados_csv_real(processos, trf1, csv_recuperados_tmp):
    qs = sampling.sample_recuperados(
        tribunal=trf1, limit=100, csv_path=csv_recuperados_tmp,
    )
    pks = list(qs.values_list('pk', flat=True))
    # Os 10 CNJs do CSV existem todos no tribunal TRF1.
    assert len(pks) == 10


def test_sample_falsos_consumidos_csv_real(processos, trf1, csv_recuperados_tmp):
    # Mesmo CSV, função análoga.
    qs = sampling.sample_falsos_consumidos(
        tribunal=trf1, limit=100, csv_path=csv_recuperados_tmp,
    )
    assert qs.count() == 10


def test_sample_fn_candidatos_csv(processos, trf1, tmp_path):
    """fn_candidatos lê CSV e filtra por NAO_LEAD + min_suspeita."""
    path = tmp_path / 'fn_candidatos_test.csv'
    cnjs_nao_lead = list(
        Process.objects.filter(
            tribunal=trf1, classificacao=Process.CLASSIF_NAO_LEAD,
        ).values_list('numero_cnj', flat=True)[:5]
    )
    with path.open('w', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(['numero_processo'])
        for c in cnjs_nao_lead:
            w.writerow([c])
    qs = sampling.sample_fn_candidatos(
        tribunal=trf1, limit=10, min_suspeita=0.10, csv_path=path,
    )
    assert qs.count() > 0
    for c in qs.values_list('classificacao', flat=True):
        assert c == Process.CLASSIF_NAO_LEAD


# ---------- exclusão de processos repetidos ----------

def test_exclui_processos_em_lote_ativo(processos, trf1, user_alice, versao_ativa):
    qs1 = sampling.sample_n1_alto(min_score=0.85, tribunal=trf1, limit=5, seed=1)
    lote = sampling.criar_lote(
        estrategia=AmostraValidacao.ESTRATEGIA_TOP_SCORE,
        queryset=qs1, criada_por=user_alice, tribunal=trf1,
        tamanho_alvo=5, seed=1,
    )
    assert lote.processos.count() == 5

    # Segundo sorteio na mesma faixa não deve trazer nenhum dos já-sorteados.
    qs2 = sampling.sample_n1_alto(min_score=0.85, tribunal=trf1, limit=50, seed=2)
    pks2 = set(qs2.values_list('pk', flat=True))
    ja_sorteados = set(lote.processos.values_list('pk', flat=True))
    assert ja_sorteados.isdisjoint(pks2)


def test_exclui_processos_anotados_recentemente_pelo_usuario(
    processos, trf1, user_alice
):
    # Anota um processo manualmente.
    p = Process.objects.filter(
        tribunal=trf1, classificacao=Process.CLASSIF_PRECATORIO,
        classificacao_score__gte=0.85,
    ).first()
    ProcessoValidacao.objects.create(
        processo=p, usuario=user_alice,
        resultado=ProcessoValidacao.RESULTADO_EH_LEAD,
        versao_modelo='v6', classificacao_no_momento='PRECATORIO',
        score_no_momento=p.classificacao_score,
    )
    qs = sampling.sample_n1_alto(
        min_score=0.85, tribunal=trf1, limit=50, seed=1, usuario=user_alice,
    )
    assert p.pk not in set(qs.values_list('pk', flat=True))


# ---------- reprodutibilidade ----------

def test_seed_reprodutivel(processos, trf1):
    qs_a = sampling.sample_borderline(
        faixa=(0.30, 0.50), tribunal=trf1, limit=10, seed=7777,
    )
    qs_b = sampling.sample_borderline(
        faixa=(0.30, 0.50), tribunal=trf1, limit=10, seed=7777,
    )
    pks_a = list(qs_a.values_list('pk', flat=True))
    pks_b = list(qs_b.values_list('pk', flat=True))
    assert pks_a == pks_b
    # E seeds diferentes → conjuntos diferentes (com altíssima prob).
    qs_c = sampling.sample_borderline(
        faixa=(0.30, 0.50), tribunal=trf1, limit=10, seed=8888,
    )
    pks_c = list(qs_c.values_list('pk', flat=True))
    assert pks_a != pks_c


# ---------- CSV inexistente ----------

def test_csv_inexistente_levanta_filenotfound(processos, trf1):
    with pytest.raises(FileNotFoundError):
        sampling.sample_recuperados(
            tribunal=trf1, csv_path='/tmp/nao_existe_xyz_999.csv',
        )


# ---------- criar_lote: atomic + snapshot ----------

def test_criar_lote_persiste_atomic(processos, trf1, user_alice, versao_ativa):
    qs = sampling.sample_n1_alto(min_score=0.85, tribunal=trf1, limit=5, seed=42)
    lote = sampling.criar_lote(
        estrategia=AmostraValidacao.ESTRATEGIA_TOP_SCORE,
        queryset=qs, criada_por=user_alice, tribunal=trf1,
        tamanho_alvo=5, seed=42, parametros={'min_score': 0.85},
    )
    assert lote.pk is not None
    assert lote.versao_modelo == 'v6'  # da fixture versao_ativa
    assert lote.seed == 42
    assert lote.parametros == {'min_score': 0.85}

    itens = list(lote.itens.order_by('ordem'))
    assert len(itens) == 5
    # Ordem 1..N determinística.
    assert [i.ordem for i in itens] == [1, 2, 3, 4, 5]
    # Snapshot do score e classificação no momento.
    for item in itens:
        assert item.score_no_sorteio >= 0.85
        assert item.classificacao_no_sorteio == Process.CLASSIF_PRECATORIO


def test_criar_lote_sem_versao_ativa_levanta(processos, trf1, user_alice):
    """Sem ClassificadorVersao.ativa=True, criar_lote falha cedo."""
    # Limpa qualquer ativa criada por data migration.
    ClassificadorVersao.objects.filter(ativa=True).update(ativa=False)
    qs = sampling.sample_n1_alto(min_score=0.85, tribunal=trf1, limit=5, seed=1)
    with pytest.raises(RuntimeError, match='ClassificadorVersao ativo'):
        sampling.criar_lote(
            estrategia=AmostraValidacao.ESTRATEGIA_TOP_SCORE,
            queryset=qs, criada_por=user_alice, tribunal=trf1,
            tamanho_alvo=5, seed=1,
        )
    # Nada persistiu.
    assert AmostraValidacao.objects.count() == 0
    assert AmostraProcesso.objects.count() == 0


def test_criar_lote_atomic_rollback_em_erro(
    processos, trf1, user_alice, versao_ativa, monkeypatch
):
    """Se bulk_create falhar, AmostraValidacao não deve persistir."""
    qs = sampling.sample_n1_alto(min_score=0.85, tribunal=trf1, limit=5, seed=1)

    def boom(*args, **kwargs):
        raise RuntimeError('simulated bulk failure')

    monkeypatch.setattr(AmostraProcesso.objects, 'bulk_create', boom)
    with pytest.raises(RuntimeError, match='simulated'):
        sampling.criar_lote(
            estrategia=AmostraValidacao.ESTRATEGIA_TOP_SCORE,
            queryset=qs, criada_por=user_alice, tribunal=trf1,
            tamanho_alvo=5, seed=1,
        )
    # Atomic rollback — nada persistiu.
    assert AmostraValidacao.objects.count() == 0
    assert AmostraProcesso.objects.count() == 0


def test_criar_lote_tamanho_alvo_invalido_levanta(processos, trf1, user_alice, versao_ativa):
    qs = sampling.sample_n1_alto(min_score=0.85, tribunal=trf1, limit=5, seed=1)
    with pytest.raises(ValueError, match='tamanho_alvo'):
        sampling.criar_lote(
            estrategia=AmostraValidacao.ESTRATEGIA_TOP_SCORE,
            queryset=qs, criada_por=user_alice, tribunal=trf1,
            tamanho_alvo=0, seed=1,
        )
