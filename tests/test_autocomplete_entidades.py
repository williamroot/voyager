"""Autocomplete de ENTIDADE canônica — endpoint + combobox do Mapa de Precatórios.

O filtro "Parte / entidade" era texto livre: quem buscava tinha de adivinhar a
grafia que o tribunal digitou ("INSS" acha 4.255.175 processos, "Instituto
Nacional do Seguro Social" acha 4.403.363 — a MESMA entidade). O índice
canônico (`search/entidades.py`, 1.141.630 entidades em prod) resolve isso: uma
linha por entidade, com todas as grafias juntas.

Este arquivo cobre os dois lados:

  BACKEND  `GET /dashboard/api/entidades/autocomplete`
    - login-gate e só GET;
    - `q` com menos de 3 caracteres NÃO toca no ES (é custo sem produto: 1-2
      letras casam com centenas de milhares das 1,14M entidades);
    - ES fora → 503 com mensagem, nunca 500 cru;
    - cache curto por termo (a segunda digitação idêntica não repete a query);
    - `n_processos` AUSENTE no índice vira `null` = DESCONHECIDO. Nunca 0 —
      zero é a afirmação "esta entidade não tem processo", e seria mentira
      enquanto a contagem não existe.

  UI  combobox no campo existente
    - ARIA de combobox (`role=combobox`/`listbox`/`option`, `aria-expanded`,
      `aria-activedescendant`), teclado (↑ ↓ Enter Esc) e clique fora;
    - texto livre continua funcionando (Enter sem item destacado busca o que
      está escrito);
    - honestidade: diz quantas grafias a entidade unifica e escreve "não
      contamos ainda" quando não há contagem de processos.
"""
from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse

pytestmark = pytest.mark.django_db

User = get_user_model()

URL_NAME = 'dashboard:entidades-autocomplete'
PAGINA = 'dashboard:comercial-mapa-page'

#: cache local: o teste não pode depender do Redis do ambiente nem sujar chave
#: compartilhada — e o TTL do endpoint é justamente uma das garantias testadas.
CACHE_LOCAL = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
                           'LOCATION': 'ac-entidades-teste'}}

#: doc do índice como ele está HOJE em prod: SEM `n_processos` (o campo está
#: sendo populado). É o caso que a UI tem de tratar como "não contamos ainda".
DOC_INSS = {
    'entidade_id': 'cnpj:29979036',
    'chave': 'cnpj',
    'raiz_cnpj': '29979036',
    'nome_canonico': 'INSTITUTO NACIONAL DO SEGURO SOCIAL',
    'variantes': ['INSTITUTO NACIONAL DO SEGURO SOCIAL',
                  'INSTITUTO NACIONAL DO SEGURO SOCIAL - INSS',
                  'Instituto Nacional do Seguro Social - INSS',
                  'INSS - CEAB/DJ'],
    'variantes_busca': ['INSTITUTO NACIONAL DO SEGURO SOCIAL',
                        'INSTITUTO NACIONAL DO SEGURO SOCIAL - INSS',
                        'Instituto Nacional do Seguro Social - INSS'],
    'n_variantes': 104,
    'documentos': ['29979036000140', '29979036000221', '29979036001201',
                   '29979036098876', '29979036000310'],
    'eh_ente_publico': True,
    'n_partes': 764,
}


def _resposta_es(*fontes):
    return {'took': 7, 'hits': {'hits': [{'_source': f} for f in fontes]}}


@pytest.fixture(autouse=True)
def cache_local():
    """Cache do endpoint em memória e ZERADO por teste.

    Sem isolar, o cache de 60s do endpoint atravessa os testes (o segundo `q=inss`
    seria servido do payload do primeiro) e a suíte passaria a medir o cache em
    vez do endpoint. LocMem também evita depender do Redis do ambiente —
    e evita um `cache.clear()` num Redis compartilhado, que já derrubou caches
    caros em prod (ver OPS).
    """
    with override_settings(CACHES=CACHE_LOCAL):
        from django.core.cache import cache
        cache.clear()
        yield
        cache.clear()


@pytest.fixture
def user(db):
    return User.objects.create_user(username='ac_entidades', password='x')


@pytest.fixture
def logado(user):
    c = Client(SERVER_NAME='localhost')
    c.force_login(user)
    return c


@pytest.fixture
def html(logado):
    resp = logado.get(reverse(PAGINA))
    assert resp.status_code == 200
    return resp.content.decode()


# --------------------------------------------------------------------------- #
# Endpoint — contrato, guardas e degradação
# --------------------------------------------------------------------------- #
def test_url_resolve():
    assert reverse(URL_NAME) == '/dashboard/api/entidades/autocomplete'


def test_anon_redireciona_login(client):
    resp = client.get(reverse(URL_NAME), {'q': 'inss'})
    assert resp.status_code == 302
    assert '/login' in resp['Location']


def test_so_get(logado):
    assert logado.post(reverse(URL_NAME), {'q': 'inss'}).status_code == 405


@pytest.mark.parametrize('termo', ['', ' ', 'i', 'in', '  in  '])
def test_termo_curto_nao_toca_no_es(logado, termo):
    """1-2 letras não é busca — é agregação cara à toa em 1,14M entidades.

    A guarda é do BACKEND (a do browser é conveniência): quem chamar o endpoint
    na mão também não derruba o cluster.
    """
    with patch('dashboard.comercial_views.get_es') as get_es:
        resp = logado.get(reverse(URL_NAME), {'q': termo})
        assert not get_es.called, 'termo curto foi ao Elasticsearch'
    assert resp.status_code == 200
    dados = resp.json()
    assert dados['itens'] == []
    assert dados['buscou'] is False
    assert dados['motivo'] in ('sem_termo', 'termo_curto')
    # a UI precisa do número pra dizer POR QUE não buscou
    assert dados['min_caracteres'] == 3


@override_settings(ENTIDADES_INDICE_SUFIXO='entidades-teste')
def test_busca_valida_usa_o_servico_e_o_indice_do_setting(logado):
    """A view não monta query: delega pra `entidades.query_autocomplete`.

    E o índice sai de setting — hoje o de TESTE (`voyager-entidades-teste`),
    promovido por variável de ambiente e não por caça a hardcode.
    """
    es = MagicMock()
    es.search.return_value = _resposta_es(DOC_INSS)
    with patch('dashboard.comercial_views.get_es', return_value=es):
        resp = logado.get(reverse(URL_NAME), {'q': 'inss'})
    assert resp.status_code == 200
    _args, kwargs = es.search.call_args
    assert kwargs['index'] == 'voyager-entidades-teste'
    # corpo veio do serviço (function_score + _source enxuto), não inventado aqui
    assert 'function_score' in kwargs['query']
    assert kwargs['size'] == 8
    assert resp.json()['indice'] == 'voyager-entidades-teste'


def test_item_traz_o_que_a_ui_mostra(logado):
    es = MagicMock()
    es.search.return_value = _resposta_es(DOC_INSS)
    with patch('dashboard.comercial_views.get_es', return_value=es):
        item = logado.get(reverse(URL_NAME), {'q': 'inss'}).json()['itens'][0]

    assert item['entidade_id'] == 'cnpj:29979036'
    assert item['nome_canonico'] == 'INSTITUTO NACIONAL DO SEGURO SOCIAL'
    # o número que justifica o autocomplete existir: 104 grafias numa linha só
    assert item['n_variantes'] == 104
    assert item['eh_ente_publico'] is True
    assert item['n_partes'] == 764
    # procedência da chave vai no payload: 'cnpj' é identidade PROVADA por
    # documento, 'nome' é heurística de grafia — quem consome precisa saber
    assert item['chave'] == 'cnpj'
    assert item['raiz_cnpj'] == '29979036'
    # amostra de grafias sai de `variantes_busca` (as com peso), não da cauda
    assert item['grafias_exemplo'] == DOC_INSS['variantes_busca'][:3]
    # payload: o INSS tem 650 CNPJs em prod — manda amostra + o TOTAL
    assert len(item['documentos']) == 3
    assert item['n_documentos'] == 5


def test_n_processos_ausente_vira_null_nunca_zero(logado):
    """Sem contagem no índice, o campo é `null` = DESCONHECIDO.

    Zero é uma AFIRMAÇÃO ("esta entidade não tem processo"). Enquanto o índice
    não tiver a contagem, afirmar isso é mentira — e a mentira apareceria
    justamente no número que o comercial usa pra priorizar.
    """
    es = MagicMock()
    es.search.return_value = _resposta_es(DOC_INSS, {**DOC_INSS,
                                                    'entidade_id': 'x', 'n_processos': None})
    with patch('dashboard.comercial_views.get_es', return_value=es):
        dados = logado.get(reverse(URL_NAME), {'q': 'inss'}).json()
    assert [i['n_processos'] for i in dados['itens']] == [None, None]
    assert dados['contagem_processos_disponivel'] is False


def test_n_processos_presente_passa_como_numero(logado):
    """Quando o índice tiver a contagem, ela passa — inclusive um 0 contado.

    0 vindo do índice é resultado de contagem (legítimo); 0 inventado por
    ausência é que não pode existir. Os dois casos coexistem neste teste.
    """
    es = MagicMock()
    es.search.return_value = _resposta_es(
        {**DOC_INSS, 'n_processos': 4418191},
        {**DOC_INSS, 'entidade_id': 'nome:zzz', 'n_processos': 0},
    )
    with patch('dashboard.comercial_views.get_es', return_value=es):
        dados = logado.get(reverse(URL_NAME), {'q': 'inss'}).json()
    assert [i['n_processos'] for i in dados['itens']] == [4418191, 0]
    assert dados['contagem_processos_disponivel'] is True


def test_es_fora_vira_503_e_nao_500(logado):
    es = MagicMock()
    es.search.side_effect = RuntimeError('connection refused')
    with patch('dashboard.comercial_views.get_es', return_value=es):
        resp = logado.get(reverse(URL_NAME), {'q': 'inss'})
    assert resp.status_code == 503
    assert 'erro' in resp.json()


def test_cache_curto_por_termo(logado):
    """Digitação repetida não repete a query — e o cache é declarado no payload."""
    es = MagicMock()
    es.search.return_value = _resposta_es(DOC_INSS)
    with patch('dashboard.comercial_views.get_es', return_value=es):
        primeira = logado.get(reverse(URL_NAME), {'q': 'inss'}).json()
        segunda = logado.get(reverse(URL_NAME), {'q': 'INSS'}).json()   # caixa não importa
        assert es.search.call_count == 1
        # termo diferente NÃO é servido do cache do outro
        logado.get(reverse(URL_NAME), {'q': 'uniao'})
        assert es.search.call_count == 2
    assert primeira.get('cache') is None
    assert segunda['cache'] is True
    assert segunda['itens'] == primeira['itens']


@pytest.mark.parametrize('bruto,esperado', [
    ('3', 3), ('999', 20), ('0', 1), ('-5', 1), ('abc', 8), (None, 8),
])
def test_n_e_clampado(logado, bruto, esperado):
    es = MagicMock()
    es.search.return_value = _resposta_es()
    params = {'q': 'inss'}
    if bruto is not None:
        params['n'] = bruto
    with patch('dashboard.comercial_views.get_es', return_value=es):
        logado.get(reverse(URL_NAME), params)
    assert es.search.call_args.kwargs['size'] == esperado


def test_filtro_de_ente_publico(logado):
    es = MagicMock()
    es.search.return_value = _resposta_es()
    with patch('dashboard.comercial_views.get_es', return_value=es):
        logado.get(reverse(URL_NAME), {'q': 'fazenda', 'ente': '1'})
    query = es.search.call_args.kwargs['query']['function_score']['query']
    assert query['bool']['filter'] == [{'term': {'eh_ente_publico': True}}]


def test_redis_fora_nao_derruba_o_autocomplete(logado):
    """Cache é otimização: Redis fora degrada pra consulta direta, não pra erro."""
    es = MagicMock()
    es.search.return_value = _resposta_es(DOC_INSS)
    with patch('dashboard.comercial_views.get_es', return_value=es), \
         patch('dashboard.comercial_views.cache.get', side_effect=RuntimeError('redis down')), \
         patch('dashboard.comercial_views.cache.set', side_effect=RuntimeError('redis down')):
        resp = logado.get(reverse(URL_NAME), {'q': 'inss'})
    assert resp.status_code == 200
    assert len(resp.json()['itens']) == 1


# --------------------------------------------------------------------------- #
# UI — combobox no campo que já existia
# --------------------------------------------------------------------------- #
def test_endpoint_registrado_na_pagina(html):
    """A página resolve a rota pelo `{% url %}` — zero path hardcoded."""
    assert 'entidades: "/dashboard/api/entidades/autocomplete"' in html


def test_aria_de_combobox_completo(html):
    """Sem o pacote ARIA inteiro, o dropdown não existe pro leitor de tela."""
    assert 'role="combobox"' in html
    assert 'aria-autocomplete="list"' in html
    assert 'aria-controls="ac-entidades"' in html
    assert ':aria-expanded="acAberto ? \'true\' : \'false\'"' in html
    assert ':aria-activedescendant="acAtivoId()"' in html
    assert 'id="ac-entidades" role="listbox"' in html
    assert 'role="option"' in html
    assert ':aria-selected="acIdx === i ? \'true\' : \'false\'"' in html
    # o foco NUNCA sai do campo (é o que faz ↑ ↓ funcionarem enquanto se digita)
    assert 'acOptId(i)' in html and "return 'ac-opt-' + i;" in html
    # e a lista aparecendo é anunciada
    assert 'aria-live="polite" x-text="acResumoAcessivel()"' in html


def test_teclado_completo(html):
    """↑ ↓ Enter Esc — e Tab/clique fora fecham."""
    assert '@keydown.arrow-down.prevent="acDesce()"' in html
    assert '@keydown.arrow-up.prevent="acSobe()"' in html
    assert '@keydown.escape.stop="fecharSugestoes()"' in html
    assert '@keydown.tab="fecharSugestoes()"' in html
    assert '@keydown.enter.prevent="enterParte()"' in html
    assert '@click.outside="fecharSugestoes()"' in html
    # ↓ no fim volta ao começo (lista circular) e ↑ no começo vai pro fim
    assert 'this.acIdx = (this.acIdx + 1) % this.acItens.length;' in html
    assert 'this.acIdx = this.acIdx <= 0 ? this.acItens.length - 1 : this.acIdx - 1;' in html


def test_texto_livre_continua_funcionando(html):
    """O autocomplete é ATALHO, não prisão: Enter sem item destacado busca o texto."""
    m = re.search(r'enterParte\(\) \{(.{0,400}?)\n      \},', html, re.S)
    assert m, 'sumiu enterParte'
    corpo = m.group(1)
    assert 'this.escolherEntidade(this.acItens[this.acIdx])' in corpo
    assert 'this.buscarParte();' in corpo
    # o campo segue x-model de texto puro (nada de <select>)
    assert 'x-model="filtros.parte"' in html
    # e o mapa continua recebendo `parte=` (nenhum contrato novo no backend)
    assert 'escolherEntidade(item) {' in html
    assert 'this.filtros.parte = item.nome_canonico;' in html


def test_nao_busca_com_1_ou_2_letras(html):
    """Espelha a guarda do backend — e some com a lista quando encurta o termo."""
    assert 'AC_MIN: 3' in html
    assert 'if (termo.length < this.AC_MIN) {' in html
    assert 'if (termo.length < this.AC_MIN) return;' in html
    # debounce próprio, mais curto que o do mapa (1,14M entidades × 71,18M docs)
    assert 'AC_DEBOUNCE: 250' in html
    assert 'setTimeout(() => this.sugerir(), this.AC_DEBOUNCE)' in html


def test_honestidade_do_item_grafias_e_contagem(html):
    """Cada item DIZ o que entrega: quantas grafias unifica e quantos processos.

    "não contamos ainda" é obrigatório enquanto o índice não tiver
    `n_processos` — a alternativa (0) seria afirmar que a entidade não tem
    processo nenhum.
    """
    assert "return 'processos: não contamos ainda';" in html
    assert "if (n === null || n === undefined)" in html
    assert "return 'unifica ' + fmt(n) + ' grafias';" in html
    # e o item diz quando o agrupamento é heurístico (sem CNPJ que prove)
    assert 'agrupado pelo nome, sem CNPJ no cadastro' in html


def test_escolher_entidade_nao_mente_sobre_o_que_o_mapa_faz(html):
    """Escolher a entidade NÃO vira busca estruturada — o mapa segue por texto.

    Medido no ES de prod em 12/08/2026: o nome canônico sozinho cobre 99,7% a
    100% do que o OR das grafias da entidade devolve (INSS 4.403.363 de
    4.418.191). Não há recall a ganhar — o que sobra é ruído da busca textual,
    e é isso que a linha avisa.
    """
    assert 'Entidade do cadastro:' in html
    assert 'como texto' in html
    assert 'pode entrar processo em que essas palavras aparecem em partes diferentes' in html
    # as ressalvas antigas continuam (elas valem pros dois caminhos)
    assert 'como ele foi escrito na publicação' in html
    assert 'dos dois lados do processo' in html


def test_escolher_entidade_nao_reabre_o_dropdown(html):
    """Bug pego no browser: o dropdown voltava sozinho depois do Enter.

    `escolherEntidade` devolve o foco ao campo, e o `@focus` reabre a lista —
    com a entidade JÁ escolhida no campo, a lista não tem nada a oferecer.
    """
    assert ('if (this.acEscolhida && this.parteNorm() === this.acEscolhida.nome_canonico) return;'
            in html)


def test_token_programatico_sobrevive_a_notificacao_repetida(html):
    """Bug pego no browser: o Alpine notifica o MESMO valor mais de uma vez.

    O token do preenchimento programático não pode ser consumido na primeira
    notificação — com ele apagado, a segunda passada tratava o valor que NÓS
    escrevemos como digitação e reabria o dropdown logo depois do Enter. Ele só
    cai quando chega um valor diferente (= o usuário digitou).
    """
    assert ("if (this._acProgramatico !== null && termo === this._acProgramatico) {\n"
            "          this.fecharSugestoes();\n"
            "          return;\n"
            "        }\n"
            "        this._acProgramatico = null;") in html


def test_dropdown_nao_empurra_o_mapa(html):
    """Lista `absolute` dentro de wrapper `relative`.

    Se ela ocupasse espaço no fluxo, o painel de filtros cresceria a cada tecla
    e o card do mapa desceria — a altura medida (319px) é gate desta tela.
    """
    assert 'relative flex-1 min-w-[16rem]' in html
    assert re.search(r'class="absolute left-0 right-0 z-30[^"]*"', html)


def test_degrada_sem_o_cadastro(html):
    """Cadastro fora do ar não pode bloquear a busca por texto."""
    assert 'Dá para buscar pelo nome mesmo assim' in html
    assert 'Nenhuma entidade com esse nome no cadastro' in html


def test_sem_title_e_sem_fonte_minuscula_no_dropdown(html):
    """Regras da casa: tooltip é `.voy-tip` (nunca `title=`) e piso de 12px."""
    bloco = html[html.find('id="ac-entidades"') - 3000:html.find('id="ac-entidades"') + 3000]
    assert not re.search(r'(?<![a-z])title=', bloco)
    assert 'text-fg-subtle' not in bloco
    assert 'opacity-' not in bloco
    assert not re.search(r'text-\[0\.[0-6]', bloco)
