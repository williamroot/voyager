"""Testes do coletor do DJe do STF (`diarios/fontes/stf/`).

Tudo aqui roda contra material REAL capturado da fonte em 16/08/2026 e
guardado em `tests/fixtures/diarios/stj_stf/`:

  · `stf_publicacoes_2026-08-13_p1_q5.json` — resposta de verdade da API
    (`total=742`, 5 publicações);
  · `stf_publicacoes_pagina_alem_do_fim.json` — `pagina=999` do MESMO dia:
    `publicacoes: []` e **`total: 0`** (a armadilha que faz o fechamento de
    paginação mentir se o total for lido da última página);
  · `stf_publicacoes_422_quantidade.json` — o 422 que a API devolve com
    `quantidade=1000`;
  · `stf_proc_ARE1617690.html` — página do portal com
    `Número Único: 0000876-17.2013.8.16.0021`;
  · `stf_detalhe_sem_numero_unico.html` — processo que o STF diz não ter número
    único (Pet 11841);
  · `stf_detalhe_incidente_inexistente.html` — HTTP 200 com 33 KB da CASCA do
    portal, sem processo nenhum.

Não há mock de fantasia: o transporte é falso (para o teste não bater na fonte),
o CONTEÚDO é o que a fonte respondeu.
"""

import json
import os
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from diarios.base import ColetorError, RespostaInvalida, UnidadeColeta, UnidadeInexistente, obter
from diarios.fontes.stf import api as stf_api
from diarios.fontes.stf.coletor import ColetorSTF, _envolvidos, _oabs
from diarios.fontes.stf.resolver_cnj import ResolvedorCNJ

FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures', 'diarios', 'stj_stf')

CNJ_ARE_1617690 = '0000876-17.2013.8.16.0021'
INCIDENTE_ARE_1617690 = 7661810


#: fixtures PEQUENAS (KB) e OBRIGATÓRIAS: sem elas o teste não é um teste, e a
#: suíte não pode ficar verde fingindo que rodou. A verificação adversarial de
#: 16/08/2026 escondeu `stf_proc_ARE1617690.html` e a suíte respondeu
#: '18 passed, 8 skipped' — sumindo justamente o núcleo (resolver acha o número
#: único, mojibake do portal, cache do resolvedor, guarda do título ponta a
#: ponta). Skip silencioso em fixture commitável é auto-amputação da suíte.
FIXTURES_OBRIGATORIAS = {
    'stf_publicacoes_2026-08-13_p1_q5.json',
    'stf_publicacoes_pagina_alem_do_fim.json',
    'stf_publicacoes_422_quantidade.json',
    'stf_proc_ARE1617690.html',
    'stf_detalhe_sem_numero_unico.html',
    'stf_detalhe_incidente_inexistente.html',
}


def fixture(nome: str) -> str:
    caminho = os.path.join(FIXTURES, nome)
    if not os.path.exists(caminho):
        if nome in FIXTURES_OBRIGATORIAS:
            pytest.fail(f'fixture obrigatória ausente: {nome} (tem que entrar no commit)')
        pytest.skip(f'fixture {nome} não presente')
    with open(caminho, encoding='utf-8') as fh:
        return fh.read()


def publicacoes_reais() -> list[dict]:
    return json.loads(fixture('stf_publicacoes_2026-08-13_p1_q5.json'))['publicacoes']


# ── transporte falso, conteúdo real ─────────────────────────────────────────
class RespostaFake:
    def __init__(self, corpo, status=200, headers=None):
        self._corpo = corpo
        self.status_code = status
        self.headers = headers or {}
        self.encoding = 'ISO-8859-1'  # o portal mente o charset; ver resolver_cnj

    @property
    def text(self):
        return self._corpo if isinstance(self._corpo, str) else json.dumps(self._corpo)

    @property
    def content(self):
        return self.text.encode('utf-8')

    def json(self):
        return self._corpo if not isinstance(self._corpo, str) else json.loads(self._corpo)


class SessaoFake:
    """Substitui a `SessaoDiario` sem mexer no contrato: mesmos métodos.

    `por_url` casa por SUBSTRING da URL — é como o resolvedor de CNJ é servido
    com a página real de CADA incidente, em vez de a mesma página para todos.
    """

    def __init__(self, respostas_get=None, respostas_post=None, por_url=None):
        self.session = SimpleNamespace(cookies={'XSRF-TOKEN': 'token-de-teste'})
        self._get = list(respostas_get or [])
        self._post = list(respostas_post or [])
        self._por_url = por_url or {}
        self.corpos_enviados = []
        self.urls = []

    def get(self, url, **kw):
        self.urls.append(url)
        for trecho, corpo in self._por_url.items():
            if trecho in url:
                return RespostaFake(corpo)
        return self._get.pop(0)

    def post(self, url, **kw):
        self.urls.append(url)
        self.corpos_enviados.append(kw.get('json'))
        return self._post.pop(0)


@pytest.fixture(autouse=True)
def _limpar_cache_do_resolvedor():
    """O resolvedor cacheia `incidente → CNJ` PARA SEMPRE (é imutável na vida
    real). Num teste isso vaza entre casos: o processo resolvido com CNJ no
    teste A voltaria do cache no teste B, que testa justamente a abstenção.
    Limpamos só as chaves dos incidentes das fixtures — nunca o cache inteiro,
    que é o Redis compartilhado do ambiente."""
    from django.core.cache import cache

    from diarios.fontes.stf.resolver_cnj import PREFIXO_CACHE
    caminho = os.path.join(FIXTURES, 'stf_publicacoes_2026-08-13_p1_q5.json')
    if not os.path.exists(caminho):
        yield
        return
    with open(caminho, encoding='utf-8') as fh:
        incidentes = [p.get('processoId') for p in json.load(fh)['publicacoes']]
    chaves = [f'{PREFIXO_CACHE}{i}' for i in incidentes if i] + [f'{PREFIXO_CACHE}{INCIDENTE_ARE_1617690}']
    cache.delete_many(chaves)
    yield
    cache.delete_many(chaves)


def portal_real() -> dict:
    """Página REAL do portal de cada um dos 5 processos da fixture do dia."""
    return {f'incidente={p["processoId"]}': fixture(f'stf_detalhe_{p["processoId"]}.html')
            for p in publicacoes_reais()}


def coletor_com(paginas: list[dict], html_portal: str | None = None,
                portal_por_incidente: dict | None = None) -> ColetorSTF:
    """Coletor real com o transporte trocado por fixture."""
    c = ColetorSTF()
    c.api.sessao = SessaoFake(
        respostas_get=[RespostaFake('"2026-08-14"')] * 4,
        respostas_post=[RespostaFake(p) for p in paginas],
    )
    c.api._token = 'token-de-teste'
    if portal_por_incidente is not None:
        c.resolver.sessao = SessaoFake(por_url=portal_por_incidente)
    elif html_portal is not None:
        c.resolver.sessao = SessaoFake(respostas_get=[RespostaFake(html_portal)] * 500)
    return c


# ── 1. Janela: epoch em MILISSEGUNDOS com corte em Brasília ─────────────────
def test_janela_do_dia_corta_a_meia_noite_de_brasilia():
    """Errar o fuso em 3h vaza publicação do dia vizinho para dentro da janela
    (a sonda viu 39 itens de 15/08 caírem numa janela de 14/08)."""
    ini, fim = stf_api.janela_epoch_ms(date(2026, 8, 13))
    brt = timezone(timedelta(hours=-3))
    assert datetime.fromtimestamp(ini / 1000, brt) == datetime(2026, 8, 13, 0, 0, tzinfo=brt)
    # `dataFim` é inclusivo: o último milissegundo do dia, não a virada.
    assert datetime.fromtimestamp((fim + 1) / 1000, brt) == datetime(2026, 8, 14, 0, 0, tzinfo=brt)


def test_busca_pede_so_divulgacao():
    """DIVULGACAO particiona o acervo (uma data por publicação). A união com
    PUBLICACAO traria o mesmo ato em dois dias e dobraria o tráfego à toa."""
    corpo = stf_api.corpo_busca(date(2026, 8, 13), pagina=1)
    assert corpo['tipoPesquisa'] == ['DIVULGACAO']
    assert corpo['quantidade'] == stf_api.QUANTIDADE_MAX


def test_quantidade_acima_do_teto_falha_local_sem_gastar_request():
    """A própria API devolve 422 acima de 500 — a fixture prova. Não vale gastar
    uma requisição para colher um erro que já se conhece."""
    erro = json.loads(fixture('stf_publicacoes_422_quantidade.json'))
    assert erro['httpCode'] == 422
    assert 'menor ou igual a 500' in erro['errors'][0]['userMessage']

    sessao = SessaoFake()
    with pytest.raises(ValueError):
        stf_api.SessaoSTF(sessao).buscar(date(2026, 8, 13), pagina=1, quantidade=1000)
    assert sessao.urls == [], 'não pode ter tocado na fonte'


# ── 2. O `total` que só vale na página 1 ────────────────────────────────────
def test_total_vem_da_primeira_pagina_e_nao_da_ultima():
    """`pagina=999` de um dia com 742 publicações devolve `total: 0`. Ler o
    total da última página faria o fechamento de paginação aprovar qualquer
    coisa — inclusive uma coleta que perdeu 700 itens."""
    alem = json.loads(fixture('stf_publicacoes_pagina_alem_do_fim.json'))
    assert alem['total'] == 0 and alem['publicacoes'] == []

    reais = publicacoes_reais()
    api = stf_api.SessaoSTF(SessaoFake(respostas_post=[
        RespostaFake({'publicacoes': reais, 'total': 742}),
    ]))
    api._token = 'x'
    lidos = list(api.iter_dia(date(2026, 8, 13)))
    assert len(lidos) == len(reais)
    assert api.ultimo_total == 742


def test_resposta_sem_as_chaves_de_dado_e_rejeitada():
    """A API não é documentada nem versionada: um dia ela pode responder 200 com
    `{"errors": [...]}`. Isso tem que explodir, não virar dia vazio."""
    api = stf_api.SessaoSTF(SessaoFake(respostas_post=[RespostaFake({'errors': [{'x': 1}]})]))
    api._token = 'x'
    with pytest.raises(RespostaInvalida):
        api.buscar(date(2026, 8, 13), pagina=1)


# ── 3. Resolvedor de CNJ: o elo que decide se a publicação vira dado ────────
def test_resolvedor_acha_o_numero_unico_na_pagina_real():
    proc = ResolvedorCNJ.parsear(INCIDENTE_ARE_1617690, fixture('stf_proc_ARE1617690.html'))
    assert proc.cnj == CNJ_ARE_1617690
    assert proc.titulo.startswith('ARE')
    assert proc.classe == 'RECURSO EXTRAORDINÁRIO COM AGRAVO'


def test_resolvedor_abstem_quando_o_stf_diz_que_nao_ha_numero_unico():
    """'Sem número único' é resposta legítima do STF (processo autuado antes da
    numeração unificada). Abster > chutar: `cnj=None`, e a publicação não é
    gravada. Medido: 4 em 40 processos de um dia real."""
    proc = ResolvedorCNJ.parsear(7556883, fixture('stf_detalhe_sem_numero_unico.html'))
    assert proc.cnj is None


def test_resolvedor_rejeita_a_casca_do_portal():
    """Incidente inexistente devolve HTTP 200 com 33 KB do site — o clássico
    '200 que não é dado'. Sem esta checagem ele viraria abstenção silenciosa e
    a publicação sumiria como se o STF tivesse dito que não há número."""
    html = fixture('stf_detalhe_incidente_inexistente.html')
    assert len(html) > 30_000, 'a casca é grande de propósito: tamanho não distingue'
    with pytest.raises(RespostaInvalida):
        ResolvedorCNJ.parsear(999999999, html)


@pytest.mark.django_db
def test_resolvedor_cacheia_o_acerto_e_nao_repergunta_ao_portal():
    """O portal é o gargalo (medido: 44 GETs = 58 s num dia de 44 publicações;
    o mesmo dia recoletado com cache quente levou 1 s). Sem cache, backfill de
    ~590 publicações/dia útil viraria hora e meia de IIS legado por dia."""
    from diarios.fontes.stf.resolver_cnj import ResolvedorCNJ as R

    sessao = SessaoFake(respostas_get=[RespostaFake(fixture('stf_proc_ARE1617690.html'))])
    r = R(sessao)
    assert r.resolver(INCIDENTE_ARE_1617690).cnj == CNJ_ARE_1617690
    # a segunda chamada não tem resposta na fila: se tocar na rede, estoura.
    assert r.resolver(INCIDENTE_ARE_1617690).cnj == CNJ_ARE_1617690
    assert (r.consultas, r.acertos_cache) == (1, 1)


def test_desafio_do_waf_tem_erro_proprio():
    """Hoje o WAF só protege a rota HTML. No dia em que ele chegar à API, o
    sintoma é 202 + `x-amzn-waf-action` — e quem lê o log precisa saber que a
    cura não é retry."""
    from diarios.fontes.stf.api import DesafioWAF

    api = stf_api.SessaoSTF(SessaoFake(respostas_get=[
        RespostaFake('"2026-08-14"', status=202, headers={'x-amzn-waf-action': 'challenge'}),
    ]))
    with pytest.raises(DesafioWAF):
        api.ultimo_dje()


def test_resolvedor_nao_depende_de_acento_no_html():
    """O portal declara ISO-8859-1 e serve UTF-8. A primeira versão deste
    resolvedor mediu 0/40 por causa disso. As regexes ancoram na classe CSS."""
    html = fixture('stf_proc_ARE1617690.html')
    mojibake = html.encode('utf-8').decode('latin-1')
    assert 'Número Único' not in mojibake
    assert ResolvedorCNJ.parsear(INCIDENTE_ARE_1617690, mojibake).cnj == CNJ_ARE_1617690


# ── 4. Parsing da publicação ────────────────────────────────────────────────
def test_item_preenche_o_que_a_fonte_tem_e_abstem_no_resto():
    c = ColetorSTF()
    pub = publicacoes_reais()[0]          # Pet 16560, divulgada em 13/08/2026
    proc = ResolvedorCNJ.parsear(INCIDENTE_ARE_1617690, fixture('stf_proc_ARE1617690.html'))
    item = c._para_item(pub, proc)

    assert item.cnj == CNJ_ARE_1617690
    assert item.external_id == f'stf:{pub["id"]}'
    # divulgação (13/08), não publicação (14/08): é o análogo exato da
    # disponibilização do DJEN — usar `publicacao` deslocaria a série em 1 dia.
    assert item.data_disponibilizacao.date() == date(2026, 8, 13)
    assert pub['publicacao'] == '2026-08-14'
    assert item.tipo_documento == 'Decisão Final'
    assert item.tipo_comunicacao == 'Publicação Monocrática'
    # sem colegiado (monocrática) ⇒ o órgão prolator é o gabinete do relator,
    # que é o dado mais distintivo desta fonte e não tem coluna própria.
    assert item.nome_orgao == 'MIN. GILMAR MENDES'
    assert item.nome_classe == 'RECURSO EXTRAORDINÁRIO COM AGRAVO'
    assert item.meio_completo == 'DJe/STF (digital.stf.jus.br)'
    assert item.status == 'Público'
    assert item.texto == pub['texto'], 'texto tem que ser verbatim'
    assert item.hash, 'fingerprint do ato é obrigatório'

    # Abstenções deliberadas — campo vazio honesto > campo chutado:
    assert item.data_envio is None, 'o `publicacao` do STF é DEPOIS, não antes'
    assert item.link == '', 'não há URL da publicação no payload'
    assert item.codigo_classe == '', 'código TPU do STF é desconhecido'


def test_codigo_classe_vazio_protege_o_catalogo_nacional():
    """`persistir_movimentacoes` usa `codigo_classe` como PK de
    `ClasseJudicial`. Preencher com 'ARE' criaria uma classe que não é TPU no
    catálogo nacional."""
    c = ColetorSTF()
    pub = publicacoes_reais()[0]
    proc = ResolvedorCNJ.parsear(INCIDENTE_ARE_1617690, fixture('stf_proc_ARE1617690.html'))
    item = c._para_item(pub, proc)
    assert (item.codigo_classe, bool(item.nome_classe)) == ('', True)


def test_envolvidos_viram_partes_e_advogados_com_oab():
    pub = next(p for p in publicacoes_reais() if p['envolvidos'])
    partes, advogados = _envolvidos(pub['envolvidos'])
    assert partes and advogados
    assert {p['polo'] for p in partes} <= {'A', 'P', 'I'}
    assert all(a['advogado']['nome'] for a in advogados)


def test_oab_multipla_preserva_o_numero_verbatim():
    """Advogado do STF costuma ter dezenas de inscrições, e o número vem sujo
    ('1566 - A', '30067/A', '10.581-A', 'A2557'): o sufixo/prefixo É parte da
    inscrição suplementar. Normalizar aqui alteraria o dado da fonte."""
    achado = _oabs(["OAB's (59957/SC, 1566 - A/RN, 30067/A/MT, 10.581-A/TO, A2557/AM)"])
    assert achado == [('59957', 'SC'), ('1566 - A', 'RN'), ('30067/A', 'MT'),
                      ('10.581-A', 'TO'), ('A2557', 'AM')]
    assert _oabs(['OAB 15181/PR']) == [('15181', 'PR')]


# ── 5. Contrato do coletor ──────────────────────────────────────────────────
def test_fonte_registrada_com_janela_medida():
    c = obter('stf')
    assert c.slug == 'stf'
    # O STF nunca entrou no DJEN ⇒ não há janela de exclusividade a fechar; o
    # início é o primeiro dia que a API tem (medido: 2020 = 2.235 publicações,
    # a mais antiga em 01/09/2020; 2019 = 0).
    assert (c.janela_inicio, c.janela_fim) == (date(2020, 9, 1), None)
    assert c.dentro_da_janela(date.today())
    assert not c.dentro_da_janela(date(2019, 6, 10))


def test_catalogo_nao_passa_do_ultimo_dje_declarado_pelo_stf():
    """Catalogar o futuro cria unidade que nasce vazia e é retentada até o
    MAX_TENTATIVAS. O `/ultimo-dje` é o teto, e custa UMA requisição."""
    c = ColetorSTF()
    c.api.sessao = SessaoFake(respostas_get=[RespostaFake('"2026-08-14"')])
    unidades = list(c.catalogar(date(2026, 8, 12), date(2026, 8, 20)))
    assert [str(u.data) for u in unidades] == ['2026-08-12', '2026-08-13', '2026-08-14']
    assert unidades[0].chave == 'stf-2026-08-12'
    assert unidades[0].tribunal_sigla == 'STF'


def test_catalogo_nao_cria_unidade_do_dia_ainda_em_curso():
    """REGRESSÃO DO BLOQUEIO ACHADO EM 16/08/2026 — o teto estava no eixo errado.

    `/ultimo-dje` é a data da última EDIÇÃO publicada (eixo `publicacao`), e a
    unidade aqui é o dia de DIVULGAÇÃO. Medido: às 09h de uma sexta o
    `/ultimo-dje` já responde a data daquela sexta, enquanto as 588 divulgações
    do dia só saem entre 16h30 e 20h30. Com o teto antigo, o dia corrente
    entrava no catálogo, era coletado com `total=0` e fechava como
    `inexistente` — status TERMINAL, com `IngestionRun` verde e sem
    reenfileiramento. O dia inteiro sumia com o run reportando sucesso.

    A regra agora é: só dia de divulgação FECHADO (≤ ontem, em BRT). O dia
    corrente entra amanhã, pelo `catalogar_fronteira(7)`, sem deixar buraco.
    """
    from django.utils import timezone as djtz

    hoje = djtz.localdate()
    c = ColetorSTF()
    # A fonte declara HOJE como último DJe — é o pior caso, e o real.
    c.api.sessao = SessaoFake(respostas_get=[RespostaFake(f'"{hoje.isoformat()}"')])
    datas = [u.data for u in c.catalogar(hoje - timedelta(days=3), hoje + timedelta(days=2))]
    assert hoje not in datas, 'o dia de hoje ainda está divulgando: catalogá-lo perde o resto'
    assert max(datas) == hoje - timedelta(days=1)


@pytest.mark.django_db
def test_total_zero_num_dia_ainda_aberto_e_falha_retentavel_nao_ausencia():
    """A outra ponta do mesmo bloqueio, para o caminho manual (`--chave`).

    Zero num dia que ainda não terminou não é "não houve divulgação", é "ainda
    não houve". Marcar `inexistente` ali é terminal e queima o dia; a resposta
    certa é falha RETENTÁVEL, com a mensagem dizendo quando voltar."""
    from django.utils import timezone as djtz

    hoje = djtz.localdate()
    c = coletor_com([{'publicacoes': [], 'total': 0}])
    with pytest.raises(ColetorError, match='ainda está em curso'):
        list(c.coletar(UnidadeColeta(chave=f'stf-{hoje}', data=hoje, tribunal_sigla='STF')))


def test_catalogo_nao_desce_abaixo_do_primeiro_dia_com_dado():
    c = ColetorSTF()
    c.api.sessao = SessaoFake(respostas_get=[RespostaFake('"2026-08-14"')])
    assert list(c.catalogar(date(2019, 1, 1), date(2019, 12, 31))) == []


@pytest.mark.django_db
def test_dia_sem_divulgacao_e_inexistente_nao_falha():
    """Fim de semana/feriado forense: `total=0`. Tratar ausência como lacuna faz
    o backfill retentar o mesmo dia para sempre (lição do `_dia_coberto`)."""
    c = coletor_com([{'publicacoes': [], 'total': 0}])
    with pytest.raises(UnidadeInexistente):
        list(c.coletar(UnidadeColeta(chave='stf-2026-08-09', data=date(2026, 8, 9),
                                     tribunal_sigla='STF')))


@pytest.mark.django_db
def test_paginacao_incompleta_falha_alto():
    """A fonte declarou 742 e entregou 5. Perder item em silêncio é o pior
    desfecho: o coletor reporta sucesso e a lacuna fica invisível."""
    c = coletor_com([{'publicacoes': publicacoes_reais(), 'total': 742}],
                    html_portal=fixture('stf_proc_ARE1617690.html'))
    with pytest.raises(ColetorError, match='paginação'):
        list(c.coletar(UnidadeColeta(chave='stf-2026-08-13', data=date(2026, 8, 13),
                                     tribunal_sigla='STF')))


@pytest.mark.django_db
def test_publicacao_sem_numero_unico_nao_e_gravada_com_cnj_chutado():
    """O caso que define a honestidade desta fonte: a publicação TEM CNJs no
    texto (do processo de origem, do TJ, do tribunal militar), mas o STF diz que
    o processo não tem número único. Nada é inventado — o item some da coleta e
    entra na contabilidade."""
    reais = publicacoes_reais()
    c = coletor_com([{'publicacoes': reais, 'total': len(reais)}],
                    html_portal=fixture('stf_detalhe_sem_numero_unico.html'))
    unidade = UnidadeColeta(chave='stf-2026-08-13', data=date(2026, 8, 13), tribunal_sigla='STF')
    assert list(c.coletar(unidade)) == []
    balanco = c.balanco[unidade.chave]
    assert balanco['sem_numero_unico'] == len(reais)
    assert balanco['aproveitados'] == 0
    assert c.esperado(unidade) == 0


def test_fingerprint_e_do_ato_e_nao_da_folha_de_estilo():
    """REGRESSÃO 16/08/2026: o `hash` estava sendo calculado sobre o CSS.

    `fingerprint_ato` hasheia os 4.000 primeiros chars normalizados, e no XHTML
    do LibreOffice que o STF serve o `<body>` só começa por volta do char 4.345
    — a janela inteira caía no `<head>`. Medido nas 205 publicações gravadas em
    dev: 18 (8,8%) dividiam a MESMA janela, e num grupo de 4 havia 3 corpos
    visíveis distintos. Como a camada 3 da dedupe lê com
    `DISTINCT ON (processo_id, hash)`, isso apagaria publicação legítima.
    """
    from diarios.base import fingerprint_ato
    from diarios.fontes.stf.coletor import _corpo_visivel

    cabecalho = '<html><head><style>' + '.P1{font-family:serif;font-size:13pt}' * 200 + '</style></head>'
    a = cabecalho + '<body><p>Nego provimento ao agravo.</p></body></html>'
    b = cabecalho + '<body><p>Dou provimento ao recurso extraordinário.</p></body></html>'
    quando = date(2026, 8, 13)

    assert len(cabecalho) > 4000, 'a fixture tem que reproduzir o cabeçalho gigante real'
    # o defeito, demonstrado: sobre o documento inteiro os dois atos colidem
    assert fingerprint_ato(CNJ_ARE_1617690, quando, a) == fingerprint_ato(CNJ_ARE_1617690, quando, b)
    # a correção: sobre o corpo visível, dois atos diferentes têm hashes diferentes
    assert (fingerprint_ato(CNJ_ARE_1617690, quando, _corpo_visivel(a))
            != fingerprint_ato(CNJ_ARE_1617690, quando, _corpo_visivel(b)))
    # e o `texto` gravado continua verbatim — o recorte é SÓ do fingerprint
    assert _corpo_visivel('texto sem body nenhum') == 'texto sem body nenhum'


@pytest.mark.django_db
def test_esperado_desconta_as_abstencoes():
    """O gate de cobertura do runner compara o gravado com este número. Devolver
    o `total` cru reprovaria a unidade pelos ~10% sem número único — que são
    abstenção correta, não falha de segmentação."""
    reais = publicacoes_reais()
    c = coletor_com([{'publicacoes': reais, 'total': len(reais)}],
                    portal_por_incidente=portal_real())
    unidade = UnidadeColeta(chave='stf-2026-08-13', data=date(2026, 8, 13), tribunal_sigla='STF')
    itens = list(c.coletar(unidade))
    assert len(itens) == c.esperado(unidade) == len(reais)
    # os 5 CNJs vêm do portal, cada um do seu processo — e mostram o retrato da
    # fonte: 2 nativos do STF (J=1), 1 da Justiça Militar da União (7.00), 1 da
    # Justiça Militar estadual (3.00) e 1 do TJPR (8.16), que é o caso em que a
    # publicação do STF gruda num processo que o acervo já pode ter.
    assert {i.cnj for i in itens} == {
        '0181305-17.2026.1.00.0000', '0122235-06.2025.1.00.0000',
        '7000825-55.2025.7.00.0000', '0107738-82.2026.3.00.0000',
        '0007936-23.2020.8.16.0174',
    }


# ── 6. Ponta a ponta, no banco ──────────────────────────────────────────────
@pytest.mark.django_db
def test_coleta_grava_movimentacao_e_e_idempotente():
    """Roda o runner de verdade (`coletar_unidade`) duas vezes: a segunda tem
    que gravar ZERO. É o critério de aceite da casa, e o que prova que o
    `external_id` namespaceado é determinístico."""
    from diarios.base import coletar_unidade
    from diarios.models import EdicaoDiario
    from tribunals.models import IngestionRun, Movimentacao, Process, Tribunal

    Tribunal.objects.get_or_create(sigla='STF', defaults={'nome': 'STF', 'sigla_djen': 'STF'})
    reais = publicacoes_reais()

    def roda():
        c = coletor_com([{'publicacoes': reais, 'total': len(reais)}],
                        portal_por_incidente=portal_real())
        edicao, _ = EdicaoDiario.objects.get_or_create(
            fonte='stf', chave='stf-2026-08-13',
            defaults={'data': date(2026, 8, 13), 'tribunal_id': 'STF'},
        )
        return coletar_unidade(c, edicao)

    primeira = roda()
    assert primeira['novas'] == len(reais)
    segunda = roda()
    assert segunda['novas'] == 0 and segunda['duplicadas'] == len(reais)

    mov = Movimentacao.objects.get(tribunal_id='STF', external_id=f'stf:{reais[0]["id"]}')
    assert mov.meio_completo == 'DJe/STF (digital.stf.jus.br)'
    assert mov.processo.numero_cnj == '0181305-17.2026.1.00.0000'   # Pet 16560, nativo do STF

    # O run é do STF, não do DJEN — sem isso o `_dia_coberto` do DJEN
    # consideraria o dia coberto e pularia a ingestão.
    assert IngestionRun.objects.filter(fonte='stf').count() == 2
    assert not IngestionRun.objects.filter(fonte='djen', tribunal_id='STF').exists()

    # SEMÂNTICA QUE PRECISA DE DECISÃO HUMANA, travada aqui para não emergir
    # sem que ninguém veja: a Pet 15758 tem número único do TJPR (8.16), mas o
    # `Process` nasce sob o tribunal STF — porque a unicidade de `Process` é
    # (tribunal, numero_cnj). Se o TJPR já tiver esse processo, passam a existir
    # DUAS linhas para ele. É o mesmo nó dos "incidentes CNJ vinculados" e do STJ.
    p = Process.objects.get(tribunal_id='STF', numero_cnj='0007936-23.2020.8.16.0174')
    assert p.numero_cnj.split('.')[-2] == '16', 'o CNJ é do tribunal de ORIGEM'


@pytest.mark.django_db
def test_cnj_do_texto_nunca_vira_processo():
    """Rede de segurança contra a tentação de "aproveitar" o CNJ solto no texto:
    nenhum `Process` pode nascer de um número que o portal não confirmou."""
    from tribunals.models import Process, Tribunal

    Tribunal.objects.get_or_create(sigla='STF', defaults={'nome': 'STF', 'sigla_djen': 'STF'})
    reais = publicacoes_reais()
    c = coletor_com([{'publicacoes': reais, 'total': len(reais)}],
                    html_portal=fixture('stf_detalhe_sem_numero_unico.html'))
    unidade = UnidadeColeta(chave='stf-2026-08-13', data=date(2026, 8, 13), tribunal_sigla='STF')
    itens = list(c.coletar(unidade))
    assert itens == []
    assert not Process.objects.filter(tribunal_id='STF').exists()


@pytest.mark.django_db
def test_mudanca_de_contrato_da_api_vira_alerta_de_drift():
    """A API do STF não é documentada nem versionada e foi achada por engenharia
    reversa: quando ela mudar, ninguém vai nos avisar. O alerta é o MESMO
    `SchemaDriftAlert` do DJEN — reusar significa que a mudança aparece na tela
    de saúde que a equipe já olha, sem código novo."""
    from tribunals.models import SchemaDriftAlert, Tribunal

    Tribunal.objects.get_or_create(sigla='STF', defaults={'nome': 'STF', 'sigla_djen': 'STF'})
    SchemaDriftAlert.objects.filter(tribunal_id='STF').delete()

    reais = publicacoes_reais()
    mutantes = [dict(p, campoNovoQueNinguemAvisou=1) for p in reais]
    del mutantes[0]['relator']
    c = coletor_com([{'publicacoes': mutantes, 'total': len(mutantes)}],
                    html_portal=fixture('stf_proc_ARE1617690.html'))
    list(c.coletar(UnidadeColeta(chave='stf-2026-08-13', data=date(2026, 8, 13),
                                 tribunal_sigla='STF')))

    tipos = set(SchemaDriftAlert.objects.filter(tribunal_id='STF').values_list('tipo', flat=True))
    assert tipos == {SchemaDriftAlert.TIPO_EXTRA, SchemaDriftAlert.TIPO_MISSING}
    SchemaDriftAlert.objects.filter(tribunal_id='STF').delete()


@pytest.mark.django_db
def test_publicacao_e_descartada_quando_o_portal_devolve_outro_processo():
    """O `processoId` é a ÚNICA cola entre a publicação (API) e o CNJ (portal).
    Se ele apontar para o incidente errado, a publicação do STF grudaria no
    processo errado — pior do que perdê-la. O título da página é o conferente:
    medido em 44 publicações de 11/08/2026, 42 títulos batem exatamente e 0
    divergem."""
    from diarios.fontes.stf.coletor import _confere_processo
    from diarios.fontes.stf.resolver_cnj import ProcessoSTF

    pub = publicacoes_reais()[0]                      # 'Pet 16560'
    assert _confere_processo(pub, ProcessoSTF(1, cnj=CNJ_ARE_1617690, titulo='Pet 16560'))
    assert _confere_processo(pub, ProcessoSTF(1, cnj=CNJ_ARE_1617690, titulo=''))
    assert not _confere_processo(pub, ProcessoSTF(1, cnj=CNJ_ARE_1617690, titulo='ARE 1617690'))

    # ponta a ponta: o portal devolve a página do ARE para publicações de Pet/RHC
    c = coletor_com([{'publicacoes': publicacoes_reais(), 'total': 5}],
                    html_portal=fixture('stf_proc_ARE1617690.html'))
    unidade = UnidadeColeta(chave='stf-2026-08-13', data=date(2026, 8, 13), tribunal_sigla='STF')
    assert list(c.coletar(unidade)) == []
    assert c.balanco[unidade.chave]['titulo_divergente'] == 5
