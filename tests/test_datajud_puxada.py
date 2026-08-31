"""A puxada nacional do Datajud: telemetria, kill switch, teto e bytes.

O risco desta peça não é dar erro — é rodar 20 horas sem ninguém conseguir ver
o que ela está fazendo, parar no meio e perder o progresso, ou bater um teto e
devolver um `success` que esconde milhões de documentos.

Cada teste abaixo cerca uma dessas quatro formas de falhar em silêncio:

  - a telemetria publica a CADA página, não no fim (run que morre não chega ao fim);
  - o kill switch para no MEIO e a retomada continua do cursor, sem repetir nem pular;
  - teto atingido é ERRO com o número real do que ficou de fora, nunca `return` mudo;
  - a página é dimensionada por BYTES medidos, e um estouro relê o MESMO ponto.
"""
import pytest
from django.core.cache import cache
from django.test import override_settings

from datajud import telemetria
from datajud import varredura as V
from datajud.client import DatajudPaginaGrandeError

from .test_datajud_varredura import FakeDatajud, FakeES, fake_bulk, src

MB = 1024 * 1024

#: cache local: a telemetria e o kill switch vivem no cache, e um teste não
#: pode depender do Redis da máquina nem sujar o do vizinho.
CACHE_LOCAL = override_settings(CACHES={'default': {
    'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
    'LOCATION': 'test-puxada'}})


class DatajudComBytes(FakeDatajud):
    """Como o dublê original, mas declarando o PESO da resposta.

    O Datajud real devolve ~225 B por doc de esqueleto; aqui o peso é
    parametrizável para que o orçamento de bytes possa ser exercitado sem
    fabricar megabytes de JSON.
    """

    def __init__(self, docs, pagina=3, bytes_por_doc=225, estoura_acima_de=None,
                 teto_bytes_visto=None):
        super().__init__(docs, pagina=pagina)
        self.bytes_por_doc = bytes_por_doc
        self.estoura_acima_de = estoura_acima_de
        self.ultimos_bytes = 0
        self.sizes_pedidos = []

    def _casa(self, d, query):
        # o dublê original só conhece `gte`; a janela usa `lt` do outro lado
        if 'range' in query and 'lt' in query['range'].get('@timestamp', {}):
            r = query['range']['@timestamp']
            if d['_ts'] >= r['lt']:
                return False
            if 'gte' in r and d['_ts'] < r['gte']:
                return False
            return True
        return super()._casa(d, query)

    def _post(self, sigla, body, cota=None):
        size = int(body.get('size') or 0)
        if size:
            self.sizes_pedidos.append(size)
        if self.estoura_acima_de and size > self.estoura_acima_de:
            # o servidor mandou uma resposta maior que o teto duro
            raise DatajudPaginaGrandeError(size * self.bytes_por_doc,
                                           self.estoura_acima_de * self.bytes_por_doc,
                                           size)
        d = super()._post(sigla, body, cota=cota)
        n = len((d.get('hits') or {}).get('hits') or [])
        self.ultimos_bytes = n * self.bytes_por_doc
        return d


@pytest.fixture
def sem_es(monkeypatch):
    es = FakeES()
    monkeypatch.setattr(V, 'bulk', fake_bulk)
    monkeypatch.setattr(V, 'ensure_index', lambda *a, **k: None)
    monkeypatch.setattr(V, 'index_name', lambda s: f'voyager-{s}')
    return es


def varredura(docs, es, **kw):
    cliente = kw.pop('client', None) or DatajudComBytes(docs, pagina=kw.get('pagina', 3))
    kw.setdefault('telemetria_ativa', False)
    return V.Varredura('TJMG', client=cliente, es=es, **kw)


# --------------------------------------------------------------------------- #
# 1. kill switch: para no MEIO e retoma do cursor
# --------------------------------------------------------------------------- #

def test_kill_switch_para_no_meio_da_passada(sem_es):
    """Um `stop` que só vale quando o job COMEÇA não é kill switch.

    Antes deste teste, `varredura_pausados` era conferido uma vez, no início de
    `varrer_acervo`: uma varredura de 20 h em curso ignorava o switch até o fim.
    Aqui o interruptor vira depois da 2ª página e a passada tem que sair na 3ª.
    """
    docs = [src(i, 1000 + i) for i in range(30)]        # 10 páginas de 3
    apertado = {'sim': False}
    v = varredura(docs, sem_es, pagina=3, parar=lambda: apertado['sim'])

    original = v._gravar
    def para_depois_de_duas(hits):
        if v.paginas >= 2:
            apertado['sim'] = True
        return original(hits)
    v._gravar = para_depois_de_duas

    r = v.rodar()
    assert r['parou_por'] == 'pausado'
    assert r['paginas'] < 10, 'o kill switch não interrompeu a passada'
    assert r['cursor'] > 0, 'parou sem cursor: a retomada recomeçaria do zero'


def test_retomada_do_cursor_nao_repete_nem_pula(sem_es):
    """O contrato do kill switch é PARAR e RETOMAR — testar só o `stop` deixa
    passar o pior caso, que é retomar errado.

    Prova por soma: a 1ª passada para no meio; a 2ª começa do cursor devolvido.
    Juntas as duas têm que produzir os 30 documentos distintos, nem 29 nem 31.
    """
    docs = [src(i, 1000 + i) for i in range(30)]
    apertado = {'sim': False}
    v1 = varredura(docs, sem_es, pagina=3, parar=lambda: apertado['sim'])
    original = v1._gravar
    def para_depois_de_tres(hits):
        if v1.paginas >= 3:
            apertado['sim'] = True
        return original(hits)
    v1._gravar = para_depois_de_tres
    r1 = v1.rodar()
    assert r1['parou_por'] == 'pausado'
    parciais = dict(sem_es.docs)
    assert len(parciais) < 30, 'a 1ª passada varreu tudo — o teste não prova nada'

    es2 = FakeES()
    v2 = varredura(docs, es2, pagina=3)
    r2 = v2.rodar(cursor=r1['cursor'])
    assert r2['parou_por'] == 'fim'

    juntos = set(parciais) | set(es2.docs)
    assert len(juntos) == 30, f'retomada perdeu documento: {len(juntos)} de 30'
    # a releitura da cauda é de propósito e idempotente: pode haver interseção,
    # mas não pode haver BURACO — é o buraco que este projeto não tolera
    assert not (set(d['numeroProcesso'] for d in docs) -
                set(sem_es.docs[k]['proc_digits'] for k in parciais) -
                set(es2.docs[k]['proc_digits'] for k in es2.docs))


@CACHE_LOCAL
@pytest.mark.django_db
def test_parada_global_desliga_a_frota_inteira():
    """Na hora do aperto ninguém enumera 59 siglas: tem que ter UMA chave."""
    from datajud.jobs import set_varredura_parada, varredura_parada
    cache.clear()
    assert varredura_parada() is False
    assert V.deve_parar('TJSP') is False
    set_varredura_parada(True)
    assert V.deve_parar('TJSP') is True
    assert V.deve_parar('TRT20') is True
    set_varredura_parada(False)
    assert V.deve_parar('TJSP') is False


@CACHE_LOCAL
@pytest.mark.django_db
def test_kill_switch_ilegivel_nao_para_a_varredura(monkeypatch):
    """Redis fora do ar não pode virar um `stop` que ninguém pediu.

    Perder 20 h de puxada porque o painel caiu seria trocar o produto pelo
    painel — o contrário do princípio nº 1.
    """
    def explode():
        raise RuntimeError('redis fora')
    monkeypatch.setattr('datajud.jobs.varredura_parada', explode)
    assert V.deve_parar('TJSP') is False


# --------------------------------------------------------------------------- #
# 2. teto é ERRO com número, nunca corte mudo
# --------------------------------------------------------------------------- #

def test_teto_de_paginas_declara_quantos_ficaram_de_fora(sem_es):
    """`for pagina in range(1, 11)` com outra roupa: parar num teto e devolver
    resumo limpo escondeu 43,6% do TJSP por 17 meses.

    O contrato: quem para por teto mede NA FONTE o que sobrou e registra erro.
    """
    docs = [src(i, 1000 + i) for i in range(30)]
    v = varredura(docs, sem_es, pagina=3)
    r = v.rodar(max_paginas=2)
    assert r['parou_por'] == 'max_paginas'
    assert r['restante_declarado'] is not None, 'teto sem número é corte mudo'
    assert r['restante_declarado'] > 0
    assert 'teto_max_paginas' in r['erros'], 'teto não virou ERRO registrado'


def test_fim_normal_nao_inventa_erro_de_teto(sem_es):
    """Controle negativo: sem teto, nenhum erro de teto — senão o teste acima
    passaria por acidente."""
    docs = [src(i, 1000 + i) for i in range(9)]
    v = varredura(docs, sem_es, pagina=3)
    r = v.rodar()
    assert r['parou_por'] == 'fim'
    assert r['restante_declarado'] is None
    assert not r['erros']


def test_perda_no_milissegundo_vira_erro_registrado(sem_es):
    """`perdidos` já era contado; agora também é ERRO, para aparecer na tela
    junto com os outros e não só num campo que ninguém lê."""
    docs = [src(i, 9000, grau='G1', classe=12078) for i in range(5)]
    v = varredura(docs, sem_es, pagina=2, teto_ms=2)
    r = v.rodar()
    assert r['perdidos'] > 0
    assert 'perdidos_no_ms' in r['erros']


# --------------------------------------------------------------------------- #
# 3. orçamento de BYTES (nunca de páginas nem de itens)
# --------------------------------------------------------------------------- #

@override_settings(DATAJUD_VARREDURA_BYTES_ALVO=100_000)
def test_pagina_e_dimensionada_pelos_bytes_medidos(sem_es):
    """100 KB de orçamento e 100 B/doc ⇒ 1.000 docs por página.

    Sem isto o dimensionamento é por ITENS, que foi exatamente o erro do OOM da
    coleta do DJEN: o código previa 3 KB por publicação e a medição deu 56 KB.
    """
    docs = [src(i, 1000 + i) for i in range(4000)]
    cli = DatajudComBytes(docs, pagina=10_000, bytes_por_doc=100)
    v = varredura(docs, sem_es, pagina=10_000, client=cli)
    v.rodar()
    # a 1ª é a SONDA (pesa antes de comprometer memória); as seguintes já saem
    # do orçamento medido
    assert cli.sizes_pedidos[0] == V.PAGINA_SONDA
    assert cli.sizes_pedidos[1] == 1000, cli.sizes_pedidos


@override_settings(DATAJUD_VARREDURA_BYTES_ALVO=100_000)
def test_doc_mais_gordo_encolhe_a_pagina_na_hora(sem_es):
    """Previsão não é teto. Quando o doc pesa 10× o previsto, a página tem que
    encolher na PRÓXIMA requisição, não daqui a uma hora."""
    docs = [src(i, 1000 + i) for i in range(4000)]
    cli = DatajudComBytes(docs, pagina=10_000, bytes_por_doc=100)
    v = varredura(docs, sem_es, pagina=10_000, client=cli)
    v.rodar(max_paginas=2)
    leve = v._proxima_pagina()

    cli2 = DatajudComBytes(docs, pagina=10_000, bytes_por_doc=1000)
    v2 = varredura(docs, sem_es, pagina=10_000, client=cli2)
    v2.rodar(max_paginas=2)
    pesado = v2._proxima_pagina()
    assert leve == 1000 and pesado == 100, (leve, pesado)
    assert pesado < leve, 'doc 10× mais gordo não encolheu a página'


def test_resposta_acima_do_teto_encolhe_e_rele_o_mesmo_ponto(sem_es):
    """Estouro do teto duro não pode perder documento.

    O servidor recusa qualquer página acima de 200 itens; a varredura tem que
    encolher e reler o MESMO cursor até caber — e no fim ter varrido tudo.
    """
    docs = [src(i, 1000 + i) for i in range(50)]
    cli = DatajudComBytes(docs, pagina=1000, bytes_por_doc=225,
                          estoura_acima_de=200)
    v = varredura(docs, sem_es, pagina=1000, client=cli)
    r = v.rodar()
    assert len(sem_es.docs) == 50, 'perdeu doc ao encolher a página'
    assert r['erros'].get('resposta_grande'), 'encolheu sem registrar o motivo'
    assert max(cli.sizes_pedidos) <= V.PAGINA_SONDA


def test_cliente_sem_bytes_degrada_para_o_teto(sem_es):
    """Um cliente que não informa bytes (dublê antigo, cliente de terceiro) não
    pode travar a página num número inventado: volta ao teto e varre."""
    docs = [src(i, 1000 + i) for i in range(10)]
    v = varredura(docs, sem_es, pagina=3, client=FakeDatajud(docs, pagina=3))
    r = v.rodar()
    assert len(sem_es.docs) == 10
    assert r['parou_por'] == 'fim'


# --------------------------------------------------------------------------- #
# 4. dry-run mede sem escrever
# --------------------------------------------------------------------------- #

def test_dry_run_nao_escreve_no_indice(sem_es):
    """"Medir sem escrever" que escreve é a mesma armadilha do run verde: o
    operador acredita que só mediu."""
    docs = [src(i, 1000 + i) for i in range(10)]
    v = varredura(docs, sem_es, pagina=3, escrever=False)
    r = v.rodar()
    assert sem_es.docs == {}, 'dry-run escreveu no índice'
    # `gravados` é TETO de escrita, não contagem: a paginação relê a cauda de
    # propósito, então 10 docs distintos rendem 14 leituras
    assert r['gravados'] >= 10, 'dry-run não contou o que ENTRARIA'
    assert r['lidos'] == r['gravados']


# --------------------------------------------------------------------------- #
# 5. telemetria
# --------------------------------------------------------------------------- #

@CACHE_LOCAL
def test_telemetria_publica_a_cada_pagina_e_nao_no_fim(sem_es):
    """Run que morre não chega ao fim. Se a telemetria só fosse escrita no
    resumo final, uma puxada que estourasse na hora 19 não teria deixado
    rastro nenhum — o mesmo defeito que o alerta da coleta do DJEN tinha."""
    cache.clear()
    docs = [src(i, 1000 + i) for i in range(30)]
    v = varredura(docs, sem_es, pagina=3)
    v.telemetria_ativa = True
    telemetria.abrir('TJMG', alvo=30)

    visto = {}
    original = v._gravar
    def espia(hits):
        r = original(hits)
        if v.paginas == 3:
            visto.update(telemetria.ler('TJMG'))
        return r
    v._gravar = espia
    v.rodar()

    assert visto.get('lidos', 0) > 0, 'nada publicado no meio da passada'
    assert visto.get('requisicoes', 0) > 0
    assert visto.get('estado') == 'rodando'
    final = telemetria.ler('TJMG')
    assert final['estado'] == 'fim'
    assert len(sem_es.docs) == 30
    assert final['lidos'] >= 30      # `lidos` conta LEITURAS: a cauda é relida


@CACHE_LOCAL
def test_eta_se_abstem_sem_alvo_medido():
    """ETA sem os dois lados é chute — e chute vira base de decisão."""
    cache.clear()
    telemetria.abrir('TJRR', alvo=None)
    telemetria.registrar_pagina(
        'TJRR', requisicoes=1, paginas=1, lidos=10_000, gravados=10_000,
        perdidos=0, esperas=0, bytes_lidos=2 * MB, bytes_por_doc=225.0,
        pagina_atual=10_000, cursor=1_700_000_000_000, decorrido=10.0)
    e = telemetria.ler('TJRR')
    assert e['docs_por_s'] == 1000.0
    assert e['eta_s'] is None, 'inventou ETA sem alvo medido'
    assert e['restante'] is None
    assert e['cursor_iso'].startswith('2023-')


@CACHE_LOCAL
def test_eta_existe_quando_o_alvo_foi_medido_dos_dois_lados():
    cache.clear()
    telemetria.abrir('TJRR', alvo=100_000, declarado=373_503)
    telemetria.registrar_pagina(
        'TJRR', requisicoes=1, paginas=1, lidos=10_000, gravados=10_000,
        perdidos=0, esperas=0, bytes_lidos=2 * MB, bytes_por_doc=225.0,
        pagina_atual=10_000, cursor=1_700_000_000_000, decorrido=10.0)
    e = telemetria.ler('TJRR')
    assert e['restante'] == 90_000
    assert e['eta_s'] == 90


@CACHE_LOCAL
def test_erro_e_contado_por_tipo():
    """`rate-limit` 400× e `Fielddata is disabled` 1× são diagnósticos opostos;
    um contador único esconderia o segundo atrás do primeiro."""
    cache.clear()
    telemetria.abrir('TRT20')
    telemetria.registrar_erro('TRT20', 'cota')
    telemetria.registrar_erro('TRT20', 'cota')
    telemetria.registrar_erro('TRT20', 'api', 'Fielddata is disabled')
    e = telemetria.ler('TRT20')
    assert e['erros'] == {'cota': 2, 'api': 1}
    assert 'Fielddata' in e['ultimo_erro']


@CACHE_LOCAL
def test_telemetria_nao_derruba_a_varredura(sem_es, monkeypatch):
    """Escrever telemetria é acessório; varrer é o produto."""
    cache.clear()
    monkeypatch.setattr(telemetria.cache, 'set',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('redis fora')))
    docs = [src(i, 1000 + i) for i in range(10)]
    v = varredura(docs, sem_es, pagina=3)
    v.telemetria_ativa = True
    r = v.rodar()
    assert len(sem_es.docs) == 10
    assert r['parou_por'] == 'fim'


# --------------------------------------------------------------------------- #
# 6. o incremental que não enxerga o buraco
# --------------------------------------------------------------------------- #

@CACHE_LOCAL
@pytest.mark.django_db
def test_incremental_que_traz_zero_com_buraco_medido_e_erro(sem_es, monkeypatch):
    """`fim` com 0 documentos é auto-confirmatório e por isso não vale nada.

    O cursor termina sempre em "máximo da fonte + 1", então a passada seguinte
    acaba exatamente onde a fonte acaba — e devolve verde mesmo quando a
    medição dos DOIS lados diz que faltam milhões. Medido em 31/08/2026: os 59
    tribunais devolveram 0 documentos em `gte cursor` enquanto o CNJ declarava
    283.987 processos que não temos.

    O contrato: 0 trazidos + alvo medido > 0 ⇒ ERRO registrado, nunca `fim`.
    """
    from tribunals.models import Tribunal
    cache.clear()
    t, _ = Tribunal.objects.get_or_create(
        sigla='TJMG', defaults={'nome': 'TJ Minas', 'sigla_djen': 'TJMG'})
    Tribunal.objects.filter(pk=t.pk).update(datajud_varredura_cursor=99_999)

    vazia = varredura([], sem_es, pagina=3)          # fonte não devolve nada
    vazia.telemetria_ativa = True
    monkeypatch.setattr(V, 'Varredura', lambda *a, **k: vazia)
    monkeypatch.setattr(V, 'medir_alvo', lambda *a, **k: {
        'declarado': 36_698_417, 'nosso': 36_678_104, 'invalidos': 0,
        'alvo': 20_313, 'erro': None})

    r = V.varrer_tribunal('TJMG')

    assert r['gravados'] == 0
    assert r.get('incremental_cego') == 20_313, 'buraco medido não virou alerta'
    estado = telemetria.ler('TJMG')
    assert 'incremental_cego' in (estado.get('erros') or {})
    # o alerta tem que dizer o TAMANHO e a SAÍDA, senão vira ruído que o
    # operador aprende a ignorar
    assert '20,313' in estado['ultimo_erro']
    assert '--do-zero' in estado['ultimo_erro']


@CACHE_LOCAL
@pytest.mark.django_db
def test_incremental_vazio_SEM_buraco_nao_alarma(sem_es, monkeypatch):
    """Controle negativo: fonte em dia e nada trazido é o caso NORMAL do
    incremental. Alarmar aqui treinaria o operador a ignorar o alarme."""
    from tribunals.models import Tribunal
    cache.clear()
    t, _ = Tribunal.objects.get_or_create(
        sigla='TJMG', defaults={'nome': 'TJ Minas', 'sigla_djen': 'TJMG'})
    Tribunal.objects.filter(pk=t.pk).update(datajud_varredura_cursor=99_999)

    vazia = varredura([], sem_es, pagina=3)
    monkeypatch.setattr(V, 'Varredura', lambda *a, **k: vazia)
    monkeypatch.setattr(V, 'medir_alvo', lambda *a, **k: {
        'declarado': 235_759, 'nosso': 235_759, 'invalidos': 0, 'alvo': 0,
        'erro': None})
    r = V.varrer_tribunal('TJMG')
    assert r['gravados'] == 0
    assert 'incremental_cego' not in r


@CACHE_LOCAL
@pytest.mark.django_db
def test_alvo_desconta_o_lixo_que_o_cnj_conta(monkeypatch):
    """O `_count` do CNJ inclui linha sem `numeroProcesso` — 5.337.680 no TJSP.

    Sem o desconto, o alvo do TJSP seria 5,6 M para sempre e o ETA prometeria
    trazer o que não existe. Com ele, sobra o resíduo REAL.
    """
    cache.clear()

    class Cli:
        def _post(self, sigla, body, cota=None):
            q = body.get('query') or {}
            invalidos = 'must_not' in (q.get('bool') or {})
            return {'hits': {'total': {'value': 5_337_680 if invalidos
                                       else 74_686_714}}}

    monkeypatch.setattr(V, 'get_es', lambda: type('E', (), {
        'options': lambda self, **k: type('O', (), {
            'count': lambda self, **kk: {'count': 69_078_849}})()})())
    r = V.medir_alvo('TJSP', client=Cli())
    assert r['declarado'] == 74_686_714
    assert r['invalidos'] == 5_337_680
    assert r['alvo'] == 270_185, 'não descontou o lixo do denominador'


@CACHE_LOCAL
@pytest.mark.django_db
def test_kill_switch_de_producao_e_chamado_do_jeito_certo(sem_es, monkeypatch):
    """A fiação, não o mecanismo — foi por aqui que a puxada morreu em produção.

    `deve_parar` recebe a SIGLA; `Varredura.parar` é chamado SEM argumento, uma
    vez por página. `varrer_tribunal` precisa fechar sobre a sigla. Ligar os
    dois direto compila, passa em todo teste que injeta um `parar` próprio, e
    estoura `TypeError` na primeira página do primeiro tribunal real — depois de
    já ter gasto as requisições da medição do alvo.

    Este teste exercita o caminho de PRODUÇÃO: ninguém injeta `parar`.
    """
    from tribunals.models import Tribunal
    cache.clear()
    t, _ = Tribunal.objects.get_or_create(
        sigla='TJMG', defaults={'nome': 'TJ Minas', 'sigla_djen': 'TJMG'})
    Tribunal.objects.filter(pk=t.pk).update(datajud_varredura_cursor=None)

    docs = [src(i, 1000 + i) for i in range(9)]
    cliente = DatajudComBytes(docs, pagina=3)
    real = V.Varredura                      # guarda ANTES de trocar

    def fabrica(sigla, *a, **k):
        # mesma classe de verdade, só com o dublê no lugar do HTTP: o `parar`
        # que `varrer_tribunal` montou continua sendo o objeto sob teste
        return real(sigla, client=cliente, es=sem_es, pagina=3,
                    telemetria_ativa=False, parar=k.get('parar'))

    monkeypatch.setattr(V, 'Varredura', fabrica)
    monkeypatch.setattr(V, 'medir_alvo', lambda *a, **k: {})

    r = V.varrer_tribunal('TJMG')          # sem `parar=`: como o job real chama
    assert r['parou_por'] == 'fim'
    assert len(sem_es.docs) == 9


# --------------------------------------------------------------------------- #
# 7. janela de `@timestamp` — o que alcança o que o CNJ reescreveu para trás
# --------------------------------------------------------------------------- #

def test_janela_fecha_dos_dois_lados(sem_es):
    """`--desde/--ate` tem que varrer só o que está DENTRO da janela.

    Sem o `lt`, uma "janela" seria só um começo diferente e varreria até o fim
    do tribunal — 69 M no TJSP em vez dos 2,4 M do mês pedido.
    """
    docs = [src(i, 1000 + i) for i in range(30)]
    v = varredura(docs, sem_es, pagina=3)
    r = v.rodar(cursor=1010, filtro={'range': {'@timestamp': {'lt': 1020}}})
    assert r['parou_por'] == 'fim'
    assert len(sem_es.docs) == 10, f'janela [1010, 1020) trouxe {len(sem_es.docs)}'


@CACHE_LOCAL
@pytest.mark.django_db
def test_janela_nao_toca_o_watermark(sem_es, monkeypatch):
    """Uma janela que termina em julho gravaria julho como watermark e apagaria
    agosto do futuro. É a mesma razão pela qual a passada por classe não grava —
    e o `--ate` só protege se virar FILTRO, não só parâmetro do laço."""
    from tribunals.models import Tribunal
    cache.clear()
    t, _ = Tribunal.objects.get_or_create(
        sigla='TJMG', defaults={'nome': 'TJ Minas', 'sigla_djen': 'TJMG'})
    Tribunal.objects.filter(pk=t.pk).update(datajud_varredura_cursor=9_999_999)

    docs = [src(i, 1000 + i) for i in range(30)]
    pronta = varredura(docs, sem_es, pagina=3)
    monkeypatch.setattr(V, 'Varredura', lambda *a, **k: pronta)
    monkeypatch.setattr(V, 'medir_alvo', lambda *a, **k: {})
    V.varrer_tribunal('TJMG', desde=1010,
                      filtro={'range': {'@timestamp': {'lt': 1020}}})
    t.refresh_from_db()
    assert t.datajud_varredura_cursor == 9_999_999, \
        'a janela andou com o watermark para trás'


def test_ate_vira_FILTRO_e_nao_so_parametro_do_laco():
    """A proteção do watermark mora no `filtro`, não no laço.

    `varrer_tribunal` só deixa de gravar o cursor quando `filtro` é verdadeiro.
    Se `--ate` fosse implementado como um simples fim de laço, a janela
    terminaria em julho e gravaria julho como watermark — apagando agosto do
    futuro. Este teste olha a FIAÇÃO do comando, que é onde esse erro cabe.
    """
    from datajud.management.commands.datajud_varredura import Command
    c = Command()
    f = c._filtro({'classe': None, 'ate': '2026-08-01'})
    assert f == {'range': {'@timestamp': {'lt': 1785542400000}}}

    f2 = c._filtro({'classe': 12078, 'ate': '2026-08-01'})
    assert f2['bool']['filter'][0] == {'term': {'classe.codigo': 12078}}
    assert f2['bool']['filter'][1]['range']['@timestamp']['lt'] == 1785542400000

    assert c._filtro({'classe': None, 'ate': None}) is None


def test_ms_aceita_iso_e_epoch():
    from datajud.management.commands.datajud_varredura import _ms
    assert _ms('2026-07-01') == 1782864000000        # 00:00 UTC
    assert _ms(1782864000000) == 1782864000000
    assert _ms('1782864000000') == 1782864000000
    assert _ms(None) is None
    assert _ms('2026-07-01T12:00:00') == 1782864000000 + 12 * 3600 * 1000
