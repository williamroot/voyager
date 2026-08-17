"""Testes dos coletores de diário oficial de ENTE DEVEDOR (`diarios_entes/`).

Tudo aqui roda contra material REAL capturado das fontes em 16/08/2026 — as
respostas JSON, o `.txt` de 482.437 chars da gazeta de Maceió e, de propósito,
as três CASCAS que devolvem HTTP 200 sem dado nenhum. Nenhum mock de fantasia:
o que a fonte devolve é o que está em `tests/fixtures/diarios/doe_entes/`.

Os três testes que a arquitetura pediu nominalmente estão marcados com
[ACEITE] na docstring:
  · o parser acha os CNJs da tabela de Maceió;
  · `Terms` x `SearchTerms` no DOE-SP (117 x 487.579) — o parâmetro errado é
    ignorado em silêncio e devolve o universo;
  · a SPA catch-all do RS (2.700 bytes de index.html com HTTP 200) é rejeitada.
"""

import json
import os
from datetime import date

import pytest

from diarios.base import RespostaInvalida, UnidadeColeta, UnidadeInexistente, achar_cnjs
from diarios_entes.coletor import (
    CONFIANCA_ALTA,
    CONFIANCA_BAIXA,
    ItemEnte,
    dobrar,
    exigir_json,
    exigir_texto,
    html_para_texto,
    janela_de_texto,
)
from diarios_entes.fontes.doe_sp import DoeSpColetor, _exigir_busca_por_termo
from diarios_entes.fontes.querido_diario import QueridoDiarioColetor
from diarios_entes.models import ESFERA_ESTADUAL, ESFERA_MUNICIPAL, PublicacaoOficial

FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures', 'diarios', 'doe_entes')

#: A tabela de convocação da Câmara de Conciliação de Precatórios de Maceió
#: (gazeta de 30/04/2026, edição 7397). É o gabarito do parser: nome do credor
#: + CNJ do precatório + horário da sessão. Copiado verbatim do `.txt`.
CNJS_MACEIO = [
    '0501276-27.2026.8.02.9003',   # Maicon dos Santos Freitas, 09:00, sala 1
    '0501769-38.2025.8.02.9003',   # Janny Karla de Mendonça Silva, 09:10, sala 1
    '0503634-96.2025.8.02.9003',   # Kelly Silva Marques Viana, 09:20, sala 1
]


def caminho(nome: str) -> str:
    return os.path.join(FIXTURES, nome)


def ler_bytes(nome: str) -> bytes:
    with open(caminho(nome), 'rb') as fh:
        return fh.read()


def ler_json(nome: str) -> dict:
    return json.loads(ler_bytes(nome))


class RespostaFalsa:
    """O mínimo de `requests.Response` que os validadores olham.

    De propósito guarda os BYTES do arquivo capturado: os testes de casca
    dependem do Content-Type e do tamanho reais (20.943 e 2.700 bytes), não de
    uma string inventada.
    """

    def __init__(self, corpo: bytes, content_type: str = 'application/json'):
        self.content = corpo
        self.status_code = 200
        self.headers = {'Content-Type': content_type}

    @property
    def text(self) -> str:
        return self.content.decode('utf-8', errors='replace')

    def json(self):
        return json.loads(self.text)


# ─────────────────────────────────────────────────────────────────────────────
# 1. O achado: a tabela de precatórios dentro da gazeta municipal
# ─────────────────────────────────────────────────────────────────────────────
def test_parser_acha_os_cnjs_da_tabela_de_maceio():
    """[ACEITE] O que justifica a fonte inteira.

    A gazeta é o diário municipal INTEIRO (482.437 chars de PROCON, licitação e
    decreto). Dentro dele, uma tabela PARTE x PRECATÓRIO Nº x HORÁRIO com os
    credores convocados para acordo direto. Se o parser não achar esses números,
    a fonte não entrega nada — é só peso de download.
    """
    texto = ler_bytes('qd_gazeta_amostra.txt').decode('utf-8')
    achados = achar_cnjs(texto)
    for cnj in CNJS_MACEIO:
        assert cnj in achados, f'CNJ da tabela de convocação não foi achado: {cnj}'
    assert len(achados) >= 40, f'a convocação tem dezenas de precatórios, achou {len(achados)}'


def test_janela_preserva_a_tabela_verbatim_e_descarta_o_resto():
    """O recorte não pode cortar o que interessa nem guardar o diário inteiro.

    Guardar os 482.437 chars de cada gazeta (média de 772.500; a do Rio de
    30/07/2026 tem 6,6 MB) num Postgres já disk-I/O-bound é caro e inútil: 99%
    é assunto de outro órgão. A janela tem que conter a tabela COMPLETA — nome
    do credor colado no número do precatório — e ser uma fração do documento.
    """
    texto = ler_bytes('qd_gazeta_amostra.txt').decode('utf-8')
    trecho, inteiro = janela_de_texto(texto, ['precatori'])

    assert not inteiro
    assert len(trecho) < len(texto) * 0.10, 'a janela não pode virar o diário inteiro'
    assert 'CÂMARA DE CONCILIAÇÃO DE PRECATÓRIOS' in trecho
    # nome + número na mesma linha: é o par que vira lead
    assert 'Maicon dos Santos Freitas 0501276-27.2026.8.02.9003' in trecho
    for cnj in CNJS_MACEIO:
        assert cnj in trecho


def test_dobrar_preserva_o_comprimento():
    """Invariante que sustenta o recorte: a busca é feita numa cópia sem acento
    e os índices dela são usados para fatiar o texto ORIGINAL. Se a dobra mudar
    o comprimento, o recorte sai deslocado e o verbatim vira lixo."""
    original = 'CÂMARA DE CONCILIAÇÃO — PRECATÓRIOS Nº 0501276-27.2026.8.02.9003'
    assert len(dobrar(original)) == len(original)
    assert 'camara de conciliacao' in dobrar(original)


# ─────────────────────────────────────────────────────────────────────────────
# 2. As cascas: HTTP 200 que não é dado
# ─────────────────────────────────────────────────────────────────────────────
def test_rejeita_spa_catch_all_do_querido_diario():
    """`queridodiario.ok.org.br/api/gazettes` (host do SITE, não da API) devolve
    HTTP 200 com 20.943 bytes da SPA Angular. A API é `api.queridodiario...`.
    Um health-check por status code aprovaria isso."""
    corpo = ler_bytes('qd_spa_casca.html')
    assert len(corpo) > 20000
    with pytest.raises(RespostaInvalida):
        exigir_json(RespostaFalsa(corpo, 'text/html; charset=UTF-8'), contexto='QD casca')


def test_rejeita_spa_catch_all_do_rs():
    """[ACEITE] `diariooficial.rs.gov.br/doe/materias/feed/rss.xml` — caminho
    ANUNCIADO pelo próprio site no <link rel="alternate"> — devolve 200 com
    2.700 bytes de index.html. Qualquer path inventado devolve o mesmo. É o
    erro dos '180 milhões de PDFs' em miniatura."""
    corpo = ler_bytes('rs_spa_casca.html')
    assert len(corpo) < 3000
    with pytest.raises(RespostaInvalida):
        exigir_json(RespostaFalsa(corpo, 'text/html'), contexto='RS rss')
    # e como "texto de gazeta" também não passa
    with pytest.raises(RespostaInvalida):
        exigir_texto(corpo, contexto='RS rss')


def test_texto_de_gazeta_de_verdade_passa():
    assert len(exigir_texto(ler_bytes('qd_gazeta_amostra.txt'), contexto='Maceió')) > 400_000


def test_aspas_so_em_frase_no_querido_diario(monkeypatch):
    """REGRESSÃO 16/08/2026: a aspa em TERMO SOLTO custava 66% do recall.

    A medição que justificava as aspas foi feita numa FRASE ('câmara de
    conciliação de precatórios': 64 com aspas contra 10.000 sem, porque sem elas o
    OpenSearch vira OR e casa o país) e valia só para ela. Em termo solto era o
    inverso: 12 dias corridos na API viva deram 14 gazetas com aspas contra 41
    sem — e as 8 diferenças auditadas continham 'precatóri' verbatim. A perda
    acontecia ANTES de o coletor ver o documento: sem log, sem alerta, sem
    contador, com o dia fechando `vazia` no watermark.

    Este teste olha o parâmetro que SAI no request, que é onde o defeito vivia.
    """
    coletor = QueridoDiarioColetor()
    pedidos = []

    def falsa_get(url, **kw):
        pedidos.append((kw.get('params') or {}).get('querystring'))
        return RespostaFalsa(b'{"total_gazettes": 0, "gazettes": []}')

    monkeypatch.setattr(coletor.sessao, 'get', falsa_get)
    coletor._buscar('precatório', date(2026, 8, 13))
    coletor._buscar('câmara de conciliação de precatórios', date(2026, 8, 13))

    assert pedidos == ['precatório', '"câmara de conciliação de precatórios"']


# ─────────────────────────────────────────────────────────────────────────────
# 3. DOE-SP: a armadilha do parâmetro ignorado
# ─────────────────────────────────────────────────────────────────────────────
def test_terms_e_searchterms_nao_sao_a_mesma_coisa():
    """[ACEITE] `SearchTerms` é IGNORADO e devolve o universo.

    Mesmo período, mesma palavra, mesma API:
        Terms=precatório        → totalItems 117
        SearchTerms=precatório  → totalItems 487.579  (o ano inteiro do DOE-SP)
    Erro de NOME de parâmetro não dá erro — dá um número 4.000x maior. Por isso
    o teste assere CONTEÚDO: com `Terms` o termo aparece no excerpt de todos os
    itens; com `SearchTerms`, de nenhum.
    """
    certo = ler_json('doesp_precatorio_2026.json')
    errado = ler_json('doesp_searchterms_universo.json')
    assert certo['totalItems'] == 117
    assert errado['totalItems'] == 487_579

    com_termo = sum(1 for i in certo['items'] if 'precatori' in dobrar(i.get('excerpt') or ''))
    sem_termo = sum(1 for i in errado['items'] if 'precatori' in dobrar(i.get('excerpt') or ''))
    assert com_termo == len(certo['items'])
    assert sem_termo == 0

    # e o coletor tem que RECUSAR o payload do parâmetro ignorado
    _exigir_busca_por_termo(certo['items'], 'precatório', contexto='fixture Terms')
    with pytest.raises(RespostaInvalida):
        _exigir_busca_por_termo(errado['items'], 'precatório', contexto='fixture SearchTerms')


def test_busca_do_dia_inteiro_tambem_e_recusada_como_busca_por_termo():
    """A outra forma do mesmo bug: um dia inteiro do DOE-SP (3.164 publicações
    de 14/08/2026) não é resultado de busca por precatório. Se o filtro cair, é
    isso que chega — e sem a guarda entraria tudo."""
    dia = ler_json('doesp_dia_2026-08-14_pg1.json')
    assert dia['totalItems'] == 3164
    with pytest.raises(RespostaInvalida):
        _exigir_busca_por_termo(dia['items'], 'precatório', contexto='dia inteiro')


def test_publicacao_aleatoria_do_doe_sp_nao_tem_cnj():
    """Trava a expectativa: DOE de ente NÃO é um DJEN paralelo.

    Nas 100 publicações da primeira página de 14/08/2026 (concurso, licitação,
    ato de pessoal) não há um único CNJ. Quem vender esta fonte como 'segunda
    porta do acervo' vai quebrar este teste — que é o objetivo.
    """
    dia = ler_json('doesp_dia_2026-08-14_pg1.json')
    com_cnj = [i for i in dia['items'] if achar_cnjs(f'{i.get("title")} {i.get("excerpt")}')]
    assert com_cnj == []


def test_apostila_de_acao_judicial_traz_cnj_e_orgao():
    """O filão da fonte: o Estado averbando que cumpriu decisão judicial.
    Traz CNJ, vara e nome — sinal direto para o Estágio do Crédito."""
    pub = ler_json('doesp_publicacao_precatorio_com_cnj.json')
    texto = html_para_texto(pub['content'])
    achados = achar_cnjs(texto)
    assert '0064062-18.2011.8.26.0114' in achados
    assert len(achados) >= 4
    assert 'precatório' in texto.lower()
    assert '<div' not in texto and 'style=' not in texto, 'o extrator lê texto, não tag'


# ─────────────────────────────────────────────────────────────────────────────
# 4. Os coletores, ponta a ponta, com as respostas reais das fontes
# ─────────────────────────────────────────────────────────────────────────────
def _qd_com_respostas_reais(monkeypatch, dia_vazio: bool = False) -> QueridoDiarioColetor:
    """Coletor do QD servido pelas respostas capturadas de 30/04/2026.

    O roteador reproduz o que a fonte FEZ naquele dia, conferido consulta a
    consulta ao vivo: das 7 consultas, só 'câmara de conciliação de precatórios'
    e 'precatório' casaram — e casaram a MESMA gazeta. É o caminho que exercita
    a união por gazeta (1 documento, 2 consultas).
    """
    coletor = QueridoDiarioColetor()
    busca = ler_bytes('qd_busca_dia_2026-04-30.json')
    vazio = ler_bytes('qd_dia_vazio_2026-08-16.json')
    gazeta = ler_bytes('qd_gazeta_amostra.txt')
    # FRASE entre aspas, TERMO SOLTO nu — é assim que a fonte é consultada desde
    # 16/08/2026 (a aspa em termo solto custava 66% do recall; ver
    # `querido_diario._buscar`). O roteador reproduz a chave EXATA que sai no
    # request, então ele também é a trava dessa correção.
    casaram = {'"câmara de conciliação de precatórios"', 'precatório'}

    def falsa_get(url, **kw):
        params = kw.get('params') or {}
        if url.startswith('https://data.queridodiario'):
            return RespostaFalsa(gazeta, 'text/plain; charset=utf-8')
        if 'querystring' in params:
            if not dia_vazio and params['querystring'] in casaram:
                return RespostaFalsa(busca)
            return RespostaFalsa(b'{"total_gazettes": 0, "gazettes": []}')
        return RespostaFalsa(vazio)   # sondagem "houve diário hoje?"

    monkeypatch.setattr(coletor.sessao, 'get', falsa_get)
    return coletor


def test_qd_coleta_a_gazeta_de_maceio_com_os_campos_que_a_fonte_da(monkeypatch):
    coletor = _qd_com_respostas_reais(monkeypatch)
    unidade = UnidadeColeta(chave='2026-04-30', data=date(2026, 4, 30))
    itens = list(coletor.coletar(unidade))

    assert len(itens) == 1, 'a mesma gazeta casada por 2 consultas é UM documento'
    item = itens[0]
    assert item.ente == 'Maceió'
    assert (item.uf, item.territory_id) == ('AL', '2704302')   # IBGE casa com o SICONFI
    assert item.data_publicacao == date(2026, 4, 30)
    assert item.esfera == ESFERA_MUNICIPAL
    assert item.edicao == '7397'
    assert item.link_texto.endswith('.txt')
    assert item.external_id == 'qd-municipal:2704302-23d74df838cb'
    assert item.consultas == ['câmara de conciliação de precatórios', 'precatório']
    assert item.confianca == CONFIANCA_ALTA, 'casou FRASE, não só o termo solto'
    assert item.texto_integral_chars == 482_437
    for cnj in CNJS_MACEIO:
        assert cnj in item.cnjs


def test_qd_abstem_no_que_a_fonte_nao_da(monkeypatch):
    """Campo vazio honesto > campo chutado. O Querido Diário não diz órgão nem
    tipo de documento (a unidade dele é a gazeta inteira, não o ato), então
    esses campos ficam VAZIOS em vez de receberem um rótulo inventado."""
    coletor = _qd_com_respostas_reais(monkeypatch)
    item = next(iter(coletor.coletar(UnidadeColeta(chave='x', data=date(2026, 4, 30)))))
    assert item.orgao == ''
    assert item.tipo_documento == ''


def test_qd_dia_sem_diario_e_inexistente_e_nao_falha(monkeypatch):
    """Domingo 16/08/2026: a API devolve `total_gazettes: 0` para o país todo.
    Isso é AUSÊNCIA, não falha — o watermark fecha como `inexistente` e nunca
    mais retenta. É a lição do `_dia_coberto` do DJEN, que retentava para
    sempre o dia que simplesmente não existia."""
    coletor = _qd_com_respostas_reais(monkeypatch, dia_vazio=True)
    with pytest.raises(UnidadeInexistente):
        list(coletor.coletar(UnidadeColeta(chave='2026-08-16', data=date(2026, 8, 16))))


def test_qd_catalogo_nao_toca_a_rede():
    """Catalogar é barato de propósito: 1 unidade por dia, zero request. O caro
    (download de 100 kB a 6,6 MB por gazeta) só acontece na coleta."""
    coletor = QueridoDiarioColetor()
    unidades = list(coletor.catalogar(date(2026, 4, 28), date(2026, 4, 30)))
    assert [u.chave for u in unidades] == ['2026-04-28', '2026-04-29', '2026-04-30']
    assert all(u.tribunal_sigla is None for u in unidades)   # ente não tem tribunal


def _doesp_com_respostas_reais(monkeypatch) -> DoeSpColetor:
    coletor = DoeSpColetor()
    busca = ler_bytes('doesp_busca_dia_2026-08-14_terms.json')
    detalhe = ler_bytes('doesp_apostila_delegado_2026-08-14.json')

    def falsa_get(url, **kw):
        params = kw.get('params') or {}
        if '/v2/publications/' in url:
            return RespostaFalsa(detalhe)
        if params.get('Terms') == 'precatório':
            return RespostaFalsa(busca)
        if 'Terms' in params:
            return RespostaFalsa(b'{"items": [], "totalItems": 0, "currentPage": 1, '
                                 b'"totalPages": 0, "hasNextPage": false}')
        return RespostaFalsa(b'{"totalItems": 3164}')

    monkeypatch.setattr(coletor.sessao, 'get', falsa_get)
    return coletor


def test_doesp_coleta_apostila_com_cnj(monkeypatch):
    coletor = _doesp_com_respostas_reais(monkeypatch)
    unidade = UnidadeColeta(chave='2026-08-14', data=date(2026, 8, 14),
                            meta={'publicacoes_no_dia': 3164})
    itens = list(coletor.coletar(unidade))

    assert len(itens) == 1, '1 publicação em 3.164 — é isso que a fonte entrega'
    item = itens[0]
    assert item.esfera == ESFERA_ESTADUAL
    assert (item.ente, item.uf) == ('Estado de São Paulo', 'SP')
    assert item.territory_id == '', 'IBGE de 7 dígitos é de município: abster no estadual'
    assert item.data_publicacao == date(2026, 8, 14)
    assert item.tipo_documento == 'Apostila'
    assert 'Secretaria da Segurança Pública' in item.orgao
    assert item.link.startswith('https://doe.sp.gov.br/executivo/')
    assert item.external_id == 'doe-sp:53f13aa6-7fdb-4626-20c1-08def9293e7f'
    assert item.confianca == CONFIANCA_BAIXA, 'casou termo solto, não frase'
    assert '0002003-35.2026.8.26.0189' in item.cnjs
    assert item.cpfs_no_texto >= 1
    assert item.texto_completo, 'a publicação do DOE-SP cabe inteira (nada de janela)'


def test_doesp_catalogo_pula_dia_sem_diario(monkeypatch):
    """Sábado/domingo devolvem `totalItems: 0` e o dia NEM VIRA unidade — não
    há o que retentar depois."""
    coletor = DoeSpColetor()
    monkeypatch.setattr(coletor.sessao, 'get',
                        lambda url, **kw: RespostaFalsa(b'{"totalItems": 0}'))
    assert list(coletor.catalogar(date(2026, 8, 15), date(2026, 8, 16))) == []


def test_doesp_ignora_publicacao_sem_o_termo_no_corpo(monkeypatch):
    """Confirmação no CORPO, não na contagem. Se o detalhe vier sem o termo que
    a busca prometeu, é ruído (ou contrato quebrado) e não entra."""
    coletor = DoeSpColetor()
    outro = json.dumps({
        'id': 'x', 'date': '2026-08-14T00:00:00', 'title': 'Portaria',
        'slug': 'executivo/portaria-x', 'content': '<p>Designa servidor para função.</p>',
    }).encode()

    def falsa_get(url, **kw):
        if '/v2/publications/' in url:
            return RespostaFalsa(outro)
        if (kw.get('params') or {}).get('Terms') == 'precatório':
            return RespostaFalsa(ler_bytes('doesp_busca_dia_2026-08-14_terms.json'))
        return RespostaFalsa(b'{"items": [], "totalItems": 0, "currentPage": 1, '
                             b'"totalPages": 0, "hasNextPage": false}')

    monkeypatch.setattr(coletor.sessao, 'get', falsa_get)
    itens = list(coletor.coletar(UnidadeColeta(chave='2026-08-14', data=date(2026, 8, 14),
                                               meta={'publicacoes_no_dia': 3164})))
    assert itens == []


def test_janelas_sao_medidas_e_nao_chutadas():
    """As duas datas foram medidas ao vivo em 16/08/2026:
      · QD: gazeta mais antiga do acervo = Cuiabá/MT 02/01/1990
        (`sort_by=ascending_date`);
      · DOE-SP: bissecção dia a dia — 09/07/2023 devolve totalItems=0,
        10/07/2023 devolve 2.473, e todo mês anterior a julho/2023 devolve 0.
    Sem `janela_fim`: as duas são fontes correntes, e não disputam janela com o
    DJEN (universos disjuntos — Executivo x Judiciário)."""
    assert QueridoDiarioColetor.janela_inicio == date(1990, 1, 2)
    assert DoeSpColetor.janela_inicio == date(2023, 7, 10)
    assert QueridoDiarioColetor.janela_fim is None
    assert DoeSpColetor.janela_fim is None


def test_gabarito_da_fonte_nao_vira_gate_do_runner():
    """O DOE-SP declara 3.164 publicações no dia e nós gravamos 1. Se
    `esperado()` devolvesse o total da fonte, o gate de cobertura do runner
    reprovaria 100% das coletas CORRETAS."""
    assert DoeSpColetor().esperado(UnidadeColeta(chave='x', data=date(2026, 8, 14),
                                                 meta={'publicacoes_no_dia': 3164})) is None


# ─────────────────────────────────────────────────────────────────────────────
# 5. Persistência: model próprio, idempotência e vínculo que não chuta
# ─────────────────────────────────────────────────────────────────────────────
def _item_maceio(monkeypatch) -> ItemEnte:
    coletor = _qd_com_respostas_reais(monkeypatch)
    return next(iter(coletor.coletar(UnidadeColeta(chave='2026-04-30', data=date(2026, 4, 30)))))


@pytest.mark.django_db
def test_persiste_em_model_proprio_e_nao_em_movimentacao(monkeypatch):
    """A decisão de modelo, travada em teste: `Movimentacao.tribunal` é FK NOT
    NULL e publicação do Executivo não tem tribunal. Gravar lá com tribunal
    sintético contaminaria heatmap de saúde, lag por tribunal e pipeline diário."""
    from tribunals.models import Movimentacao

    coletor = QueridoDiarioColetor()
    item = _item_maceio(monkeypatch)
    antes = Movimentacao.objects.count()

    novas, dup = coletor.persistir([item], UnidadeColeta(chave='2026-04-30',
                                                         data=date(2026, 4, 30)), None)
    assert (novas, dup) == (1, 0)
    assert Movimentacao.objects.count() == antes, 'nada pode ter ido para Movimentacao'

    pub = PublicacaoOficial.objects.get(fonte='qd-municipal',
                                        external_id=item.external_id)
    assert (pub.ente, pub.uf, pub.territory_id) == ('Maceió', 'AL', '2704302')
    assert pub.confianca == CONFIANCA_ALTA
    assert CNJS_MACEIO[0] in pub.cnjs
    assert pub.texto_integral_chars == 482_437 > len(pub.texto)
    assert pub.link_texto.endswith('.txt'), 'o integral tem que continuar recuperável'


@pytest.mark.django_db
def test_recoleta_do_mesmo_dia_e_idempotente(monkeypatch):
    """Todo retry re-coleta o dia inteiro. Se duplicasse, um backfill de meses
    encheria a tabela de cópias — o external_id é derivado do sha1 do arquivo
    na fonte justamente para isso."""
    coletor = QueridoDiarioColetor()
    item = _item_maceio(monkeypatch)
    unidade = UnidadeColeta(chave='2026-04-30', data=date(2026, 4, 30))

    assert coletor.persistir([item], unidade, None) == (1, 0)
    assert coletor.persistir([item], unidade, None) == (0, 1)
    assert PublicacaoOficial.objects.filter(fonte='qd-municipal').count() == 1


@pytest.mark.django_db
def test_vincula_processo_existente_e_nao_cria_processo_novo(monkeypatch):
    """Vínculo OPORTUNISTA: liga ao `Process` que já está no acervo e ignora o
    resto. Criar processo a partir daqui exigiria inventar o `tribunal` (FK
    obrigatória) a partir dos dígitos J.TR — e um CNJ citado num edital do
    Executivo não prova sequer que o processo é nosso. Abster > chutar."""
    from tribunals.models import Process, Tribunal

    tribunal, _ = Tribunal.objects.get_or_create(
        sigla='TJAL', defaults={'nome': 'TJAL', 'sigla_djen': 'TJAL'})
    conhecido = Process.objects.create(tribunal=tribunal, numero_cnj=CNJS_MACEIO[0])
    total_antes = Process.objects.count()

    coletor = QueridoDiarioColetor()
    item = _item_maceio(monkeypatch)
    coletor.persistir([item], UnidadeColeta(chave='2026-04-30', data=date(2026, 4, 30)), None)

    pub = PublicacaoOficial.objects.get(external_id=item.external_id)
    assert list(pub.processos.values_list('pk', flat=True)) == [conhecido.pk]
    assert len(pub.cnjs) >= 40, 'os outros 40 CNJs continuam registrados no texto'
    assert Process.objects.count() == total_antes, 'nenhum processo foi inventado'


@pytest.mark.django_db
def test_publicacao_sem_cnj_entra_solta():
    """0 de 30 publicações aleatórias do DOE-SP têm CNJ. Se só entrasse o que
    tem CNJ, a fonte perderia o decreto de abertura de crédito para pagamento
    de precatório — que é sinal de desfecho mesmo sem citar processo."""
    coletor = DoeSpColetor()
    item = ItemEnte(
        external_id='doe-sp:sem-cnj-1', esfera=ESFERA_ESTADUAL, ente='Estado de São Paulo',
        uf='SP', data_publicacao=date(2026, 8, 14),
        titulo='Decreto de abertura de crédito suplementar',
        texto='Fica aberto crédito suplementar para pagamento de precatórios judiciais.',
    )
    assert coletor.persistir([item], UnidadeColeta(chave='2026-08-14',
                                                   data=date(2026, 8, 14)), None) == (1, 0)
    pub = PublicacaoOficial.objects.get(external_id='doe-sp:sem-cnj-1')
    assert pub.cnjs == []
    assert pub.processos.count() == 0


@pytest.mark.django_db
def test_as_duas_fontes_nao_colidem_no_external_id():
    """O namespace `<slug>:` do `diarios/base.py` é o que permite as duas portas
    (e as futuras) dividirem a mesma tabela sem uma engolir a outra."""
    qd, doesp = QueridoDiarioColetor(), DoeSpColetor()
    base = {'esfera': ESFERA_MUNICIPAL, 'ente': 'X',
            'data_publicacao': date(2026, 8, 14), 'texto': 't'}
    qd.persistir([ItemEnte(external_id='qd-municipal:2704302-aaaaaaaaaaaa', **base)],
                 UnidadeColeta(chave='k', data=date(2026, 8, 14)), None)
    doesp.persistir([ItemEnte(external_id='doe-sp:aaaaaaaaaaaa', **base)],
                    UnidadeColeta(chave='k', data=date(2026, 8, 14)), None)
    assert PublicacaoOficial.objects.count() == 2
    assert set(PublicacaoOficial.objects.values_list('fonte', flat=True)) == {'qd-municipal', 'doe-sp'}
