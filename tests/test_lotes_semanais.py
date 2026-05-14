"""Testes do pipeline semanal de lotes de validação (T21).

Cobertura:
- gerar_lotes_semanais_fn cria 1 lote por tribunal ativo (com mock de minerar_fn)
- 0 candidatos não cria lote
- Erro num tribunal não bloqueia os outros
- Notificação Slack (mock requests.post)
- Notificação Email (mock send_mail)
- --no-notificar não chama _notificar_lotes_semanais
- Scheduler registra job quando enabled
- Scheduler skip quando disabled
"""
from __future__ import annotations

import csv
from io import StringIO
from pathlib import Path
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core import mail
from django.core.management import call_command

from tribunals import jobs as tjobs
from tribunals.models import (
    AmostraValidacao,
    ClassificadorVersao,
    Process,
    Tribunal,
)

pytestmark = pytest.mark.django_db

User = get_user_model()


# ── fixtures ────────────────────────────────────────────────────────────────

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
def versao_ativa(db):
    ClassificadorVersao.objects.filter(ativa=True).update(ativa=False)
    v, _ = ClassificadorVersao.objects.update_or_create(
        versao='v6', defaults={'pesos': {'_intercept_': 0.0}, 'ativa': True},
    )
    return v


def _cnj(trib_sigla: str, i: int) -> str:
    seq = f'{i:07d}'
    if trib_sigla == 'TRF1':
        return f'{seq}-12.2025.4.01.3700'
    return f'{seq}-12.2025.4.03.6100'


def _criar_nao_leads(trib, n: int, *, offset: int = 0, score: float = 0.45):
    """Cria N Process NAO_LEAD pra `trib`. Retorna list de CNJs."""
    objs = []
    for i in range(n):
        objs.append(Process(
            tribunal=trib,
            numero_cnj=_cnj(trib.sigla, offset + i),
            classe_codigo='12078',
            classe_nome='Cumprimento de Sentença',
            classificacao=Process.CLASSIF_NAO_LEAD,
            classificacao_score=score,
            classificacao_versao='v6',
            total_movimentacoes=10,
        ))
    Process.objects.bulk_create(objs)
    return [o.numero_cnj for o in objs]


def _csv_com_cnjs(tmp_path: Path, cnjs: list[str], name: str = 'fn_candidatos_20260512.csv') -> Path:
    """Cria CSV minimal com header numero_processo e os CNJs."""
    p = tmp_path / name
    with p.open('w', newline='', encoding='utf-8') as fh:
        w = csv.writer(fh)
        w.writerow(['numero_processo', 'suspeita_score', 'motivos'])
        for c in cnjs:
            w.writerow([c, '0.50', 'E1'])
    return p


# ── Teste 1: cria 1 lote por tribunal ativo ────────────────────────────────

def test_gera_lote_por_tribunal_ativo(trf1, trf3, versao_ativa, tmp_path, monkeypatch):
    cnjs_t1 = _criar_nao_leads(trf1, 10, offset=1000)
    cnjs_t3 = _criar_nao_leads(trf3, 10, offset=2000)
    csv_t1 = _csv_com_cnjs(tmp_path, cnjs_t1, 'fn_candidatos_20260512_t1.csv')
    csv_t3 = _csv_com_cnjs(tmp_path, cnjs_t3, 'fn_candidatos_20260512_t3.csv')

    # Mock minerar_fn: não faz nada (CSV já está em tmp_path).
    def fake_call_command(name, *args, **kwargs):
        assert name == 'minerar_fn'

    # Mock _ultimo_csv_mining: retorna o CSV correto pra sigla baseada em --tribunal arg.
    csvs_por_trib = {'TRF1': csv_t1, 'TRF3': csv_t3}
    state = {'last_sigla': None}

    def fake_call_command_capture(name, *args, **kwargs):
        # args: ('--tribunal', 'TRF1', '--limit', '...')
        if '--tribunal' in args:
            idx = args.index('--tribunal')
            state['last_sigla'] = args[idx + 1]

    def fake_ultimo(sigla=None):
        return csvs_por_trib.get(state['last_sigla'])

    monkeypatch.setattr(
        'tribunals.jobs.call_command', fake_call_command_capture, raising=False,
    )
    # call_command é importado dentro da função; precisa interceptar via django.core.management
    monkeypatch.setattr(
        'django.core.management.call_command', fake_call_command_capture,
    )
    monkeypatch.setattr(tjobs, '_ultimo_csv_mining', fake_ultimo)

    resultados = tjobs.gerar_lotes_semanais_fn(
        tribunais=['TRF1', 'TRF3'],
        tamanho_por_tribunal=5,
        notificar=False,
    )

    assert set(resultados.keys()) == {'TRF1', 'TRF3'}
    assert resultados['TRF1']['lote_id'] is not None
    assert resultados['TRF3']['lote_id'] is not None
    assert resultados['TRF1']['count'] >= 1
    assert resultados['TRF3']['count'] >= 1

    # 2 AmostraValidacao criados
    lotes = AmostraValidacao.objects.filter(estrategia='fn_candidatos')
    assert lotes.count() == 2
    assert {l.tribunal_id for l in lotes} == {'TRF1', 'TRF3'}


# ── Teste 2: 0 candidatos não cria lote ────────────────────────────────────

def test_zero_candidatos_nao_cria_lote(trf1, versao_ativa, tmp_path, monkeypatch):
    # Cria NAO_LEAD mas CSV aponta pra CNJ inexistente.
    _criar_nao_leads(trf1, 5, offset=5000)
    csv_path = _csv_com_cnjs(tmp_path, ['9999999-99.9999.4.01.9999'])

    monkeypatch.setattr(
        'django.core.management.call_command', lambda *a, **k: None,
    )
    monkeypatch.setattr(tjobs, '_ultimo_csv_mining', lambda sigla=None: csv_path)

    resultados = tjobs.gerar_lotes_semanais_fn(
        tribunais=['TRF1'], tamanho_por_tribunal=5, notificar=False,
    )

    assert resultados['TRF1']['lote_id'] is None
    assert resultados['TRF1']['count'] == 0
    assert AmostraValidacao.objects.filter(estrategia='fn_candidatos').count() == 0


# ── Teste 3: erro num tribunal não bloqueia outros ─────────────────────────

def test_erro_um_tribunal_nao_bloqueia(trf1, trf3, versao_ativa, tmp_path, monkeypatch):
    cnjs_t3 = _criar_nao_leads(trf3, 5, offset=6000)
    csv_t3 = _csv_com_cnjs(tmp_path, cnjs_t3, 'fn_candidatos_t3.csv')

    state = {'last_sigla': None}

    def fake_call_command(name, *args, **kwargs):
        if '--tribunal' in args:
            idx = args.index('--tribunal')
            state['last_sigla'] = args[idx + 1]
        if state['last_sigla'] == 'TRF1':
            raise RuntimeError('falha proposital no TRF1')

    def fake_ultimo(sigla=None):
        if state['last_sigla'] == 'TRF3':
            return csv_t3
        return None

    monkeypatch.setattr('django.core.management.call_command', fake_call_command)
    monkeypatch.setattr(tjobs, '_ultimo_csv_mining', fake_ultimo)

    resultados = tjobs.gerar_lotes_semanais_fn(
        tribunais=['TRF1', 'TRF3'], tamanho_por_tribunal=5, notificar=False,
    )

    assert resultados['TRF1'].get('error') is True
    assert resultados['TRF1']['lote_id'] is None
    assert resultados['TRF3']['lote_id'] is not None
    assert resultados['TRF3']['count'] >= 1


# ── Teste 4: notificação Slack ─────────────────────────────────────────────

def test_notificacao_slack(settings):
    settings.SLACK_WEBHOOK_URL = 'https://slack.example/webhook'
    resultados = {
        'TRF1': {'lote_id': 1, 'count': 5, 'csv_path': '/tmp/x.csv'},
        'TRF3': {'lote_id': 2, 'count': 3, 'csv_path': '/tmp/y.csv'},
    }
    with mock.patch('requests.post') as mock_post:
        out = tjobs._notificar_lotes_semanais(resultados)
    assert out['slack'] is True
    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert args[0] == 'https://slack.example/webhook'
    payload = kwargs['json']
    assert '2 novos lotes' in payload['text']
    assert '8 processos' in payload['text']  # 5 + 3
    assert 'TRF1=5' in payload['text']
    assert 'TRF3=3' in payload['text']


# ── Teste 5: notificação Email ─────────────────────────────────────────────

def test_notificacao_email(settings, db):
    settings.SLACK_WEBHOOK_URL = ''
    settings.EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
    # Garante user com permission can_validate_lead
    perm = Permission.objects.get(codename='can_validate_lead')
    grupo, _ = Group.objects.get_or_create(name='validadores_leads_test')
    grupo.permissions.add(perm)
    u = User.objects.create_user(
        username='valid_u', email='valid@example.com', password='x',
    )
    u.groups.add(grupo)

    resultados = {'TRF1': {'lote_id': 1, 'count': 5}}
    mail.outbox = []
    out = tjobs._notificar_lotes_semanais(resultados)

    assert out['email'] >= 1
    assert len(mail.outbox) == 1
    msg = mail.outbox[0]
    assert 'valid@example.com' in msg.to
    assert '1 novos lotes' in msg.subject


# ── Teste 6: --no-notificar não chama _notificar_lotes_semanais ────────────

def test_no_notificar_skip(trf1, versao_ativa, tmp_path, monkeypatch):
    cnjs = _criar_nao_leads(trf1, 5, offset=7000)
    csv_path = _csv_com_cnjs(tmp_path, cnjs)

    monkeypatch.setattr('django.core.management.call_command', lambda *a, **k: None)
    monkeypatch.setattr(tjobs, '_ultimo_csv_mining', lambda sigla=None: csv_path)

    with mock.patch.object(tjobs, '_notificar_lotes_semanais') as mock_notif:
        tjobs.gerar_lotes_semanais_fn(
            tribunais=['TRF1'], tamanho_por_tribunal=3, notificar=False,
        )
    mock_notif.assert_not_called()


def test_notificar_true_chama_helper(trf1, versao_ativa, tmp_path, monkeypatch):
    cnjs = _criar_nao_leads(trf1, 5, offset=7500)
    csv_path = _csv_com_cnjs(tmp_path, cnjs)

    monkeypatch.setattr('django.core.management.call_command', lambda *a, **k: None)
    monkeypatch.setattr(tjobs, '_ultimo_csv_mining', lambda sigla=None: csv_path)

    with mock.patch.object(tjobs, '_notificar_lotes_semanais', return_value={}) as mock_notif:
        tjobs.gerar_lotes_semanais_fn(
            tribunais=['TRF1'], tamanho_por_tribunal=3, notificar=True,
        )
    mock_notif.assert_called_once()


# ── Teste 7+8: scheduler registra/skipa job conforme setting ───────────────

def test_scheduler_registra_quando_enabled(settings, db, trf1):
    settings.VALIDACAO_LOTES_SEMANAIS_ENABLED = True
    from djen.scheduler import create_scheduler
    sched = create_scheduler()
    try:
        job = sched.get_job('gerar_lotes_semanais_fn')
        assert job is not None
    finally:
        sched.shutdown(wait=False) if sched.running else None


def test_scheduler_skip_quando_disabled(settings, db, trf1):
    settings.VALIDACAO_LOTES_SEMANAIS_ENABLED = False
    from djen.scheduler import create_scheduler
    sched = create_scheduler()
    try:
        job = sched.get_job('gerar_lotes_semanais_fn')
        assert job is None
    finally:
        sched.shutdown(wait=False) if sched.running else None


# ── Teste 9: management command --sync executa inline ──────────────────────

def test_management_command_sync(trf1, versao_ativa, tmp_path, monkeypatch):
    cnjs = _criar_nao_leads(trf1, 5, offset=8000)
    csv_path = _csv_com_cnjs(tmp_path, cnjs)

    monkeypatch.setattr('django.core.management.call_command', lambda *a, **k: None)
    monkeypatch.setattr(tjobs, '_ultimo_csv_mining', lambda sigla=None: csv_path)

    out = StringIO()
    call_command(
        'gerar_lotes_semanais_fn',
        '--tribunais', 'TRF1',
        '--tamanho', '3',
        '--no-notificar',
        '--sync',
        stdout=out,
    )
    output = out.getvalue()
    assert 'Resultados' in output
    assert 'TRF1' in output
