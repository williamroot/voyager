"""A tela de Estoque não pode inventar saldo nem somar cliente em silêncio.

CONTEXTO. Esta tela põe lado a lado duas contagens que parecem a mesma coisa e
não são: o estoque (`Process.classificacao`, o rótulo de HOJE) e o consumo
(`LeadConsumption`, o registro HISTÓRICO de que um cliente puxou o processo).

Três armadilhas foram medidas em 01/09/2026, e cada uma tem teste aqui:

1. `LeadConsumption` **não tem unique constraint** — re-consumo cria registro
   novo. 1.224.278 registros para 811.360 processos distintos: 51% de inflação
   para quem contar `count(*)`.

2. Os dois clientes **não se somam**. `juriscope` tem 405.740 processos e
   **todos** também estão no `falcon`: a interseção é 405.740 e o exclusivo do
   juriscope é 0. Somar dá 1.217.100 e conta 405.740 duas vezes.

3. O consumo (811.360) é **14,7×** o estoque de `PRECATORIO` (55.285).
   `estoque − consumido` dá −756.075 e não significa dívida nenhuma: são
   conjuntos que não se subtraem. A partição medida (`ambos`, `so_estoque`,
   `so_consumo`) é o que substitui a subtração.

E o campo de CONTROLE, que é a regra da casa: um rótulo de classificação fora
do catálogo do model sumiria de todas as trilhas em silêncio; um JOIN que
perde linha daria um cruzamento menor e igualmente verde. Os dois derrubam o
bloco, com o nome, em vez de publicar número torto.
"""
import pytest
from django.core.cache import cache

from dashboard import estoque as E
from tribunals.models import ApiClient, LeadConsumption, Process, Tribunal

P = Process


@pytest.fixture(autouse=True)
def cache_limpo():
    """O cache NÃO é isolado entre testes; a transação do banco é.

    Sem isto, `test_aquecer_...` deixa `estoque:v1:<trilha>` gravado e o
    próximo arquivo de teste lê um payload que ele não escreveu — falha que
    aparece longe da causa e some quando se roda o arquivo sozinho.
    """
    chaves = [f'{E.CHAVE}:{t}' for t in E.TRILHAS] + [E.CHAVE]
    cache.delete_many(chaves)
    yield
    cache.delete_many(chaves)


@pytest.fixture
def acervo(db):
    """Um acervo minúsculo que reproduz as três armadilhas de propósito.

    Estoque:
      TJSP  2 PRECATORIO · 2 PRE_PRECATORIO · 1 DIREITO_CREDITORIO · 1 NAO_LEAD
      TRF1  1 PRE_PRECATORIO · 1 NAO_LEAD · 1 sem classificação

    Consumo: 5 processos distintos em 8 registros, com re-consumo e com o
    juriscope inteiramente contido no falcon.
    """
    # `get_or_create`: a migration 0002 já semeia TRF1..TRF6 e TJSP.
    tjsp, _ = Tribunal.objects.get_or_create(
        sigla='TJSP', defaults={'nome': 'TJSP', 'sigla_djen': 'TJSP'})
    trf1, _ = Tribunal.objects.get_or_create(
        sigla='TRF1', defaults={'nome': 'TRF1', 'sigla_djen': 'TRF1'})

    def proc(tribunal, n, classificacao):
        return P.objects.create(tribunal=tribunal,
                                numero_cnj=f'000000{n}-00.2024.8.26.0000',
                                classificacao=classificacao)

    p = {
        'prec_a': proc(tjsp, 1, P.CLASSIF_PRECATORIO),
        'prec_b': proc(tjsp, 2, P.CLASSIF_PRECATORIO),
        'pre_a': proc(tjsp, 3, P.CLASSIF_PRE_PRECATORIO),
        'pre_b': proc(tjsp, 4, P.CLASSIF_PRE_PRECATORIO),
        'dc': proc(tjsp, 5, P.CLASSIF_DIREITO_CREDITORIO),
        'nao': proc(tjsp, 6, P.CLASSIF_NAO_LEAD),
        'pre_trf1': proc(trf1, 7, P.CLASSIF_PRE_PRECATORIO),
        'nao_trf1': proc(trf1, 8, P.CLASSIF_NAO_LEAD),
        'sem': proc(trf1, 9, None),
    }

    falcon = ApiClient.objects.create(nome='falcon', api_key='k-falcon')
    juriscope = ApiClient.objects.create(nome='juriscope', api_key='k-juriscope')

    def consumo(processo, cliente, resultado):
        LeadConsumption.objects.create(processo=processo, cliente=cliente,
                                       resultado=resultado)

    # falcon: 5 processos distintos, 6 registros (prec_a re-consumido)
    consumo(p['prec_a'], falcon, LeadConsumption.RESULTADO_VALIDADO)
    consumo(p['prec_a'], falcon, LeadConsumption.RESULTADO_PENDENTE)   # re-consumo
    consumo(p['prec_b'], falcon, LeadConsumption.RESULTADO_VALIDADO)
    consumo(p['pre_a'], falcon, LeadConsumption.RESULTADO_PENDENTE)
    consumo(p['dc'], falcon, LeadConsumption.RESULTADO_SEM_EXPEDICAO)
    # o NAO_LEAD consumido é a fatia que faz a subtração dar negativo
    consumo(p['nao'], falcon, LeadConsumption.RESULTADO_VALIDADO)
    # juriscope: 2 processos, ambos já do falcon — contenção total
    consumo(p['prec_a'], juriscope, LeadConsumption.RESULTADO_VALIDADO)
    consumo(p['dc'], juriscope, LeadConsumption.RESULTADO_VALIDADO)

    return p


# --------------------------------------------------------------------------
# armadilha 1 — registro não é processo
# --------------------------------------------------------------------------

@pytest.mark.django_db
def test_registro_e_processo_distinto_sao_numeros_DIFERENTES(acervo):
    """8 registros para 5 processos. Quem contar `count(*)` erra 60%."""
    pay = E.calcular()['precatorio']
    assert pay['total_consumos'] == 8
    assert pay['total_consumido_distinto'] == 5
    assert pay['total_consumos'] != pay['total_consumido_distinto']


@pytest.mark.django_db
def test_resultado_e_por_REGISTRO_e_o_payload_DIZ_isso(acervo):
    """A soma dos resultados fecha com os REGISTROS, não com os processos."""
    pay = E.calcular()['precatorio']
    assert pay['consumo_resultado_unidade'] == 'registros'
    assert sum(pay['consumo_por_resultado'].values()) == pay['total_consumos']
    assert sum(pay['consumo_por_resultado'].values()) != pay['total_consumido_distinto']


# --------------------------------------------------------------------------
# armadilha 2 — os dois clientes não se somam
# --------------------------------------------------------------------------

@pytest.mark.django_db
def test_clientes_aparecem_SEPARADOS_e_a_soma_nao_e_a_uniao(acervo):
    """falcon 5 + juriscope 2 = 7, mas a união distinta é 5.

    Se o código somasse os clientes para responder "quantos processos foram
    consumidos", diria 7 — 40% a mais. A sobreposição é publicada com número.
    """
    bloco = E.calcular()['precatorio']['consumo_clientes']
    por_nome = {i['cliente']: i for i in bloco['itens']}
    assert por_nome['falcon']['processos'] == 5
    assert por_nome['juriscope']['processos'] == 2
    assert bloco['soma_dos_clientes'] == 7
    assert bloco['uniao_distinta'] == 5
    assert bloco['sobreposicao'] == 2
    assert 'SOMA' in bloco['nota'] and 'UNIÃO' in bloco['nota']


@pytest.mark.django_db
def test_o_total_consumido_e_a_UNIAO_nunca_a_soma_dos_clientes(acervo):
    pay = E.calcular()['precatorio']
    assert pay['total_consumido_distinto'] == pay['consumo_clientes']['uniao_distinta']
    assert pay['total_consumido_distinto'] != pay['consumo_clientes']['soma_dos_clientes']


# --------------------------------------------------------------------------
# armadilha 3 — a subtração não é saldo
# --------------------------------------------------------------------------

@pytest.mark.django_db
def test_cruzamento_particiona_em_tres_fatias_que_fecham(acervo):
    """`ambos + so_estoque + so_consumo` cobre a união dos dois conjuntos.

    Trilha precatório = PRECATORIO(2) + PRE_PRECATORIO(3) = 5 no estoque.
    Consumidos nessa trilha: prec_a, prec_b, pre_a = 3.
    Consumidos fora dela: dc (DIREITO_CREDITORIO) e nao (NAO_LEAD) = 2.
    """
    c = E.calcular()['precatorio']['cruzamento']
    assert c['ambos'] == 3
    assert c['so_estoque'] == 2
    assert c['so_consumo'] == 2
    assert c['ambos'] + c['so_consumo'] == 5          # todo o consumo distinto
    assert c['ambos'] + c['so_estoque'] == 5          # todo o estoque da trilha


@pytest.mark.django_db
def test_o_payload_NAO_publica_saldo_nem_subtracao(acervo):
    """Nenhuma chave de saldo, e a nota diz por quê.

    O perigo aqui não é o número errado: é o número plausível. Um `saldo:
    max(estoque - consumido, 0)` seria verde, redondo e sem sentido.
    """
    pay = E.calcular()['precatorio']
    proibidas = {'saldo', 'restante', 'diferenca', 'estoque_menos_consumido'}
    assert not (proibidas & set(pay)), 'apareceu chave de saldo no payload'
    assert not (proibidas & set(pay['cruzamento']))
    assert 'não é saldo' in pay['cruzamento']['nota']


@pytest.mark.django_db
def test_consumido_fora_do_estoque_aparece_com_a_classificacao_de_HOJE(acervo):
    """A pergunta que a subtração não responde: o que os consumidos SÃO hoje."""
    pay = E.calcular()['precatorio']
    atual = pay['consumo_por_classificacao_atual']
    assert atual[P.CLASSIF_PRECATORIO] == 2
    assert atual[P.CLASSIF_PRE_PRECATORIO] == 1
    assert atual[P.CLASSIF_DIREITO_CREDITORIO] == 1
    assert atual[P.CLASSIF_NAO_LEAD] == 1


# --------------------------------------------------------------------------
# trilhas
# --------------------------------------------------------------------------

@pytest.mark.django_db
def test_a_trilha_precatorio_soma_N1_com_N2_e_publica_os_dois_separados(acervo):
    pay = E.calcular()['precatorio']
    assert pay['rotulos'] == ['PRECATORIO', 'PRE_PRECATORIO']
    assert pay['estoque_por_rotulo'] == {'PRECATORIO': 2, 'PRE_PRECATORIO': 3}
    assert pay['total_estoque'] == 5


@pytest.mark.django_db
def test_a_trilha_direito_creditorio_e_so_o_N3(acervo):
    pay = E.calcular()['direito_creditorio']
    assert pay['rotulos'] == ['DIREITO_CREDITORIO']
    assert pay['total_estoque'] == 1
    assert pay['cruzamento']['ambos'] == 1
    assert pay['cruzamento']['so_consumo'] == 4


@pytest.mark.django_db
def test_por_tribunal_traz_os_dois_lados_e_nao_esconde_quem_so_tem_consumo(acervo):
    linhas = {l['t']: l for l in E.calcular()['precatorio']['por_tribunal']}
    assert linhas['TJSP']['estoque'] == 4          # 2 PRECATORIO + 2 PRE
    assert linhas['TJSP']['consumido'] == 5
    assert linhas['TJSP']['consumos'] == 8         # registros, com re-consumo
    assert linhas['TJSP']['por_cliente'] == {'falcon': 5, 'juriscope': 2}
    assert linhas['TJSP']['resultado']['validado'] == 5   # REGISTROS, não processos
    assert linhas['TRF1']['estoque'] == 1
    assert linhas['TRF1']['consumido'] == 0


# --------------------------------------------------------------------------
# campos de controle
# --------------------------------------------------------------------------

@pytest.mark.django_db
def test_rotulo_fora_do_catalogo_DERRUBA_o_bloco_com_o_motivo(acervo):
    """Casing legado já custou 982 linhas nesta casa (`'VALIDADO'`, 05/2026).

    Um rótulo desconhecido não apareceria em trilha nenhuma. O controle exige
    que 100% do que está classificado use um rótulo do model; sem ele, o
    processo abaixo sumiria calado e o total continuaria verde.
    """
    P.objects.filter(numero_cnj__startswith='0000001').update(classificacao='precatorio')
    pay = E.calcular()['precatorio']
    assert 'total_estoque' not in pay
    assert 'cruzamento' not in pay
    assert any('controle do catálogo' in m for m in pay['nao_medidos']), pay['nao_medidos']
    assert any('precatorio' in m for m in pay['nao_medidos'])
    # o bloco de consumo, que não depende da régua torta, continua publicado
    assert pay['total_consumido_distinto'] == 5


@pytest.mark.django_db
def test_join_que_perde_linha_DERRUBA_o_cruzamento(acervo, monkeypatch):
    """O controle compara duas medições independentes do mesmo número.

    A varredura conta o consumo DEPOIS de juntar com `tribunals_process`;
    `_consumo()` conta antes, sem join. Aqui a segunda é forçada a divergir —
    é o que aconteceria com FK órfã ou filtro escondido no join.
    """
    real = E._consumo

    def mentiroso():
        d = real()
        d['total_consumido_distinto'] += 7
        return d

    monkeypatch.setattr(E, '_consumo', mentiroso)
    pay = E.calcular()['precatorio']
    assert 'cruzamento' not in pay
    assert 'por_tribunal' not in pay
    assert any('controle do join' in m for m in pay['nao_medidos']), pay['nao_medidos']


@pytest.mark.django_db
def test_bloco_que_falhou_sai_da_lista_COM_o_nome(acervo, monkeypatch):
    """Meia régua é pior que régua nenhuma — mas régua parcial anunciada é ok."""
    def explode():
        raise RuntimeError('banco fora')

    monkeypatch.setattr(E, '_consumo', explode)
    pay = E.calcular()['precatorio']
    assert 'total_consumido_distinto' not in pay
    assert any('banco fora' in m for m in pay['nao_medidos'])


# --------------------------------------------------------------------------
# custo — regra nº 7
# --------------------------------------------------------------------------

@pytest.mark.django_db
def test_ler_SO_le_cache_e_nunca_calcula(acervo, monkeypatch):
    """Uma medição de rodapé sem teto já derrubou o site (regra nº 7)."""
    def nao_deveria():
        raise AssertionError('ler() calculou no caminho da requisição')

    monkeypatch.setattr(E, 'calcular', nao_deveria)
    monkeypatch.setattr(E, '_varredura', nao_deveria)
    cache.delete(f'{E.CHAVE}:precatorio')
    assert E.ler('precatorio') is None
    assert E.ler('trilha_que_nao_existe') is None


@pytest.mark.django_db
def test_aquecer_grava_UMA_chave_por_trilha(acervo):
    for trilha in E.TRILHAS:
        cache.delete(f'{E.CHAVE}:{trilha}')
    E.aquecer()
    for trilha in E.TRILHAS:
        p = E.ler(trilha)
        assert p and p['trilha'] == trilha
    # uma varredura só serve as duas: os totais de consumo são idênticos
    assert (E.ler('precatorio')['total_consumido_distinto'] ==
            E.ler('direito_creditorio')['total_consumido_distinto'])


@pytest.mark.django_db
def test_toda_consulta_carrega_teto_de_espera():
    """`statement_timeout` em toda query — o banco está sob carga."""
    fonte = open(E.__file__, encoding='utf-8').read()
    assert fonte.count('SET LOCAL statement_timeout') == 2
    assert 'TIMEOUT_VARREDURA' in fonte and 'TIMEOUT_CONSUMO' in fonte
