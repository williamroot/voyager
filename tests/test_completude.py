"""A tela de completude não pode inventar denominador.

CONTEXTO. Toda outra tela deste projeto mede contagem PRÓPRIA — quantos runs,
quantas movimentações. Isso responde "quanto trabalhamos", não "quanto do acervo
temos", e a diferença entre as duas perguntas custou três perdas medidas (a
tabela do CLAUDE.md).

O que estes testes protegem é o CONTRATO DE HONESTIDADE da tela:

  · porta sem gabarito externo mostra "sem total declarado", nunca uma
    porcentagem inventada — abster > chutar;
  · a recuperação mostra as DUAS réguas, porque nenhuma sozinha é honesta (a
    razão itens/página tem falso positivo do downshift de 5xx; a data do run
    subestima). Mostrar uma só seria escolher a que soa melhor;
  · a idade da medição externa é visível — número declarado envelhece e passa a
    mentir em silêncio;
  · a tela NUNCA computa no caminho da requisição (o 502 de 10/08 nasceu disso).

ADENDO DE 01/09/2026 — o defeito do NÚMERO CONGELADO AO LADO DO VIVO. Dois
achados no mesmo dia, a mesma família:

  1. a coluna `recuperável` mostrava 64.895.691 no TJSP (constante de 18/08) ao
     lado de `nunca_refeito = 0` (medido agora). Lido junto, isso só pode
     significar "faltam 64,9 milhões" — e não faltavam, já tinham voltado;
  2. o card do Datajud dividia o nosso `_count` de hoje por um declarado de
     14/08 e publicava **lacuna −1.394.989 · 100,4%**.

E o defeito do ARREDONDAMENTO: `floatformat:1` mostrava **100,0%** para 3.998
dias de 3.999 — escondendo exatamente o único que ainda dava trabalho.
"""
import datetime
from unittest.mock import patch

import pytest
from django.urls import reverse

from dashboard import completude_medicoes as M

URL = 'dashboard:completude'


@pytest.fixture
def logado(client, django_user_model):
    u = django_user_model.objects.create_user('comp@t.local', password='x' * 12)
    client.force_login(u)
    return client


def test_url_resolve():
    assert reverse(URL) == '/dashboard/completude/'


@pytest.mark.django_db
def test_exige_login(client):
    assert client.get(reverse(URL)).status_code in (302, 403)


@pytest.mark.django_db
def test_miss_de_cache_nao_computa_nada(logado):
    """No miss a tela diz 'medindo' — jamais roda a medição no request."""
    with patch('dashboard.completude_views.cache.get', return_value=None), \
         patch('dashboard.completude_warm.warm_completude') as warm:
        r = logado.get(reverse(URL))
    assert r.status_code == 200
    assert r.context['pendente'] is True
    assert not warm.called, 'computou no caminho da requisição — foi assim que o site caiu'


@pytest.mark.django_db
def test_porta_sem_gabarito_nao_inventa_porcentagem(logado):
    """O DJEN não tem total declarado: a completude dele se mede dia a dia."""
    dados = {'portas': {'djen': {'temos': 1_386_220_582}}, 'medido_em': datetime.datetime.now()}
    with patch('dashboard.completude_views.cache.get', return_value=dados):
        r = logado.get(reverse(URL))
    djen = next(p for p in r.context['portas'] if p['slug'] == 'djen')
    assert djen['gabarito'] == 'dia_a_dia'
    assert 'pct' not in djen, 'inventou porcentagem sem denominador da fonte'
    assert 'sem total declarado' in r.content.decode()


@pytest.mark.django_db
def test_porta_nao_subtrai_congelado_de_vivo(logado):
    """O defeito de 01/09: `temos` de hoje menos `declarado` de 14/08.

    Deu **lacuna −1.394.989 · 100,4%** — uma lacuna negativa sem nenhum achado
    por trás. A porta não publica mais `pct`/`lacuna` de gaveta: a única
    diferença que sai na tela é a do CONFRONTO, e ela é entre dois números
    colhidos no mesmo instante.
    """
    dados = {'portas': {'datajud': {'temos': 344_630_543}},
             'medido_em': datetime.datetime.now()}
    with patch('dashboard.completude_views.cache.get', return_value=dados):
        r = logado.get(reverse(URL))
    dj = next(p for p in r.context['portas'] if p['slug'] == 'datajud')
    assert 'pct' not in dj and 'lacuna' not in dj, (
        'voltou a dividir o nosso número vivo por um declarado congelado')
    assert dj['temos']['n'] == 344_630_543
    # o número exato que a tela publicou em 01/09 como "lacuna"
    assert '1.394.989' not in r.content.decode()


@pytest.mark.django_db
def test_confronto_vivo_ganha_do_retrato(logado):
    """Par colhido junto: os dois lados da mesma rodada, com a data da rodada."""
    dados = {'portas': {'datajud': {'temos': 344_630_543}},
             'datajud': {'declarado': 350_430_801, 'invalidos': 5_516_272,
                         'util': 344_914_529, 'nosso': 344_603_487,
                         'falta': 283_987, 'sobra': 27_055, 'tribunais': 59,
                         'sem_fonte': [], 'pct': 99.917,
                         'ate': datetime.datetime.now()},
             'medido_em': datetime.datetime.now()}
    with patch('dashboard.completude_views.cache.get', return_value=dados):
        r = logado.get(reverse(URL))
    c = r.context['confronto']
    assert c['origem'] == 'medido'
    assert c['falta'] == 283_987 and c['sobra'] == 27_055
    h = r.content.decode()
    assert '350.430.801' in h and '5.516.272' in h, 'não mostrou o denominador bruto e o desconto'
    assert 'numeroProcesso' in h, 'não explicou o que foi descontado'


@pytest.mark.django_db
def test_sem_rodada_o_retrato_sai_etiquetado(logado):
    """Retrato histórico pode ir pra tela — desde que DIGA que é retrato."""
    with patch('dashboard.completude_views.cache.get',
               return_value={'portas': {'datajud': {'temos': 344_630_543}}}):
        r = logado.get(reverse(URL))
    c = r.context['confronto']
    assert c['origem'] == 'historico'
    assert c['quando'] == M.CONFRONTO_DATAJUD['medido_em']
    assert 'retrato de' in r.content.decode().lower()


@pytest.mark.django_db
def test_o_percentual_nao_arredonda_pra_cem(logado):
    """99,97% é 99,97%. Um dia sobrando em 3.999 não pode virar 100,0%."""
    dados = {'portas': {},
             'recuperacao': [{'sigla': 'TJRS', 'alvo': 3999, 'flat': 3700,
                              'pos_corte': 3900, 'falta': 299, 'recuperavel': 1,
                              'recuperado': 2, 'dias_refeitos': 3,
                              'nunca_refeito': 1, 'falso_pos': 298,
                              'pct_flat': 92.5, 'pct_corte': 97.5,
                              'pct_honesto': 100.0 * 3998 / 3999}],
             'resumo_recup': {'alvo': 3999, 'nunca_refeito': 1, 'falso_pos': 298,
                              'falta_razao': 299, 'recuperado': 106_251_809,
                              'pct_flat': 92.5, 'pct_corte': 97.5,
                              'pct_honesto': 100.0 * 3998 / 3999},
             'medido_em': datetime.datetime.now()}
    with patch('dashboard.completude_views.cache.get', return_value=dados):
        h = logado.get(reverse(URL)).content.decode()
    assert '99,97' in h, 'arredondou o teto e escondeu o único dia que faltava'
    assert '100,0%' not in h and '100,00%' not in h


@pytest.mark.django_db
def test_recuperavel_sai_com_a_data_da_estimativa_e_ao_lado_do_medido(logado):
    """O defeito de 01/09: constante de 18/08 ao lado de `nunca_refeito = 0`.

    Lida junto, a linha do TJSP dizia "faltam 64,9 milhões". Não faltavam.
    """
    dados = {'portas': {},
             'recuperacao': [{'sigla': 'TJSP', 'alvo': 338, 'flat': 321,
                              'pos_corte': 330, 'falta': 17,
                              'recuperavel': 64_895_691, 'recuperado': 48_877_504,
                              'dias_refeitos': 328, 'nunca_refeito': 0,
                              'falso_pos': 17, 'pct_flat': 95.0,
                              'pct_corte': 97.6, 'pct_honesto': 100.0}],
             # total do cabeçalho DIFERENTE do da linha de propósito: é o que
             # distingue "a coluna existe" de "o número apareceu em algum lugar"
             'resumo_recup': {'alvo': 338, 'nunca_refeito': 0, 'falso_pos': 17,
                              'falta_razao': 17, 'recuperado': 99_999_111,
                              'pct_flat': 95.0, 'pct_corte': 97.6, 'pct_honesto': 100.0},
             'medido_em': datetime.datetime.now()}
    with patch('dashboard.completude_views.cache.get', return_value=dados):
        h = logado.get(reverse(URL)).content.decode()
    assert '48.877.504' in h, 'a LINHA do TJSP não publicou o que já foi recuperado'
    assert '(328d)' in h, 'não disse em quantos dias a recuperação aconteceu'
    assert '18/08/2026' in h, 'a coluna congelada saiu sem a data da estimativa'
    assert 'estimado em' in h


@pytest.mark.django_db
def test_bloco_sem_medicao_sai_pelo_nome(logado):
    """Zerado, nunca. O nome é o que distingue 'deu zero' de 'não mediu'."""
    with patch('dashboard.completude_views.cache.get',
               return_value={'portas': {}, 'medido_em': datetime.datetime.now()}):
        r = logado.get(reverse(URL))
    assert r.context['nao_medidos'], 'medição ausente saiu como zero'
    assert 'Não medido nesta passada' in r.content.decode()


@pytest.mark.django_db
def test_zero_medido_continua_aparecendo(logado):
    """`_num(0)` é dicionário: zero MEDIDO é resultado, não ausência."""
    dados = {'portas': {}, 'medido_em': datetime.datetime.now(),
             'recup_nacional': {'recuperado': 0, 'estimado': 212_308_169,
                                'dias': 0, 'alvo': 0, 'nunca_refeito': 0},
             'vazao': {'serie': [{'dia': datetime.date(2026, 8, 18), 'n': 0}],
                       'piso': 59, 'pico': 0, 'ultimo': 0}}
    with patch('dashboard.completude_views.cache.get', return_value=dados):
        r = logado.get(reverse(URL))
    assert r.context['recup_nacional']['recuperado'] == {'n': 0, 'txt': '0'}
    assert not r.context['nao_medidos'], 'zero medido foi tratado como não medido'


@pytest.mark.django_db
def test_mostra_as_duas_reguas_da_recuperacao(logado):
    """Uma régua só seria escolher a que soa melhor."""
    dados = {
        'portas': {},
        'recuperacao': [{'sigla': 'TJSP', 'alvo': 329, 'flat': 92, 'pos_corte': 76,
                         'falta': 237, 'recuperavel': 64_895_691,
                         'pct_flat': 28.0, 'pct_corte': 23.1}],
        'resumo_recup': {'alvo': 329, 'pct_flat': 28.0, 'pct_corte': 23.1},
        'medido_em': datetime.datetime.now(),
    }
    with patch('dashboard.completude_views.cache.get', return_value=dados):
        h = logado.get(reverse(URL)).content.decode()
    assert '28' in h and '23' in h, 'não mostrou as duas leituras'
    assert 'falso positivo' in h, 'não avisou que a régua da razão tem falso positivo'


@pytest.mark.django_db
def test_medicao_velha_fica_visivel(logado):
    """Número declarado sem idade à vista envelhece e mente em silêncio."""
    with patch.object(M, 'PORTAS', [dict(M.PORTAS[1], medido_em=datetime.date(2020, 1, 1))]), \
         patch('dashboard.completude_views.cache.get', return_value={'portas': {}}):
        r = logado.get(reverse(URL))
    assert r.context['portas'][0]['idade_medicao'] > 2000
    assert 'd</span>' in r.content.decode() or 'idade' in r.content.decode().lower()


@pytest.mark.django_db
def test_sem_diarios_explica_em_vez_de_so_zerar(logado):
    """Tela zerada tem que dizer POR QUE está zerada — senão não se distingue
    'não coletamos' de 'o coletor quebrou'."""
    with patch('dashboard.completude_views.cache.get', return_value={'portas': {}}):
        h = logado.get(reverse(URL)).content.decode()
    assert 'Nenhuma edição catalogada' in h
    assert '2007' in h, 'não disse o que a porta fechada custa'


def test_fase2_bate_com_a_lista_de_recuperavel():
    """Regressão: tribunal na Fase 2 sem recuperável medido viraria linha com
    denominador vazio na tela."""
    faltando = [s for s in M.FASE_2 if s not in M.RECUPERAVEL_POR_TRIBUNAL]
    assert not faltando, f'sem medição de recuperável: {faltando}'

def _linha(sigla, alvo, flat, nunca, rec=0, est=0, fora=None, dias=0):
    return {'sigla': sigla, 'alvo': alvo, 'flat': flat, 'pos_corte': flat,
            'falta': alvo - flat, 'recuperavel': est, 'recuperado': rec,
            'dias_refeitos': dias, 'nunca_refeito': nunca, 'falso_pos': 0,
            'fora_do_alvo': fora,
            'pct_flat': 100.0 * flat / alvo, 'pct_corte': 100.0 * flat / alvo,
            'pct_honesto': 100.0 * (alvo - nunca) / alvo}


@pytest.mark.django_db
def test_fase2_sozinha_dizia_que_tinha_acabado(logado):
    """A Fase 2 fechou em 99,97% — e a Fase 3 está em 16%.

    Publicar só a de cima faz quem lê concluir que a recuperação terminou.
    Terminou **um terço**: 5.157 de 11.173 dias no alvo da casa.
    """
    dados = {'portas': {}, 'medido_em': datetime.datetime.now(),
             'recuperacao': [_linha('TJSP', 338, 321, 0)],
             'resumo_recup': {'alvo': 3999, 'nunca_refeito': 1, 'refeitos': 3998,
                              'falso_pos': 227, 'falta_razao': 228,
                              'recuperado': 106_252_318, 'pct_flat': 94.3,
                              'pct_corte': 95.6, 'pct_honesto': 100.0 * 3998 / 3999},
             'fase3': [_linha('TJMT', 932, 180, 750, est=863_224)],
             'resumo_fase3': {'alvo': 7174, 'nunca_refeito': 6015, 'refeitos': 1159,
                              'falso_pos': 93, 'falta_razao': 6108,
                              'recuperado': 4_417_708, 'pct_flat': 14.9,
                              'pct_corte': 15.3, 'pct_honesto': 100.0 * 1159 / 7174,
                              'fora_dias': 1230, 'fora_nunca': 1152,
                              'alvo_com_fora': 8404, 'nunca_com_fora': 7167,
                              'pct_com_fora': 100.0 * 1237 / 8404},
             'recup_nacional': {'recuperado': 115_359_154, 'estimado': 212_308_169,
                                'dias': 4246, 'alvo': 12403, 'nunca_refeito': 7168,
                                'alvo_da_casa': 11173, 'nunca_da_casa': 6016,
                                'refeitos_da_casa': 5157, 'pct': 54.3,
                                'pct_honesto': 46.16}}
    with patch('dashboard.completude_views.cache.get', return_value=dados):
        h = logado.get(reverse(URL)).content.decode()
    assert 'Fase 3' in h, 'a tela dizia que a recuperação acabou sem mostrar o resto'
    assert '16,16' in h, 'não publicou o percentual honesto da Fase 3'
    assert '5.157' in h and '11.173' in h, 'não publicou o nacional honesto'
    # e a Fase 2 continua dizendo a fração, não só a porcentagem
    assert '3.998/3.999' in h


@pytest.mark.django_db
def test_tjpr_aparece_marcado_e_fora_das_somas(logado):
    """Ele é 43% do que resta na Fase 3 e NÃO é buraco nosso.

    Sumir da tela seria esconder; entrar na soma seria acusar a casa por uma
    decisão comercial. Aparece marcado, e o total traz as duas leituras.
    """
    dados = {'portas': {}, 'medido_em': datetime.datetime.now(),
             'fase3': [_linha('TJPR', 1230, 74, 1152, est=38_807_963,
                              fora='fora do alvo por decisão comercial'),
                       _linha('TJMT', 932, 180, 750, est=863_224)],
             'resumo_fase3': {'alvo': 7174, 'nunca_refeito': 6015, 'refeitos': 1159,
                              'pct_honesto': 16.1556, 'fora_dias': 1230,
                              'fora_nunca': 1152, 'alvo_com_fora': 8404,
                              'nunca_com_fora': 7167, 'pct_com_fora': 14.72},
             'recup_nacional': {'alvo_da_casa': 11173, 'refeitos_da_casa': 5157}}
    with patch('dashboard.completude_views.cache.get', return_value=dados):
        h = logado.get(reverse(URL)).content.decode()
    assert 'TJPR' in h, 'sumiu da tela'
    assert 'fora do alvo' in h, 'entrou como se fosse buraco nosso'
    assert '16,16' in h and '14,7' in h, 'não deu as DUAS leituras do total'
    assert '8.404' in h


@pytest.mark.django_db
def test_vazao_mostra_o_piso_junto_com_a_serie(logado):
    """59 runs/dia é a coleta diária dos 59 tribunais e mais nada.

    Sem o piso à vista, 59 parece produção — e uma vazão que desabou e fica lá
    é o mutirão desligado, com todo run verde.
    """
    serie = [{'dia': datetime.date(2026, 8, d), 'n': n} for d, n in
             ((23, 1641), (27, 80), (28, 59), (29, 59), (30, 59))]
    dados = {'portas': {}, 'medido_em': datetime.datetime.now(),
             'vazao': {'serie': serie, 'piso': 59, 'pico': 1641, 'ultimo': 59}}
    with patch('dashboard.completude_views.cache.get', return_value=dados):
        r = logado.get(reverse(URL))
    v = r.context['vazao']
    assert v['parada_ha'] == 3, 'não contou os dias parados no piso'
    assert [p['no_piso'] for p in v['serie']] == [False, False, True, True, True]
    h = r.content.decode()
    assert '1.641' in h and '59' in h
    assert 'piso' in h.lower(), 'publicou a série sem dizer qual é o piso'
    assert 'Há 3 dias no piso' in h


@pytest.mark.django_db
def test_vazao_nao_medida_sai_pelo_nome(logado):
    with patch('dashboard.completude_views.cache.get',
               return_value={'portas': {}, 'medido_em': datetime.datetime.now()}):
        r = logado.get(reverse(URL))
    assert any('vazão' in n for n in r.context['nao_medidos'])


@pytest.mark.django_db
def test_regua_parcial_nao_vira_confronto(logado):
    """10 tribunais medidos NÃO são o país.

    A rodada mede alguns tribunais por vez; publicar o acumulado no meio do
    caminho poria "a fonte declara 37.193.323" ao lado de "temos 344.630.543".
    Régua meio construída fica de fora — mas o PROGRESSO aparece, senão a tela
    pareceria parada.
    """
    dados = {'portas': {'datajud': {'temos': 344_630_543}},
             'datajud': {'parcial': True, 'medidos': 10, 'esperado': 60,
                         'declarado': 37_193_323, 'invalidos': 0,
                         'util': 37_193_323, 'nosso': 37_193_272,
                         'falta': 51, 'sobra': 0, 'tribunais': 10,
                         'sem_fonte': [], 'pct': 99.9999,
                         'ate': datetime.datetime.now()},
             'medido_em': datetime.datetime.now()}
    with patch('dashboard.completude_views.cache.get', return_value=dados):
        r = logado.get(reverse(URL))
    assert r.context['confronto']['origem'] == 'historico'
    h = r.content.decode()
    assert '37.193.323' not in h, 'publicou uma régua de 10 tribunais como se fosse o país'
    assert '10/60' in h, 'escondeu que a rodada está em curso'


def test_agregar_so_fecha_com_a_regua_inteira():
    """CONTROLE do bloco acima, do lado de quem mede."""
    from dashboard import completude_datajud as DJ
    em = datetime.datetime.now()
    par = {'declarado': 100, 'invalidos': 0, 'nosso': 90, 'em': em, 'erro': None}
    parcial = DJ.agregar({'_rodada': {'esperado': 3}, 'TJSP': dict(par)})
    assert parcial['parcial'] is True and parcial['medidos'] == 1
    cheio = DJ.agregar({'_rodada': {'esperado': 2},
                        'TJSP': dict(par), 'TJRJ': dict(par)})
    assert cheio['parcial'] is False
    assert cheio['declarado'] == 200 and cheio['nosso'] == 180
    assert cheio['falta'] == 20 and cheio['sobra'] == 0


def test_agregar_nao_desconta_invalido_que_nao_mediu():
    """Descontar um número que não se mediu seria inventar completude."""
    from dashboard import completude_datajud as DJ
    em = datetime.datetime.now()
    ag = DJ.agregar({'_rodada': {'esperado': 1},
                     'TJSP': {'declarado': 100, 'invalidos': None, 'nosso': 90,
                              'em': em, 'erro': None}})
    assert ag['invalidos'] == 0, 'descontou o que não mediu'
    assert ag['sem_invalidos'] == ['TJSP'], 'não denunciou pelo nome'
    assert ag['falta'] == 10


def test_agregar_publica_falta_e_sobra_separadas():
    """Somadas, se anulariam: sobra de um cobriria falta de outro."""
    from dashboard import completude_datajud as DJ
    em = datetime.datetime.now()
    ag = DJ.agregar({'_rodada': {'esperado': 2},
                     'A': {'declarado': 100, 'invalidos': 0, 'nosso': 90, 'em': em, 'erro': None},
                     'B': {'declarado': 100, 'invalidos': 0, 'nosso': 110, 'em': em, 'erro': None}})
    assert (ag['falta'], ag['sobra']) == (10, 10)
    assert ag['util'] - ag['nosso'] == 0, 'o líquido esconderia as duas'


def test_agregar_deixa_de_fora_quem_nao_tem_fonte_mas_diz_o_nome():
    from dashboard import completude_datajud as DJ
    em = datetime.datetime.now()
    ag = DJ.agregar({'_rodada': {'esperado': 2},
                     'TJSP': {'declarado': 100, 'invalidos': 0, 'nosso': 90, 'em': em, 'erro': None},
                     'STF': {'declarado': None, 'invalidos': None, 'nosso': None,
                             'em': em, 'erro': 'index_not_found'}})
    assert ag['tribunais'] == 1
    assert ag['sem_fonte'] == ['STF']


def test_precisa_rodada_pergunta_antes_de_gastar_rede():
    """Sem esta pergunta a rodada bateria no CNJ 48× por dia à toa."""
    from dashboard import completude_datajud as DJ
    agora = datetime.datetime.now()
    velho = agora - datetime.timedelta(hours=DJ.IDADE_MAX_H + 1)
    assert DJ.precisa_rodada({}) is True, 'estado vazio tem que medir'
    assert DJ.precisa_rodada(
        {'_rodada': {'esperado': 2}, 'A': {'em': agora}}) is True, 'régua incompleta'
    assert DJ.precisa_rodada(
        {'_rodada': {'esperado': 1}, 'A': {'em': agora}}) is False, 'remediu sem precisar'
    assert DJ.precisa_rodada(
        {'_rodada': {'esperado': 1}, 'A': {'em': velho}}) is True, 'deixou envelhecer'
    # estado de versão anterior: sem `esperado` não dá pra saber se fechou, e
    # presumir que fechou congelaria o país numa régua de 11 tribunais
    assert DJ.precisa_rodada(
        {'_rodada': {'em': agora}, 'A': {'em': agora}}) is True, 'presumiu régua cheia'


def test_agendar_rodada_nao_mede_no_aquecimento():
    """O aquecimento roda a cada 30 min e não pode ficar preso na API do CNJ."""
    from dashboard import completude_datajud as DJ
    with patch.object(DJ, 'precisa_rodada', return_value=True), \
         patch.object(DJ.warm_completude_datajud, 'delay') as delay, \
         patch.object(DJ, 'medir_rodada') as medir, \
         patch.object(DJ.cache, 'get', return_value=None), \
         patch.object(DJ.cache, 'set'):
        assert DJ.agendar_rodada() is True
    assert delay.called, 'não enfileirou'
    assert not medir.called, 'mediu INLINE — é isso que segura o aquecimento'


def test_agendar_rodada_respeita_a_trava():
    from dashboard import completude_datajud as DJ
    with patch.object(DJ.cache, 'get', return_value=1), \
         patch.object(DJ.warm_completude_datajud, 'delay') as delay:
        assert DJ.agendar_rodada() is False
    assert not delay.called, 'empilhou rodada contra a API do CNJ'


def test_agregar_sem_par_nenhum_abstem():
    from dashboard import completude_datajud as DJ
    assert DJ.agregar(None) is None
    assert DJ.agregar({}) is None


@pytest.mark.django_db
def test_porcentagem_em_css_sai_sem_virgula(logado):
    """`LANGUAGE_CODE='pt-br'` renderiza 99.9 como "99,9", e `width: 99,9%`
    é CSS INVÁLIDO — a barra some sem erro nenhum no console."""
    import re
    dados = {'portas': {'datajud': {'temos': 344_630_543}},
             'datajud': {'declarado': 350_430_801, 'invalidos': 5_516_266,
                         'util': 344_914_535, 'nosso': 344_630_543,
                         'falta': 283_992, 'sobra': 0, 'tribunais': 59,
                         'sem_fonte': ['STF'], 'ate': datetime.datetime.now(),
                         'pct': 100.0 * 344_630_543 / 344_914_535},
             'diarios': [{'slug': 'tjsp-dje', 'total': 8, 'resolvidas': 7,
                          'pendentes': 1, 'falhas': 0, 'pct': 87.5,
                          'de': datetime.date(2007, 10, 1),
                          'ate': datetime.date(2025, 3, 13), 'por_status': {}}],
             'medido_em': datetime.datetime.now()}
    with patch('dashboard.completude_views.cache.get', return_value=dados):
        h = logado.get(reverse(URL)).content.decode()
    larguras = re.findall(r'style="width:\s*([^"]*)"', h)
    assert larguras, 'nenhuma barra renderizou — o teste não está olhando nada'
    assert not [w for w in larguras if ',' in w], larguras


# ── o arredondamento que escondia o teto ─────────────────────────────────── #

@pytest.mark.parametrize('parte,total,esperado', [
    (3998, 3999, '99,97'),      # o caso real: 1 dia sobrando em 3.999
    (3999, 3999, '100,0'),      # completo de verdade PODE dizer 100
    (0, 3999, '0,0'),           # zero de verdade PODE dizer 0
    (1, 10_000_000, '0,00001'), # quase-zero não vira zero
])
def test_pct_exato_nunca_arredonda_pro_extremo(parte, total, esperado):
    from dashboard.templatetags.voyager_extras import pct_exato
    assert pct_exato(100.0 * parte / total) == esperado


def test_pct_exato_abstem_do_que_nao_e_numero():
    from dashboard.templatetags.voyager_extras import pct_exato
    assert pct_exato(None) == '—'
    assert pct_exato('') == '—'
    assert pct_exato('cem') == '—'
