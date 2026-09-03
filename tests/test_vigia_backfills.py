"""Vigia dos backfills longos (#119) — cada teste cerca um jeito de mentir.

O risco desta peça não é dar erro: é dizer "tudo bem" enquanto um backfill de
659 milhões de documentos está morto, ou dizer "parado" para um que está a todo
vapor e fazer alguém religar por cima. Os dois enganos custam caro, e o segundo
custa mais — em 02/09/2026 a tarefa do ES que ninguém via estava estrangulada a
500 docs/s justamente porque a soma dela com a tela de estado dava 503.

Cada teste abaixo cerca uma dessas mentiras:

  - container fora do ar com `update_by_query` VIVO não é "parado";
  - erro ao ler as tarefas do ES ABSTÉM, nunca conclui "parado";
  - pendente parado de verdade sai como ERRO com o número real;
  - reta final não acende alarme (alarme que acende sempre é alarme gasto);
  - `proc_digits` vazio/malformado derruba a fé no `_count` (`exists` mente);
  - doc com a CHAVE presente e valor `null` conta como AUSENTE;
  - medição que falha não apaga as outras duas do retrato;
  - FK: uma por vez, nunca duas (VALIDATE conflita consigo mesmo);
  - o warm da tela de estado escreve a MESMA chave que a view lê.
"""
import datetime
import logging

import pytest
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone

from tribunals import vigia_backfills as V

CACHE_LOCAL = override_settings(CACHES={'default': {
    'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
    'LOCATION': 'test-vigia-backfills'}})


@pytest.fixture(autouse=True)
def _propaga_para_o_caplog():
    """`settings.LOGGING` põe `propagate: False` no logger `voyager` (evita
    saída duplicada em produção), e o `caplog` do pytest consome pelo root —
    sem isto, TODA asserção sobre alarme passaria por vacuidade. Mesmo padrão
    de `tests/test_classificador_reload.py`."""
    lg = logging.getLogger('voyager')
    antes, lg.propagate = lg.propagate, True
    yield
    lg.propagate = antes


# --------------------------------------------------------------------------- #
# Dublê do ES. Entende só as formas que o módulo produz — de propósito.
# --------------------------------------------------------------------------- #
class FakeES:
    def __init__(self, docs=None, tarefas=None, erro_tasks=False):
        self.docs = docs or []
        self._tarefas = tarefas if tarefas is not None else {'nodes': {}}
        self.erro_tasks = erro_tasks
        self.tasks = self

    # -- count -------------------------------------------------------------
    def count(self, index=None, query=None, request_timeout=None):
        return {'count': len(self._filtra(query))}

    def _filtra(self, q):
        b = (q or {}).get('bool')
        if b is None:
            return list(self.docs)
        saida = []
        for d in self.docs:
            ok = True
            for cl in b.get('must_not', []):
                if 'exists' in cl and d.get(cl['exists']['field']) is not None:
                    ok = False
                if 'range' in cl:
                    ok = False        # fora-da-janela: nossos docs estão dentro
            for cl in b.get('filter', []):
                if 'exists' in cl and d.get(cl['exists']['field']) is None:
                    ok = False
            if ok:
                saida.append(d)
        return saida

    # -- search (amostra) ---------------------------------------------------
    def search(self, index=None, size=None, request_timeout=None,
               source_includes=None, query=None):
        return {'hits': {'hits': [{'_source': d} for d in self.docs[:size]]}}

    # -- tasks --------------------------------------------------------------
    def list(self, actions=None, detailed=None, request_timeout=None):
        if self.erro_tasks:
            raise RuntimeError('ES fora')
        return self._tarefas


def doc(i, digits='sentinela'):
    proc = f'{i:07d}-11.2024.8.26.0100'
    d = {'proc': proc}
    if digits == 'sentinela':
        d['proc_digits'] = ''.join(c for c in proc if c.isdigit())
    elif digits is not None:
        d['proc_digits'] = digits
    return d


def tarefa_viva(updated=13_572_000, total=27_888_685, rps=500.0):
    """Retrato do que o ES devolve de verdade num `slices=8`.

    ⚠️ O PAI vem com `updated=0, total=0, rps=0` — medido em produção em
    02/09/2026. Quem só lê o pai publica `None%` e `0.0 d/s`. Os números estão
    nas 8 filhas, e é somando elas que se chega aos 13,5 M de 27,9 M.
    """
    tasks = {'ABC:305727375': {
        'action': 'indices:data/write/update/byquery',
        'running_time_in_nanos': 64 * 60 * 1_000_000_000,
        'status': {'updated': 0, 'total': 0, 'requests_per_second': 0.0},
    }}
    for i in range(8):
        tasks[f'ABC:30572738{i}'] = {
            'action': 'indices:data/write/update/byquery',
            'parent_task_id': 'ABC:305727375',
            'running_time_in_nanos': 64 * 60 * 1_000_000_000,
            'status': {'updated': updated // 8, 'total': total // 8,
                       'requests_per_second': rps / 8},
        }
    return {'nodes': {'n1': {'tasks': tasks}}}


@pytest.fixture
def es(monkeypatch):
    def _mk(**kw):
        fake = FakeES(**kw)
        monkeypatch.setattr('search.client.get_es', lambda: fake)
        monkeypatch.setattr('search.client.index_name',
                            lambda s: f'voyager-{s}')
        return fake
    return _mk


# --------------------------------------------------------------------------- #
# 1. O erro que o coordenador mediu: a tarefa vive no ES, não no container
# --------------------------------------------------------------------------- #
@CACHE_LOCAL
def test_tarefa_viva_no_es_nao_e_parado_mesmo_sem_progresso_nenhum(es, caplog):
    """`docker ps -a` vazio + `_count` imóvel + tarefa viva = ESCREVENDO.

    Medido em 02/09/2026: o container `r106_proc_digits` tinha sumido e o
    `update_by_query` estava em 48,7% havia 1h04. Concluir "parado" aqui faz
    alguém religar por cima e DOBRAR a carga num nó que já deu 503.
    """
    cache.clear()
    es(docs=[doc(i, digits=None) for i in range(50)], tarefas=tarefa_viva())
    agora = timezone.now()
    # histórico com pendente IMÓVEL há 8 h — o pior caso para o alarme
    cache.set(V.CHAVE_HIST, [{'em': agora - datetime.timedelta(hours=8),
                              'digits': 50, 'fase': None, 'fks': 0}], 3600)

    r = V.tick_vigia_backfills()

    vereditos = {p['veredito'] for p in r['parados'] if p['o_que'] == 'proc_digits'}
    assert 'parado' not in vereditos, 'declarou parado um backfill que ESTÁ escrevendo'
    assert 'escrevendo' in vereditos
    assert not any('BACKFILL PARADO' in x.message for x in caplog.records
                   if 'proc_digits' in str(x.args))


@CACHE_LOCAL
def test_so_a_tarefa_raiz_conta_slices_nao_viram_oito_backfills(es):
    """`slices=8` = 1 pai + 8 filhas. Contar as filhas faria 1 virar 9."""
    cache.clear()
    fake = es(docs=[doc(i, digits=None) for i in range(5)], tarefas=tarefa_viva())
    d = V.medir_proc_digits()
    assert len(d['tarefas']) == 1, 'as 8 fatias viraram 8 backfills'
    t = d['tarefas'][0]
    assert t['id'] == 'ABC:305727375' and t['fatias'] == 8
    # os contadores do PAI são zero: sem somar as filhas, o card publica
    # `None%` e `0.0 d/s` — foi o que foi para produção na 1ª versão.
    assert t['pct'] == 48.7, f'não somou as fatias: pct={t["pct"]}'
    assert t['updated'] == 13_572_000
    assert t['rps'] == 500.0, 'o throttle tem que ir para a tela'


@CACHE_LOCAL
def test_rps_menos_um_do_es_nao_vira_teto_inventado(es):
    """`-1` no ES é SEM throttle. Somá-lo daria um número que ninguém pediu."""
    cache.clear()
    t = tarefa_viva(rps=-8.0)      # cada fatia declara -1
    es(docs=[doc(i, digits=None) for i in range(5)], tarefas=t)
    d = V.medir_proc_digits()
    assert d['tarefas'][0]['rps'] in (0.0, None), d['tarefas'][0]['rps']


@CACHE_LOCAL
def test_erro_ao_ler_tarefas_abstem_nunca_conclui_parado(es, caplog):
    """`[]` por ERRO não é `[]` por ausência. Abster > chutar."""
    cache.clear()
    es(docs=[doc(i, digits=None) for i in range(5)], erro_tasks=True)
    d = V.medir_proc_digits()
    assert d['tarefas'] == []
    assert any('ABSTIDO' in rec.message for rec in caplog.records)


# --------------------------------------------------------------------------- #
# 2. Parado de verdade sai com o número real — e reta final não acende
# --------------------------------------------------------------------------- #
@CACHE_LOCAL
def test_parado_de_verdade_e_erro_com_o_numero_real(caplog):
    cache.clear()
    agora = timezone.now()
    hist = [{'em': agora - datetime.timedelta(hours=7), 'fase': 19_300_000},
            {'em': agora, 'fase': 19_300_000}]
    r = {'parados': []}
    prog = V._progresso(hist, 'fase', agora)
    V._alerta_parado(r, 'fase', 19_300_000, prog, V.FASE_PISO_ALARME, 400_000,
                     'backfill_fase')
    assert r['parados'][0]['veredito'] == 'parado'
    msgs = [rec.getMessage() for rec in caplog.records]
    assert any('BACKFILL PARADO' in m and '19,300,000' in m for m in msgs), msgs


@CACHE_LOCAL
def test_reta_final_nao_acende_alarme():
    """Alarme que acende sozinho no fim é alarme gasto (lição de 21/08/2026)."""
    agora = timezone.now()
    hist = [{'em': agora - datetime.timedelta(hours=7), 'fase': 1000},
            {'em': agora, 'fase': 1000}]
    r = {'parados': []}
    V._alerta_parado(r, 'fase', 1000, V._progresso(hist, 'fase', agora),
                     V.FASE_PISO_ALARME, 0, 'backfill_fase')
    assert r['parados'][0]['veredito'] == 'reta_final'


@CACHE_LOCAL
def test_progresso_dentro_do_ruido_amostral_nao_conta_como_progresso():
    """Queda menor que o ±2σ da amostragem não prova que andou."""
    agora = timezone.now()
    hist = [{'em': agora - datetime.timedelta(hours=7), 'fase': 19_300_000},
            {'em': agora, 'fase': 19_100_000}]          # -200k, ruído é 400k
    r = {'parados': []}
    V._alerta_parado(r, 'fase', 19_100_000, V._progresso(hist, 'fase', agora),
                     V.FASE_PISO_ALARME, 400_000, 'backfill_fase')
    assert r['parados'][0]['veredito'] == 'parado'


@CACHE_LOCAL
def test_sem_janela_de_historico_o_veredito_e_aquecendo_nao_parado():
    agora = timezone.now()
    hist = [{'em': agora, 'fase': 19_300_000}]
    r = {'parados': []}
    V._alerta_parado(r, 'fase', 19_300_000, V._progresso(hist, 'fase', agora),
                     V.FASE_PISO_ALARME, 0, 'backfill_fase')
    assert r['parados'][0]['veredito'] == 'aquecendo'


# --------------------------------------------------------------------------- #
# 3. As duas mentiras do `exists`
# --------------------------------------------------------------------------- #
@CACHE_LOCAL
def test_proc_digits_vazio_nao_conta_como_presente(es, caplog):
    """`exists` conta string vazia como valor. A amostra tem que pegar."""
    cache.clear()
    es(docs=[doc(i, digits='') for i in range(10)])
    d = V.medir_proc_digits()
    assert d['amostra_vazio'] == 10 and d['amostra_ok'] == 0
    r = {'parados': []}
    V._alerta_controles(r, d)
    assert any(p['veredito'] == 'exists_mente' for p in r['parados'])
    assert any('VOLTOU A MENTIR' in rec.getMessage() for rec in caplog.records)


@CACHE_LOCAL
def test_chave_presente_com_valor_null_conta_como_AUSENTE(es):
    """`'campo' in _source` MENTE: doc antigo tem a chave com `null`.

    Este é o defeito que virou 60/60 → 38/60 numa medição desta casa. O módulo
    usa `.get(...) is not None`; trocar por `in` faria estes 10 docs contarem
    como preenchidos.
    """
    cache.clear()
    es(docs=[doc(i, digits=None) | {'proc_digits': None} for i in range(10)])
    d = V.medir_proc_digits()
    assert d['amostra_ausente'] == 10, 'chave com null passou por preenchida'
    assert d['amostra_ok'] == 0


@CACHE_LOCAL
def test_proc_digits_com_tamanho_errado_e_malformado_nao_ok(es):
    cache.clear()
    es(docs=[doc(i, digits='123') for i in range(10)])
    d = V.medir_proc_digits()
    assert d['amostra_errado'] == 10 and d['amostra_ok'] == 0


@CACHE_LOCAL
def test_faltante_fora_da_janela_e_erro_a_premissa_da_fatia_caiu(caplog):
    r = {'parados': []}
    V._alerta_controles(r, {'fora_da_janela': 4_212, 'amostra_n': 300,
                            'amostra_ok': 300, 'amostra_vazio': 0,
                            'amostra_errado': 0, 'pct_exists': 57.7,
                            'pct_amostra': 100.0})
    assert any(p['veredito'] == 'janela_furada' for p in r['parados'])
    assert any('deixaria buraco' in rec.getMessage() for rec in caplog.records)


# --------------------------------------------------------------------------- #
# 4. O retrato não pode sumir
# --------------------------------------------------------------------------- #
@CACHE_LOCAL
def test_medicao_que_falha_nao_apaga_as_outras_do_retrato(monkeypatch, es):
    """Retrato que some é o defeito que este módulo existe para consertar."""
    cache.clear()
    es(docs=[doc(i) for i in range(5)])
    monkeypatch.setattr(V, 'medir_fase',
                        lambda: (_ for _ in ()).throw(RuntimeError('PG em contenção')))
    monkeypatch.setattr(V, 'medir_fks', lambda: {'pendentes': [], 'validando': []})
    monkeypatch.setattr(V, 'medir_teto_fase', lambda forcar=False: None)

    r = V.tick_vigia_backfills()

    assert r['fase'] is None
    assert any('fase:' in e for e in r['erros'])
    assert r['proc_digits'] is not None, 'a falha de uma medição apagou a outra'
    assert V.estado() is not None, 'o tique não deixou retrato'


# --------------------------------------------------------------------------- #
# 5. FKs: uma por vez. VALIDATE conflita CONSIGO MESMO.
# --------------------------------------------------------------------------- #
class FilaFake:
    def __init__(self):
        self.enfileirados = []

    def fetch_job(self, job_id):
        return None

    def enqueue(self, fn, *a, **kw):
        self.enfileirados.append((a, kw.get('job_id')))


@CACHE_LOCAL
def test_nao_enfileira_fk_enquanto_alguem_valida(monkeypatch):
    cache.clear()
    fila = FilaFake()
    monkeypatch.setattr('django_rq.get_queue', lambda n: fila)
    r = V._enfileirar_fk([['tribunals_process', 'fk_a'], ['x', 'fk_b']],
                         validando=[[123, 900, 'active', 'IO']])
    assert r['motivo'] == 'ja_validando'
    assert fila.enfileirados == [], 'abriu um 2º VALIDATE — ele espera e REFAZ'


@CACHE_LOCAL
def test_enfileira_no_maximo_uma_fk_por_tique(monkeypatch):
    cache.clear()
    fila = FilaFake()
    monkeypatch.setattr('django_rq.get_queue', lambda n: fila)
    r = V._enfileirar_fk([['tribunals_process', 'fk_a'],
                          ['tribunals_process', 'fk_b'],
                          ['tribunals_processoparte', 'fk_c']], validando=[])
    assert r['enfileirada'] == 'fk_a'
    assert len(fila.enfileirados) == 1
    assert fila.enfileirados[0][1] == 'vigia:fk:fk_a', 'job_id não determinístico'


@CACHE_LOCAL
def test_fk_teimosa_sai_da_fila_e_vira_achado(monkeypatch, caplog):
    """FK que não cabe no teto seria re-enfileirada para sempre, varrendo
    131 GB a cada 15 min — a mesma perda silenciosa com outra roupa."""
    cache.clear()
    fila = FilaFake()
    monkeypatch.setattr('django_rq.get_queue', lambda n: fila)
    for _ in range(V.FK_TENTATIVAS_MAX):
        V._marcar_tentativa('fk_a')

    r = V._enfileirar_fk([['tribunals_process', 'fk_a'],
                          ['tribunals_process', 'fk_b']], validando=[])

    assert r['enfileirada'] == 'fk_b', 'insistiu na teimosa e pulou a que anda'
    assert r['teimosas'] == ['fk_a']
    assert any('achado, não fila' in rec.getMessage() for rec in caplog.records)


@CACHE_LOCAL
def test_validar_com_sucesso_zera_o_contador_de_tentativas():
    """Senão uma falha transitória (lock, restart) condenaria a FK para sempre."""
    cache.clear()
    V._marcar_tentativa('fk_a')
    V._marcar_tentativa('fk_a')
    assert V._tentativas()['fk_a'] == 2
    V._marcar_tentativa('fk_a', delta=0)
    assert V._tentativas()['fk_a'] == 0


@CACHE_LOCAL
def test_todas_teimosas_nao_enfileira_nada_e_diz_por_que(monkeypatch):
    cache.clear()
    fila = FilaFake()
    monkeypatch.setattr('django_rq.get_queue', lambda n: fila)
    for _ in range(V.FK_TENTATIVAS_MAX):
        V._marcar_tentativa('fk_a')
    r = V._enfileirar_fk([['t', 'fk_a']], validando=[])
    assert r['motivo'] == 'todas_teimosas' and fila.enfileirados == []


@CACHE_LOCAL
def test_teto_do_validate_e_menor_que_o_do_job():
    """Se o SQL não estourar ANTES do RQ, o erro vem como "work-horse morto" e
    o operador perde o nome da constraint e o tempo gasto."""
    assert V.FK_SQL_TIMEOUT_S < V.FK_JOB_TIMEOUT_S
    # e ambos com folga sobre a varredura medida de 2.527 s que ainda trabalhava
    assert V.FK_SQL_TIMEOUT_S > 2 * 2527


@CACHE_LOCAL
def test_kill_switch_das_fks_nao_cega_a_medicao(monkeypatch):
    """Parar a auto-cura não pode apagar o número — card vazio some da vista."""
    cache.clear()
    cache.set(V.CHAVE_FK_OFF, 1, None)
    fila = FilaFake()
    monkeypatch.setattr('django_rq.get_queue', lambda n: fila)
    r = V._enfileirar_fk([['t', 'fk_a']], validando=[])
    assert r['motivo'] == 'fk_pausada' and fila.enfileirados == []


# --------------------------------------------------------------------------- #
# 6. O warm da tela de estado escreve a MESMA chave que a view lê
# --------------------------------------------------------------------------- #
def test_warm_de_estado_cobre_exatamente_as_chaves_que_a_view_le():
    """O defeito do #64: warm computando e view no miss porque a chave era
    montada em dois lugares. Aqui há UMA função de chave, e o teste prova que
    o alvo do warm bate com ela."""
    from dashboard.tasks import agg_estado_warm_alvos
    from search import agg_estado as A

    alvos = agg_estado_warm_alvos()
    assert {uf for uf, _ in alvos} == set(A.UFS_VALIDAS), \
        'alguma UF válida ficaria sem warm — e é ela que paga os 20 s'
    assert {m for _, m in alvos} == {A.METRICA_DEFAULT}, \
        'warm de métrica não-default triplica a passada de ES (ver a docstring)'
    # a chave do warm é a chave da view, byte a byte
    for uf, metrica in alvos[:3]:
        assert (A.cache_key_estado(uf, metrica, None)
                == f'{A._CACHE_PREFIX}:{uf}:{metrica}:v1')


def test_warm_ttl_do_estado_e_maior_que_o_intervalo_e_menor_que_uma_semana():
    """4 h: três passadas podem falhar antes de alguém pagar a agregação fria,
    e warm morto degrada para lento-e-correto, não para uma semana atrás."""
    from search import agg_estado as A
    assert A.WARM_TTL > 3600, 'menor que o intervalo do warm: expira entre passadas'
    assert A.WARM_TTL < 24 * 3600, 'número de ontem servido com cara de novo'


def test_cache_key_de_estado_normaliza_uf_e_metrica():
    """Chave que aceita 'sp' e 'SP' como coisas diferentes é fragmentação, não
    cache — foi o que custou 504 na tela de leads (#64)."""
    from search import agg_estado as A
    assert A.cache_key_estado('sp', 'todos') == A.cache_key_estado('SP', 'TODOS')
    # métrica desconhecida cai no default, nunca cria chave nova
    assert A.cache_key_estado('SP', 'inventada') == A.cache_key_estado('SP', 'todos')


# --------------------------------------------------------------------------- #
# 7. O selo do card: alarme que acende sempre é alarme que ninguém lê
# --------------------------------------------------------------------------- #
@CACHE_LOCAL
def test_selo_vermelho_so_com_anomalia_de_verdade(monkeypatch):
    """`escrevendo`, `reta_final` e `aquecendo` são estados SADIOS.

    Pintar o selo pelo tamanho de `parados` acendia "2 alerta(s)" em vermelho
    num card perfeitamente saudável — pego renderizando o card com o retrato
    REAL de produção em 02/09/2026.
    """
    from dashboard.views import _vigia_backfills_estado
    monkeypatch.setattr(
        'tribunals.vigia_backfills.estado',
        lambda: {'medido_em': timezone.now(), 'parados': [
            {'o_que': 'proc_digits', 'veredito': 'escrevendo'},
            {'o_que': 'fase', 'veredito': 'aquecendo'}]})
    assert _vigia_backfills_estado()['alertas'] == []

    monkeypatch.setattr(
        'tribunals.vigia_backfills.estado',
        lambda: {'medido_em': timezone.now(), 'parados': [
            {'o_que': 'fase', 'veredito': 'parado'},
            {'o_que': 'proc_digits', 'veredito': 'escrevendo'}]})
    alertas = _vigia_backfills_estado()['alertas']
    assert len(alertas) == 1 and alertas[0]['veredito'] == 'parado'


# --------------------------------------------------------------------------- #
# 8. `partes`: a cobertura do `backfill_partes_djen` (#121)
# --------------------------------------------------------------------------- #
@CACHE_LOCAL
def test_partes_mede_dos_dados_e_separa_o_que_veio_do_djen(db):
    """Duas barras, duas réguas: cobertura total e a fatia `fonte='djen'`.

    Sem a segunda, o enricher subindo a barra e o backfill subindo a barra são
    o mesmo número na tela — e só um deles é o que este vigia está vigiando.
    Um card que fica verde por trabalho de outro é a versão em pixel do "run
    verde, log limpo, número redondo".
    """
    from tribunals.models import Parte, Process, ProcessoParte, Tribunal
    trib = Tribunal.objects.create(sigla='VGX', nome='vigia teste')
    procs = [Process.objects.create(numero_cnj=f'000000{i}-11.2024.5.08.0009',
                                    tribunal=trib) for i in range(3)]
    p1 = Parte.objects.create(nome='DO ENRICHER', tipo='desconhecido')
    p2 = Parte.objects.create(nome='DO DJEN', tipo='desconhecido')
    ProcessoParte.objects.create(processo=procs[0], parte=p1, polo='ativo',
                                 papel='AUTOR', fonte=None)
    ProcessoParte.objects.create(processo=procs[1], parte=p2, polo='ativo',
                                 papel='', fonte='djen')

    r = V.medir_partes_djen()

    # `TABLESAMPLE` numa tabela minúscula pode devolver 0 páginas; o que este
    # teste trava é o CONTRATO do retorno, não a estatística.
    for chave in ('linhas', 'amostra_n', 'com_parte', 'com_parte_djen', 'pct',
                  'pct_djen', 'pct_erro_pp', 'faltam_estimado'):
        assert chave in r, f'o card perderia `{chave}`'
    assert r['com_parte_djen'] <= r['com_parte'], (
        'a fatia do DJEN não pode ser maior que a cobertura total')


@CACHE_LOCAL
def test_teto_de_partes_nao_conta_lista_vazia_como_destinatario(db):
    """`[]` não é ausência para `IS NOT NULL` — é o `exists` do ES de novo.

    A esmagadora maioria das movimentações guarda `destinatarios = []`. Medir
    por presença de coluna publicaria um teto de ~100% alcançável, e o card
    diria que o backfill pode chegar onde ele não chega.
    """
    from django.utils import timezone as tz
    from tribunals.models import Movimentacao, Process, Tribunal
    trib = Tribunal.objects.create(sigla='VGY', nome='vigia teto')
    p = Process.objects.create(numero_cnj='0000009-11.2024.5.08.0009', tribunal=trib)
    Movimentacao.objects.create(processo=p, tribunal=trib, external_id='vgy-1',
                                data_disponibilizacao=tz.now(),
                                destinatarios=[], destinatario_advogados=[])
    cache.delete(V.CHAVE_TETO_PARTES)

    r = V.medir_teto_partes(forcar=True)

    assert r['alcancavel'] <= r['amostra_sem_parte']
    assert r['janela_movs'] == 3, 'medir fora da janela do backfill dá teto falso'
    assert 'medido_em' in r, 'teto sem data é número sem idade'


@CACHE_LOCAL
def test_partes_parado_grita_com_o_comando_de_RETOMADA(caplog):
    """Quem religa não pode ser convidado a passar `--de 0`.

    O backfill guarda checkpoint por shard: o comando do alarme é a retomada,
    e não a largada. Passar `--de 0` refaria a varredura inteira do zero.
    """
    cache.clear()
    agora = timezone.now()
    hist = [{'em': agora - datetime.timedelta(hours=7), 'partes': 60_000_000},
            {'em': agora, 'partes': 60_000_000}]
    r = {'parados': []}
    with caplog.at_level(logging.ERROR):
        V._alerta_parado(r, 'partes', 60_000_000,
                         V._progresso(hist, 'partes', agora),
                         V.PARTES_PISO_ALARME, 400_000,
                         'backfill_partes_djen --shard nacional')
    assert any(p['veredito'] == 'parado' for p in r['parados'])
    msg = ' '.join(rec.getMessage() for rec in caplog.records)
    assert '--shard nacional' in msg
    assert '--de 0' not in msg


@CACHE_LOCAL
def test_o_limite_da_amostra_de_partes_nao_estreita_o_intervalo(monkeypatch, db):
    """Cortar LINHAS corta PÁGINAS junto — e o ±2σ tem que sentir.

    Medido em 02/09/2026: com `pct=0,06` a amostra pedia ~64.500 linhas, o
    `LIMIT` entregava 25.000, e o erro saía `±1,51 pp` calculado sobre as
    páginas PEDIDAS. O honesto era ±2,43. Intervalo estreito demais é a pior
    espécie de número redondo — ele encerra a investigação.
    """
    largo = V.medir_partes_djen()
    monkeypatch.setattr(V, 'PARTES_AMOSTRA_MAX', 1)
    apertado = V.medir_partes_djen()
    if largo['amostra_n'] > 1 and apertado['amostra_n'] <= 1:
        assert apertado['paginas_amostradas'] <= largo['paginas_amostradas'], (
            'o LIMIT cortou a amostra e o número de páginas não mudou — '
            'o erro publicado seria estreito demais')


# --------------------------------------------------------------------------- #
# 7. magistrados (#125): a régua é DISCO, e o alarme é o ORÇAMENTO
# --------------------------------------------------------------------------- #
def _mg(**kw):
    """Retrato mínimo de `medir_magistrados`, com o que o alarme lê."""
    base = {'atuacoes': 1_000_000, 'magistrados': 5_000, 'bytes_por_linha': 300.0,
            'gasto_bytes': 1024 ** 3, 'orcamento_bytes': 20 * 1024 ** 3,
            'projecao_linhas': 190_000_000, 'projecao_bytes': 57 * 1024 ** 3,
            'pausado': False}
    base.update(kw)
    return base


@CACHE_LOCAL
def test_tabela_vazia_e_NAO_INICIADO_e_nao_grita():
    """A migration cria as tabelas vazias de propósito. "Ninguém ligou ainda"
    é estado legítimo — gritar aqui seria alarme que nasce aceso."""
    r = {'parados': []}
    V._alerta_magistrados(r, _mg(atuacoes=0, gasto_bytes=0),
                          {'veredito': None, 'delta': 0, 'horas': 6.0})
    assert r['parados'][0]['veredito'] == 'nao_iniciado'


@CACHE_LOCAL
def test_pausado_de_proposito_nao_e_parado_e_o_numero_continua_na_tela():
    """Mesma escolha do `djen_recup_f3`: `--parar` não cega a tela. Card vazio
    some da vista; número alto ao lado de PAUSADO, não."""
    r = {'parados': []}
    V._alerta_magistrados(r, _mg(pausado=True),
                          {'veredito': None, 'delta': 0, 'horas': 6.0})
    (p,) = r['parados']
    assert p['veredito'] == 'pausado'
    assert p['atuacoes'] == 1_000_000 and p['bytes_por_linha'] == 300.0


@CACHE_LOCAL
def test_orcamento_estourado_e_ERRO_com_a_extrapolacao_medida(caplog):
    """Regra nº 2: teto é alerta COM NÚMERO. E o número que interessa a quem
    vai decidir a fatia seguinte é a extrapolação MEDIDA, não a derivada."""
    r = {'parados': []}
    with caplog.at_level(logging.ERROR):
        V._alerta_magistrados(r, _mg(gasto_bytes=21 * 1024 ** 3),
                              {'veredito': None, 'delta': -500, 'horas': 6.0})
    assert r['parados'][0]['veredito'] == 'orcamento_cheio'
    texto = caplog.text
    assert 'ORÇAMENTO DE DISCO ESTOURADO' in texto and '57.0 GiB' in texto


@CACHE_LOCAL
def test_crescimento_inverte_o_sinal_do_progresso():
    """O histórico deste vigia guarda PENDENTE nas outras colunas e LINHAS
    GRAVADAS nesta. `base − atual` fica negativo quando o trabalho anda; ler
    isso como "não andou" declararia parado um backfill que está escrevendo."""
    r = {'parados': []}
    V._alerta_magistrados(r, _mg(), {'veredito': None, 'delta': -250_000,
                                     'horas': 6.0})
    assert r['parados'] == [], 'cresceu 250 mil linhas e foi chamado de parado'


@CACHE_LOCAL
def test_parado_de_verdade_grita_com_o_comando_de_retomada(caplog):
    r = {'parados': []}
    with caplog.at_level(logging.ERROR):
        V._alerta_magistrados(r, _mg(), {'veredito': None, 'delta': 0,
                                         'horas': 6.0})
    assert r['parados'][0]['veredito'] == 'parado'
    assert 'backfill_magistrados --shard nacional' in caplog.text


@pytest.mark.django_db
@CACHE_LOCAL
def test_bytes_por_linha_ausente_nao_vira_zero():
    """Zero aqui viraria "cabe tudo" — o pior arredondamento possível numa
    régua de disco."""
    cache.clear()
    r = V.medir_magistrados()
    assert r['bytes_por_linha'] is None or r['bytes_por_linha'] > 0
    assert r['projecao_bytes'] is None or r['projecao_bytes'] > 0


@pytest.mark.django_db
@CACHE_LOCAL
def test_a_contagem_cara_nao_leva_junto_o_numero_do_orcamento(monkeypatch):
    """O tamanho é catálogo e custa nada; a contagem exata varre índice e vai
    encarecer. Num `try` só, o dia em que a contagem estourar o teto apagaria
    o card inteiro — inclusive o ORÇAMENTO, que é o número que não pode sumir.
    """
    real = V._com_teto

    def _falha_na_contagem(fn, segundos=V.SQL_TIMEOUT_S):
        if segundos == V.MAG_CONTA_TIMEOUT_S:
            raise RuntimeError('canceling statement due to statement timeout')
        return real(fn, segundos)

    monkeypatch.setattr(V, '_com_teto', _falha_na_contagem)
    r = V.medir_magistrados()
    assert r['bytes'] is not None, 'o orçamento sumiu junto com a contagem'
    assert r['fonte_linhas'] == 'reltuples', \
        'caiu para estimativa e não disse — a procedência tem de ir na tela'
