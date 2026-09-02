"""e-SAJ: o incidente é o precatório — e ele não tem CNJ.

## O achado (sonda ao vivo de 01-02/09/2026)

100 processos do TJSP amostrados por página aleatória do heap (sementes
20260901 e 20260902), estrato `tem_sinal_precatorio` — a fatia que o produto
vende. 90 respostas conclusivas, 53 delas no estrato do crédito:

  · **58,5%** dos processos do estrato carregam incidente vinculado;
  · foram lidos **210 incidentes**, e **201 (95,7%) são `Precatório` /
    `Requisição de Pequeno Valor` SEM número CNJ nenhum** — o e-SAJ os
    identifica por `<classe> (<CNJ do principal>) (<seq>)` e por um
    `processo.codigo` interno;
  · dos 9 que TÊM CNJ próprio (cumprimento de sentença, IDPJ), 3 estão no nosso
    acervo e 6 não estão. **Controle: 4 de 4 processos-pai presentes** — a
    amostra veio do nosso banco, então o controle TEM que dar 100%.

Processo sem CNJ não entra no DJEN nem no Datajud. A página do incidente é a
ÚNICA porta para ele — e é lá que está a ficha do crédito por beneficiário:
`Reqte` (quem recebe), `Ent. Devedora` (quem deve) e o valor requisitado.

## As três premissas que a sonda derrubou

1. **Captcha**: 5 de 5 páginas de incidente abriram pelo `show.do` sem
   `uuidCaptcha`. O comentário de `ESAJ_SEGUIR_INCIDENTES` afirmava o oposto.
2. **`#classeProcesso`**: 0 de 5. A promoção de classe do incidente para o pai,
   que dependia dele, era código morto — nunca disparou uma vez.
3. **Teto**: `MAX_INCIDENTES = 12` cortava calado. Existem processos com 59 e
   87 incidentes na amostra; o 87 é um processo com 86 precatórios, ou seja, 86
   beneficiários — víamos 12 e reportávamos `ok`.

Fixtures são HTML REAL da sonda (`tests/fixtures/tjsp/esaj_incidente_*.html`),
nunca sintetizado. Todo teste tem controle negativo: página que NÃO é incidente
(processo comum, segredo, lista, "não existe") tem que devolver `None`.
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from enrichers.esaj import (
    TIPO_PRECATORIO,
    TIPO_RPV,
    BaseEsajEnricher,
    TjspEnricher,
    tipo_de_incidente,
)

FIXTURES = Path(__file__).parent / 'fixtures' / 'tjsp'


def _ler(nome: str) -> str:
    return (FIXTURES / nome).read_text(encoding='utf-8', errors='replace')


@pytest.fixture
def enr():
    return TjspEnricher(pool=MagicMock())


# --------------------------- 1. O cabeçalho (#78) ---------------------------

# (fixture, tipo, classe, CNJ do principal, sequencial, situação)
INCIDENTES_REAIS = [
    ('esaj_incidente_precatorio.html', TIPO_PRECATORIO, 'Precatório',
     '0018347-36.2022.8.26.0576', '01', 'Extinto'),
    ('esaj_incidente_rpv.html', TIPO_RPV, 'Requisição de Pequeno Valor',
     '0002904-43.2025.8.26.0090', '01', ''),
    ('esaj_incidente_precatorio_com_valor.html', TIPO_PRECATORIO, 'Precatório',
     '0003289-49.2025.8.26.0297', '01', ''),
]


@pytest.mark.parametrize('nome,tipo,classe,principal,seq,situacao', INCIDENTES_REAIS)
def test_cabecalho_do_incidente(enr, nome, tipo, classe, principal, seq, situacao):
    ficha = enr.parsear_incidente(_ler(nome))
    assert ficha is not None, 'página real de incidente foi recusada'
    assert ficha['tipo'] == tipo
    assert ficha['classe'] == classe
    assert ficha['cnj_principal'] == principal
    assert ficha['sequencial'] == seq
    assert ficha['situacao'] == situacao
    assert ficha['cnj_proprio'] == '', 'precatório/RPV não tem número próprio'


@pytest.mark.parametrize('nome,_t,_c,_p,_s,_si', INCIDENTES_REAIS)
def test_requerente_e_entidade_devedora(enr, nome, _t, _c, _p, _s, _si):
    """#78: os dois campos que a ficha da parte pede saem do cabeçalho, por
    papel, sem LLM. Um credor e um ente devedor em 3 de 3 páginas reais."""
    ficha = enr.parsear_incidente(_ler(nome))
    assert len(ficha['requerentes']) == 1, ficha['requerentes']
    assert len(ficha['ent_devedoras']) == 1, ficha['ent_devedoras']
    assert ficha['requerentes'][0]['nome']
    assert ficha['ent_devedoras'][0]['nome']
    # E o papel é o cru da fonte — a categoria canônica vive em `tipo`.
    assert ficha['ent_devedoras'][0]['papel'].startswith('ENT')


def test_entidade_devedora_vai_pro_polo_passivo(enr):
    """Quem DEVE o precatório é polo passivo. Caía em 'outros', e é esse polo
    que alimenta o 'quem deve' do Overview (`search/agg_estado.py`)."""
    ficha = enr.parsear_incidente(_ler('esaj_incidente_precatorio.html'))
    passivos = [p['nome'] for p in ficha['partes']['passivo']]
    assert 'MUNICÍPIO DE SÃO JOSÉ DO RIO PRETO' in passivos
    assert not [p for p in ficha['partes']['outros'] if p['papel'].startswith('ENT')]
    # Controle: o credor continua no ativo (a mudança não moveu o outro polo).
    assert any(p['papel'] == 'REQTE' for p in ficha['partes']['ativo'])


def test_valor_sai_quando_a_fonte_publica_e_abstem_quando_nao(enr):
    """`#valorAcaoProcesso` existe em 1 das 3 fixtures. Presente = valor da
    requisição DAQUELE beneficiário; ausente = string vazia, nunca zero."""
    com = enr.parsear_incidente(_ler('esaj_incidente_precatorio_com_valor.html'))
    sem = enr.parsear_incidente(_ler('esaj_incidente_precatorio.html'))
    assert com['valor'] == '18.113,27'
    assert sem['valor'] == ''


def test_contexto_processual_do_incidente(enr):
    ficha = enr.parsear_incidente(_ler('esaj_incidente_precatorio.html'))
    assert ficha['foro'] == 'Foro de São José do Rio Preto'
    assert ficha['vara'] == 'Anexo do Juizado Especial da Fazenda Publica'
    assert ficha['assunto'] == 'Indenização por Dano Moral'
    assert ficha['controle'] == '2021/002588'
    assert ficha['recebido_em'].startswith('18/09/2023')


# ------------------------- 2. Controles negativos ---------------------------

@pytest.mark.parametrize('nome', [
    'esaj_cpopg_detalhe.html',          # processo comum: tem `#numeroProcesso`
    'esaj_cpopg_nao_existe.html',
    'esaj_cpopg_segredo.html',
    'esaj_cpopg_lista.html',
    'esaj_cposg_lista.html',
])
def test_pagina_que_nao_e_incidente_abstem(enr, nome):
    assert enr.parsear_incidente(_ler(nome)) is None


def test_html_vazio_ou_lixo_abstem(enr):
    assert enr.parsear_incidente('') is None
    assert enr.parsear_incidente(None) is None
    assert enr.parsear_incidente('<html><body>oi</body></html>') is None


def test_incidente_com_cnj_proprio_e_a_cadeia_do_credito(enr):
    """A outra forma real de incidente — e o elo que o #28 persegue.

    `esaj_cpopg_detalhe_com_incidentes.html` é o **cumprimento provisório de
    sentença** `0018347-36.2022.8.26.0576`: ele TEM CNJ próprio (está no
    cabeçalho), aponta para o conhecimento `1012705-02.2021.8.26.0576` no link
    'Processo principal', e pendura 7 precatórios/RPV que não têm CNJ nenhum.
    Um crédito, três degraus, e só o do meio é alcançável pelo DJEN."""
    ficha = enr.parsear_incidente(_ler('esaj_cpopg_detalhe_com_incidentes.html'))
    assert ficha is not None
    assert ficha['cnj_proprio'] == '0018347-36.2022.8.26.0576'
    assert ficha['cnj_principal'] == '1012705-02.2021.8.26.0576'
    assert ficha['sequencial'] == '', 'incidente com número próprio não tem sequencial'
    assert ficha['tipo'] is None and ficha['classe'] == 'Cumprimento Provisório de Sentença'


def test_precatorio_nao_tem_cnj_proprio(enr):
    """O oposto do teste acima, e o coração do #28: no precatório/RPV o
    cabeçalho e o link trazem o MESMO número — o do principal. `cnj_proprio`
    vazio é a afirmação de que ele não tem número, não uma ausência de leitura."""
    for nome, _t, _c, principal, _s, _si in INCIDENTES_REAIS:
        ficha = enr.parsear_incidente(_ler(nome))
        assert ficha['cnj_proprio'] == '', nome
        assert ficha['cnj_principal'] == principal, nome


def test_classe_nao_medida_nao_vira_tipo(enr):
    """Abster > chutar: incidente de classe que a sonda não mediu sai com
    `tipo=None` e o rótulo cru ao lado — nunca rotulado precatório no chute."""
    assert tipo_de_incidente('Precatório') == TIPO_PRECATORIO
    assert tipo_de_incidente('PRECATORIO') == TIPO_PRECATORIO
    assert tipo_de_incidente('Requisição de Pequeno Valor') == TIPO_RPV
    assert tipo_de_incidente('Agravo de Instrumento') is None
    assert tipo_de_incidente('Cumprimento de sentença') is None
    assert tipo_de_incidente('') is None

    html = _ler('esaj_incidente_precatorio.html').replace(
        'Precatório&nbsp;(0018347', 'Agravo de Instrumento&nbsp;(0018347')
    ficha = enr.parsear_incidente(html)
    assert ficha['tipo'] is None
    assert ficha['classe'] == 'Agravo de Instrumento'


# ------------------- 3. O teto é alerta, nunca corte mudo -------------------

class _EnricherDeTeste(TjspEnricher):
    """Sem rede: devolve a mesma página de incidente para todo href."""

    def __init__(self, pagina, **kw):
        super().__init__(pool=MagicMock(), **kw)
        self._pagina = pagina
        self.buscados = []

    def _fetch_incidente(self, href):
        self.buscados.append(href)
        return self._pagina


def _pai_com_incidentes(n: int) -> str:
    """Página de pai real, com o bloco de incidente repetido n vezes."""
    html = _ler('esaj_cpopg_detalhe_com_incidentes.html')
    marca = ('<a class="incidente" href="/cpopg/show.do?localPesquisa.cdLocal=576'
             '&processo.codigo=G0000J7130001&processo.foro=576" target="_top" >')
    assert marca in html, 'a fixture mudou — o link de incidente não está mais lá'
    extras = ''.join(
        marca.replace('G0000J7130001', f'FAKE{i:09d}') + f'Precatório - {i:05d}</a>'
        for i in range(100, 100 + n))
    return html.replace(marca, extras + marca, 1)


def test_censo_de_incidentes_do_pai_real():
    """A fixture do cumprimento tem 7 incidentes reais (4 precatórios + 3 RPV)."""
    from bs4 import BeautifulSoup
    e = _EnricherDeTeste(_ler('esaj_incidente_precatorio.html'))
    soup = BeautifulSoup(_ler('esaj_cpopg_detalhe_com_incidentes.html'), 'html.parser')
    assert len(e._extrair_incidentes(soup)) == 7
    partes = {'ativo': [], 'passivo': [], 'outros': []}
    censo = e._agregar_incidentes(soup, partes)
    assert censo == {'total': 7, 'lidos': 7, 'falhas': 0, 'truncado': False,
                     'estourou_tempo': False, 'fichas': censo['fichas']}
    assert len(censo['fichas']) == 7
    # As partes do incidente entraram no pai, com o ente devedor no passivo.
    assert 'MUNICÍPIO DE SÃO JOSÉ DO RIO PRETO' in [p['nome'] for p in partes['passivo']]


def _erros_do(e, soup, partes):
    """Captura o que foi para ERROR. `caplog` não serve: os loggers `voyager.*`
    não propagam para o root nesta configuração e o registro sairia vazio —
    um teste que passa sem testar nada."""
    with patch.object(e.logger, 'error') as err:
        censo = e._agregar_incidentes(soup, partes)
    return censo, [c.args[0] % c.args[1:] if c.args[1:] else c.args[0]
                   for c in err.call_args_list]


def test_teto_atingido_e_erro_com_o_numero_real():
    """Regra nº 2: teto é ERRO registrado com o número real, nunca `return`
    discreto.

    O teto vale 100 porque a distribuição foi medida (53 processos do estrato
    de crédito, 210 incidentes): com 12, dois processos eram truncados e a
    colheita ficava em **41,9% dos incidentes** — os dois maiores (59 e 87)
    guardam 146 dos 210. Teto que parece inofensivo pela contagem de PROCESSOS
    e come dois terços do DADO é o `for pagina in range(1, 11)` de novo."""
    from bs4 import BeautifulSoup
    e = _EnricherDeTeste(_ler('esaj_incidente_precatorio.html'))
    soup = BeautifulSoup(_pai_com_incidentes(100), 'html.parser')
    censo, erros = _erros_do(e, soup, {'ativo': [], 'passivo': [], 'outros': []})
    assert censo['total'] == 107
    assert censo['truncado'] is True
    assert censo['lidos'] == BaseEsajEnricher.MAX_INCIDENTES
    assert len(e.buscados) == BaseEsajEnricher.MAX_INCIDENTES, 'não pode gastar proxy além do teto'
    assert erros and '107' in erros[0], \
        'o teto foi cortado calado — é exatamente o for pagina in range(1, 11)'


def test_sem_truncar_nao_grita():
    """Controle negativo do teste acima: no teto EXATO não há erro."""
    from bs4 import BeautifulSoup
    e = _EnricherDeTeste(_ler('esaj_incidente_precatorio.html'))
    soup = BeautifulSoup(_pai_com_incidentes(BaseEsajEnricher.MAX_INCIDENTES - 7), 'html.parser')
    censo, erros = _erros_do(e, soup, {'ativo': [], 'passivo': [], 'outros': []})
    assert censo['total'] == BaseEsajEnricher.MAX_INCIDENTES
    assert censo['truncado'] is False
    assert erros == []


def test_incidente_ilegivel_conta_como_falha_e_nao_some():
    """Página que não é incidente (ou fetch vazio) NÃO vira `lido`: entra em
    `falhas`. Sucesso silencioso sobre resposta vazia é a assinatura de erro
    que esta casa mais paga."""
    from bs4 import BeautifulSoup
    e = _EnricherDeTeste(_ler('esaj_cpopg_detalhe.html'))   # processo, não incidente
    soup = BeautifulSoup(_ler('esaj_cpopg_detalhe_com_incidentes.html'), 'html.parser')
    censo = e._agregar_incidentes(soup, {'ativo': [], 'passivo': [], 'outros': []})
    assert censo['lidos'] == 0
    assert censo['falhas'] == 7
    assert censo['total'] == 7


def test_teto_de_TEMPO_tambem_e_erro_e_o_pai_sobrevive():
    """O teto de contagem sozinho não protege o job.

    `ENRICH_TIMEOUT` é 300 s e o `_emit` do processo-pai só acontece DEPOIS do
    seguimento: um processo com 100 incidentes num pool degradado (9,2 s por
    requisição, medido em produção em 02/09/2026) estouraria o job e perderia
    também o CADASTRO DO PAI, que já estava na mão. O budget corta o
    seguimento, registra ERRO com o número real e devolve o que leu."""
    from bs4 import BeautifulSoup
    e = _EnricherDeTeste(_ler('esaj_incidente_precatorio.html'))
    e.BUDGET_INCIDENTES_S = 0.0001          # o prazo vence no 1º incidente
    soup = BeautifulSoup(_ler('esaj_cpopg_detalhe_com_incidentes.html'), 'html.parser')
    censo, erros = _erros_do(e, soup, {'ativo': [], 'passivo': [], 'outros': []})
    assert censo['total'] == 7
    assert censo['estourou_tempo'] is True
    assert censo['truncado'] is True, 'quem estourou o tempo também viu pela metade'
    assert censo['lidos'] < 7
    assert erros and 'TEMPO' in erros[0] and '7' in erros[0]


def test_dentro_do_budget_nao_grita():
    """Controle negativo: com prazo folgado, nenhum erro e tudo lido."""
    from bs4 import BeautifulSoup
    e = _EnricherDeTeste(_ler('esaj_incidente_precatorio.html'))
    soup = BeautifulSoup(_ler('esaj_cpopg_detalhe_com_incidentes.html'), 'html.parser')
    censo, erros = _erros_do(e, soup, {'ativo': [], 'passivo': [], 'outros': []})
    assert censo['estourou_tempo'] is False
    assert censo['lidos'] == 7
    assert erros == []
