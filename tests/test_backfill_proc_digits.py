"""Backfill do `proc_digits` em `voyager-movimentacoes`.

O risco desta peça não é dar erro — é rodar 4 dias, devolver `success`, e
deixar o campo meio-cheio. Um campo que existe em parte do índice é PIOR que
um campo ausente: ele produz confiança falsa, e foi exatamente assim que
`proc_digits` chegou a faltar em 74,65% de 1,55 bilhão de documentos sem que
nada na tela, no log ou no gate da migração dissesse uma palavra.

Cada teste abaixo cerca uma forma de falhar em silêncio:

  - fatia que fica pela metade não passa pro dia seguinte (gate dos dois lados);
  - `proc_digits` vazio não conta como presente — o `exists` do ES conta;
  - o conteúdo é conferido por AMOSTRA, não por `exists` (20 dígitos, batendo
    com o `proc`);
  - a fatia MISTA (docs com e sem o campo no mesmo dia) passa — o gate compara
    o delta, não o total, senão abortaria todo julho num backfill correto;
  - faltante que APARECE durante a passada não sai como sucesso;
  - teto atingido é ERRO com o número real do que ficou de fora, nunca `return`;
  - tarefa do ES que não volta é ERRO com o id, não espera eterna;
  - kill switch para no MEIO, falha FECHADO, e a retomada não repete;
  - guardas de disco e de cluster `red` abortam antes de escrever;
  - `--medir` não escreve nada.
"""
import datetime

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from search.management.commands import es_backfill_proc_digits as M

CACHE_LOCAL = override_settings(CACHES={'default': {
    'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
    'LOCATION': 'test-backfill-proc-digits'}})

DIA_A = '2026-04-01'
DIA_B = '2026-04-02'
DIA_C = '2026-04-03'


def doc(i, dia, digits=None):
    d = {'id': i, 'proc': f'{i:07d}-11.2024.8.26.0100',
         'detected_at': f'{dia}T04:00:00+00:00'}
    if digits is not None:
        d['proc_digits'] = digits
    return d


def digitos(proc):
    return ''.join(c for c in proc if c.isdigit())


# --------------------------------------------------------------------------- #
# Dublê do ES. O matcher entende só as formas que o comando produz — de
# propósito: um dublê que aceita qualquer query não prova que a query está certa.
# --------------------------------------------------------------------------- #

class FakeES:
    def __init__(self, docs, *, defeito=None, livre_bytes=900e9, status='yellow'):
        self.docs = docs
        #: hook pra simular o UBQ que não fecha a fatia (ou fecha errado).
        #: `defeito(doc)` devolve o valor a gravar, ou `False` pra não gravar.
        self.defeito = defeito
        self.livre_bytes = livre_bytes
        self.status = status
        self.ubqs = []            # fatias em que o UBQ foi de fato chamado
        self.indices = self._Indices()
        self.cluster = self._Cluster(self)
        self.nodes = self._Nodes(self)
        self.tasks = self._Tasks(self)

    # -- matcher ---------------------------------------------------------- #

    def _casa(self, d, q):
        if 'match_all' in q:
            return True
        if 'exists' in q:
            # `.get(...) is not None` e não `in`: doc antigo tem a CHAVE com
            # valor null e passaria no `in`.
            return d.get(q['exists']['field']) is not None
        if 'term' in q:
            (campo, valor), = q['term'].items()
            return d.get(campo) == valor
        if 'range' in q:
            (campo, r), = q['range'].items()
            v = d.get(campo)
            if v is None:
                return False
            if 'gte' in r and v[:10] < r['gte'][:10]:
                return False
            if 'lt' in r and v[:10] >= r['lt'][:10]:
                return False
            return True
        if 'bool' in q:
            b = q['bool']
            for c in b.get('filter', []):
                if not self._casa(d, c):
                    return False
            for c in b.get('must_not', []):
                if self._casa(d, c):
                    return False
            return True
        if 'function_score' in q:
            return self._casa(d, q['function_score']['query'])
        raise AssertionError(f'query que o dublê não conhece: {q}')

    def _filtrar(self, q):
        return [d for d in self.docs if self._casa(d, q)]

    # -- API usada pelo comando ------------------------------------------- #

    def count(self, index, query, request_timeout=None):
        assert request_timeout, 'ES sem teto de espera (regra nº 7)'
        return {'count': len(self._filtrar(query))}

    def search(self, index, size, query, request_timeout=None, source_includes=None):
        assert request_timeout, 'ES sem teto de espera (regra nº 7)'
        hits = self._filtrar(query)[:size]
        return {'hits': {'hits': [{'_id': str(d['id']), '_source': dict(d)} for d in hits]}}

    def update_by_query(self, index, query, script, request_timeout=None, **kw):
        assert request_timeout, 'ES sem teto de espera (regra nº 7)'
        assert kw.get('wait_for_completion') is False
        self.ubqs.append((query, kw))
        alvo = self._filtrar(query)
        upd = 0
        for d in alvo:
            valor = digitos(d['proc'])
            if self.defeito is not None:
                valor = self.defeito(d)
                if valor is False:
                    continue
            d['proc_digits'] = valor
            upd += 1
        self._status = {'updated': upd, 'noops': 0, 'version_conflicts': 0,
                        'total': len(alvo)}
        return {'task': f'no:{len(self.ubqs)}'}

    class _Tasks:
        def __init__(self, es):
            self.es = es

        def get(self, task_id, request_timeout=None):
            return {'completed': True, 'task': {'status': self.es._status}}

    class _Indices:
        def refresh(self, index, request_timeout=None):
            assert request_timeout, 'refresh sem teto de espera'

    class _Cluster:
        def __init__(self, es):
            self.es = es

        def health(self, request_timeout=None):
            return {'status': self.es.status}

    class _Nodes:
        def __init__(self, es):
            self.es = es

        def stats(self, metric=None, request_timeout=None):
            return {'nodes': {'n': {'fs': {'total': {
                'available_in_bytes': self.es.livre_bytes}}}}}


@pytest.fixture
def es_patch(monkeypatch):
    def _instalar(fake):
        monkeypatch.setattr(M, 'get_es', lambda: fake)
        monkeypatch.setattr(M.time, 'sleep', lambda *_: None)
        return fake
    return _instalar


def rodar(**extra):
    o = {'rodar': True, 'de': DIA_A, 'ate': DIA_C, 'rps': 4000, 'slices': 8,
         'lote': 1000, 'disco_min_gb': 200, 'timeout_fatia_h': 24.0}
    o.update(extra)
    call_command('es_backfill_proc_digits', **o)


# --------------------------------------------------------------------------- #

@CACHE_LOCAL
def test_backfill_fecha_as_fatias_e_o_gate_confere_dos_dois_lados(es_patch):
    cache.clear()
    fake = es_patch(FakeES([doc(i, DIA_A) for i in range(1, 6)]
                           + [doc(i, DIA_B) for i in range(6, 9)]))
    rodar()
    assert all(d.get('proc_digits') == digitos(d['proc']) for d in fake.docs)
    assert len(fake.ubqs) == 2                       # uma por dia COM faltante


@CACHE_LOCAL
def test_fatia_pela_metade_e_erro_nao_passa_pro_dia_seguinte(es_patch):
    """O modo de falha caro: UBQ verde deixando resto. `updated` não é gate."""
    cache.clear()
    # o defeito não grava o doc de id par: a fatia fecha "com sucesso" mas
    # metade continua sem o campo
    fake = es_patch(FakeES([doc(i, DIA_A) for i in range(1, 7)]
                           + [doc(i, DIA_B) for i in range(7, 10)],
                           defeito=lambda d: False if d['id'] % 2 == 0 else digitos(d['proc'])))
    with pytest.raises(CommandError, match='GATE FALHOU'):
        rodar()
    assert len(fake.ubqs) == 1, 'não pode ter ido pro dia seguinte com buraco'


@CACHE_LOCAL
def test_gate_pega_faltante_que_apareceu_durante_a_passada(es_patch):
    """`updated == alvo` não prova fatia fechada: o índice é vivo.

    Se o caminho de escrita voltar a gravar doc sem `proc_digits` (foi assim
    que o buraco nasceu), o delta bate certinho e a fatia continua furada. Só
    o `must_not exists == 0` pega isso.
    """
    cache.clear()
    docs = [doc(i, DIA_A) for i in range(1, 4)]
    fake = es_patch(FakeES(docs))
    ubq_original = fake.update_by_query

    def ubq(**kw):
        r = ubq_original(**kw)
        docs.append(doc(99, DIA_A))          # chegou sem o campo no meio da passada
        return r
    fake.update_by_query = ubq

    with pytest.raises(CommandError, match='ainda sem proc_digits'):
        rodar()


@CACHE_LOCAL
def test_fatia_mista_passa_o_gate(es_patch):
    """Uma fatia de `detected_at` tem docs COM e SEM o campo ao mesmo tempo.

    Julho/2026 tem 96,9 M sem e 63,8 M com — o enriquecimento reescreve
    movimentação antiga com o doc builder novo, e a `detected_at` dela continua
    sendo a antiga. Um gate que comparasse `exists` da fatia com o número de
    faltantes abortaria todas essas fatias num backfill CORRETO. O gate compara
    o delta.
    """
    cache.clear()
    ja_tem = [doc(i, DIA_A, digits=digitos(f'{i:07d}-11.2024.8.26.0100'))
              for i in range(1, 8)]
    faltando = [doc(i, DIA_A) for i in range(8, 11)]
    fake = es_patch(FakeES(ja_tem + faltando))
    rodar()
    assert len(fake.ubqs) == 1
    assert all(d['proc_digits'] == digitos(d['proc']) for d in fake.docs)


@CACHE_LOCAL
def test_gate_pega_proc_digits_vazio_que_o_exists_conta_como_presente(es_patch):
    """`exists` do ES conta string vazia como valor. O gate não pode."""
    cache.clear()
    fake = es_patch(FakeES([doc(i, DIA_A) for i in range(1, 4)],
                           defeito=lambda d: ''))
    with pytest.raises(CommandError, match='vazio'):
        rodar()
    assert all(d['proc_digits'] == '' for d in fake.docs)


@CACHE_LOCAL
def test_gate_confere_o_conteudo_por_amostra_nao_so_a_presenca(es_patch):
    """Gravar "alguma coisa" não é gravar os 20 dígitos do `proc`."""
    cache.clear()
    es_patch(FakeES([doc(i, DIA_A) for i in range(1, 4)],
                    defeito=lambda d: '000'))
    with pytest.raises(CommandError, match='20 dígitos'):
        rodar()


@CACHE_LOCAL
def test_teto_de_docs_e_erro_com_o_numero_real(es_patch):
    """Regra nº 2: teto é ERRO registrado, nunca `return` discreto."""
    cache.clear()
    fake = es_patch(FakeES([doc(i, DIA_A) for i in range(1, 6)]
                           + [doc(i, DIA_B) for i in range(6, 12)]))
    with pytest.raises(CommandError) as e:
        rodar(max_docs=8)
    msg = str(e.value)
    assert 'TETO' in msg
    assert '6' in msg and 'ainda faltam' in msg      # o número REAL do que ficou
    assert len(fake.ubqs) == 1, 'o teto não pode cortar uma fatia no meio'


@CACHE_LOCAL
def test_kill_switch_para_no_meio_e_a_retomada_nao_repete(es_patch):
    cache.clear()
    docs = [doc(i, DIA_A) for i in range(1, 4)] + [doc(i, DIA_B) for i in range(4, 7)]
    fake = es_patch(FakeES(docs))

    # alguém aperta o kill switch enquanto a 1ª fatia está sendo escrita
    ubq_original = fake.update_by_query

    def ubq(**kw):
        cache.set(M.PAUSA_KEY, 1, timeout=None)
        return ubq_original(**kw)
    fake.update_by_query = ubq

    rodar()
    assert len(fake.ubqs) == 1
    feitos = [d for d in docs if d.get('proc_digits')]
    assert len(feitos) == 3 and all(d['detected_at'].startswith(DIA_A) for d in feitos)

    # retomada: a fatia já feita custa um `_count` e escreve zero
    cache.delete(M.PAUSA_KEY)
    fake.ubqs.clear()
    rodar()
    assert len(fake.ubqs) == 1, 'a fatia já fechada não pode ser reescrita'
    assert all(d.get('proc_digits') == digitos(d['proc']) for d in docs)


@CACHE_LOCAL
def test_kill_switch_falha_fechado_quando_o_cache_cai(monkeypatch):
    """Kill switch que falha aberto não é kill switch."""
    cache.clear()
    monkeypatch.setattr(M.cache, 'get', lambda *_a, **_k: (_ for _ in ()).throw(OSError('redis')))
    assert M.parado() is True


@CACHE_LOCAL
def test_guarda_de_disco_aborta_antes_de_escrever(es_patch):
    cache.clear()
    fake = es_patch(FakeES([doc(1, DIA_A)], livre_bytes=100e9))
    with pytest.raises(CommandError, match='disco livre'):
        rodar()
    assert fake.ubqs == []


@CACHE_LOCAL
def test_cluster_red_aborta_antes_de_escrever(es_patch):
    cache.clear()
    fake = es_patch(FakeES([doc(1, DIA_A)], status='red'))
    with pytest.raises(CommandError, match='RED'):
        rodar()
    assert fake.ubqs == []


@CACHE_LOCAL
def test_medir_nao_escreve(es_patch):
    cache.clear()
    fake = es_patch(FakeES([doc(i, DIA_A) for i in range(1, 4)]))
    call_command('es_backfill_proc_digits', medir=True, de=DIA_A, ate=DIA_C)
    assert fake.ubqs == []
    assert all('proc_digits' not in d for d in fake.docs)


def test_a_janela_padrao_e_a_medida_nao_um_arredondamento():
    """A janela existe porque foi MEDIDA (0 faltantes fora dela em 31/08/2026).
    Se alguém 'arredondar' pra ano cheio, o backfill vira varredura de 1,55 bi."""
    assert datetime.date(2026, 4, 1) == M.JANELA_INICIO
    assert datetime.date(2026, 8, 1) == M.JANELA_FIM


@CACHE_LOCAL
def test_tarefa_que_nao_volta_e_erro_com_o_id_nao_espera_eterna(es_patch):
    """Esperar para sempre é o jeito mais silencioso deste comando falhar:
    "rodando" e "travado" ficam indistinguíveis."""
    cache.clear()
    fake = es_patch(FakeES([doc(1, DIA_A)]))
    fake.tasks.get = lambda task_id, request_timeout=None: {
        'completed': False, 'task': {'status': {'updated': 7, 'total': 9}}}
    with pytest.raises(CommandError) as e:
        rodar(timeout_fatia_h=0.0)
    assert 'TETO de espera' in str(e.value)
    assert 'no:1' in str(e.value), 'a mensagem tem que dar o id da tarefa'
    assert '7' in str(e.value) and '9' in str(e.value)
