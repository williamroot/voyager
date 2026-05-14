"""Testes do management command `gerar_lote_validacao` (T9)."""
from __future__ import annotations

import csv
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command

from tribunals.models import (
    AmostraValidacao,
    ClassificadorVersao,
    Process,
    Tribunal,
)

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
def versao_ativa(db):
    ClassificadorVersao.objects.filter(ativa=True).update(ativa=False)
    v, _ = ClassificadorVersao.objects.update_or_create(
        versao='v6', defaults={'pesos': {'_intercept_': 0.0}, 'ativa': True},
    )
    return v


@pytest.fixture
def operador(db):
    return User.objects.create_user(username='operador', password='x')


@pytest.fixture
def processos(trf1):
    """Cria 60 processos distribuídos em classes/scores."""
    objs = []
    for i in range(60):
        if i < 15:
            cl = Process.CLASSIF_PRECATORIO
            score = 0.85 + (i % 10) / 100
        elif i < 30:
            cl = Process.CLASSIF_PRE_PRECATORIO
            score = 0.50 + (i % 10) / 100
        elif i < 45:
            cl = Process.CLASSIF_DIREITO_CREDITORIO
            score = 0.30 + (i % 10) / 100
        else:
            cl = Process.CLASSIF_NAO_LEAD
            score = 0.10 + (i % 15) / 100
        objs.append(Process(
            tribunal=trf1, numero_cnj=f'{i:07d}-99.2025.4.01.3700',
            classificacao=cl, classificacao_score=score, classificacao_versao='v6',
        ))
    Process.objects.bulk_create(objs)
    return Process.objects.filter(tribunal=trf1)


@pytest.fixture
def csv_recuperados(tmp_path, processos, trf1):
    path = tmp_path / 'recup.csv'
    cnjs = list(
        Process.objects.filter(tribunal=trf1).values_list('numero_cnj', flat=True)[:5]
    )
    with path.open('w', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(['numero_processo'])
        for c in cnjs:
            w.writerow([c])
    return str(path)


@pytest.fixture
def csv_fn(tmp_path, processos, trf1):
    path = tmp_path / 'fn.csv'
    cnjs = list(
        Process.objects.filter(
            tribunal=trf1, classificacao=Process.CLASSIF_NAO_LEAD,
        ).values_list('numero_cnj', flat=True)[:5]
    )
    with path.open('w', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(['numero_processo'])
        for c in cnjs:
            w.writerow([c])
    return str(path)


# ---------- estratégias com --dry-run ----------

def _run(estrategia, **kw):
    out = StringIO()
    call_command('gerar_lote_validacao', estrategia=estrategia, stdout=out, **kw)
    return out.getvalue()


def test_dry_run_top_score(processos, trf1, operador):
    out = _run('top_score', tribunal='TRF1', tamanho=5, dry_run=True, usuario='operador')
    assert '[dry-run]' in out
    assert AmostraValidacao.objects.count() == 0


def test_dry_run_borderline(processos, trf1, operador):
    out = _run('borderline', tribunal='TRF1', tamanho=5, dry_run=True, usuario='operador',
               faixa_borderline='0.30,0.70')
    assert '[dry-run]' in out


def test_dry_run_low_score(processos, trf1, operador):
    out = _run('low_score', tribunal='TRF1', tamanho=5, dry_run=True, usuario='operador')
    assert '[dry-run]' in out


def test_dry_run_on_demand(processos, trf1, operador):
    out = _run('on_demand', tribunal='TRF1', tamanho=5, dry_run=True, usuario='operador')
    assert '[dry-run]' in out


def test_dry_run_recuperados(processos, trf1, operador, csv_recuperados):
    out = _run('recuperados', tribunal='TRF1', tamanho=5, dry_run=True,
               usuario='operador', csv_input=csv_recuperados)
    assert '[dry-run]' in out


def test_dry_run_falsos_consumidos(processos, trf1, operador, csv_recuperados):
    out = _run('falsos_consumidos', tribunal='TRF1', tamanho=5, dry_run=True,
               usuario='operador', csv_input=csv_recuperados)
    assert '[dry-run]' in out


def test_dry_run_fn_candidatos(processos, trf1, operador, csv_fn):
    out = _run('fn_candidatos', tribunal='TRF1', tamanho=5, dry_run=True,
               usuario='operador', csv_input=csv_fn)
    assert '[dry-run]' in out


# ---------- persistência ----------

def test_persiste_top_score(processos, trf1, operador, versao_ativa):
    out = _run('top_score', tribunal='TRF1', tamanho=5, usuario='operador', seed=42)
    assert 'Lote' in out
    lote = AmostraValidacao.objects.first()
    assert lote is not None
    assert lote.estrategia == 'top_score'
    assert lote.tamanho_alvo == 5
    assert lote.tribunal_id == 'TRF1'
    assert lote.itens.count() == 5


# ---------- erro: usuário inexistente ----------

def test_usuario_inexistente_sem_flag_erra(processos, trf1, versao_ativa):
    with pytest.raises(CommandError, match='não existe'):
        _run('top_score', tribunal='TRF1', tamanho=5, usuario='naoexiste')


def test_system_user_criado_com_flag(processos, trf1, versao_ativa):
    out = _run('top_score', tribunal='TRF1', tamanho=5, usuario='system',
               allow_system_user=True)
    assert 'Lote' in out
    u = User.objects.get(username='system')
    assert u.is_active is False


# ---------- erro: estratégia inválida ----------

def test_estrategia_invalida(operador):
    # argparse choices bloqueia antes — erro tipo CommandError
    with pytest.raises((CommandError, SystemExit)):
        _run('XPTO', tribunal='TRF1', tamanho=5, usuario='operador')


# ---------- faixa-borderline custom ----------

def test_faixa_borderline_custom(processos, trf1, operador, versao_ativa):
    out = _run('borderline', tribunal='TRF1', tamanho=5,
               usuario='operador', faixa_borderline='0.40,0.55',
               seed=1)
    # Não falha (faixa parseada corretamente).
    assert ('Lote' in out) or ('[dry-run]' in out)


def test_faixa_borderline_invalida(processos, trf1, operador):
    with pytest.raises(CommandError, match='faixa-borderline'):
        _run('borderline', tribunal='TRF1', tamanho=5,
             usuario='operador', faixa_borderline='abc')


# ---------- tribunal inexistente ----------

def test_tribunal_inexistente(operador):
    with pytest.raises(CommandError, match='não existe'):
        _run('top_score', tribunal='TJZZ', tamanho=5, usuario='operador')
