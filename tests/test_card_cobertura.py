"""O card de cobertura não pode mentir com ar de precisão.

CONTEXTO. O princípio nº 1 do Voyager é COMPLETUDE, e até 27/08/2026 nenhuma
tela mostrava o progresso dele: "só tínhamos 13% do acervo nacional" vivia numa
nota de acompanhamento e o número de hoje vivia num terminal.

A armadilha específica deste card é o DENOMINADOR. O `voyager-acervo` tem
342.046.902 documentos para ≈289.277.192 CNJs distintos, porque o Datajud emite
um doc por (tribunal, grau) e 24,3% dos processos têm dois ou mais graus.
Dividir pelos documentos dá 29,2%; dividir pelos CNJs dá 34,5%. **Cinco pontos
de diferença, e a versão errada é a que parece mais rigorosa.**

O que estes testes protegem:
  1. a conta usa CNJ distinto, não documento;
  2. as ressalvas aparecem NA TELA (aproximação HLL, estimativa do planner,
     data do retrato do esqueleto) — não em tooltip, não no código;
  3. sem cache a tela ABSTÉM em vez de calcular no caminho da requisição — foi
     uma medição de rodapé sem teto que derrubou o site (regra nº 7).
"""
import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.urls import reverse

from dashboard import cobertura_nacional as CN

PAYLOAD = {
    'medido_em': '2026-08-27T14:30:00+00:00',
    'acervo_docs': 342_046_902,
    'cnjs_distintos': 289_277_192,
    'docs_por_cnj': 1.18,
    'acervo_varrido_em': '2026-08-14',
    'total_pg': 103_330_128,
    'nosso_indice': 99_864_837,
    'cobertura': 35.72,
    'cobertura_ha_30d': 22.0,
    'ganho_pp': 13.72,
    'novos_janela': 39_728_864,
    'faltam': 185_947_064,
    'serie': [{'d': '2026-08-26', 'acum': 103_077_779, 'novos': 770_294, 'cob': 35.63},
              {'d': '2026-08-27', 'acum': 103_330_128, 'novos': 252_349, 'cob': 35.72}],
    'tribunais': [{'t': 'TJSP', 'datajud': 68_813_731, 'nosso': 16_215_462, 'cob': 23.6},
                  {'t': 'TJMG', 'datajud': 36_574_365, 'nosso': 8_266_912, 'cob': 22.6}],
    'dias_serie': 45,
    'busca': {
        'no_indice': 99_864_837, 'com_parte': 18_036_990, 'com_advogado': 12_634_368,
        'partes_djen_linhas': 2_605_464, 'ms_busca_parte': 17.4,
        'pct_indexado': 96.6, 'pct_com_parte': 18.1, 'pct_com_advogado': 12.7,
        'funil': [{'k': 'no banco', 'v': 103_330_128}, {'k': 'na busca', 'v': 99_864_837},
                  {'k': 'com parte', 'v': 18_036_990}, {'k': 'com advogado', 'v': 12_634_368}],
    },
}


@pytest.fixture
def logado(db, client):
    u = get_user_model().objects.create_user('quem', password='x')
    client.force_login(u)
    return client


def _html(client):
    r = client.get(reverse('dashboard:acompanhamento'))
    assert r.status_code == 200
    return r.content.decode()


@pytest.mark.django_db
def test_card_mostra_a_cobertura_e_o_ganho(logado):
    cache.set(CN.CHAVE, PAYLOAD, 60)
    h = _html(logado)
    assert '35.72%' in h or '35,72%' in h
    assert '+13.72 pp' in h or '+13,72 pp' in h
    assert 'Cobertura do acervo nacional' in h


@pytest.mark.django_db
def test_o_denominador_e_CNJ_distinto_e_a_tela_DIZ_isso(logado):
    """A diferença entre 29,2% e 34,5% mora aqui."""
    cache.set(CN.CHAVE, PAYLOAD, 60)
    h = _html(logado)
    assert '289.277.192' in h, 'não mostrou o denominador de CNJs distintos'
    assert '342.046.902' in h, 'não mostrou os documentos, então ninguém entende a diferença'
    assert 'distintos' in h and 'grau' in h, (
        'a tela não explica por que um número não é o outro — e aí alguém '
        'refaz a conta com o denominador errado')


@pytest.mark.django_db
def test_as_ressalvas_estao_na_tela(logado):
    cache.set(CN.CHAVE, PAYLOAD, 60)
    h = _html(logado)
    assert 'HyperLogLog' in h, 'aproximação apresentada como exata'
    assert 'planner' in h, 'estimativa do reltuples apresentada como contagem'
    assert '2026-08-14' in h and 'retrato' in h, (
        'não avisou que o esqueleto é um retrato — alguém vai usar para afirmar '
        'que um processo NÃO existe')


@pytest.mark.django_db
def test_sem_medicao_a_tela_ABSTEM(logado):
    """Nunca calcular no caminho da requisição: a cardinalidade leva ~36 s."""
    cache.delete(CN.CHAVE)
    h = _html(logado)
    assert 'Ainda não medido' in h
    assert 'Abster' in h
    assert '35.72%' not in h


@pytest.mark.django_db
def test_ler_nunca_calcula(monkeypatch):
    """`ler()` é só cache. Se algum dia chamar `calcular()`, a página trava."""
    cache.delete(CN.CHAVE)
    def explode():
        raise AssertionError('ler() chamou calcular() — 36 s no caminho da requisição')
    monkeypatch.setattr(CN, 'calcular', explode)
    assert CN.ler() is None


def test_a_serie_e_reconstruida_de_tras_pra_frente():
    """O ponto de HOJE é exato por construção; o erro fica no passado distante.

    Ninguém guardou snapshot diário, então a série vem do total de hoje menos o
    que entrou em cada dia. A propriedade importa: o número que o usuário vai
    citar (o de hoje) é o exato.
    """
    fonte = open('dashboard/cobertura_nacional.py').read()
    i = fonte.find('def _serie_ingestao')
    corpo = fonte[i:i + 1800]
    assert 'acum -= por_dia' in corpo, 'série deixou de ser regressiva'
    assert 'total_hoje' in corpo


@pytest.mark.django_db
def test_o_funil_mostra_onde_o_dado_some(logado):
    """Cobertura do acervo responde "temos?". O funil responde "acho pela pessoa?"."""
    cache.set(CN.CHAVE, PAYLOAD, 60)
    h = _html(logado)
    assert 'Da coleta até achar pela pessoa' in h
    assert '18.1%' in h or '18,1%' in h, 'não mostrou a cobertura real de partes'
    assert '17.4 ms' in h or '17,4 ms' in h, 'não mostrou a latência medida'
    assert '2.605.464' in h, 'não mostrou as partes tiradas do texto, que são de graça'


@pytest.mark.django_db
def test_a_tela_denuncia_a_mentira_do_exists(logado):
    """O `exists` dizia 100% onde havia 18,1%. Quem lê o card precisa saber."""
    cache.set(CN.CHAVE, PAYLOAD, 60)
    h = _html(logado)
    assert 'exists' in h
    assert 'string vazia' in h, (
        'sem essa frase, o próximo a medir usa `exists` e reporta 100% de novo')


def test_o_modulo_mede_parte_por_CONTEUDO_e_nao_por_exists():
    fonte = open('dashboard/cobertura_nacional.py').read()
    i = fonte.find('def _busca_e_partes')
    corpo = fonte[i:i + 3200]
    assert "partes:/.+/" in corpo, 'voltou a medir por presença'
    assert "'exists'" not in corpo.split('conta(')[1][:400], 'usou exists para contar parte'


@pytest.mark.django_db
def test_o_script_dos_graficos_ESPERA_o_dom(logado):
    """Os gráficos ficaram em caixa vazia por 1 dia — e sem erro no console.

    `{% block content %}` está na LINHA 806 do `base.html`; `setupChart` só é
    definido na 827. Script inline do card roda ANTES de a função existir, e a
    primeira versão fazia `return` calado nesse caso: números apareciam,
    gráficos não, console limpo. Corte mudo escrito justamente no arquivo em que
    a gente caça corte mudo.
    """
    cache.set(CN.CHAVE, PAYLOAD, 60)
    h = _html(logado)
    assert "addEventListener('DOMContentLoaded'" in h, (
        'o script voltou a rodar antes do base.html definir setupChart')
    assert 'console.error' in h, (
        'o guard voltou a ser mudo — gráfico que não renderiza tem que gritar')
    i = h.find("typeof setupChart !== 'function'")
    assert i > 0
    trecho = h[i:i + 400]
    assert 'console.error' in trecho, 'o caminho do setupChart ausente ficou silencioso'


def test_nenhum_guard_do_card_faz_return_calado():
    """Todo `return` de guarda precisa de um `console.error` antes."""
    import re
    fonte = open('dashboard/templates/dashboard/acompanhamento.html').read()
    i = fonte.find("addEventListener('DOMContentLoaded'")
    assert i > 0
    corpo = fonte[i:fonte.find('</script>', i)]
    for m in re.finditer(r'\n    return;', corpo):
        antes = corpo[max(0, m.start() - 320):m.start()]
        assert 'console.error' in antes, (
            'guard sem grito: o card volta a falhar em silêncio')


def test_nenhum_comentario_JS_escreve_tag_do_django():
    """Comentário de JS NÃO protege tag do Django — ele parseia mesmo assim.

    Escrever a tag de bloco literal dentro de um `//` quebrou o template inteiro
    com "'block' tag with name 'content' appears more than once". O arquivo já
    avisava do parente disto (cerquilha atravessando linha vaza literal na tela);
    esta é a variante que faltava.
    """
    import re
    fonte = open('dashboard/templates/dashboard/acompanhamento.html').read()
    for m in re.finditer(r'^\s*//.*$', fonte, re.M):
        assert '{%' not in m.group(0), (
            f'tag do Django dentro de comentário JS: {m.group(0)[:80]!r}')
