"""A tela de Estoque não pode inventar saldo, nem somar cliente em silêncio.

Cada teste corresponde a um erro medido — não a uma preferência de estilo:

  1. **Saldo não existe.** Consumo é histórico e cumulativo; estoque é o rótulo
     de hoje. `estoque − consumido` não responde nada em direção nenhuma — a
     resposta é a fatia `so_estoque` do cruzamento.
  2. **Cliente não soma.** Os 405.740 processos do `juriscope` estavam TODOS no
     `falcon` (01/09/2026): somar dá 1.217.100 contra a união real de 811.360,
     contando 405.740 duas vezes. A tela mostra cliente, união e sobreposição.
  3. **Registro ≠ processo distinto**, e a unidade do bloco de resultado NÃO é
     adivinhada — vem declarada em `consumo_resultado_unidade`.
  4. **Bloco sem medição sai da tela PELO NOME.** Zerado, nunca.
  5. **A guarda é pelo NÚMERO.** `'0'` é string verdadeira no `if` do Django e
     já fez painel zerado se anunciar como medido.

O payload dos testes espelha o que `dashboard/estoque.py` publica de verdade
(`_montar`), não um contrato imaginado.
"""
import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.urls import reverse

from dashboard import estoque_views as EV

CHAVE = f'{EV.CHAVE_BASE}:precatorio'

PAYLOAD = {
    'em': '2026-09-01T10:00:00+00:00',
    'nao_medidos': [],
    'trilha': 'precatorio',
    'rotulos': ['PRECATORIO', 'PRE_PRECATORIO'],
    'total_consumos': 1224278,
    'total_consumido_distinto': 811360,
    'consumo_por_resultado': {'validado': 700000, 'pendente': 400000,
                              'sem_expedicao': 100000, 'erro': 24278},
    'consumo_resultado_unidade': 'registros',
    'consumo_clientes': {
        'itens': [{'cliente': 'falcon', 'registros': 818538, 'processos': 811360},
                  {'cliente': 'juriscope', 'registros': 405740, 'processos': 405740}],
        'soma_dos_clientes': 1217100,
        'uniao_distinta': 811360,
        'sobreposicao': 405740,
        'nota': '1.217.100 é a SOMA por cliente e 811.360 é a UNIÃO distinta.',
    },
    'total_estoque': 936755,
    'estoque_por_rotulo': {'PRECATORIO': 55285, 'PRE_PRECATORIO': 881470},
    'estoque_nao_classificados': 0,
    'estoque_total_processos': 104000000,
    'segundos_varredura': 52,
    'por_tribunal': [
        {'t': 'TJSP', 'estoque': 240180, 'consumido': 355900, 'consumos': 501000,
         'ambos': 190000,
         'por_cliente': {'falcon': 355900, 'juriscope': 180000},
         'resultado': {'validado': 300000, 'pendente': 160000, 'sem_expedicao': 41000}},
        {'t': 'TRF3', 'estoque': 90010, 'consumido': 120400, 'consumos': 180000,
         'ambos': 70000,
         'por_cliente': {'falcon': 120400, 'juriscope': 60000},
         'resultado': {'validado': 100000, 'pendente': 60000, 'sem_expedicao': 20000}},
    ],
    'cruzamento': {
        'ambos': 541185,
        'so_estoque': 395570,
        'so_consumo': 270175,
        'nota': 'Dos 811.360 processos distintos já consumidos, 541.185 (66,7%) '
                'estão hoje na trilha precatorio.',
    },
    'consumo_por_classificacao_atual': {
        'PRE_PRECATORIO': 490552, 'NAO_LEAD': 195683,
        'DIREITO_CREDITORIO': 74492, 'PRECATORIO': 50633,
    },
    # 55.285 − 50.633 = 4.652 · 881.470 − 490.552 = 390.918 · soma 395.570,
    # que é exatamente o `so_estoque` acima. O controle da decomposição.
}


@pytest.fixture
def logado(db, client):
    u = get_user_model().objects.create_user('estoque-tela', password='x')
    client.force_login(u)
    return client


def _html(c, trilha='precatorio'):
    r = c.get(reverse('dashboard:estoque'), {'trilha': trilha})
    assert r.status_code == 200, r.status_code
    return r.content.decode()


def _fonte(nome='dashboard/templates/dashboard/estoque.html'):
    with open(nome, encoding='utf-8') as f:
        return f.read()


# ---------------------------------------------------------------- renderização

@pytest.mark.django_db
def test_o_template_nao_vaza_tag_nem_comentario(logado):
    """Cerquilha atravessando linha já vazou literal na tela desta casa."""
    cache.set(CHAVE, PAYLOAD, 60)
    h = _html(logado)
    for veneno in ('{{', '{%', 'endcomment', 'endblock', 'TemplateSyntaxError'):
        assert veneno not in h, f'vazou {veneno!r} na tela'


@pytest.mark.django_db
def test_estado_normal_mostra_os_dois_lados_inteiros(logado):
    cache.set(CHAVE, PAYLOAD, 60)
    h = _html(logado)
    assert 'Os dois lados' in h
    assert '936.755' in h                      # estoque da trilha
    assert '811.360' in h                      # processos distintos
    assert '1.224.278' in h                    # registros de consumo
    assert 'TJSP' in h and 'TRF3' in h


@pytest.mark.django_db
def test_a_composicao_da_trilha_fica_VISIVEL(logado):
    """`precatorio` soma dois rótulos. Recorte escondido ninguém confere."""
    cache.set(CHAVE, PAYLOAD, 60)
    h = _html(logado)
    assert 'PRECATORIO' in h and 'PRE_PRECATORIO' in h
    assert '55.285' in h and '881.470' in h     # os dois SEPARADOS


@pytest.mark.django_db
def test_sem_cache_a_tela_CARREGA_e_diz_que_nao_mediu(logado):
    """Quebrar seria pior; mostrar zero seria muito pior."""
    cache.delete(CHAVE)
    cache.delete(EV.CHAVE_BASE)
    h = _html(logado)
    assert 'Ainda não medido' in h
    assert 'Os dois lados' not in h
    assert '52 segundos' in h                  # diz POR QUE não mediu


# ------------------------------------------------------ as quatro proibições

@pytest.mark.django_db
def test_a_subtracao_dos_dois_lados_nunca_aparece(logado):
    """`estoque − consumido` = 125.395 e o estoque real que resta é 395.570."""
    cache.set(CHAVE, PAYLOAD, 60)
    h = _html(logado)
    errado = PAYLOAD['total_estoque'] - PAYLOAD['total_consumido_distinto']
    assert f'{errado:,}'.replace(',', '.') not in h
    assert 'não existe saldo' in h.lower()
    assert '395.570' in h                      # a resposta de verdade


@pytest.mark.django_db
def test_clientes_com_uniao_e_sobreposicao_no_lugar_da_soma(logado):
    cache.set(CHAVE, PAYLOAD, 60)
    h = _html(logado)
    assert 'falcon' in h and 'juriscope' in h
    assert '1.217.100' in h                    # a soma, ROTULADA como soma
    assert 'união' in h.lower()
    assert '405.740' in h                      # a sobreposição
    assert 'soma dos clientes não é o total' in h.lower()


@pytest.mark.django_db
def test_a_unidade_do_resultado_e_DECLARADA_nao_adivinhada(logado):
    cache.set(CHAVE, PAYLOAD, 60)
    h = _html(logado)
    assert 'registros' in h
    assert 'Conferência' in h
    assert 'fecham' in h                       # a soma bate com 1.224.278


@pytest.mark.django_db
def test_resultado_que_nao_fecha_e_DENUNCIADO(logado):
    """Soma que não bate é achado, não ruído."""
    p = dict(PAYLOAD, consumo_por_resultado={'validado': 10, 'pendente': 5})
    cache.set(CHAVE, p, 60)
    h = _html(logado)
    assert 'não fecham' in h
    assert 'incompleto' in h


@pytest.mark.django_db
def test_resultado_fora_dos_tres_destaques_nao_some(logado):
    """`erro` sumir quebraria a conferência sem ninguém perceber."""
    cache.set(CHAVE, PAYLOAD, 60)
    h = _html(logado)
    assert 'Outros resultados' in h
    assert '24.278' in h


@pytest.mark.django_db
def test_a_tela_diz_qual_numero_esta_mostrando(logado):
    """Registros e distintos diferem 51% — confundi-los é o erro fácil."""
    cache.set(CHAVE, PAYLOAD, 60)
    h = _html(logado)
    assert 'processos distintos' in h
    assert 'registros de consumo' in h
    assert 're-consumo' in h


@pytest.mark.django_db
def test_bloco_sem_medicao_sai_da_tela_PELO_NOME(logado):
    """Meia régua é pior que régua nenhuma."""
    p = dict(PAYLOAD)
    p.pop('cruzamento')
    p['nao_medidos'] = ['estoque classificado e cruzamento: timeout de 900s']
    cache.set(CHAVE, p, 60)
    h = _html(logado)
    assert 'Sem medição nesta passagem' in h
    assert 'Cruzamento estoque × consumo' in h       # detectado pela tela
    assert 'timeout de 900s' in h                    # veio do medidor
    assert 'Onde os dois lados se encontram' not in h


# ------------------------------------------------- a guarda é pelo NÚMERO

def test_zero_string_nunca_vira_bloco_medido():
    """`'0'` é verdadeiro no `if` do Django — por isso a guarda é `_num()`."""
    assert EV._num('0') is None
    assert EV._num('') is None
    assert EV._num(None) is None
    assert EV._num('1.234') is None
    assert EV._num(True) is None             # bool é int em Python; não é medida
    assert EV._num(0) == {'n': 0, 'txt': '0'}
    assert EV._num(936755) == {'n': 936755, 'txt': '936.755'}


@pytest.mark.django_db
def test_total_que_veio_como_texto_nao_vira_painel_zerado(logado):
    p = dict(PAYLOAD, total_estoque='0')
    cache.set(CHAVE, p, 60)
    h = _html(logado)
    assert '936.755' not in h                        # o painel sumiu
    assert 'Sem medição nesta passagem' in h         # e disse que sumiu
    assert 'Estoque marcado' in h.split('Sem medição nesta passagem')[1]


@pytest.mark.django_db
def test_zero_MEDIDO_continua_aparecendo(logado):
    """Zero medido é resultado; zero inventado é mentira."""
    p = dict(PAYLOAD, total_estoque=0)
    cache.set(CHAVE, p, 60)
    h = _html(logado)
    assert 'estoque marcado' in h.lower()
    assert 'Sem medição nesta passagem' not in h


# ------------------------------------------------------------ cruzamento

@pytest.mark.django_db
def test_cruzamento_so_desenha_a_barra_com_as_TRES_fatias(logado):
    """Barra com uma fatia faltando desenha um todo que não é o todo."""
    p = dict(PAYLOAD, cruzamento={'ambos': 541185, 'so_estoque': 395570})
    cache.set(CHAVE, p, 60)
    h = _html(logado)
    assert 'Onde os dois lados se encontram' in h    # o que veio, aparece
    assert 'união =' not in h                        # o todo, não


def test_cruzamento_nao_e_deduzido_dos_totais():
    """Interseção de conjunto não se calcula com duas contagens."""
    p = dict(PAYLOAD)
    p.pop('cruzamento')
    ctx, faltando = EV._normalizar(p)
    assert ctx['cruzamento'] is None
    assert 'Cruzamento estoque × consumo' in faltando


@pytest.mark.django_db
def test_o_que_sobra_aparece_ABERTO_POR_ROTULO(logado):
    """"395.570 em estoque" faz parecer que há muito precatório. Não há."""
    cache.set(CHAVE, PAYLOAD, 60)
    h = _html(logado)
    assert 'O que sobra' in h
    assert '4.652' in h                            # PRECATORIO que resta
    assert '390.918' in h                          # PRE_PRECATORIO que resta
    assert '91.6% já foi puxado' in h or '91,6% já foi puxado' in h


def test_a_decomposicao_do_saldo_fecha_com_o_cruzamento():
    """Soma das parcelas TEM que dar o `so_estoque`. É o controle do bloco."""
    ctx, _ = EV._normalizar(PAYLOAD)
    parcelas = [i['v']['n'] for i in ctx['saldo']['itens']]
    assert sum(parcelas) == PAYLOAD['cruzamento']['so_estoque']
    assert parcelas == [4652, 390918]


@pytest.mark.django_db
def test_decomposicao_que_NAO_fecha_nao_e_publicada(logado):
    """Régua que não bate com a própria fonte não vai pra tela."""
    p = dict(PAYLOAD)
    p['cruzamento'] = dict(PAYLOAD['cruzamento'], so_estoque=999999)
    ctx, faltando = EV._normalizar(p)
    assert ctx['saldo'] is None
    assert 'O que sobra, por rótulo' in faltando

    cache.set(CHAVE, p, 60)
    h = _html(logado)
    assert '4.652' not in h                        # o card não foi publicado
    assert 'Sem medição nesta passagem' in h
    assert 'O que sobra, por rótulo' in h.split('Sem medição nesta passagem')[1]


def test_o_saldo_nao_e_deduzido_quando_falta_o_rotulo():
    """Parcela ausente derruba o bloco inteiro; meia decomposição mente."""
    p = dict(PAYLOAD)
    p['estoque_por_rotulo'] = {'PRECATORIO': 55285}      # falta PRE_PRECATORIO
    ctx, faltando = EV._normalizar(p)
    assert ctx['saldo'] is None
    assert 'O que sobra, por rótulo' in faltando


# ------------------------------------------------------- clientes dinâmicos

def test_a_lista_de_clientes_vem_do_PAYLOAD_nao_do_codigo():
    """Cliente novo sumiria da tela em silêncio se a lista fosse fixa."""
    p = dict(PAYLOAD)
    p['consumo_clientes'] = dict(PAYLOAD['consumo_clientes'])
    p['consumo_clientes']['itens'] = (
        list(PAYLOAD['consumo_clientes']['itens'])
        + [{'cliente': 'cliente_novo', 'registros': 10, 'processos': 9}])
    ctx, _ = EV._normalizar(p)
    nomes = [i['cliente'] for i in ctx['clientes']['itens']]
    assert 'cliente_novo' in nomes
    assert 'cliente_novo' in ctx['colunas_cliente']


def test_cliente_sem_cor_validada_fica_fora_do_GRAFICO_e_e_anunciado():
    """Cor gerada na hora abre a porta pra paleta padrão do ECharts."""
    p = dict(PAYLOAD)
    p['consumo_clientes'] = dict(PAYLOAD['consumo_clientes'])
    p['consumo_clientes']['itens'] = [
        {'cliente': f'c{i}', 'registros': 10, 'processos': 9} for i in range(5)]
    ctx, _ = EV._normalizar(p)
    assert len(ctx['series_clientes']) == len(EV.CORES_CLIENTE)
    assert ctx['clientes']['sem_cor'] == ['c3', 'c4']


def test_nenhuma_cor_de_serie_e_gerada_na_hora():
    fonte = _fonte()
    assert 'CORES_CLIENTE' not in fonte, 'a paleta vive no Python, não no template'
    script = fonte.split('<script>')[1].split('</script>')[0]
    for suspeito in ('hsl(', 'Math.random', 'colorLista[i %', '% cores.length'):
        assert suspeito not in script, f'cor gerada/ciclada no script: {suspeito}'


# ------------------------------------------------------------ regra nº 7

def test_a_view_nunca_calcula():
    """A varredura custa minutos. Medir na requisição derrubaria o site."""
    cache.delete(CHAVE)
    cache.delete(EV.CHAVE_BASE)
    assert EV._payload('precatorio') is None


def test_a_view_le_pelo_ler_do_medidor(monkeypatch):
    """Se o medidor expõe `ler(trilha)`, é ele que manda — não a chave crua."""
    from dashboard import estoque as medidor
    chamadas = []

    def falso(trilha='precatorio'):
        chamadas.append(trilha)
        return dict(PAYLOAD, trilha=trilha)

    monkeypatch.setattr(medidor, 'ler', falso)
    p = EV._payload('direito_creditorio')
    assert chamadas == ['direito_creditorio']
    assert p['trilha'] == 'direito_creditorio'


def test_payload_sem_carimbo_de_trilha_nao_serve_as_duas_abas():
    """Servir o mesmo número em Precatório e em Direito Creditório é ficção."""
    p = dict(PAYLOAD)
    p.pop('trilha')
    cache.set(EV.CHAVE_BASE, p, 60)
    cache.delete(CHAVE)
    cache.delete(f'{EV.CHAVE_BASE}:direito_creditorio')
    try:
        assert EV._payload('precatorio') is None
        assert EV._payload('direito_creditorio') is None
    finally:
        cache.delete(EV.CHAVE_BASE)


@pytest.mark.django_db
def test_a_chave_de_trilha_alterna_de_verdade(logado):
    cache.set(CHAVE, PAYLOAD, 60)
    cache.delete(f'{EV.CHAVE_BASE}:direito_creditorio')
    assert 'Os dois lados' in _html(logado, 'precatorio')
    assert 'Ainda não medido' in _html(logado, 'direito_creditorio')


@pytest.mark.django_db
def test_trilha_invalida_cai_no_padrao_sem_explodir(logado):
    cache.set(CHAVE, PAYLOAD, 60)
    r = logado.get(reverse('dashboard:estoque'), {'trilha': '../etc/passwd'})
    assert r.status_code == 200
    assert 'Precatório' in r.content.decode()


# ------------------------------------------------------- padrões da casa

def test_o_template_nao_usa_classe_de_cor_MORTA():
    """`success-fg` e `warn` não estão no tailwind.config — não geram nada."""
    fonte = _fonte()
    for morta in ('text-success-fg', 'bg-warn/', 'text-warn-fg', 'border-warn'):
        assert morta not in fonte, f'{morta} é classe morta neste projeto'


def test_o_template_nao_usa_title_nem_text_base_como_tamanho():
    fonte = _fonte()
    assert ' title="' not in fonte, 'explicação vai em .voy-tip, não em title='
    assert 'class="text-base' not in fonte, '`text-base` aqui é COR, não tamanho'


def test_todo_return_do_script_grita_no_console():
    """Um `return` calado deixou dois gráficos um dia inteiro em caixa vazia."""
    script = _fonte().split('<script>')[1].split('</script>')[0]
    linhas = script.splitlines()
    for i, linha in enumerate(linhas):
        if linha.strip() != 'return;':
            continue
        # sobe até a linha que ABRE a guarda; tudo entre ela e o `return` é o
        # corpo da guarda, e é ali que o grito tem que estar.
        corpo, j = [], i - 1
        while j >= 0 and not linhas[j].rstrip().endswith('{'):
            corpo.append(linhas[j])
            j -= 1
        corpo.append(linhas[j] if j >= 0 else '')
        assert any('console.' in c for c in corpo), (
            f'`return` sem grito na linha {i + 1} do script: '
            + '\n'.join(reversed(corpo)))


def test_o_script_espera_o_DOM():
    """`setupChart` só existe 21 linhas DEPOIS do bloco de conteúdo."""
    assert "addEventListener('DOMContentLoaded'" in _fonte().split('<script>')[1]


def test_nenhuma_tag_do_django_dentro_de_comentario_de_javascript():
    """O Django parseia tag mesmo dentro de `//` — já matou um template."""
    for linha in _fonte().splitlines():
        corte = linha.strip()
        if corte.startswith('//'):
            assert '{%' not in corte and '{{' not in corte, corte


def test_todo_icone_usado_existe_no_sprite():
    """Ícone inexistente rende SVG vazio, sem erro nenhum."""
    import re
    fonte = _fonte()
    sprite = _fonte('dashboard/static/dashboard/voyager-icons.svg')
    existem = set(re.findall(r'id="voy-([a-z0-9-]+)"', sprite))
    usados = set(re.findall(r'voy_icon "([a-z0-9-]+)"', fonte))
    assert usados, 'nenhum ícone no template?'
    assert usados <= existem, f'ícone inexistente: {usados - existem}'


def test_porcentagem_em_CSS_sai_sem_localizacao():
    """`LANGUAGE_CODE='pt-br'` renderiza 2.3 como "2,3" e quebra `width:`."""
    for linha in _fonte().splitlines():
        if 'style="width:' in linha or 'style="width: ' in linha:
            assert 'unlocalize' in linha, linha.strip()
