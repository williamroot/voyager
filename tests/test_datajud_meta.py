"""Regressão do bug `AttributeError: 'list' object has no attribute 'get'` em
`datajud.ingestion._meta_updates_from_source` (datajud/ingestion.py:53).

O Datajud às vezes aninha `assuntos` (e ocasionalmente `classe`/`orgaoJulgador`)
como lista-de-lista em vez de lista-de-dict. O código chamava `.get` direto →
~23% dos failed jobs da fila `datajud` (~56k) eram esse AttributeError.
`_as_dict` desce listas aninhadas até o dict.
"""
from types import SimpleNamespace

from datajud.ingestion import _as_dict, _meta_updates_from_source


def _proc():
    return SimpleNamespace(
        classe_codigo='', assunto_codigo='', orgao_julgador_codigo='',
        orgao_julgador_nome='', data_autuacao=None, valor_causa=None,
    )


def test_as_dict_desce_listas_aninhadas():
    assert _as_dict({'codigo': 1}) == {'codigo': 1}
    assert _as_dict([{'codigo': 1}]) == {'codigo': 1}
    assert _as_dict([[{'codigo': 1}]]) == {'codigo': 1}
    assert _as_dict([]) == {}
    assert _as_dict(None) == {}
    assert _as_dict('x') == {}


def test_assuntos_aninhado_como_lista_nao_quebra():
    # caso real do bug: assuntos = [[{...}]]
    source = {'assuntos': [[{'codigo': 1234, 'nome': 'Furto'}]]}
    upd = _meta_updates_from_source(_proc(), source)  # não pode levantar
    assert upd['assunto_codigo'] == '1234'
    assert upd['assunto_nome'] == 'Furto'


def test_assuntos_dict_normal_continua_funcionando():
    source = {'assuntos': [{'codigo': 5, 'nome': 'Dano Moral'}]}
    upd = _meta_updates_from_source(_proc(), source)
    assert upd['assunto_codigo'] == '5'
    assert upd['assunto_nome'] == 'Dano Moral'


def test_classe_e_orgao_como_lista_nao_quebram():
    source = {
        'classe': [{'codigo': 7, 'nome': 'Procedimento'}],
        'orgaoJulgador': [[{'codigo': 9, 'nome': 'Vara Única'}]],
    }
    upd = _meta_updates_from_source(_proc(), source)
    assert upd['classe_codigo'] == '7'
    assert upd['orgao_julgador_codigo'] == '9'
    assert upd['orgao_julgador_nome'] == 'Vara Única'


def test_source_vazio_nao_quebra():
    assert _meta_updates_from_source(_proc(), {}) == {}
