"""Indexador com model VELHO tem que PARAR, não escrever valor de enchimento.

MEDIDO em 29/08/2026, em produção. As 24 réplicas de `worker_es_index` (na
`.102`) estavam de pé havia 4 dias. O bind mount `.:/app` entrega o `git pull`,
mas Python não recarrega módulo já importado — então o processo carregava um
`tribunals/models.py` anterior a 28/08: `Process` sem `grau`, `ProcessoParte`
sem `fonte`.

`search/documents.py` lê esses dois campos com `getattr(obj, campo, padrão)`
"para não explodir enquanto a coluna não estiver em toda a frota". O `getattr`
fez o que foi escrito para fazer: devolveu o padrão. Cada documento reindexado
saiu COMPLETO e ERRADO.

Amostra de conglomerado no mesmo dia (n=116.713, semente 20260829):

    grau no Postgres ............ 78,57%
    grau no índice ..............  1,03%
    processos da amostra com parte do DJEN ................ 399
      … com a participação no índice ...................... 399
      … com `participacoes.fonte = 'djen'` no índice ......   0

Nada disso deu erro: fila `es_index` em zero, job `finished`, doc presente.
Depois de `docker compose restart worker_es_index`, o MESMO job no MESMO
processo passou a gravar `grau='G1'` e `fonte='djen'` nos 5 processos-piloto.

O que este teste trava: o padrão silencioso virou ERRO. Indexar com valor de
enchimento é pior do que não indexar — dado pela metade produz confiança falsa
(princípio nº 1).
"""
import pytest

from search import documents


@pytest.fixture(autouse=True)
def _limpar_cache_da_conferencia():
    documents._MODELO_CONFERIDO = None
    yield
    documents._MODELO_CONFERIDO = None


def test_modelo_em_dia_passa():
    """Com o `tribunals/models.py` do repo, a conferência não reclama."""
    assert documents._campos_faltando() == []
    documents.exigir_modelo_em_dia()      # não levanta


def test_modelo_velho_levanta_com_o_nome_do_campo(monkeypatch):
    monkeypatch.setitem(documents.CAMPOS_EXIGIDOS, 'Process',
                        ('grau', 'campo_que_so_existe_na_versao_nova'))
    with pytest.raises(documents.ModeloDesatualizado) as exc:
        documents.exigir_modelo_em_dia()
    msg = str(exc.value)
    assert 'Process.campo_que_so_existe_na_versao_nova' in msg, (
        'a mensagem tem que NOMEAR o campo — "algo está velho" não é diagnóstico')
    assert 'restart' in msg.lower() or 'REINICIE' in msg, (
        'a mensagem tem que dizer o que fazer: o bind mount já entregou o '
        'arquivo, o que falta é reiniciar o processo')


def test_processo_to_doc_confere_antes_de_montar(monkeypatch):
    """A conferência roda ANTES de qualquer campo — senão o doc já saiu errado."""
    chamou = []
    monkeypatch.setattr(documents, 'exigir_modelo_em_dia',
                        lambda: chamou.append(1))

    class _Explode:
        def __getattr__(self, nome):
            raise AssertionError('processo_to_doc leu campo antes de conferir '
                                 'o modelo')

    with pytest.raises(Exception):
        documents.processo_to_doc(_Explode())
    assert chamou, 'processo_to_doc não chamou `exigir_modelo_em_dia`'


def test_campos_exigidos_cobre_todo_getattr_do_builder():
    """Campo novo lido com `getattr(..., padrão)` TEM que entrar na lista.

    Senão a próxima coluna nova repete a história inteira: o builder abstém em
    silêncio e o índice envelhece sem um log.
    """
    import re
    fonte = open('search/documents.py').read()
    # `getattr(proc, 'grau', '')` / `getattr(pp, 'fonte', None)`
    lidos = set(re.findall(r"getattr\(\s*(?:proc|pp)\s*,\s*'([a-z_]+)'", fonte))
    declarados = {c for campos in documents.CAMPOS_EXIGIDOS.values() for c in campos}
    faltam = lidos - declarados
    assert not faltam, (
        f'campos lidos com getattr defensivo e FORA de CAMPOS_EXIGIDOS: '
        f'{sorted(faltam)} — eles voltam a virar valor de enchimento silencioso')


# --------------------------------------------------------------------------- #
# `reindexar_processos` — o backfill de 101,5 M de docs
# --------------------------------------------------------------------------- #
# A primeira versão da corrida usava `qs.order_by('id').iterator(chunk_size=bs)`
# sobre a tabela inteira. Medido em produção em 29/08/2026: o Postgres escolheu
# ordenar as 103,6 M de linhas em DISCO (`wait_event = BuffileRead`, sort
# externo) e ficou **175 s sem devolver a primeira linha**. O comando imprimiu
# o cabeçalho, mais nada, e parecia travado. Com `WHERE id > cursor LIMIT
# janela` o plano vira Index Scan na pkey.

class _RespostaFake:
    status_code = 200
    text = ''


class _SessaoFake:
    """Substitui `requests.Session` — registra os `_bulk` sem falar com o ES."""

    def __init__(self):
        self.corpos = []

    def post(self, url, data=None, headers=None, timeout=None):
        self.corpos.append(data.decode('utf-8'))
        return _RespostaFake()


@pytest.fixture
def _processos(db):
    from tribunals.models import Process, Tribunal
    t, _ = Tribunal.objects.get_or_create(
        sigla='TJRX', defaults={'nome': 'TJRX', 'sigla_djen': 'TJRX'})
    return [Process.objects.create(numero_cnj=f'000{i:04d}-77.2025.8.26.0100',
                                   tribunal=t, grau='G1').pk
            for i in range(25)]


def test_reindex_anda_por_keyset_e_indexa_todo_mundo(_processos, monkeypatch):
    import json

    from django.core.management import call_command

    from search.management.commands import reindexar_processos as cmd

    sessao = _SessaoFake()
    monkeypatch.setattr(cmd.requests, 'Session', lambda: sessao)
    # janela e batch pequenos: força VÁRIAS voltas do keyset
    call_command('reindexar_processos', batch_size=4, janela=7, sleep=0)

    ids = []
    for corpo in sessao.corpos:
        linhas = [l for l in corpo.split('\n') if l]
        for i in range(0, len(linhas), 2):
            ids.append(json.loads(linhas[i])['index']['_id'])
    assert sorted(ids) == sorted(_processos), (
        'o keyset perdeu ou repetiu processo — é exatamente o modo de falha de '
        'quem avança o cursor antes de mandar o lote')
    # e o campo que sumiu do índice inteiro tem que estar no doc
    docs = [json.loads(l) for corpo in sessao.corpos
            for j, l in enumerate([x for x in corpo.split('\n') if x]) if j % 2]
    assert all(d['grau'] == 'G1' for d in docs)


def test_reindex_nao_usa_iterator_sobre_a_tabela_inteira():
    fonte = open('search/management/commands/reindexar_processos.py').read()
    # só o CÓDIGO — o comentário que conta a história cita o `.iterator()`
    codigo = '\n'.join(l for l in fonte.splitlines()
                       if not l.lstrip().startswith('#'))
    assert '.iterator(' not in codigo, (
        'voltou o cursor sobre a tabela ordenada inteira — 175 s de sort em '
        'disco antes da primeira linha')
    assert 'filter(id__gt=cursor)' in codigo


def test_reindex_conta_bulk_que_falhou(_processos, monkeypatch, capsys):
    """Sem fila e sem retry, bulk que falha aqui é documento PERDIDO."""
    from django.core.management import call_command

    from search.management.commands import reindexar_processos as cmd

    class _Ruim(_SessaoFake):
        def post(self, *a, **kw):
            r = _RespostaFake()
            r.status_code = 503
            r.text = 'indisponivel'
            return r

    monkeypatch.setattr(cmd.requests, 'Session', lambda: _Ruim())
    call_command('reindexar_processos', batch_size=10, janela=10, sleep=0)
    err = capsys.readouterr().err
    assert 'ERRO' in err and 'bulks falharam' in err, (
        'bulk que falha saiu em silêncio — era um stderr.write e segue')
    assert '25 documentos' in err.replace('.', '').replace(',', ''), (
        'o alerta tem que trazer o NÚMERO de documentos perdidos')
