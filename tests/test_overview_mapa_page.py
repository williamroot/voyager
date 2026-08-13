"""Testes da PÁGINA do Mapa de Precatórios (dashboard/overview_mapa.html).

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

URL_NAME = 'dashboard:overview-mapa-page'

#: fonte do template — usada nas asserções ESTRUTURAIS (ex.: "não chama o
#: setupChart da casa", "não sobrou title="), que não dá pra fazer no HTML
#: renderizado porque o base.html define `function setupChart(el, opts)` e usa
#: `title=` legítimo em outros lugares (nav, botões do layout).
TEMPLATE_SRC = (
    Path(dashboard.__file__).parent / 'templates' / 'dashboard' / 'overview_mapa.html'
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
    assert reverse(URL_NAME) == '/dashboard/overview/mapa/'


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
    # Honestidade: possível e confirmado seguem NOMEADOS e DISTINTOS. A faixa de
    # 4 chips ("Possível" / "Confirmado" / ...) que ficava em cima do mapa saiu —
    # a definição de cada um já vivia no "Como ler estes números", e a faixa
    # gastava área nobre repetindo. O que se guarda aqui é o conceito, não o
    # chip: os dois termos canônicos e a diferença entre eles continuam na tela.
    assert 'Possíveis precatórios' in html or 'Possíveis' in html
    assert 'Confirmados pela IA' in html
    assert 'Indício forte, não certeza' in html          # possível ≠ certeza
    assert 'bateu o martelo' in html                     # confirmado = a IA leu


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
    # ⚠️ o texto de `valor` MUDOU em 13/08/2026. O antigo ("não informado quer
    # dizer que o tribunal não publicou") descrevia as duas causas do vazio como
    # se fossem uma pendência do tribunal. Medido: o PJe da consulta pública não
    # expõe valor da causa em tribunal NENHUM (27 processos reais, 0 ocorrência
    # da string "valor" no HTML) — só e-SAJ e 2 portais próprios publicam, 5 UFs.
    assert 'onde não publica, não há o que buscar' in html               # valor
    assert 'o número não existe na fonte' in html                        # semFonte
    assert 'nós é que ainda não buscamos' in html                        # valorPendente
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
    # Valor informado: a entrada deixou de dizer só "não é falta de dinheiro" e
    # passou a dizer o TAMANHO do buraco (5 de 27 publicam) + a entrada nova da
    # hachura, que é o vazio DEFINITIVO (a fonte não tem o campo).
    assert 'Só 5 dos 27 estados publicam esse número' in html   # Valor informado
    assert 'falta de valor não é falta de dinheiro' in html
    assert 'O tribunal não publica valor — ' in html            # faixa hachurada
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
    # o vazio da escala é dito com PALAVRA, e varia com a métrica ativa —
    # causas diferentes, frases diferentes (nunca o mesmo rótulo pra tudo)
    assert "'o tribunal publica, mas ainda não buscamos'" in html   # modo R$
    assert "return 'sem processo na nossa base'" in html   # modo Todos
    assert "return 'ainda não analisado'" in html          # modo Possíveis
    # e no modo R$ o vazio DEFINITIVO ganhou faixa própria (hachura): o cinza
    # chapado sozinho dizia "vocês não buscaram" para 23 dos 28 buckets
    assert 'rotuloSemFonte' in html and 'mostrarFaixaSemFonte' in html
    assert 'o tribunal não publica valor da causa' in html


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


def test_terceiro_modo_todos_uniao(html):
    """3º modo do segmentado: Possíveis | Confirmados | **Todos** (a união).

    `todos` vem do backend como UNIÃO possível ∪ confirmado — jamais somado
    aqui. Medido no índice em 12/08/2026: dos 47.720 confirmados pela IA só
    6.421 também têm sinal no texto, então somar contaria a interseção em dobro;
    e os outros 41.299 (87%) só aparecem NESTE modo.
    """
    # botão presente, com o mesmo contrato de acessibilidade dos irmãos
    assert ">Todos</button>" in html
    assert "@click=\"sinalMode='todos'\"" in html
    assert ":aria-pressed=\"sinalMode==='todos'\"" in html
    # A lente NÃO desliga mais no modo R$. Desligava quando só pintava o mapa;
    # agora governa também os KPI e o "Ataque primeiro", que continuam válidos
    # com o mapa em R$ — travar os 3 botões congelaria a página inteira por
    # causa do eixo de cor de um card só. A regra vira texto na tela.
    assert ":disabled=\"metricMode==='valor'\"" not in html
    assert 'A lente segue valendo para os números' in html, \
        'tirar o disabled sem explicar deixa o usuário sem saber o que a lente faz em R$'

    # a métrica lê o campo do backend — e NUNCA soma os dois
    assert "if (this.sinalMode === 'todos') return Number(b.todos) || 0;" in html
    for soma in ('potencial + b.confirmado', 'potencial||0) + (b.confirmado',
                 'potencial || 0) + (b.confirmado'):
        assert soma not in CODIGO, f'união calculada por SOMA: {soma}'

    # rótulos: "ou", não "+" — "+" leria como soma, que é justamente o erro
    assert 'Possíveis ou confirmados' in html
    assert 'Possíveis + confirmados' not in html

    # glossário explica união E nega a soma, em ≤140 caracteres
    import re as _re
    m = _re.search(r"todos:\s*'([^']+)'", html)
    assert m, 'chave `todos` ausente do GLOSSARIO'
    texto = m.group(1)
    assert len(texto) <= 140, f'glossário com {len(texto)} caracteres'
    assert 'não é a soma' in texto
    assert 'conta uma vez' in texto
    assert ' ou ' in texto

    # bloco didático ganhou a linha do modo novo
    assert 'sem contar ninguém duas vezes' in html
    assert 'duas buscas independentes' in html

    # cor própria: se a união usasse laranja, o mapa diria "possíveis" com um
    # número que também tem os confirmados
    assert "if (this.sinalMode === 'todos') {" in html
    assert '#93c5fd' in html and '#1e3a8a' in html   # rampa azul (pale-blue)
    assert 'bg-pale-blue' in html
    # `text-surface` (não `text-white`): pale-blue inverte com o tema
    assert 'bg-pale-blue text-surface' in html


def test_modo_todos_nao_diz_nao_analisado(html):
    """No modo Todos NÃO existe "ainda não analisado" — e não pode dizer que existe.

    `todos` é sempre numérico, inclusive onde o backfill do sinal não passou
    (SP: `potencial` null e 4.218 confirmados). Se o mapa pintasse cinza ali,
    esconderia justamente o que este modo existe pra revelar.
    """
    # metricaDe não devolve null por falta de sinal neste modo: o branch de
    # `potDesconhecido` fica DEPOIS do branch de `todos`
    pos_todos = html.find("if (this.sinalMode === 'todos') return Number(b.todos)")
    pos_pot = html.find('return this.potDesconhecido(b) ? null : (b.potencial || 0);')
    assert 0 < pos_todos < pos_pot, 'o modo todos tem que curto-circuitar o "não analisado"'
    # e o rótulo da faixa cinza troca de frase
    assert "if (this.sinalMode === 'todos') return 'sem processo na nossa base';" in html


def test_uniao_e_defensiva_com_payload_velho(html):
    """Cache de 2min do endpoint pode servir resposta anterior ao deploy.

    Sem o campo `todos`, a opção não deve existir (em vez de inventar número).
    """
    assert 'todosDisponivel' in html
    assert 'temTodos' in html
    assert 'x-show="todosDisponivel()"' in html
    assert "if (this.sinalMode === 'todos' && !this.todosDisponivel())" in html
    # o botão da união só existe com o dado; o KPI virou card único do ALVO
    # (não há mais uma coluna extra pra esconder — a lente é que troca o número)
    assert 'x-show="todosDisponivel()" x-cloak' in html
    assert "if (this.sinalMode !== 'todos' && this.todosDisponivel())" in html, \
        'a linha de contexto do KPI só cita a união quando o campo existe'


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
    # a nota segue a ORDEM ativa (concentração/quantidade/valor), não só a
    # densidade — ordenando por R$, prioridade 100 é de quem tem mais dinheiro
    assert 'Math.round(100 * this.metricaOrdem(b) / max)' in html
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
    # "desabilitar" nunca pode ser opacidade + pointer-events (segue acionável
    # por Enter no teclado, e opacidade não diz por quê). Hoje não há mais
    # controle desabilitado nesta página — a regra virou frase visível —, então
    # o que se guarda é o anti-padrão.
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
    """A linha de R$ diz QUAL vazio é — e o rodapé conta cada causa.

    Antes a linha SUMIA quando não havia valor e o rodapé dizia "o valor ainda
    não foi informado pelo tribunal": as duas coisas erradas de uma vez. O
    sumiço vira ausência (o leitor não sabe que faltou algo) e o "ainda não
    informado" promete um número que, em 23 dos 28 buckets, nunca vem — a
    consulta pública daqueles tribunais não tem o campo.
    """
    assert 'temValor' in html and 'semValor' in html
    assert 'valorCurto' in html and 'semFonteNa' in html and 'pendenteNa' in html
    assert 'o tribunal não publica o valor' in html
    assert 'o tribunal publica e nós é que ainda não buscamos' in html
    assert 'o valor ainda não foi informado pelo tribunal' not in html


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


#: fatia do template do topo do componente até o grid do mapa — é nela que se
#: mede DENSIDADE (quantas faixas o usuário rola antes de ver o produto).
_INI_COMPONENTE = TEMPLATE_SRC.find('<div x-data="comercialMapa()"')
_INI_MAPA = TEMPLATE_SRC.find('---- grid: mapa + painel lateral ----')
CABECALHO = TEMPLATE_SRC[_INI_COMPONENTE:_INI_MAPA]


def test_uma_faixa_so_antes_do_mapa(html):
    """Densidade: o produto é o MAPA e ele não pode nascer fora da dobra.

    Eram QUATRO faixas de largura total antes dele (legenda/glossário → filtros
    → lente → KPI) = 706px de cabeçalho medidos em Chromium a 1440x900, com o
    mapa começando a 91px da dobra; em 1366x768 ele não começava. Agora é UM
    bloco (`#cockpit`) e o card do mapa começa em 319px (1440) / 365px (1366).
    """
    assert _INI_COMPONENTE > 0 and _INI_MAPA > _INI_COMPONENTE
    # um `card` só entre o topo do componente e o mapa
    assert CABECALHO.count('class="card') == 1, (
        'voltou a empilhar faixa de largura total antes do mapa')
    assert 'id="cockpit"' in CABECALHO
    # o KPI não é mais um grid de 3 cards largos com o número no canto
    assert 'grid grid-cols-1 sm:grid-cols-3' not in CODIGO


def test_lente_e_numero_que_ela_governa_no_mesmo_bloco(html):
    """A lente escolhe o alvo; o número do alvo é o herói do MESMO bloco.

    Antes a lente vivia numa faixa e o número que ela troca aparecia em outra,
    abaixo — quem chega não liga um ao outro. Tudo isto tem que estar dentro do
    `#cockpit`: lente, herói, os dois números de contexto, filtros e ajuda.
    """
    ini = TEMPLATE_SRC.find('id="cockpit"')
    bloco = TEMPLATE_SRC[ini:_INI_MAPA]
    for marca in ('aria-label="Nível de certeza"', 'id="tip-alvo"',
                  'id="tip-volume"', 'id="tip-valor"',
                  'id="mapa-filtros"', 'id="mapa-ajuda"'):
        assert marca in bloco, f'{marca} ficou fora do cockpit'
    # o único `card` da fatia é o próprio cockpit
    assert bloco.count('class="card') == 1


def test_hierarquia_do_numero_heroi(html):
    """Premium é hierarquia: UM número herói, os outros em escala menor.

    Os 3 KPI eram `text-2xl` iguais lado a lado — sem hierarquia, o olho não
    sabia onde pousar. Agora o número da lente é 4xl/5xl e os dois de contexto
    são `text-xl`.
    """
    assert "'text-4xl sm:text-5xl'" in html
    assert html.count('text-xl font-mono text-fg') == 2   # base + valor
    # `text-2xl` uniforme nos três cards não pode voltar
    assert 'text-2xl font-mono' not in CODIGO
    # a frase do desconhecido não pode ser renderizada em 48px
    assert "alvoTotal() === null) ? 'text-xl'" in html


def test_filtros_em_disclosure_com_chips_do_que_esta_ativo(html):
    """Filtro é ferramenta de exceção: fica fechado, mas nunca escondido.

    O card de filtros era uma faixa inteira (5 campos numa linha, "Valor da
    causa" sozinho na outra com meia linha vazia). Virou disclosure fechado por
    padrão — e o que está ATIVO aparece como chip, com o valor por extenso e um
    botão de fechar que remove só aquele.
    """
    assert 'filtrosAbertos: false' in html          # fechado por padrão
    assert 'aria-controls="mapa-filtros"' in html
    assert ':aria-expanded="filtrosAbertos"' in html
    assert 'chipsFiltros()' in html and 'nFiltros()' in html
    assert 'limparFiltro(c.k)' in html              # remove UM filtro
    assert 'Remover filtro ' in html                # nome acessível do fechar
    assert 'limpar tudo' in html
    # 3 colunas com o par de valor ocupando 2 => 6 células pra 5 controles,
    # nenhuma faixa de grade vazia (medido em browser)
    assert 'lg:grid-cols-3' in html
    assert 'lg:grid-cols-4 gap-3' not in CODIGO
    # o aviso que muda a leitura do número não pode ficar atrás do disclosure
    assert 'filtrandoPorValor' in html
    assert html.count('Processo sem valor informado fica de fora do resultado') >= 1
    assert 'Processo sem valor informado fica de fora do resultado.' in html


def test_lente_ativa_tem_contraste_aa_medido(html):
    """`bg-mission text-white` media 3,56:1 nos DOIS temas (piso 4,5:1).

    Orange-600 com branco por cima é exatamente o que a doc do design system
    proíbe: a var SEM sufixo é cor de MARCA, não de texto. O contrato que passa
    é o mesmo do botão da união — variante `-fg` de fundo + `text-surface`, que
    inverte junto com o tema (medido: 5,2:1 no claro, 10:1 no escuro).
    """
    assert 'bg-mission-fg text-surface' in html
    assert 'bg-accent-fg text-surface' in html
    assert 'bg-pale-blue text-surface' in html
    assert 'text-white' not in CODIGO, 'branco sobre tinta de marca reprova AA'
    # o aviso de ranking parcial era `text-warning` sobre `bg-warning/10`:
    # 2,86:1 medido no claro — o alerta vinha do ícone, não do texto
    assert 'bg-warning/10 text-fg' in html
    # sky-600 em TEXTO de 12px no card branco = 4,10:1 medido (reprova 4,5:1).
    # Em ícone segue válido: objeto gráfico tem piso 3:1.
    for linha in CODIGO.split('\n'):
        if 'text-info' in linha:
            assert 'voy_icon' in linha, f'text-info em texto (4,10:1): {linha.strip()[:70]}'


def test_cards_do_mapa_e_do_ranking_nao_esticam(html):
    """Item de grade estica até a altura da linha: o ranking (10 estados) é
    ~250px mais alto que o mapa e o card do mapa ganhava 250px de nada no pé.
    Card vazio lê como quebrado — era metade do "espaço sobrando"."""
    assert 'lg:grid-cols-5 items-start' in html


def test_mapa_repinta_no_resize(html):
    """`chart.resize()` SOZINHO apaga o mapeamento de cor — o mapa MENTIA.

    Medido em Chromium: antes do resize RO=#ea580c / PR=#9a3412 (as faixas);
    depois de um único `resize()` os dois viram #5470c6 (cor 0 da paleta padrão
    do ECharts) e o choropleth inteiro renderiza no cinza de "sem dado", que
    pela nossa legenda quer dizer "ainda não analisado". Arrastar a janela,
    abrir o DevTools ou girar o tablet bastava. O `visualMap` continua no
    option com os `pieces` certos; é o estágio visual que não roda de novo.
    Repintar (setOption completo) devolve a cor — verificado lendo os pixels.
    """
    assert 'self.pintar();' in html
    m = re.search(r"addEventListener\('resize',(.{0,400}?)\}\);", html, re.S)
    assert m, 'sumiu o handler de resize do mapa'
    assert 'pintar()' in m.group(1), 'resize sem repintura: o mapa volta a mentir'


@pytest.mark.django_db
def test_pagina_nao_cacheia_no_browser(client, django_user_model):
    """Sem `no-store` o browser serve o HTML do disco e ESCONDE o deploy.

    Aconteceu em 12/08/2026: o usuário reclamou 3× do layout mostrando prints com
    textos que já tinham sido removidos — estava vendo HTML velho em cache. O
    Cloudflare não era o culpado (`cf-cache-status: DYNAMIC`); era o browser.
    """
    u = django_user_model.objects.create_user('cachetest', password='x', is_staff=True)
    client.force_login(u)
    r = client.get('/dashboard/overview/mapa/')
    assert r.status_code == 200
    cc = r.headers.get('Cache-Control', '')
    assert 'no-store' in cc or 'no-cache' in cc, f'Cache-Control fraco: {cc!r}'


# --------------------------------------------------------------------------- #
# Filtro por PARTE / ENTIDADE ("dá pra ver TODOS os processos do INSS?")
# --------------------------------------------------------------------------- #
def test_filtro_por_parte_existe_e_entra_no_ciclo_normal(html):
    """O campo existe, é debounced e participa do ciclo de filtro da página.

    Debounce é requisito de custo, não de gosto: cada busca é uma agregação
    sobre 71,18M documentos no ES — disparar por tecla multiplicaria isso por
    letra digitada.
    """
    assert 'Parte / entidade' in html
    assert 'id="f-parte"' in html
    assert 'x-model="filtros.parte"' in html
    # entra no `filtros` (é ele que vira querystring do fetch E do link do estado)
    assert re.search(r'filtros:\s*\{\s*\n?\s*parte:', html), 'parte fora do objeto filtros'
    # debounce no input + Enter pra quem não quer esperar
    assert '@input.debounce.600ms="buscarParte()"' in html
    # Enter passa por `enterParte()` desde o autocomplete: com um item do
    # dropdown destacado ele ESCOLHE a entidade; sem item destacado cai no
    # `buscarParte()` de sempre (o campo não virou prisão de autocomplete).
    assert '@keydown.enter.prevent="enterParte()"' in html
    assert 'this.fecharSugestoes();\n        this.buscarParte();' in html
    assert '@input="aplicarFiltros()"' not in CODIGO, 'busca a cada tecla em 71M docs'
    # placeholder com exemplo REAL (nome que existe na base), não "digite aqui"
    assert 'ex.: INSS, Fazenda Pública do Estado de São Paulo' in html
    # chip do que está ativo + fechar por chip + limpar tudo já são genéricos:
    # basta o rótulo estar registrado
    assert re.search(r'_ROTULOS_FILTRO:\s*\{\s*\n?\s*parte:', html)


def test_chip_da_parte_mostra_o_que_foi_BUSCADO(html):
    """Com "IN" no campo, o mapa ainda é o da busca anterior.

    Se o chip lesse o campo de texto ele anunciaria um filtro que não foi
    aplicado — a tela diria "Parte: IN" mostrando os números de "INSS".
    """
    assert "const v = (k === 'parte') ? this._parteAplicada : this.filtros[k];" in html
    # e a página DIZ, em texto, que o mapa está atrasado em relação ao campo
    assert 'O mapa ainda mostra' in html
    assert 'partePendente()' in html
    # o `temFiltro` (que governa a barra de chips) segue o aplicado, não o digitado
    assert 'temFiltro() { return this.chipsFiltros().length > 0; }' in html


def test_parte_propaga_pra_pagina_do_estado(html):
    """A página dedicada do estado tem que herdar a parte buscada.

    `urlPaginaEstado` monta a querystring a partir de `filtros` — o teste
    garante que ela não passou a montar uma lista fixa de chaves (que deixaria
    a parte pra trás sem ninguém perceber).
    """
    m = re.search(r'urlPaginaEstado\(uf\) \{(.{0,400}?)\n      \},', html, re.S)
    assert m, 'sumiu urlPaginaEstado'
    assert 'this.querystring()' in m.group(1)
    qs = re.search(r'querystring\(\) \{(.{0,400}?)\n      \},', html, re.S)
    assert qs and 'Object.entries(this.filtros)' in qs.group(1)


def test_honestidade_do_filtro_de_parte_em_texto_visivel(html):
    """As três armadilhas do dado ficam na TELA, não no hover.

    (1) casa pelo nome escrito na publicação — grafia diferente, número
    diferente (medido: "INSS" 4.255.175 × nome por extenso 4.403.363);
    (2) casa em qualquer polo — não é "quem deve";
    (3) todas as palavras precisam bater (AND).
    """
    assert 'como ele foi escrito na publicação' in html
    assert 'dos dois lados do processo' in html          # não é só devedor
    assert 'Todas as palavras precisam aparecer' in html  # AND
    assert '“Fazenda São Paulo” acha, “Fazenda do Estado SP” não acha' in html
    assert '4,26 milhões' in html and '4,40 milhões' in html
    # a explicação é texto do documento (aria-describedby), nunca tooltip/title
    assert 'id="ajuda-parte"' in html and 'aria-describedby="ajuda-parte"' in html
    # com o painel fechado a ressalva não some: volta resumida na barra de chips
    assert 'Busca pelo nome escrito na publicação, dos dois lados do processo.' in html


def test_atalhos_usam_o_nome_de_maior_recall_medido(html):
    """Atalho tem que trazer MAIS processo que a sigla — medido, não achado.

    Contagens no índice em 12/08/2026 (todas as palavras exigidas, que é como
    o backend monta a query):
        INSS 4.255.175 × Instituto Nacional do Seguro Social 4.403.363
        União 1.597.369 × União Federal 1.508.785
        Fazenda do Estado de São Paulo 82.481 × Fazenda Pública ... 77.372
    """
    assert "{rotulo: 'INSS', q: 'Instituto Nacional do Seguro Social'}" in html
    assert "{rotulo: 'União', q: 'União'}" in html
    assert "{rotulo: 'Fazenda de SP', q: 'Fazenda do Estado de São Paulo'}" in html
    # o atalho preenche o campo (o texto buscado fica à vista — é ele que
    # explica a diferença de contagem)
    assert 'usarAtalhoParte(a.q)' in html
    assert 'os atalhos preenchem o nome por extenso' in html


def test_busca_nao_dispara_com_1_ou_2_letras_nem_repete(html):
    """Guarda de custo: 1-2 letras não é busca, e o debounce repete o valor."""
    assert 'PARTE_MIN: 3' in html
    assert 'if (p.length > 0 && p.length < this.PARTE_MIN) return;' in html
    assert 'if (p === this._parteAplicada) return;' in html
    assert 'Escreva ao menos' in html   # e a tela DIZ por que não buscou


def test_aviso_de_concentracao_na_justica_federal(html):
    """FED não é pintada no mapa — e com filtro de parte ela costuma dominar.

    Medido: o INSS tem 4.213.070 dos seus 4.403.363 processos na Justiça
    Federal (95,7%). Sem o aviso, o choropleth quase todo cinza lê como "o INSS
    mal tem processo" — o oposto do dado.
    """
    assert 'avisoFED()' in html and 'pctFED()' in html
    assert 'que não é pintada no mapa' in html
    # só com filtro de parte e só quando FED passa de metade do recorte
    assert 'if (!this._parteAplicada) return null;' in html
    assert "return (p !== null && p >= 50) ? fmtPct(p) : null;" in html


def test_vazio_por_filtro_nao_vira_falta_de_cobertura(html):
    """Com filtro ativo, o cinza é do FILTRO — não da nossa leitura.

    Medido com `parte=INSS`: 15 dos 27 estados ficam cinza porque não têm
    processo do INSS na base. A legenda dizia "ainda não analisado" nos 15 —
    culpando a nossa cobertura por uma ausência que o usuário mesmo pediu. E o
    aviso do "Ataque primeiro" dizia "já lemos as publicações de 11 dos 27
    estados", que no recorte é a mesma mentira ao contrário.
    """
    assert "if (this.temFiltro()) {" in html
    assert "'sem processo com estes filtros'" in html
    assert "'ainda não analisado ou sem processo com estes filtros'" in html
    # quando as DUAS causas convivem (AP tem processo do INSS mas o sinal nunca
    # foi lido lá), a frase diz as duas em vez de escolher uma
    assert 'const naoLido' in html
    # tooltip de estado sem bucket segue a mesma regra
    assert 'nenhum processo deste estado com os filtros atuais' in html
    # aviso do ranking: texto por função, porque a CAUSA muda com o filtro
    assert 'avisoParcial()' in html
    assert "'Com estes filtros, ' + geo + ' dos 27 estados têm processo'" in html
    assert 'Os demais nem entram nesta comparação.' in html


# --------------------------------------------------------------------------- #
# Ranking = DENSIDADE PURA (backend 1aa6c03) — a copy tinha que virar junto
# --------------------------------------------------------------------------- #
def test_ataque_primeiro_descreve_densidade_e_nao_a_formula_antiga(html):
    """O ranking respondia 3 perguntas num número só e a copy descrevia isso.

    `score_foco` virou `possíveis ÷ processos do estado` — valor e cobertura
    saíram da conta. Medido em prod: o MT tem a 6ª maior densidade do país
    (5,59%) e aparecia em 21º, punido por já termos olhado 24,4% dele e por ter
    publicado valor menor que o de Alagoas. Dizer "muito precatório, COM VALOR,
    e que quase não olhamos ainda" passou a ser mentira sobre a própria lista.
    """
    # a frase agora vem do critério escolhido (o texto fixo virava mentira ao
    # ordenar por quantidade ou valor)
    assert 'rotuloOrdem()' in html
    assert 'que fatia dos processos do estado cita precatório' in html
    assert 'com valor, e que quase não olhamos ainda' not in CODIGO
    assert 'muito precatório em relação ao total E pouco explorado' not in CODIGO
    assert 'o estado mais promissor da lista' not in CODIGO
    # o tooltip da prioridade diz O QUE é 100 e como refazer a conta
    assert 'comparando só os estados desta lista' in html          # segue relativo
    assert '100 = o maior no critério escolhido' in html
    # a conta é a do critério ativo — cada ordem explica a sua
    assert 'A conta é possíveis ÷ processos do estado.' in html
    assert 'contaOrdem()' in html
    # glossário canônico acompanha (fonte única de explicação)
    assert 'que fatia dos processos do estado cita precatório' in html


def test_densidade_visivel_na_linha_e_no_tooltip(html):
    """A nota agora é uma conta que o usuário refaz de cabeça — então aparece.

    Antes o único número exposto era `prioridade 87` (relativo) e o "cálculo
    interno: 0,1115" (o score cru, que embutia valor e cobertura e ninguém
    conseguia reproduzir).
    """
    assert 'densidadePct' in html and 'densLabel' in html
    # calculada do PRÓPRIO bucket (alvo ÷ volume): o endpoint tem cache de 2min
    # e pode servir `score_foco` no formato antigo logo depois do deploy
    assert 'return 100 * alvo / vol;' in html
    # a fatia muda de significado com a lente — a frase muda junto
    assert "' dos processos citam precatório'" in html
    assert "' dos processos a IA confirmou'" in html
    # na linha do ranking E no tooltip do mapa
    assert 'x-text="densLabel(b)"' in html and 'x-text="densLabel(t)"' in html
    assert "'Densidade: ' + fmtPct(densPct)" in html
    # o "cálculo interno" virou a divisão que gera a nota, não o score cru
    assert "fmt(alvoB) + ' ÷ ' + fmt(b.volume)" in html
    assert 'String(b.score_foco ?? 0)' not in CODIGO
    # e a linha mostra os dois números da divisão
    assert "fmt(b.volume) + ' na base'" in html


def test_cobertura_virou_bandeira_e_nao_desconto(html):
    """"Já analisamos X%" saiu da fórmula e virou informação exibida.

    Enquanto multiplicava `(1 − cobertura)`, trabalhar um estado rebaixava esse
    estado no ranking — o comercial nunca via de novo o que já tinha começado a
    atacar. Agora quem decide se vale reatacar é ele, lendo a bandeira.
    """
    assert "('já analisamos ' + p)" in html
    assert "'não sabemos quanto já analisamos'" in html
    assert 'rounded border border-border px-1.5 py-0.5" x-text="cobLabel(b)"' in html
    assert 'não desconto na prioridade' in html      # dito no bloco didático
    # a frase automática do estado não pode mais implicar rebaixamento
    assert 'território praticamente inexplorado, prioridade alta' not in CODIGO
    assert 'densidade alta, mas já bem trabalhado por nós' in html


def test_escolher_entidade_manda_o_ID_nao_so_o_nome(html):
    """Escolher no autocomplete tem que filtrar pelas GRAFIAS, não pelo nome.

    O nome canônico vira `match` AND no backend e casa palavra solta em partes
    diferentes do processo. Medido em prod: por texto, "MUNICIPIO DE SAO PAULO"
    traz 45,5% de lixo (Município de São LUÍS + uma pessoa chamada PAULO) e
    "UNIAO FEDERAL" traz 18,3% (Defensoria da União + Caixa Econômica FEDERAL).
    O `entidade_id` vira o OR das grafias exatas.
    """
    assert 'this.filtros.entidade_id = item.entidade_id;' in html
    # a chave precisa existir no objeto inicial: Alpine só observa o declarado,
    # e o querystring() itera as chaves de `filtros`
    assert "entidade_id: ''," in html
    # soltar a entidade volta pro texto livre
    assert "this.filtros.entidade_id = '';" in html


def test_escolher_entidade_recarrega_mesmo_com_texto_igual(html):
    """`carregar()` direto, não `buscarParte()`.

    A guarda de `buscarParte` compara só o texto normalizado; digitar "uniao
    federal" e depois escolher "UNIAO FEDERAL" no dropdown dá o mesmo texto, e
    a busca não dispararia — o `entidade_id` nunca chegaria ao backend.
    """
    ini = html.find('escolherEntidade(item) {')
    fim = html.find('esquecerEntidade()', ini)
    corpo = html[ini:fim]
    assert 'this.carregar();' in corpo
    assert 'this.buscarParte();' not in corpo


def test_entidade_id_nao_vira_chip(html):
    """O chip é pra humano: "entidade_id: cnpj:29979036" não diz nada.

    Quem informa é o chip de `parte` (o nome) — o id fica invisível de propósito.
    """
    ini = html.find('_ROTULOS_FILTRO')
    fim = html.find('chipsFiltros()', ini)
    assert 'entidade_id' not in html[ini:fim]


def test_siglas_sempre_visiveis_no_mapa(html):
    """A sigla do estado não pode depender de hover.

    Antes só aparecia no `emphasis` — quem não sabe o desenho do Brasil de cor
    não sabia que estado estava olhando, e em touch não existe hover.
    """
    ini = html.find("type: 'map'")
    fim = html.find('itemStyle:', ini)
    bloco = html[ini:fim]
    assert 'label: {show: false}' not in bloco, 'o rótulo voltou a depender de hover'
    assert 'show: true,' in bloco
    assert 'formatter: (p) => p.name' in bloco, 'o rótulo tem que ser a SIGLA'
    # contorno em vez de cor chapada: o mapa tem 4 tons por baixo e qualquer
    # cor fixa some em metade deles
    assert 'textBorderColor' in bloco and 'textBorderWidth' in bloco
    # DF/SE/AL se sobrepõem no Sudeste/Nordeste
    assert 'hideOverlap: true' in bloco
    # e o hover continua destacando
    assert 'emphasis:' in bloco


def test_painel_do_estado_diz_a_posicao_nacional(html):
    """Clicar num estado fora do top-10 deixava a pergunta óbvia sem resposta.

    O painel mostrava os números do estado e o "Ataque primeiro" ao lado exibia
    OUTROS 10 — sem nada dizendo onde aquele estado está na lista.
    """
    assert 'posicaoNacional' in html
    assert 'na lista "Ataque primeiro"' in html
    assert "x-text=\"'Está em ' + posicaoNacional() + '.'\"" in html


def test_prioridade_do_tribunal_nao_e_tautologica(html):
    """"prioridade 100" num estado de UM tribunal não diz nada.

    Pior: colidia na mesma tela com a prioridade NACIONAL do mesmo estado (SP
    aparecia como "prioridade 15" no tooltip do mapa e "TJSP prioridade 100" no
    painel). São réguas diferentes; agora a tela diz qual é qual.
    """
    assert 'único tribunal do estado' in html
    assert "x-show=\"tribunais.length > 1\"" in html
    assert "prioridadeLabel(t, tribunais) + ' entre os daqui'" in html


def test_ataque_primeiro_tem_3_criterios_de_ordem(html):
    """"Onde atacar" tem mais de uma resposta certa — quem escolhe é o dono.

    Medido em prod (13/08/2026), São Paulo está em 21º por concentração, 7º por
    volume e 2º por valor. Ordenar só por concentração escondia o maior mercado
    de precatório do país da lista que se chama "Ataque primeiro".
    """
    assert 'ordemRanking' in html
    assert 'Critério de ordenação dos estados' in html
    assert "'Concentração'" in html and "'Volume'" in html and "'Valor R$'" in html
    # "Volume", não "Quantidade": o card do mapa já tem um toggle
    # "Quantidade ↔ R$" e dois botões iguais com sentidos diferentes é armadilha
    assert "quantidade: {t: 'Quantidade'" not in html
    # a lista, a barra e a nota usam a MESMA métrica da ordem ativa
    assert 'metricaOrdem(b) - this.metricaOrdem(a)' in html
    assert '100 * this.metricaOrdem(b) / max' in html


def test_ordem_por_valor_so_pontua_quem_tem_valor(html):
    """Ordenando por R$ a régua é OUTRA — e sem valor não há nota.

    O sinal do texto não importa aqui (por isso a regra antiga devolvia sempre
    `false`), MAS quem está sem valor não pode receber "prioridade 0": isso
    afirma "é o pior da lista" quando o certo é "não dá pra medir". São 23 dos
    28 buckets — a maioria da lista ganharia uma nota inventada.
    O RÓTULO é que separa as duas causas do vazio.
    """
    assert "if (this.ordemRanking === 'valor') {" in html
    assert 'return !this.temValor(b);' in html
    # o rótulo diz a causa certa, em vez de "prioridade não medida"
    assert "'valor ainda não buscado' : 'sem valor publicado'" in html
    # barra vazia (não o mínimo de 3%, que desenharia "quase nada")
    assert 'if (this.prioridadeIndisponivel(b)) return 0;' in html


# --------------------------------------------------------------------------- #
# R$ VAZIO: a fonte não publica × nós não buscamos
# --------------------------------------------------------------------------- #
def test_valor_vazio_diz_qual_dos_dois_vazios_e(html):
    """"Valor informado: não informado" mentia por omissão.

    Medido em 13/08/2026 (27 processos reais, HTML cru): a consulta pública do
    PJe não expõe valor da causa em tribunal NENHUM — a página tem 6 campos e a
    string "valor" não aparece uma vez. Publicam só e-SAJ (TJSP/TJAL/TJAC) e 2
    portais próprios (TJPA/TJMT): 5 UFs de 27, 2,8% dos 71,18M docs. Logo, na
    esmagadora maioria dos buckets "não informado" NÃO é fila nossa — é um
    campo que não existe. O backend marca cada bucket com `fonte_publica_valor`
    e a tela escolhe a frase a partir dele; sem a marca (payload velho do cache
    de 2min) ela não afirma nenhuma das duas.
    """
    assert 'fonte_publica_valor' in html
    assert 'fonteSemValor' in html and 'fonteComValor' in html
    # as duas frases, por extenso e curtas
    assert 'este tribunal não publica esse valor' in html
    assert 'ainda não buscamos esse valor' in html
    assert 'R$ não publicado pelo tribunal' in html
    assert 'R$ ainda não buscado' in html
    # federal no plural (são TRF1/3/5, não "o tribunal")
    assert 'os tribunais federais não publicam esse valor' in html
    # sem marca não se inventa causa
    assert "return 'não informado';" in html
    # o tooltip do choropleth passou a usar a frase com causa
    assert "'Valor informado: ' + self.valorLabel(b)" in html


def test_modo_reais_avisa_que_compara_5_com_23(html):
    """O modo R$ e a ordem por R$ comparam quem publica com quem não publica.

    Alagoas lidera com R$ 1,3 tri por ser um dos poucos que abre o número, não
    por ter mais crédito — sem isso escrito na tela, o ranking é uma armadilha.
    E tem que ser TEXTO VISÍVEL: quem olha o mapa e conclui não passa o mouse
    em nada antes.
    """
    # aviso do modo R$ (mapa) — com a contagem tirada dos próprios buckets
    assert 'Mapa por R$: só' in html
    assert "x-text=\"ufsComFonte()\"" in html and "x-text=\"ufsGeo().length\"" in html
    assert 'não entram na comparação' in html
    assert 'cor mais forte é de quem publica, não de quem tem mais crédito' in html
    # aviso da ORDEM por R$ (ranking "Ataque primeiro")
    assert "ordemRanking === 'valor' && ufsSemFonte() > 0" in html
    assert 'Esta ordem só compara os' in html
    assert 'Quem lidera aqui lidera por publicar' in html
    # KPI global qualificado: a soma nunca é do país inteiro
    assert "' estados publicam esse valor'" in html


def test_quem_nao_publica_tem_faixa_propria_no_mapa(html):
    """Faixa PRÓPRIA (hachura), não "R$ 0" nem o mesmo cinza.

    Três motivos, medidos: (1) na escala de cor, valor 0 na faixa mais baixa
    diria "quase nenhum dinheiro aqui" — falso; (2) o cinza chapado já significa
    "ainda não analisado/buscamos", e 23 dos 28 buckets cairiam nele, jogando
    na nossa conta um vazio que é da fonte; (3) dois cinzas chapados não se
    distinguem a um metro nem no daltonismo. Hachura é FORMA — a convenção
    cartográfica de "não se aplica" — e sobrevive aos dois temas.
    """
    assert 'decalSemFonte' in html
    assert 'rotation: -Math.PI / 4' in html
    # só no modo R$: no modo Quantidade o estado tem dado normal
    assert "if (emR$ && this.fonteSemValor(b)) d.itemStyle = {decal: decalSemFonte};" in html
    # a legenda repete o desenho do canvas (mesmo ângulo, mesmo passo)
    assert 'repeating-linear-gradient(-45deg' in html
    assert 'corHachura' in html
