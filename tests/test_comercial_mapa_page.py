"""Testes da PÁGINA do Mapa de Precatórios (dashboard/comercial_mapa.html).

A view (`comercial_mapa_page`) só renderiza o shell HTML — todos os dados vêm
dos 3 endpoints JSON via fetch no browser, então este teste NÃO precisa de ES.
Cobre: login-gate (302 pra anônimo), render 200 pra logado, o GeoJSON dos
estados vindo de {% static %} (não CDN), o registerMap('brasil') presente, a
ausência de qualquer referência externa (CSP: zero CDN) e — desde a auditoria
adversarial de UX — as regras de honestidade/acessibilidade que a página não
pode regredir (escala por faixas, sem filtro destrutivo na legenda, sem
sequestro de scroll, glossário fora do drill-down, zero `title=`).
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

URL_NAME = 'dashboard:comercial-mapa-page'

#: fonte do template — usada nas asserções ESTRUTURAIS (ex.: "não chama o
#: setupChart da casa", "não sobrou title="), que não dá pra fazer no HTML
#: renderizado porque o base.html define `function setupChart(el, opts)` e usa
#: `title=` legítimo em outros lugares (nav, botões do layout).
TEMPLATE_SRC = (
    Path(dashboard.__file__).parent / 'templates' / 'dashboard' / 'comercial_mapa.html'
).read_text(encoding='utf-8')


def _sem_comentarios(src: str) -> str:
    """Template sem comentários — base das asserções NEGATIVAS.

    Vários comentários citam de propósito o que foi corrigido ("`roam: true`
    sequestrava o scroll"). Sem tirá-los, o próprio comentário derruba o teste
    que garante que o bug não voltou.
    """
    src = re.sub(r'\{#.*?#\}', '', src, flags=re.S)
    src = re.sub(r'\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}', '', src, flags=re.S)
    src = re.sub(r'^\s*//.*$', '', src, flags=re.M)      # linha inteira de comentário JS
    src = re.sub(r'(?<![:\'"])//.*$', '', src, flags=re.M)  # comentário no fim da linha
    return src


#: código/markup efetivo (sem comentários) — usar em toda asserção "não pode
#: existir". As asserções POSITIVAS usam o HTML renderizado.
CODIGO = _sem_comentarios(TEMPLATE_SRC)


@pytest.fixture
def user(db):
    return User.objects.create_user(username='mapa_gate', password='x')


@pytest.fixture
def html(user):
    c = Client(SERVER_NAME='localhost')
    c.force_login(user)
    resp = c.get(reverse(URL_NAME))
    assert resp.status_code == 200
    return resp.content.decode()


def test_url_resolve():
    assert reverse(URL_NAME) == '/dashboard/comercial/mapa/'


def test_anon_redireciona_login(client):
    resp = client.get(reverse(URL_NAME))
    assert resp.status_code == 302
    assert '/login' in resp['Location']


def test_logado_renderiza_200(html):
    # nome canônico do produto (era "Mapa Comercial de Precatórios")
    assert 'Mapa de Precatórios' in html
    # subtítulo curto, uma linha, com largura máxima (era 300+ caracteres soltos)
    assert 'Em que estados atacar primeiro' in html
    assert 'max-w-3xl' in html
    # honestidade: possível e confirmado nomeados e distintos
    assert 'Possível' in html and 'Confirmado' in html


def test_geojson_local_e_registermap(html):
    # GeoJSON servido via {% static %} (não CDN)
    assert '/static/' in html and 'brasil-uf.geojson' in html
    # mapa registrado no ECharts com a chave 'brasil'
    assert "registerMap('brasil'" in html


def test_potencial_null_tratado_como_desconhecido(html):
    """Fix do QA adversarial: potencial null / sinal_processado=false NUNCA vira "0".

    O shell precisa carregar a lógica honesta: helper potDesconhecido (null/
    sinal_processado false ⇒ fora da escala de cor + rótulo distinto), o texto
    do tooltip com PALAVRA (não glifo, não opacidade) e o aviso de ranking
    parcial no Ataque primeiro.
    """
    assert 'potDesconhecido' in html
    assert 'sinal_processado' in html
    # tooltip do choropleth: desconhecido tem rótulo próprio, não "Possíveis: 0"
    assert 'Possíveis precatórios: ainda não analisado' in html
    # aviso honesto do ranking (derivado dos buckets), na copy nova
    assert 'Já lemos as publicações de' in html
    assert 'dos 27 estados' in html
    assert 'nem entrou nesta comparação' in html
    # painéis usam potLabel (palavra pra não-analisado) em vez de fmt cru
    assert 'potLabel' in html
    assert 'possíveis: não analisado' in html


def test_glossario_canonico(html):
    """Glossário canônico definido 1x e reusado via glos() — fonte única."""
    assert 'window.GLOSSARIO' in html
    # trechos-chave de cada texto do glossário reescrito (1 por métrica)
    assert 'O tribunal tem mais — estes são os que temos' in html        # volume
    assert 'não que falte dinheiro' in html                              # valor
    assert 'Indício forte, não certeza' in html                          # potencial
    assert 'bateu o martelo' in html or 'Mais certeiro que os possíveis' in html
    assert 'território praticamente virgem' in html                      # cobertura
    assert 'Prioridade de 0 a 100' in html                               # score
    assert 'O número é desconhecido, não é zero' in html                 # naoAnalisado
    assert 'não cabe em um só' in html                                   # federal
    # linha de contexto da métrica ativa no tooltip do choropleth
    assert 'glosMetricaAtiva' in html


def test_ajuda_como_ler_no_nivel_da_pagina(html):
    """O <dl> didático não pode viver DENTRO do drill-down.

    Quem nunca clica num estado nunca lia o melhor material da tela. Agora é
    de página, colapsável, com a preferência persistida.
    """
    assert 'Como ler estes números' in html
    assert 'ajudaAberta' in html and 'toggleAjuda' in html
    assert 'voy-mapa:ajuda-drill' in html   # chave do localStorage (preservada)

    # ESTRUTURA: o <dl> aparece ANTES do painel de drill-down no documento
    pos_dl = TEMPLATE_SRC.find('id="mapa-ajuda"')
    pos_drill = TEMPLATE_SRC.find('drill-down: tribunais da UF selecionada')
    assert pos_dl > 0 and pos_drill > 0
    assert pos_dl < pos_drill, 'o <dl> voltou pra dentro do drill-down'

    # os 6 itens, na copy nova (termos canônicos, sem "pot"/"conf"/"score")
    assert 'já trouxemos para cá' in html                       # Processos na base
    assert 'ofício requisitório ou RPV' in html                 # Possíveis
    assert 'entra aqui por engano' in html                      # Possíveis
    assert 'A nossa IA leu o processo e bateu o martelo' in html  # Confirmados
    assert 'Não é falta de dinheiro' in html                    # Valor informado
    assert 'onde atacar primeiro' in html                       # Prioridade

    # frase de leitura automática, na copy nova
    assert 'insightUf' in html
    assert 'citam precatório' in html
    assert 'Ainda não lemos as publicações de' in html


def test_termos_canonicos_e_jargao_fora(html):
    """Um conceito = um nome. Jargão interno não aparece em texto visível."""
    assert 'Processos na base' in html
    assert 'Valor informado' in html
    assert 'Possíveis precatórios' in html
    assert 'Confirmados pela IA' in html
    assert 'Já analisado' in html or 'analisado' in html
    assert 'Justiça Federal — fora do mapa' in html
    # rótulos velhos/jargão fora
    assert 'Choropleth' not in CODIGO
    assert 'Camada Federal' not in CODIGO
    assert 'sinal DJEN' not in CODIGO
    assert 'Tipo de sinal' not in CODIGO
    assert 'score de foco' not in CODIGO
    # abreviações do ranking mortas (as do template; `pot ` etc. eram literais)
    for morto in ("'pot '", "'conf '", "'val '", "'vol '", "'score '", "'validado '"):
        assert morto not in CODIGO, f'abreviação ressuscitada: {morto}'


def test_dinheiro_tem_faixa_de_trilhao(html):
    """`fmtBRL` parava em `bi`: R$ 2,88 tri saía como "R$ 2880,0 bi" no KPI hero."""
    assert "[1e12, 'tri']" in html
    # zero/nulo = "não informado" (o tribunal não publicou), nunca "R$ 0"
    assert "return 'não informado'" in html
    assert 'R$ 0' not in CODIGO
    # contagem compacta em pt-BR, LOCAL: o fmtCompact global (k/M) fica intacto
    assert 'fmtQtdCompacta' in html
    assert "' mil'" in html and "' mi'" in html
    assert 'fmtCompact(' not in CODIGO, 'voltou a usar o fmtCompact global (k/M)'
    # separador decimal pt-BR: nenhum toFixed sem replace de ponto por vírgula
    for m in re.finditer(r'toFixed\(1\)((?:\.replace\([^)]*\))*)', CODIGO):
        assert "replace('.', ',')" in m.group(1), f'toFixed sem vírgula: {m.group(0)}'


def test_porcentagem_pt_br_e_vazios_distintos(html):
    """Três vazios, três palavras — nunca o mesmo glifo pra tudo."""
    assert 'menos de 0,1%' in html          # 0 < n < 0,1 não vira "0,0%"
    assert 'não sabemos ainda' in html      # cobertura_pct nula
    assert 'ainda não analisado' in html    # nós não lemos
    assert 'não informado' in html          # o tribunal não publicou
    assert 'nenhum processo deste estado na nossa base' in html   # não temos o processo
    # rótulos velhos dos vazios fora
    assert 'desconhecida' not in CODIGO
    assert 'não processado' not in CODIGO


def test_escala_de_cor_por_faixas_com_legenda_html(html):
    """A escala linear 0→max apagava RO/RN/TO/RR ao lado do PR (267.975).

    Agora: faixas por QUANTIL, `visualMap` fora do canvas (show:false) e legenda
    HTML no header do card — recalculada quando os dados/filtros mudam.
    """
    # faixas por quantil (não linear) recalculadas na pintura
    assert 'calcularFaixas' in html
    assert '[0.25, 0.5, 0.75]' in html
    assert 'this.faixas = this.calcularFaixas' in html
    # visualMap piecewise, fora do canvas
    assert "type: 'piecewise'" in html
    assert 'show: false' in html
    assert 'pieces:' in html
    assert 'min: 0' not in CODIGO, 'voltou a escala linear min:0 → max'
    # legenda em HTML, com swatch e rótulo humano por faixa
    assert 'rotuloSemDado' in html
    assert 'Cor mais forte = mais' in html
    assert "'até '" in html and "'mais de '" in html
    # o vazio da escala é dito com PALAVRA, e varia com a métrica ativa
    assert "'valor não informado' : 'ainda não analisado'" in html


def test_legenda_nao_filtra_nem_sequestra_scroll(html):
    """Dois bugs BLOQUEANTES da auditoria de UX.

    1. `calculable: true` dava alças que REMOVEM estados da escala — o estado
       saía da cor e ficava cinza, que a nossa legenda define como "ainda não
       analisado": o mapa passava a mentir, sem desfazer.
    2. `roam: true` sequestrava a roda do mouse (zoom em vez de rolar) e travava
       o dedo no touch; zoom errado só voltava com F5.
    """
    assert 'calculable: false' in html
    assert 'calculable: true' not in CODIGO
    assert 'roam: false' in html
    assert 'roam: true' not in CODIGO
    assert 'scaleLimit' not in CODIGO


def test_layout_medido_nao_regride(html):
    """Geometria do mapa MEDIDA em browser real (Chromium, canvas pixel a pixel).

    - `aspectScale: 1` — o default do ECharts é 0.75 (calibrado pro mapa da
      China) e esmagava o Brasil: proporção 0.799 (o "magro" original).
    - `layoutSize`/`layoutCenter` — sem eles o `getLayoutRect` aplica fator 0.8
      e o mapa desenhava 409x384 num container 1080x480 (38% da largura).
    - caixa com `aspect-ratio` casando a proporção do mapa (1.066) em vez de
      altura fixa: com `height: 40rem` num card largo a moldura era 1.69:1 pro
      conteúdo 1.07:1 e sobravam ~438px de vazio nas laterais — era ISSO que
      lia como "desproporcional". Medido depois: caixa 766x719 e mapa 720x676
      (ambos 1.065), mapa ocupando 94% da caixa em card largo E estreito.
    Referência: o Brasil físico é 1.040 (41,6° x 39,0° corrigidos por cos da
    latitude média -14,2°), então 1.066 tem 2,5% de folga — a forma está certa.
    Mexer em qualquer um destes sem medir de novo regride o bug.
    """
    assert 'aspectScale: 1' in html
    assert "layoutSize: '100%'" in html
    assert "layoutCenter: ['50%', '50%']" in html
    assert 'aspect-ratio: 1.066' in html, 'a caixa precisa casar a proporção do mapa'
    assert 'max-width: 47.9rem' in html, 'sem o cap a caixa cresce sem limite em tela larga'
    assert 'height: 40rem' not in html, 'altura fixa volta a criar moldura vazia'
    # grade 3/5 pro mapa (era 2/3: card largo demais pro conteúdo 1.066)
    assert 'lg:grid-cols-5' in html
    assert 'lg:col-span-3 card' in html


def test_tooltip_rico_sem_setupchart(html):
    """Regressão: o setupChart da casa serializa opts com JSON.stringify e
    DESCARTA funções, matando o `tooltip.formatter` (caía em "series0")."""
    assert 'setOption(opts, true)' in html
    assert 'setupChart(' not in CODIGO
    assert 'x-ref="mapa" data-echart' not in html
    # série nomeada com o termo canônico => nunca mais "series0" nem "Valor (R$)"
    assert "name: emR$ ? 'Valor informado'" in html
    # troca de tema observada de verdade (não há evento 'voy:theme' no base)
    assert "addEventListener('voy:theme'" not in CODIGO
    assert 'MutationObserver' in html
    # tooltip traz prioridade legível + o cálculo interno auditável
    assert 'de 100' in html
    assert 'cálculo interno' in html


def test_prioridade_0_100_substitui_score_cru(html):
    """`0.0142` é ilegível e sem âncora; `prioridade 87` é comparável."""
    assert 'prioridadeEm' in html and 'prioridadeLabel' in html
    assert 'Math.round(100 * (b.score_foco || 0) / max)' in html
    assert 'prioridade não medida' in html
    assert 'toFixed(4)' not in CODIGO, 'voltou o score cru de 4 casas'
    # explicação de que a escala é relativa à lista filtrada
    assert 'comparando só os estados desta lista' in html


def test_estado_vazio_de_filtro(html):
    """`ufs = []` sem estado vazio pinta o Brasil TODO de cinza — que pela nossa
    legenda quer dizer "ainda não analisado". O mapa mentiria por acidente."""
    assert 'vazioPorFiltro' in html
    assert 'Nenhum estado tem dado com os filtros atuais.' in html
    assert 'não quer dizer “não analisado”' in html


def test_feedback_de_carregamento_e_resposta_fora_de_ordem(html):
    """Trocar filtro leva 2-5s: o mapa antigo ficava parado sem sinal algum.

    E os 4 inputs de faixa disparam @change independentes — sem token de
    requisição, a resposta lenta do filtro antigo pintava por cima da nova.
    """
    assert 'showLoading' in html and 'hideLoading' in html
    assert 'skel.remove()' not in CODIGO, 'voltou a destruir o nó do skeleton'
    assert '_seq' in html
    assert 'seq !== this._seq' in html
    assert 'seq !== this._seqDrill' in html


def test_acessibilidade_foco_e_controles(html):
    """Teclado: foco não pode cair no <body>, ring tem que ser visível, e
    controle "desabilitado" tem que ser :disabled de verdade."""
    # foco vai pro título do painel ao abrir o drill e volta ao item ao fechar
    assert 'x-ref="drillTitulo" tabindex="-1"' in html
    assert 'drillTitulo' in html and '.focus()' in html
    assert 'data-uf-item' in html
    assert 'aria-live="polite"' in html
    # ring de foco visível (o focus:ring-accent/40 dava 2,11:1)
    assert 'focus-visible:ring-2 focus-visible:ring-accent' in html
    assert 'ring-offset-base' in html
    assert 'ring-accent/40' not in CODIGO
    # o grupo desabilitado era acionável por Enter (opacity + pointer-events)
    assert ":disabled=\"metricMode==='valor'\"" in html
    assert 'pointer-events-none' not in CODIGO
    # inputs de faixa com nome acessível + grupo rotulado
    assert 'aria-label="Ano do processo, de"' in html
    assert 'aria-label="Valor da causa, até"' in html
    assert 'role="group" aria-labelledby="lbl-valor"' in html
    # segmentados anunciam o estado
    assert 'aria-pressed' in html


def test_sem_title_de_glossario_e_sem_hex_em_texto(html):
    """Explicação é `.voy-tip` (hover + foco + TAP), nunca `title=`.

    E cor de texto vem de token Tailwind — hex literal só dentro do canvas do
    ECharts (série/visualMap) e em swatch de amostra.
    """
    # zero title= de glossário. Sobram só os DOIS `title` do Django (o
    # {% block title %} da aba e o argumento do include do page_header), que não
    # são atributo HTML.
    sem_django = CODIGO.replace('{% block title %}', '')
    sem_django = re.sub(r"with title='[^']*'", '', sem_django)
    # o lookbehind poupa `subtitle=` (que contém "title=" por acidente)
    assert not re.search(r'(?<![a-z])title=', sem_django)
    assert ':title=' not in CODIGO
    assert 'cursor-help' not in CODIGO   # afordância do title nativo
    # zero cor de TEXTO por hex inline
    assert 'style="color:#' not in CODIGO
    # componente do design system em uso, com o par aria-describedby/id
    assert 'class="voy-tip' in html
    assert 'aria-describedby="tip-volume"' in html
    assert 'id="tip-volume"' in html
    assert 'role="tooltip"' in html
    # tokens de cor da casa em vez de hex
    assert 'text-mission-fg' in html and 'bg-mission' in html


def test_sem_opacidade_em_texto_e_piso_de_12px(html):
    """"Não sabemos" tem que ser dito com PALAVRA, não com transparência
    (opacity-70 dava 2,5:1 = ilegível, na informação mais importante da
    honestidade). E nenhum número abaixo de 12px."""
    assert 'opacity-' not in CODIGO
    assert 'opacity:.' not in CODIGO
    # nenhum tamanho de fonte arbitrário abaixo de text-xs (0,75rem = 12px)
    assert not re.search(r'text-\[0\.[0-6]', CODIGO), 'fonte abaixo de 12px'
    # texto de DADO usa fg-muted (fg-subtle é 2,x:1 em número pequeno)
    assert 'text-fg-subtle' not in re.sub(
        r'(disabled:text-fg-subtle|hover:border-fg-subtle|bg-fg-subtle)', '', CODIGO
    )


def test_ruido_do_ranking_removido(html):
    """`R$ 0` repetido em 10 linhas é ruído: a lista diz UMA vez."""
    assert 'temValor' in html and 'semValor' in html
    assert 'o valor ainda não foi informado pelo tribunal' in html


def test_comentario_de_template_nao_vaza_na_tela(html):
    """`{# ... #}` MULTILINHA não existe no Django e vazava LITERAL na página.

    O lexer é `({%.*?%}|{{.*?}}|{#.*?#})` **sem** re.DOTALL: comentário que
    atravessa a linha não é reconhecido e sai como texto. A página tinha 3
    (inclusive "Altura MEDIDA (render headless do ECharts…)" logo acima do
    mapa). Comentário de várias linhas = `{% comment %}`.
    """
    multilinha = [
        i for i, linha in enumerate(TEMPLATE_SRC.split('\n'), 1)
        if '{#' in linha and '#}' not in linha
    ]
    assert not multilinha, f'{{# #}} multilinha (vaza na tela) nas linhas {multilinha}'
    # e o HTML renderizado não pode conter marcação de template nenhuma no corpo
    corpo = html[html.find('Em que estados atacar primeiro'):]
    assert '{#' not in corpo and '{%' not in corpo


def test_sem_cdn_externo_na_pagina(html):
    """A página não pode introduzir nenhuma URL http(s) externa (CSP)."""
    # marcador do CORPO (o <title> da aba fica no <head>, antes do Google Fonts
    # do base.html — que é offsite legítimo e fora do escopo desta página)
    marca = 'Em que estados atacar primeiro'
    corpo = html[html.find(marca):] if marca in html else html
    proibidos = ('cdn.jsdelivr', 'unpkg.com', 'cdnjs', 'code.jquery',
                 'fonts.googleapis', 'fonts.gstatic')
    for termo in proibidos:
        assert termo not in corpo, f'referência externa proibida no corpo: {termo}'
    # nenhum http(s):// no fonte do template
    assert not re.search(r'https?://', CODIGO), 'URL absoluta no template'
