"""Testes da TELA DE BUSCA DE PROCESSOS.

    dashboard/templates/dashboard/busca.html   (`dashboard:busca`)

A tela é um SHELL: todo número vem de `GET /dashboard/api/busca/processos/` via
fetch no browser. Este arquivo não precisa de Elasticsearch nem de Postgres
carregado — e testa justamente que a página abre mesmo com o índice fora.

O que estes testes travam (nada aqui é gosto; cada item nasceu de uma medição no
índice real de produção em 13/08/2026, sobre 71.181.213 processos):

  * CPF/CNPJ da parte existe em 0,14% dos processos (102.622 docs). A tela tem
    de dizer isso ANTES da busca e DEPOIS do zero — senão o operador lê "zero"
    como "essa pessoa não tem processo", que é a conclusão oposta da verdade;
  * o documento está gravado COM MÁSCARA no índice (`29.979.036/0001-40` acha
    23.217; `29979036000140` acha ZERO). A tela nunca pode pedir formato;
  * VARA existe em 1,8% e VALOR DA CAUSA em 2,8%: filtrar por eles descarta
    quase toda a base, e o preço vai ao lado do campo, antes do clique;
  * o "0 resultados" seco do PJe é o pior defeito da busca dele — a tela
    distingue dígito verificador errado, filtro caro, campo sem cobertura e
    processo fora do acervo, cada um com o número que sustenta e um botão;
  * detecção de tipo no cliente (CNJ mod-97, CPF/CNPJ dígito verificador, OAB
    com UF): é o que substitui os 8 campos vazios do PJe;
  * o estado inteiro vive na querystring — back/forward e link compartilhado;
  * `/` foca a busca, o que exige `name="q"` no input (o atalho global do
    `base.html` procura por `input[name="q"], input[name="cnj"]`; trocar o nome
    quebraria o atalho SEM erro nenhum no console);
  * guardas da casa: zero `title=`, zero hex em cor, zero URL externa,
    comentário `{# #}` numa linha só, nada de `text-base` (que nesta config do
    Tailwind é COR — o token `base` é o fundo da página — e não tamanho), e
    todo alias de ícone existindo no sprite (alias inexistente vira QUADRADO
    VAZIO, silenciosamente).

SHIM DE ROTA (temporário, some sozinho)
=======================================
A view/rota é entregue por outro agente. Enquanto `dashboard:busca` não
existir, o fixture `rotas` registra uma view mínima que renderiza o MESMO
template — o que este arquivo testa é o template. Quando a rota real entrar, o
shim se desliga sozinho (o `reverse` passa a resolver) e os testes seguem
valendo, agora contra a view de verdade.
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
BUSCA_SRC = (TPL_DIR / 'busca.html').read_text(encoding='utf-8')
BASE_SRC = (TPL_DIR / 'base.html').read_text(encoding='utf-8')
SPRITE = (Path(dashboard.__file__).parent / 'static' / 'dashboard'
          / 'voyager-icons.svg').read_text(encoding='utf-8')


def _sem_comentarios(src: str) -> str:
    """Template sem comentários — base das asserções NEGATIVAS.

    Vários comentários citam de propósito o que foi corrigido (`text-base`,
    `title=`). Sem tirá-los, o próprio comentário derruba o teste que garante
    que o bug não voltou.
    """
    src = re.sub(r'\{#.*?#\}', '', src, flags=re.S)
    src = re.sub(r'\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}', '', src, flags=re.S)
    src = re.sub(r'^\s*//.*$', '', src, flags=re.M)
    src = re.sub(r'(?<![:\'"])//.*$', '', src, flags=re.M)
    return src


COD = _sem_comentarios(BUSCA_SRC)


# --------------------------------------------------------------------------- #
# Shim de rota (ver docstring do módulo)
# --------------------------------------------------------------------------- #
def _registrar_shim():
    from django.contrib.auth.decorators import login_required
    from django.shortcuts import render
    from django.urls import path
    from django.views.decorators.http import require_GET

    import dashboard.urls as durls

    @login_required
    @require_GET
    def _busca(request):
        return render(request, 'dashboard/busca.html', {})

    durls.urlpatterns += [path('busca/', _busca, name='busca')]
    clear_url_caches()


@pytest.fixture(scope='session', autouse=True)
def rotas(django_db_setup):
    try:
        reverse('dashboard:busca')
    except NoReverseMatch:
        _registrar_shim()
    yield


@pytest.fixture
def user(db):
    return User.objects.create_user(username='busca_gate', password='x')


@pytest.fixture
def cli(user):
    c = Client(SERVER_NAME='localhost')
    c.force_login(user)
    return c


@pytest.fixture
def pagina(cli):
    resp = cli.get(reverse('dashboard:busca'))
    assert resp.status_code == 200
    return resp.content.decode()


# --------------------------------------------------------------------------- #
# Rota, gate e shell
# --------------------------------------------------------------------------- #
def test_rota_resolve():
    assert reverse('dashboard:busca')


def test_anonimo_redireciona_para_login(client):
    resp = client.get(reverse('dashboard:busca'))
    assert resp.status_code == 302
    assert '/login' in resp['Location']


def test_pagina_renderiza(pagina):
    assert 'buscaProcessos()' in pagina
    assert 'Busca de processos' in pagina


def test_shell_puro_nao_toca_o_es(cli, monkeypatch):
    """Se o Elasticsearch cair, a PÁGINA ainda tem que abrir.

    Quem fala do índice fora do ar é o card 'signal lost', não um 500.
    """
    import search.client as sc

    def _explode(*a, **kw):
        raise AssertionError('a página da busca NÃO pode consultar o ES no render')

    monkeypatch.setattr(sc, 'get_es', _explode)
    assert cli.get(reverse('dashboard:busca')).status_code == 200


def test_endpoint_resolvido_pelo_django_com_fallback(pagina):
    """A rota do endpoint vem do `url` tag; o default cobre a janela sem rota."""
    assert 'window.BUSCA_URLS' in pagina
    assert '/dashboard/api/busca/processos/' in pagina
    # a rota do detalhe do processo é SEMPRE resolvida pelo Django
    assert '/dashboard/processos/987654321/' in pagina


# --------------------------------------------------------------------------- #
# O campo único: detecção de tipo (o que substitui os 8 campos do PJe)
# --------------------------------------------------------------------------- #
def test_detector_cobre_os_quatro_tipos(pagina):
    for fn in ('function cnjValido', 'function cpfValido', 'function cnpjValido',
               'function parseOab', 'function detectarTipo'):
        assert fn in pagina, fn


def test_cnj_valida_digito_verificador_por_mod_97(pagina):
    """Sem o mod-97 a tela não sabe distinguir 'digitou errado' de 'não existe'.

    É a única forma de dizer, ANTES de gastar a query, que o número copiado tem
    um dígito trocado — coisa que o formulário do PJe não faz.
    """
    assert '% 97' in pagina
    assert '(98 - r) === dv' in pagina


def test_documento_nao_exige_mascara(pagina):
    """O índice guarda COM máscara e o usuário digita como quiser.

    Medido: `29.979.036/0001-40` acha 23.217 e `29979036000140` acha ZERO. Pedir
    formato ao usuário seria transferir a ele um defeito do nosso índice.
    """
    assert 'Máscara é indiferente' in pagina
    # a detecção normaliza pra dígitos antes de decidir o tipo
    assert "replace(/\\D/g, '')" in pagina


def test_nome_com_numero_nao_vira_documento(pagina):
    """'José 123' é nome, não CPF: só vira documento se NÃO houver letra."""
    assert r'/^[\d.\-\/\s]+$/' in pagina


def test_usuario_pode_discordar_do_detector(pagina):
    """Detecção automática sem escape seria uma gaiola pior que o PJe.

    Um nome pode ser de parte OU de advogado e nenhuma heurística resolve isso:
    quem sabe é quem digitou.
    """
    assert 'buscar como:' in pagina
    assert 'usarModo(' in pagina
    assert 'alternativas()' in pagina


def test_atalhos_de_teclado(pagina):
    # `/` global do base.html procura input[name="q"] — o nome é contrato
    assert 'name="q"' in pagina
    assert '@keydown.enter.prevent="buscar()"' in pagina
    assert 'aoEscape()' in pagina
    assert '<span class="kbd">/</span>' in pagina


# --------------------------------------------------------------------------- #
# As quatro honestidades de cobertura
# --------------------------------------------------------------------------- #
def test_cobertura_do_documento_esta_na_tela(pagina):
    assert '102622' in pagina                      # docs com CPF/CNPJ
    assert '71181213' in pagina                    # universo
    assert 'quer dizer que a pessoa não tem processo' in pagina


def test_vara_e_valor_dizem_o_preco_antes_do_clique(pagina):
    """O preço do filtro vai AO LADO do campo, antes do clique.

    A tela NÃO crava percentual: quem manda é `cobertura.campos` do payload
    (`pctDe(...)`), e as constantes só existem como fallback datado. Cravar
    número aqui foi como o primeiro corte desta tela errou "vara = 1,8%" —
    esse é o `juizo`; o `orgao_julgador`, que ninguém tinha medido, alcança
    17.891.683 processos (25,09%), 14× mais.
    """
    assert "pctDe('orgao_julgador')" in pagina
    assert "pctDe('juizo')" in pagina
    assert "pctDe('valor_causa')" in pagina
    # "vara" são DOIS campos com alcances muito diferentes. Mostrar só o maior
    # prometeria um alcance que o filtro não tem; fundir os dois, idem.
    assert 'pelo órgão julgador' in pagina and 'pelo juízo' in pagina
    assert 'o campo veio vazio do tribunal' in pagina
    assert 'filtrar aqui descarta o resto' in pagina


def test_fallback_de_cobertura_e_datado_e_perde_do_payload(pagina):
    """Número velho IDENTIFICADO vale mais que campo ausente — e muito mais que
    número velho passando por atual."""
    assert 'BUSCA_COBERTURA_MEDIDA' in pagina
    assert 'medido_em' in pagina
    assert 'Percentuais medidos no índice em' in pagina        # payload
    assert 'Percentuais medidos no índice de produção' in pagina  # fallback, datado


def test_cobertura_do_payload_ganha_do_numero_medido(pagina):
    """O número medido é FALLBACK. Quando o backend manda, a tela usa o dele.

    As chaves do fallback são as DO ÍNDICE (`participacoes.documento`,
    `orgao_julgador`) — as mesmas que o backend usa em `cobertura.campos`.
    Sem isso haveria uma tabela de tradução no meio, que envelhece calada
    quando um dos lados renomeia um campo.
    """
    assert 'json.cobertura' in pagina
    assert 'cobertura.campos' in pagina
    assert "'participacoes.documento'" in pagina
    assert 'coberturaDe(' in pagina


def test_tempo_e_cache_sao_ditos(pagina):
    """945ms frio contra 54ms morno é diferença que o operador percebe."""
    assert 'levou_ms' in pagina
    assert 'json.cache' in pagina
    assert '(do cache)' in pagina


def test_400_e_criterio_recusado_nao_infra_caida(pagina):
    """Confundir os dois faz a tela dizer "o acervo caiu" quando quem errou foi
    a digitação — exatamente a confusão que o PJe produz."""
    assert 'resp.status === 400' in pagina
    assert 'O acervo recusou o critério.' in pagina
    assert 'Buscar assim mesmo' in pagina
    assert 'documento_forcar' in pagina


def test_expansao_de_documento_e_oferecida_nunca_automatica(pagina):
    """Norma da casa: abster > chutar. Matriz+filiais e documento mascarado por
    sigilo são INDÍCIO, não prova de identidade."""
    assert 'expandir: {raiz: false, parciais: false, forcar: false}' in pagina
    assert 'Incluir matriz e filiais' in pagina
    assert 'Incluir os mascarados' in pagina
    assert 'indício, não prova de identidade' in pagina


def test_vara_tem_autocomplete_de_valores_reais(pagina):
    """29.488 grafias incompatíveis entre tribunais: sem lista, o usuário chuta
    a grafia, recebe zero e conclui que o processo não existe."""
    assert 'BUSCA_URLS.varas' in pagina
    assert 'role="listbox"' in pagina
    assert 'aria-activedescendant' in pagina
    assert 'vara_exata' in pagina
    assert 'processos' in pagina


def test_avisos_do_backend_aparecem_junto_do_resultado(pagina):
    """A ressalva precisa estar onde a decisão é tomada, não só no vazio."""
    assert 'json.avisos' in pagina
    assert 'Ressalvas desta busca' in pagina


def test_valor_da_causa_nao_e_valor_do_credito(pagina):
    assert 'não é o processo de R$ 0' in pagina or 'não é processo de R$ 0' in pagina


# --------------------------------------------------------------------------- #
# Os quatro estados
# --------------------------------------------------------------------------- #
def test_os_quatro_estados_existem(pagina):
    for estado in ("estado() === 'inicial'", "estado() === 'carregando'",
                   "estado() === 'ok'", "estado() === 'vazio'"):
        assert estado in pagina, estado
    assert "estado() === 'erro'" in pagina


def test_vazio_inicial_ensina_em_vez_de_ficar_morto(pagina):
    assert 'Cole o que você tem' in pagina
    assert 'EXEMPLOS' in pagina
    assert '1105916-36.2026.8.26.0053' in pagina          # CNJ de dígito válido
    assert '29.979.036/0001-40' in pagina                 # o CNPJ que acha 23.217
    assert 'OAB/SP 123456' in pagina


def test_carregando_usa_skeleton_com_a_forma_do_cartao(pagina):
    assert 'skeleton' in pagina
    assert 'acquiring' in pagina


def test_zero_explica_o_porque_e_oferece_saida(pagina):
    """O '0 resultados' seco do PJe é o defeito nº 1 que esta tela ataca."""
    assert 'diagnosticos()' in pagina
    assert 'limitante()' in pagina
    for causa in ('dígito verificador não confere',
                  'Zero aqui não quer dizer',
                  'é caro',
                  'não está no nosso acervo'):
        assert causa in pagina, causa
    assert 'rodarAcao(' in pagina


def test_zero_por_documento_manda_para_o_nome(pagina):
    """Achar 0 num campo de 0,14% precisa terminar numa AÇÃO, não num beco."""
    assert 'Buscar pelo nome' in pagina
    assert 'O nome cobre 100% do acervo' in pagina


def test_cnj_valido_e_ausente_oferece_consulta_ao_vivo(pagina):
    assert 'Consultar ao vivo' in pagina
    assert 'consultaRapida' in pagina


# --------------------------------------------------------------------------- #
# Resultado: hierarquia, não tabela crua
# --------------------------------------------------------------------------- #
def test_resultado_tem_hierarquia_de_leitura(pagina):
    assert 'fmtCnj(it.proc)' in pagina
    assert 'polo ativo' in pagina and 'polo passivo' in pagina
    assert 'ultimaMovFrase(it)' in pagina
    assert 'urlProcesso(it)' in pagina


def test_nao_inventa_polo_quando_o_tribunal_nao_mandou(pagina):
    """`participacoes` tem cobertura ~0 em prod; supor 'autor x réu' pela ordem
    da string concatenada seria inventar o dado mais importante do cartão."""
    assert 'temPolos(it)' in pagina
    assert 'este tribunal não separou os polos' in pagina


def test_selos_nao_fundem_confirmado_com_possivel(pagina):
    """`classificacao=PRECATORIO` é veredito; `tem_sinal_precatorio` é indício."""
    assert "it.classificacao === 'PRECATORIO'" in pagina
    assert 'tem_sinal_precatorio' in pagina
    assert 'sinal de precatório' in pagina
    assert 'segredo de justiça' in pagina


def test_total_aproximado_e_dito(pagina):
    """O ES capa o total em 10.000 (`relation: gte`) — a tela não pode mentir."""
    assert "totalRelacao === 'gte'" in pagina
    assert 'mais de ' in pagina


# --------------------------------------------------------------------------- #
# Estado na URL
# --------------------------------------------------------------------------- #
def test_o_link_e_o_estado_da_tela(pagina):
    assert 'lerQuerystring()' in pagina
    assert 'URLSearchParams' in pagina
    assert 'history.pushState' in pagina
    assert 'history.replaceState' in pagina
    assert "addEventListener('popstate'" in pagina


def test_cursor_fica_fora_da_url(pagina):
    """Cursor é paginação, não critério: na URL ele traria a página 2 de uma
    busca antiga colada na página 1 da nova."""
    assert 'paramsDoCriterio()' in pagina
    corpo = COD.split('paramsDoCriterio()')[1].split('sincronizarUrl')[0]
    assert 'cursor' not in corpo


# --------------------------------------------------------------------------- #
# Acessibilidade
# --------------------------------------------------------------------------- #
def test_acessibilidade_do_campo_e_dos_grupos(pagina):
    assert 'role="combobox"' in pagina
    assert 'aria-describedby="busca-ajuda-campo"' in pagina
    assert 'aria-live="polite"' in pagina
    assert 'resumoAcessivel()' in pagina
    assert 'aria-controls="busca-filtros"' in pagina
    assert pagina.count(':aria-pressed=') >= 4
    assert '<label for="busca-q"' in pagina


def test_tooltip_e_voy_tip_nunca_title(pagina):
    assert 'voy-tip' in pagina
    assert 'role="tooltip"' in pagina
    assert 'aria-describedby="tip-vara"' in pagina


# --------------------------------------------------------------------------- #
# Guardas da casa
# --------------------------------------------------------------------------- #
def test_sem_atributo_title(pagina):
    """Tooltip é `.voy-tip`; `title=` nativo é zero em touch e no teclado.

    Só o TEMPLATE desta tela: o `base.html` carrega um `title=` legado no rodapé
    do menu, que não é meu escopo. As tags do Django saem antes da varredura —
    `{% include ... with title='...' %}` é argumento de include, não atributo.
    """
    sem_tags = re.sub(r'\{%.*?%\}', '', COD, flags=re.S)
    assert not re.search(r'\stitle\s*=', sem_tags)


def test_sem_text_base(pagina):
    """`text-base` aqui é COR (o token `base` é o fundo da página): num tema
    escuro vira texto invisível. Tamanho se escreve `text-[1rem]`."""
    assert 'text-base' not in COD
    assert 'text-[1rem]' in COD


def test_sem_hex_em_cor(pagina):
    assert not re.search(r'(?:color|background|border)\s*:\s*#[0-9a-fA-F]{3,8}', COD)
    assert not re.search(r'\btext-\[#', COD)


def test_sem_url_externa(pagina):
    """CSP estrita: ZERO CDN. Nada de http(s):// no template."""
    achados = re.findall(r'https?://[^\s"\'<>]+', COD)
    assert not achados, achados


def test_comentario_multilinha_so_em_comment(pagina):
    """`{# ... #}` que atravessa linha VAZA LITERAL (o lexer não usa DOTALL)."""
    for m in re.finditer(r'\{#', BUSCA_SRC):
        fim = BUSCA_SRC.find('#}', m.end())
        assert fim != -1
        assert '\n' not in BUSCA_SRC[m.start():fim + 2]


def test_todo_icone_existe_no_sprite(pagina):
    """Alias inexistente vira QUADRADO VAZIO, sem erro nenhum no console."""
    usados = set(re.findall(r'voy_icon\s+"([a-z0-9-]+)"', BUSCA_SRC))
    assert usados
    faltando = [a for a in usados if f'id="voy-{a}"' not in SPRITE]
    assert not faltando, faltando


def test_so_tokens_registrados_de_cor(pagina):
    """Token não registrado no tailwind.config = classe MORTA, falha em silêncio."""
    registrados = {
        'base', 'surface', 'card', 'muted', 'border', 'fg', 'fg-soft', 'fg-muted',
        'fg-subtle', 'accent', 'accent-fg', 'danger', 'warning', 'info',
        'mission', 'mission-fg', 'pulsar', 'pulsar-fg', 'golden', 'pale-blue',
        # utilitários nativos do Tailwind que não são token do tema
        'white', 'black', 'transparent', 'current', 'inherit',
    }
    usados = set(re.findall(r'\b(?:text|bg|border|ring|decoration)-([a-z][a-z-]*?)'
                            r'(?:/\d+)?(?=[\s"\'\]])', COD))
    # classes utilitárias que não são cor (border-l, text-left, ring-offset…)
    nao_cor = {'l', 'r', 't', 'b', 'x', 'y', 'left', 'right', 'center', 'sm', 'xs',
               'lg', 'xl', 'offset', 'dotted', 'dashed', 'solid', 'none', 'wide',
               'wider', 'widest', 'clip', 'ellipsis', 'nowrap', 'balance',
               'offset-base', 'offset-2'}
    desconhecidos = usados - registrados - nao_cor
    assert not desconhecidos, desconhecidos


def test_texto_pequeno_usa_a_variante_fg(pagina):
    """Contrato de contraste: `mission` é cor de MARCA (3,56:1 no claro).

    Texto < 24px precisa da variante `-fg` (5,18:1). `text-mission` só pode
    aparecer em ícone/número grande.
    """
    for m in re.finditer(r'class="([^"]*\btext-mission\b(?!-fg)[^"]*)"', COD):
        classe = m.group(1)
        assert re.search(r'\bw-\d', classe) or 'text-xl' in classe, classe


# --------------------------------------------------------------------------- #
# Menu lateral
# --------------------------------------------------------------------------- #
def test_item_de_menu_existe_e_e_seguro(pagina):
    assert 'Buscar processo' in BASE_SRC
    assert "{% url 'dashboard:busca' as busca_url %}" in BASE_SRC
    # o `if` cobre a janela em que a rota ainda não existe
    assert '{% if busca_url %}' in BASE_SRC
    assert 'Buscar processo' in pagina           # renderizado, com o shim ativo


def test_menu_nao_regride_nas_telas_irmas(cli):
    """A edição no base.html não pode quebrar as páginas que já existem."""
    for nome in ('dashboard:entidades', 'dashboard:processos', 'dashboard:overview-mapa-page'):
        assert cli.get(reverse(nome)).status_code == 200, nome
