"""Vereditos velhos voltam ao classificador.

Medido em 13/10/2026: 16.966 processos do TRF3 que são Cumprimento só pela
`fase_codigo` (movimento no último ano) seguiam NAO_LEAD, classificados antes
do fix do F1 pela fase (03–04/09/2026). O cron `reclassificar_por_prioridade`
só dava vez ao grupo 2 (vereditos mais antigos) quando o grupo 1 esvaziava — e
ele nunca esvazia. Dois consertos, dois contratos:

1. o grupo 2 tem fatia GARANTIDA em toda passada;
2. o comando `reclassificar_classificados_antes` pega o estoque de uma vez,
   e o `--dry-run` comprovadamente não enfileira nada (PATTERNS.md).
"""
from datetime import UTC, datetime, timedelta

import pytest
from django.core.management import call_command

from tribunals import jobs as tjobs
from tribunals.management.commands import reclassificar_classificados_antes as cmd
from tribunals.models import Process, Tribunal

ANTES_DO_FIX = datetime(2026, 9, 1, tzinfo=UTC)
DEPOIS_DO_FIX = datetime(2026, 9, 10, tzinfo=UTC)


@pytest.fixture
def tribs(db):
    out = {}
    for s in ('TRF3', 'TRF1'):
        out[s], _ = Tribunal.objects.get_or_create(
            sigla=s, defaults={'nome': s, 'sigla_djen': s})
    return out


def _mk(t, n, *, fase='12078', classe='436', classif=Process.CLASSIF_NAO_LEAD,
        classif_em=ANTES_DO_FIX, mov_dias=30):
    return Process.objects.create(
        tribunal=t, numero_cnj=f'{n:07d}-00.2024.4.03.6100',
        fase_codigo=fase, classe_codigo=classe, classificacao=classif,
        classificacao_em=classif_em,
        ultima_movimentacao_em=datetime.now(UTC) - timedelta(days=mov_dias))


@pytest.fixture
def cenario(tribs):
    t = tribs['TRF3']
    alvo = [
        _mk(t, 1),                              # Cumprimento pela fase, legado 436
        _mk(t, 2, classe=''),                   # legado vazio
        _mk(t, 3, classe='7', fase='156'),      # Cumprimento genérico pela fase
    ]
    fora = [
        _mk(t, 10, classe='12078'),             # já era Cumprimento pelo legado
        _mk(t, 11, fase='7'),                   # fase não é Cumprimento
        _mk(t, 12, classif_em=DEPOIS_DO_FIX),   # já classificado depois do fix
        _mk(t, 13, classif=Process.CLASSIF_PRE_PRECATORIO),   # já é lead
        _mk(t, 14, mov_dias=400),               # sem movimento no último ano
        _mk(tribs['TRF1'], 15),                 # outro tribunal
    ]
    return alvo, fora


class FilaFalsa:
    def __init__(self):
        self.lotes = []

    def __len__(self):
        return 0

    def enqueue(self, fn, ids, **kw):
        assert fn is tjobs.reclassificar_batch
        self.lotes.append(list(ids))


@pytest.fixture
def fila(monkeypatch):
    f = FilaFalsa()
    monkeypatch.setattr(cmd.django_rq, 'get_queue', lambda nome: f)
    return f


@pytest.mark.django_db
def test_recorte_default_pega_so_cumprimento_pela_fase_antes_do_fix(cenario):
    alvo, _ = cenario
    qs = cmd.montar_queryset('TRF3', datetime(2026, 9, 4, tzinfo=UTC),
                             Process.CLASSIF_NAO_LEAD, True, 365)
    assert set(qs.values_list('id', flat=True)) == {p.pk for p in alvo}


@pytest.mark.django_db
def test_dry_run_conta_e_nao_enfileira(cenario, fila, capsys):
    call_command('reclassificar_classificados_antes', '--dry-run', '--pausa', '0')
    saida = capsys.readouterr().out
    # controle POSITIVO: se não contou nada, o dry-run virou no-op e o teste mente
    assert 'alvo: 3 processos' in saida, saida
    assert fila.lotes == [], 'o --dry-run enfileirou'


@pytest.mark.django_db
def test_apply_enfileira_os_mesmos_que_o_dry_run_contou(cenario, fila, capsys):
    alvo, _ = cenario
    call_command('reclassificar_classificados_antes', '--apply', '--pausa', '0',
                 '--batch-size', '2')
    assert len(fila.lotes) == 2
    assert {pid for lote in fila.lotes for pid in lote} == {p.pk for p in alvo}


@pytest.mark.django_db
def test_mov_desde_zero_tira_o_recorte_de_movimento(cenario, fila):
    call_command('reclassificar_classificados_antes', '--apply', '--pausa', '0',
                 '--mov-desde-dias', '0')
    assert sum(len(lote) for lote in fila.lotes) == 4   # + o sem movimento recente


@pytest.mark.django_db
def test_exige_um_modo(cenario):
    from django.core.management.base import CommandError
    with pytest.raises(CommandError):
        call_command('reclassificar_classificados_antes')


# ── grupo 2 com fatia garantida no cron ────────────────────────────────────

@pytest.fixture
def enfileirados(monkeypatch):
    lotes = []
    monkeypatch.setattr(tjobs.reclassificar_batch, 'delay',
                        lambda ids: lotes.append(list(ids)))
    return lotes


@pytest.mark.django_db
def test_grupo_2_tem_fatia_mesmo_com_grupo_1_cheio(tribs, enfileirados):
    t = tribs['TRF3']
    # grupo 1: 10 desatualizados (movimento depois do veredito)
    for i in range(10):
        _mk(t, 100 + i, classif_em=datetime.now(UTC) - timedelta(days=5), mov_dias=1)
    # grupo 2: vereditos velhos, sem movimento novo
    velhos = [_mk(t, 200 + i, classif_em=ANTES_DO_FIX - timedelta(days=i),
                  mov_dias=90) for i in range(5)]

    r = tjobs.reclassificar_por_prioridade(cap=10, batch_size=100,
                                           fatia_mais_antigos=0.2)

    ids = [pid for lote in enfileirados for pid in lote]
    assert r['desatualizados'] == 8 and r['mais_antigos'] == 2
    # os 2 vereditos MAIS antigos entram, mesmo com o grupo 1 tendo 10 na fila
    assert set(ids) & {p.pk for p in velhos} == {velhos[4].pk, velhos[3].pk}


@pytest.mark.django_db
def test_grupo_2_leva_o_que_o_grupo_1_nao_usou(tribs, enfileirados):
    t = tribs['TRF3']
    _mk(t, 100, classif_em=datetime.now(UTC) - timedelta(days=60), mov_dias=1)
    for i in range(5):
        _mk(t, 200 + i, classif_em=ANTES_DO_FIX - timedelta(days=i), mov_dias=90)

    r = tjobs.reclassificar_por_prioridade(cap=10, batch_size=100,
                                           fatia_mais_antigos=0.2)

    assert r['desatualizados'] == 1
    assert r['mais_antigos'] == 5


@pytest.mark.django_db
def test_fatia_zero_volta_ao_comportamento_antigo(tribs, enfileirados):
    t = tribs['TRF3']
    for i in range(3):
        _mk(t, 100 + i, classif_em=datetime.now(UTC) - timedelta(days=60), mov_dias=1)
    _mk(t, 200, mov_dias=90)

    r = tjobs.reclassificar_por_prioridade(cap=3, batch_size=100,
                                           fatia_mais_antigos=0)

    assert r['desatualizados'] == 3 and r['mais_antigos'] == 0
