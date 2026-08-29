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
