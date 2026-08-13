"""Testes da PÁGINA DEDICADA DE UM ESTADO (dashboard/overview_estado.html).

A view (`comercial_estado_page`) só renderiza o shell — todo número vem do
endpoint `comercial-estado` via fetch no browser, então este teste NÃO precisa
de ES nem de Postgres carregado.

Cobre, além do básico (login-gate, render, CSP), as regras que a página não pode
regredir — todas nasceram de uma medição, não de gosto:

  * o dado é DESIGUAL por estado (RO: 69,6% dos processos sem tipo, ZERO
    participação indexada; MG/RO/FED com valor_causa 0%): a cobertura de cada
    bloco tem que estar ESCRITA na tela e o bloco vazio precisa dizer "ainda não
    indexamos", nunca "não há devedor público";
  * a lente (possíveis | confirmados | todos) muda os gráficos e NÃO o resumo;
    "todos" é união, nunca soma;
  * copy compartilhada com o mapa é LITERALMENTE a mesma (o mesmo número não
    pode ganhar duas explicações);
  * guardas da casa: zero `title=`, zero hex em cor de texto, zero URL externa,
    `setOption(opts, true)` (o `setupChart` mata os formatters), comentário
    `{# #}` numa linha só, e nada de `text-base` — que nesta config do Tailwind
    é COR (o token `base`), não tamanho de fonte.
"""
from __future__ import annotations

import re
from pathlib import Path

import dashboard
import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

pytestmark = pytest.mark.django_db

User = get_user_model()

URL_NAME = 'dashboard:overview-estado-page'
TPL_DIR = Path(dashboard.__file__).parent / 'templates' / 'dashboard'
TEMPLATE_SRC = (TPL_DIR / 'overview_estado.html').read_text(encoding='utf-8')
MAPA_SRC = (TPL_DIR / 'overview_mapa.html').read_text(encoding='utf-8')


def _sem_comentarios(src: str) -> str:
    """Template sem comentários — base das asserções NEGATIVAS.

    Vários comentários citam de propósito o que foi corrigido (`text-base`,
    `setupChart`, `bg-mission text-white`). Sem tirá-los, o próprio comentário
    derruba o teste que garante que o bug não voltou.
    """
    src = re.sub(r'\{#.*?#\}', '', src, flags=re.S)
    src = re.sub(r'\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}', '', src, flags=re.S)
    src = re.sub(r'^\s*//.*$', '', src, flags=re.M)
    src = re.sub(r'(?<![:\'"])//.*$', '', src, flags=re.M)
    return src


CODIGO = _sem_comentarios(TEMPLATE_SRC)


@pytest.fixture
def user(db):
    return User.objects.create_user(username='estado_gate', password='x')


@pytest.fixture
def html(user):
    c = Client(SERVER_NAME='localhost')
    c.force_login(user)
    resp = c.get(reverse(URL_NAME, kwargs={'uf': 'RO'}))
    assert resp.status_code == 200
    return resp.content.decode()


# --------------------------------------------------------------------------- #
# Rota, gate e shell
# --------------------------------------------------------------------------- #
def test_url_resolve():
    assert reverse(URL_NAME, kwargs={'uf': 'SP'}) == '/dashboard/overview/estado/SP/'


def test_anon_redireciona_login(client):
    resp = client.get(reverse(URL_NAME, kwargs={'uf': 'SP'}))
    assert resp.status_code == 302
    assert '/login' in resp['Location']


def test_logado_renderiza_200(html):
    assert 'Rondônia' in html                      # nome por extenso, não a sigla
    assert 'Voltar ao Mapa de Precatórios' in html
    assert 'Os gráficos deste estado' in html


def test_uf_minuscula_normalizada(user):
    """O link pode vir em caixa baixa; o cabeçalho não pode virar "sp"."""
    c = Client(SERVER_NAME='localhost')
    c.force_login(user)
    resp = c.get(reverse(URL_NAME, kwargs={'uf': 'sp'}))
    assert resp.status_code == 200
    corpo = resp.content.decode()
    assert 'São Paulo' in corpo
    assert 'window.ESTADO_UF = "SP"' in corpo


def test_uf_invalida_ainda_renderiza_a_pagina(user):
    """400 é do ENDPOINT, não da página.

    A navegação veio de um clique no mapa; devolver 404 seco aqui trocaria uma
    tela explicada ("não conhecemos este estado" + caminho de volta) por um erro
    de servidor. A view não valida a UF — quem valida é `agg_estado`.
    """
    c = Client(SERVER_NAME='localhost')
    c.force_login(user)
    resp = c.get(reverse(URL_NAME, kwargs={'uf': 'ZZ'}))
    assert resp.status_code == 200
    corpo = resp.content.decode()
    assert 'Não conhecemos este estado.' in corpo
    assert 'ufInvalida' in corpo


def test_shell_puro_nao_toca_o_es(user, monkeypatch):
    """A view não pode consultar o índice: se o ES cair, a PÁGINA ainda abre.

    (Quem devolve 503 é o endpoint JSON, e a página trata isso com mensagem
    humana + "Tentar de novo".)
    """
    from search import agg_estado

    def explode(*a, **kw):   # pragma: no cover - só é chamado se houver regressão
        raise AssertionError('a página do estado consultou o Elasticsearch')

    monkeypatch.setattr(agg_estado, 'get_es', explode)
    c = Client(SERVER_NAME='localhost')
    c.force_login(user)
    assert c.get(reverse(URL_NAME, kwargs={'uf': 'SP'})).status_code == 200


def test_pagina_nao_cacheia_no_browser(user):
    """Sem `no-store` o browser serve o HTML do disco e ESCONDE o deploy —
    aconteceu com o mapa em 12/08/2026 (3 prints reclamando de texto já removido)."""
    c = Client(SERVER_NAME='localhost')
    c.force_login(user)
    r = c.get(reverse(URL_NAME, kwargs={'uf': 'SP'}))
    cc = r.headers.get('Cache-Control', '')
    assert 'no-store' in cc or 'no-cache' in cc, f'Cache-Control fraco: {cc!r}'


# --------------------------------------------------------------------------- #
# Honestidade: cobertura parcial, bloco vazio, metodologia
# --------------------------------------------------------------------------- #
def test_cobertura_de_cada_bloco_fica_ao_lado_do_grafico(html):
    """"Amostra de X%" não pode virar rodapé cinza.

    Medido em 12/08/2026: em RO o `classe_nome` só existe em 37,4% do recorte —
    um gráfico de tipos ali fala por pouco mais de um terço dos processos.
    """
    assert 'cobertura_amostra' in html
    assert 'textoCobertura' in html
    assert 'Desenhado sobre' in html
    assert 'coberturaCritica' in html          # < 60% ganha moldura de aviso
    assert 'docs_com_o_campo' in html
    # a frase diz o PORQUÊ de faltar, campo a campo
    assert 'o tribunal não informou o tipo do processo' in html
    assert 'ainda não passaram pela nossa IA' in html
    assert 'ainda não indexamos as partes do processo' in html


def test_entidade_vazia_nao_vira_grafico_fantasma(html):
    """RO tem ZERO participação indexada: o bloco volta vazio.

    Vazio aqui é "ainda não indexamos", NUNCA "não há ente público devedor" — e
    quem tem que dizer isso é o texto do backend (`nota`), na tela.
    """
    assert 'entidadeVazia' in html
    assert 'Ainda não sabemos quem deve neste recorte.' in html
    assert 'notaEntidade' in html
    assert 'não</b> quer dizer que não há órgão público devendo' in html
    # e o gráfico não é pintado quando não há item
    assert "if (!itens.length) return;" in html


def test_metodologia_do_ranking_de_devedor_esta_exposta(html):
    """Auditoria de número: o que saiu do ranking e por quê."""
    assert 'advogados_excluidos' in html
    assert 'aceitos_por_complemento' in html
    assert 'buckets_fundidos' in html
    assert 'descartados_nao_ente' in html
    assert 'representam quem deve, não são quem deve' in html
    assert 'não um cadastro oficial' in html
    # top-15 tirado de um top-200: "os maiores", não "todos"
    assert 'entidadeTruncada' in html
    assert 'não todos' in html


def test_ano_fora_da_faixa_e_contado_e_explicado(html):
    """CNJ malformado gera 9400/9900; o backend joga em `fora_da_faixa`.

    Não plotamos (estouraria o eixo X) — mas o resíduo é dito, com a causa.
    """
    assert 'fora_da_faixa' in html
    assert 'ano impossível' in html
    assert 'erro de cadastro na origem, não processo antigo' in html


def test_valor_da_causa_some_quando_ninguem_publicou(html):
    """`valor_causa` é esparso: 0% em MG, RO e federal (medido 12/08/2026).

    Um "R$ 0" em corpo de KPI seria lido como "não há dinheiro aqui". O card só
    existe quando alguém publicou valor — e diz que é o valor DA CAUSA.
    """
    assert 'temValor' in html
    assert 'cobertura_valor' in html
    assert 'x-show="dados && temValor()"' in html
    assert 'não o valor do precatório' in html
    assert 'R$ 0' not in CODIGO


def test_desconhecido_vira_palavra_nunca_zero(html):
    """possíveis null / sinal_processado false / cobertura null = DESCONHECIDO."""
    assert 'possiveisDesconhecido' in html
    assert 'sinal_processado' in html
    assert 'ainda não analisado' in html
    assert 'cobDesconhecida' in html
    assert 'não sabemos ainda' in html
    # sem opacidade pra dizer "não sei" (opacity-70 dava 2,5:1)
    assert 'opacity-' not in CODIGO
    assert 'opacity:.' not in CODIGO


def test_recorte_vazio_tem_explicacao(html):
    """Lente + filtro podem zerar o recorte; 5 gráficos em branco não explicam nada."""
    assert 'escopoVazio' in html
    assert 'Nenhum processo passou por este recorte.' in html
    assert 'Isso é o filtro cortando, não o estado estar vazio' in html


# --------------------------------------------------------------------------- #
# A lente
# --------------------------------------------------------------------------- #
def test_lente_com_a_semantica_do_mapa(html):
    """Possíveis | Confirmados | Todos — união, NUNCA soma."""
    assert ">Possíveis</button>" in html
    assert ">Confirmados</button>" in html
    assert ">Todos</button>" in html
    for m in ('possiveis', 'confirmados', 'todos'):
        assert f"trocarMetrica('{m}')" in html
        assert f"metrica==='{m}'" in html
    assert 'aria-pressed' in html
    # a união vem do backend; a página nunca soma os dois
    for soma in ('possiveis + ', '.possiveis + r.confirmados', 'possiveis || 0) + ('):
        assert soma not in CODIGO, f'união calculada por SOMA: {soma}'
    assert 'não é a soma dos dois' in html
    assert 'sem contar\n        ninguém duas vezes' in html or 'sem contar' in html


def test_lente_nao_mexe_no_resumo_e_a_tela_diz_isso(html):
    """O `resumo` é o retrato do estado; só os blocos `por_*` seguem a lente.

    Sem essa frase o usuário troca a lente, vê o KPI parado e conclui que a
    página travou.
    """
    assert 'são do estado inteiro e <b class="text-fg">não mudam</b>' in html
    assert 'o estado inteiro, com os filtros acima' in html
    assert 'fraseEscopo' in html
    assert 'Os gráficos abaixo olham' in html
    # o resumo é lido do payload, não recalculado a partir dos blocos
    assert 'resumo() ? resumo().volume : 0' in html


# --------------------------------------------------------------------------- #
# Filtros: entram pela querystring e continuam visíveis
# --------------------------------------------------------------------------- #
def test_filtros_entram_pela_querystring_e_voltam_pra_url(html):
    assert 'lerQuerystring' in html
    assert 'sincronizarUrl' in html
    assert 'history.replaceState' in html
    for campo in ('classificacao', 'tipo', 'natureza', 'ano_min', 'ano_max',
                  'valor_min', 'valor_max'):
        assert campo in html, f'filtro do mapa ausente: {campo}'
    # `codigo_classe` é o mesmo campo que `natureza` no backend — aceita os dois
    assert 'codigo_classe' in html
    assert "p.set('metrica', this.metrica)" in html


def test_filtro_ativo_aparece_escrito(html):
    """Quem chega por um link do mapa não mexeu em nenhum campo: precisa VER,
    escrito, por que o número desta página não é o do estado inteiro."""
    assert 'filtrosAtivos' in html
    assert 'Filtrando por' in html
    assert 'A IA classificou como: ' in html
    assert 'Ano do processo: ' in html
    assert 'Nível de certeza: ' in html
    assert 'Remover filtro: ' in html
    # o filtro de valor esconde silenciosamente quem não tem valor publicado
    assert 'Processo sem valor informado fica de fora do resultado.' in html


# --------------------------------------------------------------------------- #
# Gráficos
# --------------------------------------------------------------------------- #
def test_cinco_graficos_com_setoption_direto(html):
    """Regressão do mapa: `setupChart` faz JSON.stringify das opções e DESCARTA
    funções — matava os formatters e o tooltip caía em "series0"."""
    assert 'setOption(opts, true)' in html
    assert 'setupChart(' not in CODIGO
    assert 'data-echart' not in CODIGO
    for ref in ('chTipo', 'chAno', 'chTribunal', 'chClassif', 'chEntidade'):
        assert f'x-ref="{ref}"' in html
        assert f'pintar{ref[2:]}' in html or ref in html
    # toda série é NOMEADA (sem isso o tooltip de fallback diz "series0")
    # 2 séries nomeiam inline; as outras 3 recebem o nome como argumento do
    # helper de barra horizontal
    assert html.count("'Processos'") >= 5
    assert 'series0' not in CODIGO
    # tooltip com formatter de verdade
    assert 'formatter: formatterTooltip' in html
    assert 'do recorte' in html


def test_tema_observado_de_verdade(html):
    """Não existe evento 'voy:theme' no base.html — o toggle só mexe na classe
    `dark` do <html> e o initAllCharts() ignora estes gráficos (sem data-echart)."""
    assert 'MutationObserver' in html
    assert "attributeFilter: ['class']" in html
    assert "addEventListener('voy:theme'" not in CODIGO
    # instância descartada quando o tema vira (o tema é fixado no init do ECharts)
    assert '_voyTema' in html
    assert 'chart.dispose()' in html


def test_rotulo_humano_no_lugar_do_enum(html):
    """"PRE_PRECATORIO" é nome de coluna de banco, não rótulo de gráfico."""
    assert "PRE_PRECATORIO: 'Pré-precatório'" in html
    assert "DIREITO_CREDITORIO: 'Direito creditório'" in html
    assert "NAO_LEAD: 'Não é lead'" in html
    assert 'ROTULO_CLASSIF' in html


def test_paleta_das_series_documentada_por_tema(html):
    """Cor de série é medida, não escolhida no olho: uma paleta por tema."""
    assert 'CORES_SERIE' in html
    assert 'CORES_CLASSIF' in html
    assert "dark:" in html and "light:" in html
    # famílias dos tokens da casa (mission / pale-blue / info / accent)
    for cor in ('#fdba74', '#93c5fd', '#7dd3fc', '#6ee7b7',
                '#c2410c', '#2563eb', '#0369a1', '#047857'):
        assert cor in html, f'cor de série sumiu da paleta: {cor}'


# --------------------------------------------------------------------------- #
# Copy: uma frase por número, igual à do mapa
# --------------------------------------------------------------------------- #
def _glossario(src: str) -> dict:
    bloco = src[src.index('window.GLOSSARIO'):]
    bloco = bloco[:bloco.index('};')]
    return dict(re.findall(r"^\s*(\w+):\s*'((?:[^'\\]|\\.)*)'", bloco, flags=re.M))


def test_glossario_compartilhado_e_identico_ao_do_mapa(html):
    """Consistência de copy vale mais que originalidade: o MESMO número não pode
    ganhar duas explicações em duas telas."""
    aqui = _glossario(html)
    la = _glossario(MAPA_SRC)
    # só as chaves que esta página REALMENTE usa — glossário sem chave morta
    compartilhadas = ('volume', 'potencial', 'confirmado', 'cobertura', 'todos')
    for k in compartilhadas:
        assert k in aqui, f'chave {k} sumiu do glossário da página do estado'
        assert k in la, f'chave {k} sumiu do glossário do mapa'
        assert aqui[k] == la[k], f'copy divergiu do mapa na chave {k}'
    # chaves novas desta página, na mesma régua (1 frase, <=140 caracteres)
    for k in ('tribunais', 'valorCausa'):
        assert k in aqui, f'chave nova ausente: {k}'
    for k, txt in aqui.items():
        assert len(txt) <= 140, f'glossário estourou 140 caracteres em {k}: {len(txt)}'


def test_sem_jargao_de_banco_na_tela(html):
    """Nada de nome de campo/enum/agregação em texto visível."""
    visivel = re.sub(r'<script.*?</script>', '', html, flags=re.S)
    # só TEXTO: nome de campo dentro de atributo Alpine (x-text/x-show) é código,
    # não sai na tela
    visivel = re.sub(r'<[^>]+>', ' ', visivel)
    for jargao in ('classe_nome', 'ano_cnj', 'valor_causa', 'tem_sinal_precatorio',
                   'participacoes', 'reverse_nested', 'bucket', 'terms agg',
                   'PRE_PRECATORIO', 'NAO_LEAD', 'Elasticsearch', 'nested'):
        assert jargao not in visivel, f'jargão vazou pra tela: {jargao}'


def test_formatacao_pt_br(html):
    assert "[1e12, 'tri']" in html                 # R$ 2,88 tri, não "2880 bi"
    assert "return 'não informado'" in html        # 0/nulo nunca é "R$ 0"
    assert 'menos de 0,1%' in html                 # 0<n<0,1 não vira "0%"
    assert 'fmtQtdCompacta' in html
    assert "' mil'" in html and "' mi'" in html
    assert 'fmtCompact(' not in CODIGO, 'voltou a usar o fmtCompact global (k/M)'
    for m in re.finditer(r'toFixed\(1\)((?:\.replace\([^)]*\))*)', CODIGO):
        assert "replace('.', ',')" in m.group(1), f'toFixed sem vírgula: {m.group(0)}'


# --------------------------------------------------------------------------- #
# Guardas da casa
# --------------------------------------------------------------------------- #
def test_sem_title_de_glossario_e_sem_hex_em_texto(html):
    """Explicação é `.voy-tip` (hover + foco + TAP) ou texto visível, nunca
    `title=` — que não abre no toque e é invisível pro teclado."""
    sem_django = CODIGO.replace('{% block title %}', '')
    sem_django = re.sub(r"with title=\w+", '', sem_django)
    assert not re.search(r'(?<![a-z])title=', sem_django)
    assert ':title=' not in CODIGO
    assert 'cursor-help' not in CODIGO
    # cor de TEXTO por token; hex só na paleta do canvas do ECharts
    assert 'style="color:#' not in CODIGO
    assert 'class="voy-tip' in html
    assert 'role="tooltip"' in html
    assert 'aria-describedby="tip-volume"' in html and 'id="tip-volume"' in html


def test_text_base_e_armadilha_de_cor_nesta_config(html):
    """`base` é token de COR no tailwind.config, então `.text-base` vira
    `color: var(--c-base)` — a cor do FUNDO — e vence o `text-fg`.

    Medido em browser: os títulos dos cards saíam em rgb(9,9,11) no tema escuro,
    invisíveis. Nenhum `text-base` pode voltar.
    """
    assert 'text-base' not in CODIGO
    assert 'text-[1rem]' in html


def test_sem_fonte_abaixo_de_12px_e_sem_fg_subtle(html):
    assert not re.search(r'text-\[0\.[0-6]', CODIGO), 'fonte abaixo de 12px'
    assert not re.search(r'text-\[([0-9]|1[01])px\]', CODIGO), 'fonte abaixo de 12px'
    # fg-subtle é 2,x:1 em número pequeno — texto de dado usa fg-muted
    assert 'text-fg-subtle' not in re.sub(
        r'(disabled:text-fg-subtle|hover:border-fg-subtle|bg-fg-subtle)', '', CODIGO)
    # tamanho de fonte dos rótulos do ECharts (canvas não tem classe Tailwind)
    assert 'fontSize: 11' not in CODIGO and 'fontSize: 10' not in CODIGO


def test_comentario_de_template_nao_vaza_na_tela(html):
    """`{# ... #}` MULTILINHA não existe no Django e vaza LITERAL na página: o
    lexer é `({%.*?%}|{{.*?}}|{#.*?#})` **sem** re.DOTALL."""
    multilinha = [
        i for i, linha in enumerate(TEMPLATE_SRC.split('\n'), 1)
        if '{#' in linha and '#}' not in linha
    ]
    assert not multilinha, f'{{# #}} multilinha (vaza na tela) nas linhas {multilinha}'
    corpo = html[html.find('Voltar ao Mapa de Precatórios'):]
    assert '{#' not in corpo and '{%' not in corpo


def test_sem_cdn_externo_na_pagina(html):
    """CSP: a página não pode introduzir nenhuma URL http(s) externa."""
    marca = 'Voltar ao Mapa de Precatórios'
    corpo = html[html.find(marca):] if marca in html else html
    for termo in ('cdn.jsdelivr', 'unpkg.com', 'cdnjs', 'code.jquery',
                  'fonts.googleapis', 'fonts.gstatic'):
        assert termo not in corpo, f'referência externa proibida no corpo: {termo}'
    assert not re.search(r'https?://', CODIGO), 'URL absoluta no template'


def test_acessibilidade(html):
    assert 'focus-visible:ring-2 focus-visible:ring-accent' in html
    assert 'ring-offset-base' in html
    assert 'aria-label="Ano do processo, de"' in html
    assert 'role="group" aria-labelledby="lbl-valor"' in html
    assert 'aria-label="O que os gráficos olham"' in html
    assert 'aria-expanded' in html and 'aria-controls="estado-ajuda"' in html
    assert 'sr-only' in MAPA_SRC   # o link novo do mapa avisa que abre outra aba


def test_estados_de_carregamento_e_erro(html):
    """Skeleton (não spinner eterno), erro humano com "Tentar de novo", e token
    de requisição pra resposta fora de ordem."""
    assert 'skeleton' in html
    assert 'acquiring signal' in html
    assert 'Não conseguimos carregar os números deste estado.' in html
    assert 'Tentar de novo' in html
    assert 'signal lost' in html
    assert '_seq' in html and 'seq !== this._seq' in html
    assert 'mostrarCarregandoNosGraficos' in html


# --------------------------------------------------------------------------- #
# A ponte: o link no mapa
# --------------------------------------------------------------------------- #
def test_mapa_tem_link_para_a_pagina_do_estado():
    """O drill-down do mapa abre a página do estado em ABA NOVA, com os filtros
    e a lente atuais — quem filtrou por ano espera chegar filtrado."""
    assert 'urlPaginaEstado' in MAPA_SRC
    assert 'overview-estado-page' in MAPA_SRC
    assert 'Ver página completa de ' in MAPA_SRC
    assert 'target="_blank" rel="noopener"' in MAPA_SRC
    assert 'metricaDaLente' in MAPA_SRC
    # tradução do nome da lente entre as duas telas (sinalMode ⇄ metrica)
    for m in ('confirmados', 'todos', 'possiveis'):
        assert f"return '{m}'" in MAPA_SRC
    # o link carrega a querystring dos filtros
    assert 'new URLSearchParams(this.querystring())' in MAPA_SRC


def test_mapa_nao_ganhou_url_externa_nem_title():
    """A única mudança no mapa é o link — as guardas dele continuam valendo."""
    mapa_codigo = _sem_comentarios(MAPA_SRC)
    assert not re.search(r'https?://', mapa_codigo)
    sem_django = mapa_codigo.replace('{% block title %}', '')
    sem_django = re.sub(r"with title='[^']*'", '', sem_django)
    assert not re.search(r'(?<![a-z])title=', sem_django)


def test_glossario_sem_chave_morta(html):
    """Chave definida e nunca chamada por `glos()` é copy que ninguém lê e que
    ninguém revisa — some antes de apodrecer."""
    definidas = set(_glossario(html))
    usadas = set(re.findall(r"glos\('(\w+)'\)", html))
    # `potencial`/`confirmado`/`todos` também entram por glosMetrica()
    usadas |= {'potencial', 'confirmado', 'todos'}
    assert definidas == usadas, f'chaves mortas: {sorted(definidas - usadas)}'
