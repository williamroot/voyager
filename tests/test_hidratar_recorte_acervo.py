"""Comando `hidratar_recorte_acervo`: mede por padrão, hidrata com --executar,
e o --limite é teto declarado (nunca corte mudo)."""
from __future__ import annotations

import json
from io import StringIO

import pytest
from django.core.management import call_command

from datajud import hidratacao
from tribunals.models import Process, Tribunal

ESQ = [
    {'proc': '0500008-51.2023.8.02.0043', 'assunto_nomes': ['Pagamento'],
     'orgao_nome': 'Foro de Delmiro Gouveia - 2º Vara'},
    # G1 e G2 do mesmo CNJ: conta uma vez
    {'proc': '0500008-51.2023.8.02.0043', 'assunto_nomes': ['Pagamento'],
     'orgao_nome': 'Foro de Delmiro Gouveia - 2º Vara'},
    {'proc': '0500004-77.2024.8.02.0043', 'assunto_nomes': ['Pagamento'],
     'orgao_nome': 'Foro de Delmiro Gouveia - 1º Vara'},
    {'proc': '0500205-98.2024.8.02.0001', 'assunto_nomes': ['Pagamento'],
     'orgao_nome': 'Foro de Maceió - 18ª Vara'},
]


@pytest.fixture
def acervo(monkeypatch):
    chamadas = {}

    def fake_iterar(sigla, classe, prefixo='', excluir_foros=(), pagina=1000):
        chamadas.update(sigla=sigla, classe=classe, prefixo=prefixo,
                        excluir_foros=excluir_foros)
        yield from ESQ

    hidratados = []

    def fake_hidratar(cnj, com_enricher=True, fila_enricher='manual'):
        hidratados.append((cnj, com_enricher, fila_enricher))
        return {'estado': 'criado', 'enricher_enfileirado': com_enricher}

    from datajud.management.commands import hidratar_recorte_acervo as cmd
    monkeypatch.setattr(cmd, 'iterar_esqueletos', fake_iterar)
    monkeypatch.setattr(cmd, 'hidratar_cnj', fake_hidratar)
    return chamadas, hidratados


def _ja_existe():
    t, _ = Tribunal.objects.get_or_create(sigla='TJAL', defaults={'nome': 'TJAL'})
    Process.objects.create(tribunal=t, numero_cnj='0500205-98.2024.8.02.0001',
                           classificacao=Process.CLASSIF_PRECATORIO)


def _run(*extra):
    out = StringIO()
    call_command('hidratar_recorte_acervo', '--tribunal', 'TJAL', '--classe', '1298',
                 '--prefixo', '050', '--excluir-foro', '9003', '--pausa', '0',
                 '--json', *extra, stdout=out)
    return [json.loads(line) for line in out.getvalue().splitlines()
            if line.startswith('{')], out.getvalue()


@pytest.mark.django_db
def test_dry_run_mede_e_nao_hidrata(acervo):
    chamadas, hidratados = acervo
    _ja_existe()
    (medida,), texto = _run()
    assert chamadas == {'sigla': 'TJAL', 'classe': '1298', 'prefixo': '050',
                        'excluir_foros': ('9003',)}
    assert medida['documentos'] == 4
    assert medida['cnjs_distintos'] == 3
    assert medida['ja_no_acervo'] == 1
    assert medida['ja_no_acervo_por_classificacao'] == {'PRECATORIO': 1}
    assert medida['faltam'] == 2
    assert hidratados == []
    assert 'DRY-RUN' in texto


@pytest.mark.django_db
def test_executar_hidrata_so_os_que_faltam_na_fila_do_tribunal(acervo):
    _, hidratados = acervo
    _ja_existe()
    (_, resultado), _ = _run('--executar')
    assert sorted(c for c, _, _ in hidratados) == [
        '0500004-77.2024.8.02.0043', '0500008-51.2023.8.02.0043']
    assert {(e, f) for _, e, f in hidratados} == {(True, 'tribunal')}
    assert resultado['hidratados'] == {'criado': 2}
    assert resultado['restam'] == 0


@pytest.mark.django_db
def test_limite_e_teto_declarado(acervo):
    _, hidratados = acervo
    (_, resultado), texto = _run('--executar', '--limite', '1')
    assert len(hidratados) == 1
    assert resultado['restam'] == 2
    assert 'TETO --limite=1 atingido' in texto


@pytest.mark.django_db
def test_fila_nenhuma_nao_enfileira_enricher(acervo):
    _, hidratados = acervo
    _run('--executar', '--fila-enricher', 'nenhuma')
    assert all(com is False for _, com, _ in hidratados)


def test_iterar_esqueletos_pagina_com_search_after(monkeypatch):
    paginas = [
        {'hits': {'hits': [{'_source': {'proc': 'a'}, 'sort': ['a']},
                           {'_source': {'proc': 'b'}, 'sort': ['b']}]}},
        {'hits': {'hits': [{'_source': {'proc': 'c'}, 'sort': ['c']}]}},
        {'hits': {'hits': []}},
    ]
    pedidos = []

    class FakeES:
        def search(self, **kw):
            pedidos.append(kw)
            return paginas[len(pedidos) - 1]

    monkeypatch.setattr(hidratacao, 'get_es', lambda: FakeES())
    got = [s['proc'] for s in hidratacao.iterar_esqueletos(
        'tjal', 1298, '050', ('9003',), pagina=2)]
    assert got == ['a', 'b', 'c']
    assert 'search_after' not in pedidos[0]
    assert pedidos[1]['search_after'] == ['b']
    assert pedidos[2]['search_after'] == ['c']
    filtros = pedidos[0]['query']['bool']['filter']
    assert {'prefix': {'proc': '050'}} in filtros
    assert {'term': {'tribunal': 'TJAL'}} in filtros
    assert pedidos[0]['query']['bool']['must_not'] == [{'wildcard': {'proc': '*.9003'}}]


def test_fila_enricher_invalida():
    with pytest.raises(ValueError):
        hidratacao.hidratar_cnj('0500008-51.2023.8.02.0043', fila_enricher='x')
