"""A varredura do Datajud, com foco no que quebra silenciosamente.

O risco desta peça não é dar erro — é varrer 343M docs e perder uma parte
sem ninguém notar. Os testes abaixo cercam exatamente isso:

  - paginar por chave NÃO-ÚNICA (`@timestamp`) não pode pular documento;
  - um milissegundo mais cheio que a página não pode travar o laço nem sumir
    com os docs em silêncio;
  - passada filtrada (nicho) NÃO pode salvar watermark — ela viu só um recorte
    do tempo, e gravar o cursor faria a varredura completa pular esse trecho.
"""
import pytest

from datajud import varredura as V


class FakeES:
    """ES de mentira que guarda os docs por _id — é assim que a idempotência
    da releitura de cauda fica visível: reindexar o mesmo id sobrescreve."""

    def __init__(self):
        self.docs = {}

    def index(self, **kw):
        self.docs[kw['id']] = kw['document']


def fake_bulk(es, acoes, **kw):
    n = 0
    for a in acoes:
        es.docs[a['_id']] = a['_source']
        n += 1
    return n, []


def src(cnj_seq, ts, grau='G1', classe=12078, tribunal='TJMG'):
    """_source no formato do Datajud (só o que a varredura lê)."""
    num = f'{cnj_seq:07d}8920228130024'
    return {
        'id': f'{tribunal}_{grau}_{num}',
        'numeroProcesso': num,
        'tribunal': tribunal,
        'grau': grau,
        'nivelSigilo': 0,
        'dataAjuizamento': '20221021175103',
        'dataHoraUltimaAtualizacao': '2026-08-04T11:42:06.483000Z',
        'classe': {'codigo': classe, 'nome': 'Cumprimento de Sentença contra a Fazenda Pública'},
        'assuntos': [{'codigo': 9419, 'nome': 'Execução Previdenciária'}],
        'orgaoJulgador': {'codigo': 5361, 'nome': '1ª Vara Cível', 'codigoMunicipioIBGE': 3113404},
        'sistema': {'codigo': 1, 'nome': 'PJe'},
        'formato': {'codigo': 1, 'nome': 'Eletrônico'},
        '_ts': ts,
    }


class FakeDatajud:
    """Datajud de mentira com o comportamento REAL que medimos:

    - só ordena por `@timestamp` (aqui, o `_ts` de cada doc);
    - devolve no máximo `pagina` docs;
    - responde `range gte`, `term @timestamp`, `term grau`, `term classe.codigo`
      e agregação por classe — que é tudo que a varredura usa.
    """

    def __init__(self, docs, pagina=3):
        self.docs = sorted(docs, key=lambda d: d['_ts'])
        self.pagina = pagina
        self.chamadas = 0

    def _casa(self, d, query):
        if 'bool' in query:
            b = query['bool']
            for c in b.get('must', []) + b.get('filter', []):
                if not self._casa(d, c):
                    return False
            for c in b.get('must_not', []):
                if self._casa(d, c):
                    return False
            return True
        if 'match_all' in query:
            return True
        if 'range' in query:
            return d['_ts'] >= query['range']['@timestamp']['gte']
        if 'term' in query:
            campo, valor = next(iter(query['term'].items()))
            if campo == '@timestamp':
                return d['_ts'] == valor
            if campo == 'grau':
                return d['grau'] == valor
            if campo == 'classe.codigo':
                return str(d['classe']['codigo']) == str(valor)
        if 'terms' in query:
            campo, valores = next(iter(query['terms'].items()))
            if campo == 'grau':
                return d['grau'] in valores
        return False

    def _post(self, sigla, body, cota=None):
        self.chamadas += 1
        casam = [d for d in self.docs if self._casa(d, body['query'])]
        if body.get('aggs'):
            porc = {}
            for d in casam:
                porc[str(d['classe']['codigo'])] = porc.get(str(d['classe']['codigo']), 0) + 1
            return {'hits': {'total': {'value': len(casam)}, 'hits': []},
                    'aggregations': {'c': {'buckets': [
                        {'key': k, 'doc_count': v} for k, v in porc.items()]}}}
        if body.get('size') == 0:
            return {'hits': {'total': {'value': len(casam)}, 'hits': []}}
        size = min(body['size'], self.pagina)
        desde = body.get('from', 0)
        janela = casam[desde:desde + size]
        return {'hits': {'total': {'value': len(casam)},
                         'hits': [{'_source': d, 'sort': [d['_ts']]}
                                  for d in janela]}}


@pytest.fixture
def sem_es(monkeypatch):
    es = FakeES()
    monkeypatch.setattr(V, 'bulk', fake_bulk)
    monkeypatch.setattr(V, 'ensure_index', lambda *a, **k: None)
    monkeypatch.setattr(V, 'index_name', lambda s: f'voyager-{s}')
    return es


def varredura(docs, es, pagina=3, teto_ms=V.TETO_JANELA):
    return V.Varredura('TJMG', client=FakeDatajud(docs, pagina=pagina),
                       pagina=pagina, es=es, teto_ms=teto_ms)


def test_varre_tudo_sem_pular_nem_duplicar(sem_es):
    """10 docs, página de 3, timestamps distintos: tem que sair 10 — nem 9 nem 11.

    Como a paginação relê a cauda de propósito (`gte cursor`, não `gt`), sem
    idempotência por _id isso daria docs repetidos.
    """
    docs = [src(i, 1000 + i) for i in range(10)]
    v = varredura(docs, sem_es)
    r = v.rodar()
    assert len(sem_es.docs) == 10
    assert r['perdidos'] == 0


def test_empate_de_timestamp_nao_perde_doc(sem_es):
    """O caso que `search_after` puro quebraria: 4 docs no MESMO milissegundo
    com página de 3. O 4º está do outro lado da borda da página."""
    docs = [src(i, 5000) for i in range(4)] + [src(9, 6000)]
    v = varredura(docs, sem_es)
    r = v.rodar()
    assert len(sem_es.docs) == 5, 'perdeu doc no empate de milissegundo'
    assert r['perdidos'] == 0


def test_ms_lotado_e_fatiado_por_grau(sem_es):
    """Milissegundo com mais docs que a página inteira: o laço não pode travar,
    e o fatiamento por grau tem que dar conta."""
    docs = ([src(i, 7000, grau='G1') for i in range(3)]
            + [src(i + 100, 7000, grau='G2') for i in range(3)]
            + [src(200, 8000)])
    v = varredura(docs, sem_es, pagina=4)
    r = v.rodar()
    assert len(sem_es.docs) == 7
    assert r['perdidos'] == 0


def test_perda_no_ms_e_declarada_nao_escondida(sem_es):
    """Quando nem o fatiamento resolve, a varredura CONTA o que ficou de fora.

    5 docs de mesmo grau e mesma classe no mesmo milissegundo, com o teto da
    janela em 2: é matematicamente impossível vê-los todos (no Datajud real o
    teto é `from+size <= 10.000`). O contrato aqui é declarar a perda, não
    fingir completude — um `perdidos: 0` mentiroso é pior que a perda.
    """
    docs = [src(i, 9000, grau='G1', classe=12078) for i in range(5)]
    v = varredura(docs, sem_es, pagina=2, teto_ms=2)
    r = v.rodar()
    assert r['perdidos'] > 0, 'perdeu doc e não avisou'


def test_ms_lotado_resolvido_por_from_sem_fatiar(sem_es):
    """5 docs no mesmo ms com página de 2 e teto de 10k: `from`+`size` dá conta
    sozinho, sem precisar fatiar por grau/classe — e sem perder nada."""
    docs = [src(i, 9000) for i in range(5)] + [src(90, 9500)]
    v = varredura(docs, sem_es, pagina=2)
    r = v.rodar()
    assert len(sem_es.docs) == 6
    assert r['perdidos'] == 0


def test_cursor_avanca_e_permite_retomar(sem_es):
    """O cursor devolvido tem que servir de watermark: retomar dali não pode
    reprocessar o acervo inteiro nem pular o que veio depois."""
    docs = [src(i, 1000 + i) for i in range(6)]
    v1 = varredura(docs, sem_es)
    r1 = v1.rodar(max_paginas=1)
    assert r1['cursor'] > 0

    novos = docs + [src(50, 9999)]
    es2 = FakeES()
    v2 = V.Varredura('TJMG', client=FakeDatajud(novos, pagina=3), pagina=3, es=es2)
    r2 = v2.rodar(cursor=9999)
    assert len(es2.docs) == 1, 'retomada trouxe o que já tinha sido varrido'
    assert r2['lidos'] == 1


def test_doc_do_datajud_traduz_os_campos_que_importam():
    d = V.doc_do_datajud(src(1, 100))
    assert d is not None
    _id, doc = d
    assert doc['proc'] == '0000001-89.2022.8.13.0024'
    assert doc['proc_digits'] == '00000018920228130024'
    assert doc['classe_codigo'] == '12078'
    assert doc['assunto_codigos'] == ['9419']
    assert doc['ano_cnj'] == 2022
    assert doc['uf'] == 'MG'
    # data no formato de 14 dígitos vira ISO — senão o campo `date` do ES recusa
    assert doc['ajuizado_em'].startswith('2022-10-21T17:51:03')


def test_doc_sem_cnj_valido_e_descartado():
    ruim = src(1, 100)
    ruim['numeroProcesso'] = '123'
    assert V.doc_do_datajud(ruim) is None


def test_assuntos_como_lista_de_listas_nao_quebra():
    """Visto em campo no TJSP: `assuntos` vindo aninhado. Antes disso, a
    varredura estourava AttributeError no meio de uma página de 10k."""
    s = src(1, 100)
    s['assuntos'] = [[{'codigo': 1, 'nome': 'A'}], {'codigo': 2, 'nome': 'B'}]
    _id, doc = V.doc_do_datajud(s)
    assert doc['assunto_codigos'] == ['1', '2']


@pytest.mark.django_db
def test_passada_filtrada_nao_salva_watermark(sem_es, monkeypatch):
    """Varrer só o nicho vê um recorte do tempo. Se isso gravasse o cursor, a
    varredura completa pularia todo o intervalo — perda silenciosa e definitiva.
    """
    from tribunals.models import Tribunal
    t, _ = Tribunal.objects.get_or_create(sigla='TJMG', defaults={'nome': 'TJ Minas', 'sigla_djen': 'TJMG'})

    docs = [src(i, 1000 + i) for i in range(4)]
    pronta = varredura(docs, sem_es)          # instancia ANTES de trocar a classe
    monkeypatch.setattr(V, 'Varredura', lambda *a, **k: pronta)
    V.varrer_tribunal('TJMG', filtro={'term': {'classe.codigo': 12078}})
    t.refresh_from_db()
    assert t.datajud_varredura_cursor is None
    # `docs` conta ESCRITAS, não documentos distintos: a paginação relê a cauda
    # de propósito (`gte`, não `gt`), então o número é teto, nunca contagem. A
    # contagem exata é um `_count` no próprio índice.
    assert t.datajud_varredura_docs >= 4
