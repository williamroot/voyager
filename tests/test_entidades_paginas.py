"""Testes das DUAS PÁGINAS DE ENTIDADES — ranking e ficha.

    dashboard/templates/dashboard/entidades.html   (ranking, `dashboard:entidades`)
    dashboard/templates/dashboard/entidade.html    (ficha,   `dashboard:entidade`)

As views só renderizam o shell — todo número vem dos endpoints
`/dashboard/api/entidades/...` via fetch no browser, então este teste NÃO
precisa de Elasticsearch nem de Postgres carregado.

Cobre, além do básico (login-gate, render, CSP), as regras que estas páginas não
podem regredir — todas nascidas de uma medição no cadastro real, não de gosto:

  * `n_processos` AUSENTE é "não contamos ainda", JAMAIS 0. Só ~182 mil das
    1,14 milhão de entidades foram contadas (a contagem é uma segunda passada
    ES→ES restrita a quem disputa a busca): 959 mil nunca foram. Zero é uma
    AFIRMAÇÃO ("esta entidade não tem processo") e seria mentira;
  * a contagem é pelo NOME ESCRITO NA PUBLICAÇÃO e casa em QUALQUER POLO — não
    é "quem deve", é "quem aparece". A tela precisa dizer isso, escrito;
  * a confiança da fusão vai em cada linha: `chave='cnpj'` é identidade provada
    por documento, `chave='nome'` é semelhança de grafia (dois homônimos podem
    ter colado). `n_partes` = 1 é nome visto uma vez na vida do acervo;
  * `valor_causa` é esparso (~2,8%) e é o valor DA CAUSA — o card de R$ só
    existe quando alguém publicou, e nunca escreve "R$ 0";
  * a Justiça Federal fica FORA do gráfico de estados (95,7% do INSS está lá;
    junto na mesma barra ela achata os 26 estados restantes num traço);
  * o bloco de identidade é AUDITORIA: as grafias e os `documentos_secundarios`
    (CNPJs que tribunais digitaram errado e que fundimos) têm de estar na tela,
    senão a fusão é inauditável;
  * guardas da casa: zero `title=`, zero hex em cor de texto, zero URL externa,
    `setOption(opts, true)` (o `setupChart` mata os formatters), comentário
    `{# #}` numa linha só, e nada de `text-base` — que nesta config do Tailwind
    é COR (o token `base`), não tamanho de fonte.

SHIM DE ROTA (temporário, some sozinho)
=======================================
As views/rotas são entregues por outro agente. Enquanto `dashboard:entidades` e
`dashboard:entidade` não existirem, o fixture `rotas` registra duas views
mínimas que renderizam os MESMOS templates — o que este arquivo testa é o
template. Quando as rotas reais entrarem, o shim se desliga sozinho (o
`reverse` passa a resolver) e os testes seguem valendo, agora contra a view de
verdade.
"""
from __future__ import annotations

import re
from pathlib import Path

import dashboard
import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import NoReverseMatch, clear_url_caches, reverse

pytestmark = pytest.mark.django_db

User = get_user_model()

TPL_DIR = Path(dashboard.__file__).parent / 'templates' / 'dashboard'
RANKING_SRC = (TPL_DIR / 'entidades.html').read_text(encoding='utf-8')
FICHA_SRC = (TPL_DIR / 'entidade.html').read_text(encoding='utf-8')
MAPA_SRC = (TPL_DIR / 'overview_mapa.html').read_text(encoding='utf-8')
ESTADO_SRC = (TPL_DIR / 'overview_estado.html').read_text(encoding='utf-8')

ID_EXEMPLO = 'cnpj:29979036'


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


COD_RANKING = _sem_comentarios(RANKING_SRC)
COD_FICHA = _sem_comentarios(FICHA_SRC)
CODIGOS = {'entidades.html': COD_RANKING, 'entidade.html': COD_FICHA}


# --------------------------------------------------------------------------- #
# Shim de rota (ver docstring do módulo)
# --------------------------------------------------------------------------- #
def _registrar_shim():
    """Publica `dashboard:entidades` / `dashboard:entidade` se ainda não houver."""
    from django.contrib.auth.decorators import login_required
    from django.shortcuts import render
    from django.urls import path
    from django.views.decorators.http import require_GET

    import dashboard.urls as durls

    @login_required
    @require_GET
    def _ranking(request):
        return render(request, 'dashboard/entidades.html', {})

    @login_required
    @require_GET
    def _ficha(request, entidade_id):
        return render(request, 'dashboard/entidade.html', {'entidade_id': entidade_id})

    durls.urlpatterns += [
        path('overview/entidades/', _ranking, name='entidades'),
        path('overview/entidade/<str:entidade_id>/', _ficha, name='entidade'),
    ]
    clear_url_caches()


@pytest.fixture(scope='session', autouse=True)
def rotas(django_db_setup):
    try:
        reverse('dashboard:entidades')
    except NoReverseMatch:
        _registrar_shim()
    yield


@pytest.fixture
def user(db):
    return User.objects.create_user(username='entidades_gate', password='x')


@pytest.fixture
def cli(user):
    c = Client(SERVER_NAME='localhost')
    c.force_login(user)
    return c


@pytest.fixture
def ranking(cli):
    resp = cli.get(reverse('dashboard:entidades'))
    assert resp.status_code == 200
    return resp.content.decode()


@pytest.fixture
def ficha(cli):
    resp = cli.get(reverse('dashboard:entidade', args=[ID_EXEMPLO]))
    assert resp.status_code == 200
    return resp.content.decode()


# --------------------------------------------------------------------------- #
# Rotas, gate e shell
# --------------------------------------------------------------------------- #
def test_rotas_resolvem():
    assert reverse('dashboard:entidades')
    assert reverse('dashboard:entidade', args=[ID_EXEMPLO])


def test_anonimo_redireciona_para_login(client):
    for url in (reverse('dashboard:entidades'),
                reverse('dashboard:entidade', args=[ID_EXEMPLO])):
        resp = client.get(url)
        assert resp.status_code == 302, url
        assert '/login' in resp['Location'], url


def test_ranking_renderiza(ranking):
    assert 'Cadastro de entidades' in ranking
    assert 'Voltar ao Mapa de Precatórios' in ranking
    assert 'entidadesRanking()' in ranking


def test_ficha_renderiza_e_recebe_o_id(ficha):
    assert 'fichaEntidade()' in ficha
    assert 'Identidade: o que foi juntado aqui' in ficha
    assert f'window.ENTIDADE_ID = "{ID_EXEMPLO}"' in ficha
    assert 'Todas as entidades' in ficha


def test_ficha_sabe_se_virar_sem_o_contexto(ficha):
    """A view manda o id no contexto; se algum dia não mandar, a URL ainda tem.

    Sem este caminho a página ficaria inerte (fetch de `/api/entidades//`) por
    causa de uma variável de contexto ausente.
    """
    assert 'idDaUrl()' in ficha
    assert 'window.location.pathname.split' in ficha


def test_shell_puro_nao_toca_o_es(cli, monkeypatch):
    """As páginas não podem consultar o índice: se o ES cair, elas ainda abrem.

    (Quem devolve 503 é o endpoint JSON, e as duas telas tratam isso com
    mensagem humana + "Tentar de novo".)
    """
    from search import agg_estado

    def explode(*a, **kw):   # pragma: no cover - só roda se houver regressão
        raise AssertionError('a página de entidades consultou o Elasticsearch')

    monkeypatch.setattr(agg_estado, 'get_es', explode)
    assert cli.get(reverse('dashboard:entidades')).status_code == 200
    assert cli.get(reverse('dashboard:entidade', args=[ID_EXEMPLO])).status_code == 200


# --------------------------------------------------------------------------- #
# Honestidade 1: ausente != zero
# --------------------------------------------------------------------------- #
def test_contagem_ausente_e_palavra_nunca_zero(ranking, ficha):
    """959 mil das 1,14 milhão de entidades NUNCA foram contadas.

    Zero seria a afirmação "esta entidade não tem processo". A tela escreve
    "não contamos ainda" e o número nunca cai em 0.
    """
    for html in (ranking, ficha):
        assert 'não contamos ainda' in html
    assert 'semContagem' in ranking
    assert 'volumeDesconhecido' in ficha
    # o ranking diz, em texto e com o número do payload, quantas entidades do
    # RECORTE nunca foram contadas — e que isso não é zero
    assert 'naoContadasNoRecorte' in ranking
    assert 'cobertura_contagem' in ranking
    assert 'nunca foram contadas e aparecem com “não contamos ainda”' in ranking
    assert 'é zero processo' in ranking
    # e explica de onde vem o buraco
    assert '959 mil' in ranking
    # nenhuma das duas escreve um zero de contagem como se fosse medição
    for nome, cod in CODIGOS.items():
        assert 'n_processos || 0' not in cod, nome
        assert 'n_processos: 0' not in cod, nome


def test_desconhecido_nao_e_sinalizado_por_opacidade(ranking, ficha):
    """Opacidade pra dizer "não sei" mediu 2,5:1 na página do estado — quem não
    enxerga transparência lê o texto apagado como se não existisse."""
    for nome, cod in CODIGOS.items():
        assert 'opacity-' not in cod, nome
        assert 'opacity:.' not in cod, nome


def test_possiveis_desconhecido_na_ficha(ficha):
    """`possiveis` null / `sinal_processado` false = ainda não analisado."""
    assert 'possiveisDesconhecido' in ficha
    assert 'sinal_processado' in ficha
    assert 'ainda não analisado' in ficha
    assert 'cobDesconhecida' in ficha
    assert 'não sabemos ainda' in ficha


# --------------------------------------------------------------------------- #
# Honestidade 2: é o nome escrito na publicação, em qualquer polo
# --------------------------------------------------------------------------- #
def test_diz_que_e_o_nome_escrito_na_publicacao(ranking, ficha):
    for html in (ranking, ficha):
        assert 'como ele foi escrito na publicação' in html
        assert 'qualquer lado do processo' in html
    # "quem deve" é justamente o que o número NÃO é
    assert 'Não é “quem deve”: é quem aparece.' in ranking
    assert 'nome genérico' in ranking.lower()
    assert 'não é um vínculo estruturado' in ficha.lower()


def test_confianca_da_fusao_esta_em_cada_linha(ranking, ficha):
    """`chave='cnpj'` é identidade PROVADA; `chave='nome'` é heurística de
    grafia, e dois homônimos de cidades diferentes podem ter colado."""
    for html in (ranking, ficha):
        assert 'CNPJ confere' in html or 'CNPJ <span' in html
        assert 'juntado pelo nome' in html
        assert "chave === 'cnpj'" in html
    assert 'provada(' in ranking and 'provada()' in ficha
    # atestação: 1 linha de cadastro é nome visto uma vez na vida
    for html in (ranking, ficha):
        assert 'poucaAtestacao' in html
        assert 'acervo viu este nome uma vez' in html
    # `confianca` do payload é lida antes de `chave` (a origem dela)
    assert "it.confianca === 'forte'" in ranking


def test_ente_publico_e_heuristica_e_a_tela_diz(ranking):
    assert 'ente público' in ranking
    assert 'não por cadastro oficial' in ranking
    assert 'eh_ente_publico' in ranking


# --------------------------------------------------------------------------- #
# Honestidade 3: ordenação, valor esparso, Justiça Federal
# --------------------------------------------------------------------------- #
def test_ordenacao_avisa_quem_fica_fora_da_disputa(ranking):
    """Ordenar por processos esconde 959 mil entidades sem contagem — dizer isso
    é o que impede a leitura "estas são as maiores do Brasil"."""
    assert 'notaOrdem' in ranking
    assert 'não disputa esta ordem' in ranking
    for chave in ('n_processos', 'n_partes', 'n_variantes'):
        assert f"chave: '{chave}'" in ranking


def test_cardume_de_facetas_esta_escrito_e_tem_ferramenta(ranking):
    """Medido no top-20 real por processos (13/08/2026): o INSS ocupa 8 posições
    e a União 3 — a mesma entidade pendurada em grafias e CNPJs diferentes.

    A lista ranqueia NOMES, não pessoas jurídicas consolidadas. Dizer isso é
    obrigatório, e a ferramenta (atestação mínima) tem que estar ao alcance.
    """
    assert 'ranqueia <b class="text-fg">nomes</b>' in ranking
    assert '8 das 20 primeiras posições' in ranking
    assert 'Atestação mínima' in ranking
    assert 'min_partes' in ranking
    assert 'notaAtestacao' in ranking
    for valor in ('valor: 0', 'valor: 2', 'valor: 10'):
        assert valor in ranking
    # o corte de 10 é o número medido, não um chute
    assert '1,14 milhão para cerca de 1.600 entidades' in ranking


def test_relevancia_so_existe_quando_ha_busca(ranking):
    """Ordenar por relevância sem termo é ordenar por relevância de nada — o
    backend cai no default e o botão aceso mentiria sobre a ordem."""
    assert 'ordemDisponivel' in ranking
    assert 'ordenarEfetivo' in ranking
    assert "'relevancia' && !this.qValido()" in ranking


def test_ranking_le_o_nome_novo_do_cursor_e_o_alias(ranking):
    """`proximo_cursor` é o contrato; `cursor_proximo` é alias que vai sumir."""
    assert 'json.proximo_cursor || json.cursor_proximo' in ranking


def test_possiveis_e_piso_e_nao_fatia(ficha):
    """`tem_sinal_precatorio` só foi computado em ~3,4% da base, e quase sempre
    onde é TRUE: dividir possíveis por processos dá uma proporção FALSA."""
    assert 'sinalEhPiso' in ficha
    assert 'cobertura_sinal' in ficha
    assert 'não uma\n      fatia' in ficha or 'não uma' in ficha
    assert 'não divida um número pelo outro' in ficha


def test_ficha_mostra_como_o_numero_foi_obtido(ficha):
    """A diferença entre este número e o de uma busca por uma grafia só precisa
    de explicação — senão parece bug."""
    assert 'Como este número foi obtido' in ficha
    assert 'nGrafiasDaConsulta' in ficha
    assert 'grafiasDescartadas' in ficha
    assert 'consultaTruncada' in ficha
    assert 'Buscar uma grafia só dá um número menor' in ficha


def test_valor_da_causa_some_quando_ninguem_publicou(ficha):
    """`valor_causa` está preenchido em ~2,8% da base. Um "R$ 0" em corpo de KPI
    seria lido como "não há dinheiro aqui"."""
    assert 'temValor' in ficha
    assert 'cobertura_valor' in ficha
    assert 'x-show="temValor()"' in ficha
    assert 'não o valor do precatório' in ficha
    assert 'R$ 0' not in COD_FICHA
    assert "return 'não informado'" in ficha


def test_justica_federal_fica_fora_do_grafico_de_estados(ficha):
    """Medido no INSS (13/08/2026): 4.212.666 de 4.402.239 estão em FED (95,7%).

    Na mesma barra dos estados ela achata os 26 restantes num traço e o leitor
    conclui "esta entidade quase não aparece em MG".
    """
    assert 'bucketFED' in ficha
    assert 'pctFED' in ficha
    assert "i.uf !== 'FED'" in ficha
    assert 'Justiça Federal' in ficha
    assert 'não entra no' in ficha
    # e o vazio resultante é explicado, não vira gráfico fantasma
    assert 'ufVazio' in ficha
    assert 'não quer dizer que ela não litiga nos estados' in ficha
    # a frase de cobertura conta a FED (ela É um bucket de uf) mas as barras
    # não a desenham — sem a ressalva, "nenhum ficou de fora" logo acima de 9
    # barras que somam 4% do total é uma contradição na mesma tela
    assert 'está nessa conta, mas fora das barras' in ficha


def test_cabecalho_da_lista_nao_empurra_a_lista_pra_baixo_da_dobra(ranking):
    """Medido em browser: com três cards empilhados (caveats, busca, ordenação)
    o 1º item começava a 909px — abaixo da dobra em 1440x900 E em 1366x768.

    É o mesmo erro que o mapa cometeu com as suas quatro faixas de cabeçalho, e
    o produto desta tela é a LISTA. Um card só (caveat + disclosure + controles)
    trouxe o 1º item pra 715px. O teste trava a ESTRUTURA; a altura em pixels
    é gate de browser (scratchpad/entgate/fold.py).
    """
    corpo = ranking[ranking.find('id="pagina-entidades"'):]
    # exatamente um card antes da contagem da lista
    ate_lista = corpo[:corpo.find('Cadastro de entidades')]
    assert ate_lista.count('class="card p-4') == 1, 'voltaram os cards empilhados'
    # e os controles moram dentro dele (separados por uma borda, não por um card)
    assert 'border-t border-border pt-3 flex flex-wrap items-end' in ranking


def test_ano_corrente_parcial_esta_dito(ficha):
    """A última ponta da linha é sempre um ano incompleto — sem a frase, o
    leitor lê queda de litígio onde há só calendário."""
    assert 'é um ano incompleto, não' in ficha
    assert 'fora_da_faixa' in ficha
    assert 'ano impossível' in ficha


# --------------------------------------------------------------------------- #
# Auditoria da fusão (o bloco de identidade)
# --------------------------------------------------------------------------- #
def test_grafias_ficam_na_tela_e_a_amostra_e_declarada(ficha):
    assert 'Como os tribunais escrevem este nome' in ficha
    assert 'grafiasParciais' in ficha
    assert 'grafias_exemplo' in ficha       # tolera o nome do campo do autocomplete
    assert 'Esta é uma <b class="text-fg">amostra</b>' in ficha


def test_documentos_secundarios_sao_expostos_como_erro_do_tribunal(ficha):
    """São CNPJs que o tribunal digitou ERRADO e que fundimos nesta entidade
    (decisão 12 de `search/entidades.py`). Esconder isso torna a fusão
    inauditável — e deixa sem resposta quem procurar por aquele CNPJ."""
    assert 'documentos_secundarios' in ficha
    assert 'CNPJs digitados errado e fundidos aqui' in ficha
    assert 'não</b> são CNPJs desta pessoa jurídica' in ficha
    assert 'documentosSecundarios' in ficha


def test_ficha_explica_como_a_entidade_foi_montada(ficha):
    assert 'Como esta entidade foi montada' in ficha
    assert 'raiz do CNPJ' in ficha
    assert 'nome normalizado' in ficha
    assert 'homônimos' in ficha
    assert 'fmtRaiz' in ficha and 'fmtDoc' in ficha


# --------------------------------------------------------------------------- #
# Gráficos
# --------------------------------------------------------------------------- #
def test_quatro_graficos_com_setoption_direto(ficha):
    """Regressão do mapa: `setupChart` faz JSON.stringify das opções e DESCARTA
    funções — matava os formatters e o tooltip caía em "series0"."""
    assert 'setOption(opts, true)' in ficha
    assert 'setupChart(' not in COD_FICHA
    assert 'data-echart' not in COD_FICHA
    for ref in ('chUf', 'chAno', 'chTribunal', 'chClassif'):
        assert f'x-ref="{ref}"' in ficha
    for pintor in ('pintarUf', 'pintarAno', 'pintarTribunal', 'pintarClassificacao'):
        assert pintor in ficha
    # toda série é NOMEADA (sem isso o tooltip de fallback diz "series0")
    assert ficha.count("'Processos'") >= 4
    assert 'series0' not in COD_FICHA
    assert 'formatter: formatterTooltip' in ficha
    # gráfico vazio não é pintado: quem fala é o estado vazio
    assert ficha.count('if (!itens.length) return;') >= 3


def test_resize_repinta_em_vez_de_so_redimensionar(ficha):
    """`resize()` sozinho reaproveita a opção anterior — é assim que série com
    mapeamento de cor perde a paleta (medido no mapa comercial, 12/08/2026)."""
    assert 'observarJanela' in ficha
    assert "addEventListener('resize'" in ficha
    assert 'setTimeout(() => this.pintar(), 120)' in ficha


def test_tema_observado_de_verdade(ficha):
    """Não existe evento 'voy:theme' no base.html — o toggle só mexe na classe
    `dark` do <html> e o initAllCharts() ignora estes gráficos."""
    assert 'MutationObserver' in ficha
    assert "attributeFilter: ['class']" in ficha
    assert "addEventListener('voy:theme'" not in COD_FICHA
    assert '_voyTema' in ficha
    assert 'chart.dispose()' in ficha


def test_rotulo_humano_no_lugar_do_enum(ficha):
    """"PRE_PRECATORIO" é nome de coluna de banco, não rótulo de gráfico. E "SP"
    é sigla de eixo, não nome de estado."""
    assert "PRE_PRECATORIO: 'Pré-precatório'" in ficha
    assert "NAO_LEAD: 'Não é lead'" in ficha
    assert 'ROTULO_CLASSIF' in ficha
    assert 'NOME_UF' in ficha
    assert "SP: 'São Paulo'" in ficha


def test_paleta_das_series_documentada_por_tema(ficha):
    """Cor de série é medida, não escolhida no olho: uma paleta por tema."""
    assert 'CORES_SERIE' in ficha
    assert 'CORES_CLASSIF' in ficha
    assert 'dark:' in ficha and 'light:' in ficha
    for cor in ('#fdba74', '#93c5fd', '#7dd3fc', '#6ee7b7',
                '#c2410c', '#2563eb', '#0369a1', '#047857'):
        assert cor in ficha, f'cor de série sumiu da paleta: {cor}'


# --------------------------------------------------------------------------- #
# Estados de carga, erro e vazio
# --------------------------------------------------------------------------- #
def test_estados_de_carregamento_e_erro(ranking, ficha):
    """Skeleton (não spinner eterno), erro humano com "Tentar de novo", e token
    de requisição pra resposta fora de ordem."""
    for html in (ranking, ficha):
        assert 'skeleton' in html
        assert 'Tentar de novo' in html
        assert 'signal lost' in html
        assert '_seq' in html and 'seq !== this._seq' in html
    assert 'acquiring signal' in ficha
    assert 'Não conseguimos carregar o cadastro de entidades.' in ranking
    assert 'Não conseguimos carregar a ficha desta entidade.' in ficha


def test_lista_vazia_e_entidade_inexistente_tem_explicacao(ranking, ficha):
    assert 'Nenhuma entidade com esse nome no cadastro.' in ranking
    assert 'semResultado' in ranking
    assert 'Não encontramos esta entidade no cadastro.' in ficha
    assert 'naoEncontrada' in ficha
    # 404 do endpoint vira tela explicada, não erro de servidor
    assert 'resp.status === 404' in ficha


def test_paginacao_por_cursor(ranking):
    assert 'cursor_proximo' in ranking
    assert 'carregarMais' in ranking
    assert 'Carregar mais entidades' in ranking
    assert 'Fim da lista.' in ranking
    # trocar o recorte volta pra página 1 (senão a página 2 do recorte antigo
    # ficaria grudada na página 1 do novo)
    assert 'recarregar()' in ranking
    assert 'this.cursor = null' in ranking


def test_busca_tem_minimo_de_caracteres_e_diz_isso(ranking):
    assert 'Q_MIN' in ranking
    assert 'qCurta' in ranking
    assert 'Escreva ao menos' in ranking
    # e o estado da tela cabe num link
    assert 'sincronizarUrl' in ranking
    assert 'history.replaceState' in ranking
    assert 'lerQuerystring' in ranking


# --------------------------------------------------------------------------- #
# Copy: uma frase por número, igual à das outras telas
# --------------------------------------------------------------------------- #
def _glossario(src: str) -> dict:
    bloco = src[src.index('window.GLOSSARIO'):]
    bloco = bloco[:bloco.index('};')]
    return dict(re.findall(r"^\s*(\w+):\s*'((?:[^'\\]|\\.)*)'", bloco, flags=re.M))


def test_ranking_nao_tem_glossario_de_tooltip(ranking):
    """A tela do ranking explica em TEXTO VISÍVEL, não em hover.

    O que é preciso saber pra não ler o número errado (é busca por texto; casa
    em qualquer polo; ausência não é zero) não pode depender de passar o mouse —
    e um glossário definido e nunca chamado seria copy que ninguém revisa.
    """
    assert 'window.GLOSSARIO' not in ranking
    assert "glos('" not in ranking


def test_glossario_da_ficha_e_identico_ao_do_mapa(ficha):
    """Consistência de copy vale mais que originalidade: o MESMO número não pode
    ganhar duas explicações em três telas.

    Com UM limite: consistência não vale mentir. `potencial` e `cobertura`
    falam de "este estado" no mapa e na página do estado — porque lá o sujeito
    É o estado. Aqui o sujeito é a entidade, então a frase troca de sujeito e
    mantém estrutura e tom. `confirmado` e `todos` descrevem um fato sobre o
    PROCESSO e continuam palavra por palavra iguais.
    """
    aqui = _glossario(ficha)
    la = _glossario(MAPA_SRC)
    # `volume` fica de fora: o número herói desta ficha é outro (processos em
    # que ESTE NOME aparece), e dar a ele a frase do volume do estado seria a
    # mesma explicação pra dois números diferentes.
    for k in ('confirmado', 'todos'):
        assert k in aqui, f'chave {k} sumiu do glossário da ficha'
        assert aqui[k] == la[k], f'copy divergiu do mapa na chave {k}'
    # as adaptadas: mesma estrutura, sujeito trocado, e NUNCA falando de estado
    for k in ('potencial', 'cobertura'):
        assert k in aqui, f'chave {k} sumiu do glossário da ficha'
        assert 'estado' not in aqui[k], (
            f'{k} fala de "estado" numa ficha de entidade: {aqui[k]!r}')
        assert aqui[k] != la[k]
    # a régua vale pras chaves novas também
    for k, txt in aqui.items():
        assert len(txt) <= 140, f'glossário estourou 140 caracteres em {k}: {len(txt)}'


def test_glossario_sem_chave_morta(ficha):
    """Chave definida e nunca chamada por `glos()` é copy que ninguém lê e que
    ninguém revisa — some antes de apodrecer."""
    definidas = set(_glossario(ficha))
    usadas = set(re.findall(r"glos\('(\w+)'\)", ficha))
    assert definidas == usadas, f'chaves mortas: {sorted(definidas - usadas)}'


def test_sem_jargao_de_banco_na_tela(ranking, ficha):
    """Nada de nome de campo/enum/agregação em texto visível."""
    for html in (ranking, ficha):
        visivel = re.sub(r'<script.*?</script>', '', html, flags=re.S)
        visivel = re.sub(r'<[^>]+>', ' ', visivel)
        for jargao in ('n_processos', 'n_partes', 'n_variantes', 'entidade_id',
                       'raiz_cnpj', 'eh_ente_publico', 'valor_causa', 'ano_cnj',
                       'PRE_PRECATORIO', 'NAO_LEAD', 'Elasticsearch', 'nested',
                       'reverse_nested', 'cursor_proximo', 'documentos_secundarios'):
            assert jargao not in visivel, f'jargão vazou pra tela: {jargao}'


def test_formatacao_pt_br(ranking, ficha):
    assert "[1e12, 'tri']" in ficha                 # R$ 6,09 bi, não "6092 mi"
    assert 'menos de 0,1%' in ficha                 # 0<n<0,1 não vira "0%"
    for html in (ranking, ficha):
        assert 'fmtQtdCompacta' in html
        assert "' mil'" in html and "' mi'" in html
    for nome, cod in CODIGOS.items():
        assert 'fmtCompact(' not in cod, f'{nome}: voltou a usar o fmtCompact global (k/M)'
        for m in re.finditer(r'toFixed\(1\)((?:\.replace\([^)]*\))*)', cod):
            assert "replace('.', ',')" in m.group(1), f'{nome}: toFixed sem vírgula'


# --------------------------------------------------------------------------- #
# Guardas da casa
# --------------------------------------------------------------------------- #
def test_sem_title_de_glossario_e_sem_hex_em_texto(ranking, ficha):
    """Explicação é `.voy-tip` (hover + foco + TAP) ou texto visível, nunca
    `title=` — que não abre no toque e é invisível pro teclado."""
    for nome, cod in CODIGOS.items():
        sem_django = cod.replace('{% block title %}', '')
        sem_django = re.sub(r'with title=\S+', '', sem_django)
        assert not re.search(r'(?<![a-z])title=', sem_django), nome
        assert ':title=' not in cod, nome
        assert 'cursor-help' not in cod, nome
        assert 'style="color:#' not in cod, nome
    assert 'class="voy-tip' in ficha
    assert 'role="tooltip"' in ficha
    assert 'aria-describedby="tip-processos"' in ficha and 'id="tip-processos"' in ficha


def test_text_base_e_armadilha_de_cor_nesta_config(ranking, ficha):
    """`base` é token de COR no tailwind.config, então `.text-base` vira
    `color: var(--c-base)` — a cor do FUNDO — e vence o `text-fg`."""
    for nome, cod in CODIGOS.items():
        assert 'text-base' not in cod, nome
    for html in (ranking, ficha):
        assert 'text-[1rem]' in html


def test_sem_fonte_abaixo_de_12px_e_sem_fg_subtle(ranking, ficha):
    for nome, cod in CODIGOS.items():
        assert not re.search(r'text-\[0\.[0-6]', cod), f'{nome}: fonte abaixo de 12px'
        assert not re.search(r'text-\[([0-9]|1[01])px\]', cod), f'{nome}: fonte abaixo de 12px'
        limpo = re.sub(r'(disabled:text-fg-subtle|hover:border-fg-subtle|bg-fg-subtle)', '', cod)
        assert 'text-fg-subtle' not in limpo, nome
        # tamanho de fonte dos rótulos do ECharts (canvas não tem classe Tailwind)
        assert 'fontSize: 11' not in cod and 'fontSize: 10' not in cod, nome


def test_comentario_de_template_nao_vaza_na_tela(ranking, ficha):
    """`{# ... #}` MULTILINHA não existe no Django e vaza LITERAL na página: o
    lexer é `({%.*?%}|{{.*?}}|{#.*?#})` **sem** re.DOTALL."""
    for nome, src in (('entidades.html', RANKING_SRC), ('entidade.html', FICHA_SRC)):
        multilinha = [
            i for i, linha in enumerate(src.split('\n'), 1)
            if '{#' in linha and '#}' not in linha
        ]
        assert not multilinha, f'{nome}: {{# #}} multilinha nas linhas {multilinha}'
    for html, marca in ((ranking, 'Cadastro de entidades'),
                        (ficha, 'Identidade: o que foi juntado aqui')):
        corpo = html[html.find(marca):]
        assert '{#' not in corpo and '{%' not in corpo


def test_sem_cdn_externo(ranking, ficha):
    """CSP estrita: as páginas não podem introduzir nenhuma URL http(s) externa.

    O recorte começa no marcador de cada página — o `base.html` tem as suas
    próprias referências (Google Fonts), que não são responsabilidade daqui.
    """
    for nome, cod in CODIGOS.items():
        assert not re.search(r'https?://', cod), f'{nome}: URL absoluta no template'
    for html, marca in ((ranking, 'Voltar ao Mapa de Precatórios'),
                        (ficha, 'Todas as entidades')):
        corpo = html[html.find(marca):]
        for termo in ('cdn.jsdelivr', 'unpkg.com', 'cdnjs', 'code.jquery',
                      'fonts.googleapis', 'fonts.gstatic'):
            assert termo not in corpo, f'referência externa proibida: {termo}'


def test_acessibilidade(ranking, ficha):
    for html in (ranking, ficha):
        assert 'focus-visible:ring-2 focus-visible:ring-accent' in html
        assert 'ring-offset-base' in html
    assert 'aria-pressed' in ranking
    assert 'role="group" aria-labelledby="lbl-ordem"' in ranking
    assert 'aria-label="Limpar a busca"' in ranking
    assert 'aria-describedby="ajuda-busca"' in ranking
    assert 'aria-expanded' in ranking and 'aria-controls="entidades-ajuda"' in ranking
    assert 'aria-label="Entidades do cadastro"' in ranking
    assert 'aria-expanded' in ficha
    # o item da lista é um link de verdade (foco por teclado de graça)
    assert '<a :href="urlFicha(it)"' in ranking


def test_ranking_lista_e_semantica(ranking):
    """Ranking é lista ordenada — leitor de tela anuncia posição e total."""
    assert '<ol' in ranking
    assert '<li>' in ranking


# --------------------------------------------------------------------------- #
# As pontes entre as telas
# --------------------------------------------------------------------------- #
def test_mapa_tem_link_para_a_lista_de_entidades():
    """O dono perguntou "cadê a página de entidades?" — ela precisa ser
    alcançável de onde ele estava (o mapa)."""
    assert "{% url 'dashboard:entidades' as url_entidades %}" in MAPA_SRC
    assert 'Todas as entidades' in MAPA_SRC
    # e o link renderiza de fato agora que a rota existe
    from django.test import Client as _C
    from django.contrib.auth import get_user_model as _G
    u = _G().objects.create_user(username='ponte_mapa', password='x')
    c = _C(SERVER_NAME='localhost')
    c.force_login(u)
    corpo = c.get(reverse('dashboard:overview-mapa-page')).content.decode()
    assert reverse('dashboard:entidades') in corpo
    assert 'Todas as entidades' in corpo


def test_mapa_aceita_o_recorte_por_entidade_vindo_da_ficha():
    """A ficha manda `?entidade_id=` pro mapa; sem ler a querystring, o mapa
    abria SEM filtro nenhum e mostrava o Brasil inteiro como se fosse a
    entidade."""
    assert 'lerQuerystringEntidade' in MAPA_SRC
    assert "p.get('entidade_id')" in MAPA_SRC


def test_ficha_linka_o_mapa_recortado_por_ela(ficha):
    assert 'urlMapa()' in ficha
    assert "p.set('entidade_id', this.id)" in ficha
    assert 'Ver esta entidade no mapa' in ficha


def test_mapa_nao_ganhou_url_externa_nem_title():
    """As guardas do mapa continuam valendo depois da mudança."""
    cod = _sem_comentarios(MAPA_SRC)
    assert not re.search(r'https?://', cod)
    sem_django = cod.replace('{% block title %}', '')
    sem_django = re.sub(r"with title='[^']*'", '', sem_django)
    assert not re.search(r'(?<![a-z])title=', sem_django)


def test_copy_de_ausencia_de_contagem_e_a_mesma_do_autocomplete():
    """O autocomplete do mapa já escreve "não contamos ainda" — as três telas
    dizem a mesma coisa com as mesmas palavras."""
    assert 'não contamos ainda' in MAPA_SRC
    assert 'não contamos ainda' in RANKING_SRC
    assert 'não contamos ainda' in FICHA_SRC


def test_ficha_reusa_a_copy_de_cobertura_da_pagina_do_estado(ficha):
    """"Desenhado sobre N de M" é o componente de honestidade da página do
    estado; a ficha usa a MESMA frase, não uma variação."""
    assert 'Desenhado sobre' in ficha
    assert 'Desenhado sobre' in ESTADO_SRC
    assert 'coberturaCritica' in ficha
    assert 'cobertura_amostra' in ficha
