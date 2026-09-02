"""O freio do `backfill_fase` e o laço de reinício que ele causou (#119).

O QUE ACONTECEU, MEDIDO EM 02/09/2026
-------------------------------------
O shard `r105_fase_3` (pk 77,22 M → 79,80 M) rodou **17 reinícios** em poucas
horas com progresso líquido ZERO:

    sobe → varre ~6 blocos → "PAROU POR CUSTO: 5.325 ms/linha" → exit 0
         → `restart: unless-stopped` ressuscita → `--sem-checkpoint` volta ao
           `--de` → sobe → varre os MESMOS ~6 blocos → …

Uma amostra provou que aquela faixa já estava **100% preenchida**. Os blocos
devolviam 1 a 11 linhas residuais e levavam 3 a 17 s de relógio — baratos. Mas
o freio dividia o tempo do bloco pelas linhas ENCONTRADAS, então 16,8 s ÷ 1
linha virava "16.808 ms/linha" e o comando concluía que estava caríssimo.

Três defeitos somados, e cada teste aqui cerca um:

 1. **a métrica** — custo é por id VARRIDO (o que o banco lê), não por linha
    escrita (o que o `WHERE` deixou passar). Numa faixa densa os dois números
    coincidem; numa faixa já feita, só o segundo mente;
 2. **o checkpoint** — era UMA chave global, então os 4 shards se
    sobrescreviam e todos rodavam `--sem-checkpoint`. Reinício voltava ao
    `--de` e jogava fora horas de varredura;
 3. **o código de saída** — parar no teto saía com **0**, e `docker ps -a`
    carimbava `Exited (0)` tanto em "varreu a faixa inteira" quanto em
    "desisti na primeira curva".
"""
from datetime import UTC, datetime
from io import StringIO

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError

from tribunals.management.commands import backfill_fase as M

BLOCO = 20_000


def _relogio(monkeypatch, segundos_por_bloco: float):
    """`time.monotonic` controlado: cada leitura avança o relógio.

    Sem isto o teste mediria microssegundos reais e NENHUM limiar dispararia —
    o teste passaria por vacuidade, que é a forma mais educada de não testar
    nada.
    """
    estado = {'t': 0.0}

    def falso():
        agora = estado['t']
        estado['t'] += segundos_por_bloco / 2.0    # duas leituras por bloco
        return agora

    monkeypatch.setattr(M.time, 'monotonic', falso)
    monkeypatch.setattr(M.time, 'sleep', lambda *_: None)


def _semeia(n_blocos: int, sigla='TRF3'):
    """Um processo COM publicação classificável no começo de cada bloco.

    É o retrato da faixa rala: 1 linha em 20.000 ids varridos.
    """
    from tribunals.models import Movimentacao, Process, Tribunal
    trib, _ = Tribunal.objects.get_or_create(
        sigla=sigla, defaults={'nome': sigla, 'sigla_djen': sigla})
    pks = []
    for i in range(n_blocos):
        pk = i * BLOCO + 1
        p = Process.objects.create(
            id=pk, tribunal=trib,
            numero_cnj=f'{pk:07d}-11.2024.4.03.6100')
        Movimentacao.objects.create(
            processo=p, tribunal=trib, external_id=f'pub{pk}', meio='D',
            data_disponibilizacao=datetime(2026, 2, 1, tzinfo=UTC),
            codigo_classe='12078', nome_classe='Cumprimento')
        pks.append(pk)
    return pks


# --------------------------------------------------------------------------- #
# 1. A métrica: por id varrido, não por linha encontrada
# --------------------------------------------------------------------------- #
@pytest.mark.django_db(transaction=True)
def test_faixa_rala_e_barata_nao_dispara_o_freio(monkeypatch):
    """8 blocos de 20.000 ids, 1 linha cada, 0,4 s por bloco.

    0,4 s ÷ 20.000 ids = 0,02 ms/id — barato por qualquer régua honesta.
    0,4 s ÷ 1 linha = 400 ms/linha — 20× o teto, pela régua que mentia.

    MUTAÇÃO que este teste pega: voltar `ms_por_id` para `dt_ms / n` faz o
    comando parar no 5º bloco e o `custo_caro` virar True — que é, linha por
    linha, o laço de reinício do `r105_fase_3`.
    """
    cache.clear()
    _semeia(8)
    _relogio(monkeypatch, segundos_por_bloco=0.4)
    saida = StringIO()

    call_command('backfill_fase', de=0, ate=8 * BLOCO, bloco=BLOCO,
                 sem_checkpoint=True, sleep=0, freio_ms_linha=12,
                 parar_ms_linha=20, json=True, stdout=saida)

    import json as _j
    r = _j.loads(saida.getvalue())
    assert r['custo_caro'] is False, (
        'o freio parou numa faixa BARATA — é o defeito do r105_fase_3')
    assert r['pk_parada'] == 8 * BLOCO, 'não varreu a faixa inteira'
    assert r['escritos'] == 8


@pytest.mark.django_db(transaction=True)
def test_faixa_realmente_cara_ainda_para(monkeypatch):
    """O freio não pode ser desligado: só medido direito.

    20.000 ids em 500 s = 25 ms/id, acima do teto de 20. Aqui parar é certo, e
    parar é ERRO com o número real.
    """
    cache.clear()
    _semeia(8)
    _relogio(monkeypatch, segundos_por_bloco=500.0)

    with pytest.raises(CommandError) as exc:
        call_command('backfill_fase', de=0, ate=8 * BLOCO, bloco=BLOCO,
                     sem_checkpoint=True, sleep=0, freio_ms_linha=12,
                     parar_ms_linha=20, stdout=StringIO())
    assert 'CUSTO' in str(exc.value)
    assert 'FALTA de' in str(exc.value), 'parou sem dizer o que ficou de fora'


@pytest.mark.django_db(transaction=True)
def test_bloco_vazio_tambem_e_medido(monkeypatch):
    """Bloco que não devolve linha nenhuma custa I/O igual.

    O código antigo só media dentro de `if n:` — uma faixa 100% vazia e
    lentíssima passava batido, e o freio nunca via a lentidão que importava.
    """
    cache.clear()
    _semeia(1)                       # só o primeiro bloco tem linha
    _relogio(monkeypatch, segundos_por_bloco=500.0)

    with pytest.raises(CommandError) as exc:
        call_command('backfill_fase', de=0, ate=8 * BLOCO, bloco=BLOCO,
                     sem_checkpoint=True, sleep=0, parar_ms_linha=20,
                     stdout=StringIO())
    assert 'CUSTO' in str(exc.value)


# --------------------------------------------------------------------------- #
# 2. Checkpoint por faixa
# --------------------------------------------------------------------------- #
def test_checkpoint_e_por_faixa_shards_nao_se_sobrescrevem():
    """4 shards, 4 chaves. Com uma chave só, o shard 4 gravava por cima do 1 e
    a retomada mandava todos para o mesmo lugar errado."""
    a = M.wm_key(11_960_000, 26_600_000)
    b = M.wm_key(44_420_000, 53_200_000)
    assert a != b
    assert a.startswith(M.WM) and str(11_960_000) in a


@pytest.mark.django_db(transaction=True)
def test_reinicio_retoma_do_checkpoint_nao_do_de(monkeypatch):
    """O que custou 9 h ao `r105_fase_1` quando ele levou lock timeout às
    01:13 e voltou do começo da faixa."""
    cache.clear()
    _semeia(4)
    _relogio(monkeypatch, segundos_por_bloco=0.1)
    saida = StringIO()

    # 1ª passada: para no teto de blocos, deixando checkpoint
    call_command('backfill_fase', de=0, ate=4 * BLOCO, bloco=BLOCO,
                 limite_blocos=2, sleep=0, json=True, stdout=saida)
    assert cache.get(M.wm_key(0, 4 * BLOCO)) == 2 * BLOCO

    # 2ª passada: MESMOS argumentos, tem que retomar em 2*BLOCO
    saida2 = StringIO()
    call_command('backfill_fase', de=0, ate=4 * BLOCO, bloco=BLOCO,
                 sleep=0, json=True, stdout=saida2)
    import json as _j
    r = _j.loads(saida2.getvalue())
    assert r['blocos'] == 2, 'refez blocos que o checkpoint já dava por feitos'


@pytest.mark.django_db(transaction=True)
def test_checkpoint_nunca_empurra_para_fora_da_faixa(monkeypatch):
    """Checkpoint de outro shard (ou lixo antigo) não pode mover o `--de`."""
    cache.clear()
    _semeia(2)
    _relogio(monkeypatch, segundos_por_bloco=0.1)
    cache.set(M.wm_key(0, 2 * BLOCO), 999_999_999, None)   # muito além do topo
    saida = StringIO()
    call_command('backfill_fase', de=0, ate=2 * BLOCO, bloco=BLOCO,
                 sleep=0, json=True, stdout=saida)
    import json as _j
    assert _j.loads(saida.getvalue())['blocos'] == 2, (
        'um checkpoint fora da faixa pulou trabalho — perda silenciosa')


# --------------------------------------------------------------------------- #
# 3. Código de saída
# --------------------------------------------------------------------------- #
@pytest.mark.django_db(transaction=True)
def test_teto_de_linhas_sai_com_erro_nao_com_zero(monkeypatch):
    """`Exited (0)` para "desisti no meio" é indistinguível de "terminei"."""
    cache.clear()
    _semeia(8)
    _relogio(monkeypatch, segundos_por_bloco=0.1)
    with pytest.raises(CommandError) as exc:
        call_command('backfill_fase', de=0, ate=8 * BLOCO, bloco=BLOCO,
                     sem_checkpoint=True, sleep=0, teto_linhas=2,
                     stdout=StringIO())
    assert 'TETO' in str(exc.value) and 'FALTA de' in str(exc.value)
    assert getattr(exc.value, 'returncode', 1) == 3


@pytest.mark.django_db(transaction=True)
def test_faixa_completa_sai_com_zero(monkeypatch):
    """O contrapositivo: terminar de verdade continua sendo exit 0."""
    cache.clear()
    _semeia(3)
    _relogio(monkeypatch, segundos_por_bloco=0.1)
    call_command('backfill_fase', de=0, ate=3 * BLOCO, bloco=BLOCO,
                 sem_checkpoint=True, sleep=0, stdout=StringIO())
