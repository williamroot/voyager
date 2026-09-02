"""O incidente do e-SAJ tem tabela própria — e é gravado sem inventar chave.

POR QUE ESTE ARQUIVO EXISTE
---------------------------
Sonda ao vivo de 01-02/09/2026 (100 processos do TJSP, sementes 20260901 e
20260902): **201 de 210 incidentes (95,7%) são `Precatório`/`Requisição de
Pequeno Valor` e não têm CNJ nenhum**. Sem número não há entrada pelo DJEN nem
pelo Datajud — a página do incidente é a única porta, e é lá que estão o
beneficiário (`Reqte`), o ente que deve (`Ent. Devedora`) e o valor
requisitado dele.

`tribunals_process` é único por `(tribunal, numero_cnj)`. Encaixar o
precatório ali exigiria **fabricar um CNJ que o tribunal não deu** — o
"abster > chutar" ao contrário. Daí `tribunals_incidente`, com a chave que
EXISTE no mundo: o `processo.codigo` do próprio e-SAJ.

O que estes testes travam:

  · a ficha vira linha COMPLETA, e o que a fonte não publica fica vazio/NULL
    (`valor` nunca vira zero — zero é uma afirmação sobre o crédito);
  · re-coletar ATUALIZA em vez de duplicar (idempotência pela chave natural),
    e não reescreve `coletado_em` — a data da descoberta é um fato;
  · ficha sem `codigo_esaj` é DESCARTADA e CONTADA, nunca gravada com chave
    vazia (duas coisas diferentes virariam uma na unique);
  · evento sem a chave `incidentes` **não apaga** o que já foi colhido — o
    payload não distingue "não tem" de "não seguimos neste evento", e apagar
    na dúvida faria de todo enriquecimento comum um apagador silencioso;
  · o `Ent. Devedora` entra no **polo passivo** com `fonte='esaj_incidente'` —
    a mesma convenção que o #118 usou para o ente devedor do diário, porque
    a tela "Quem deve" filtra por passivo e a coluna `fonte` existe para dizer
    de onde a linha veio.
"""
from datetime import date
from decimal import Decimal

import pytest

from enrichers import drainer
from enrichers.esaj import FONTE_INCIDENTE
from tribunals.models import Incidente, Process, ProcessoParte, Tribunal

pytestmark = pytest.mark.django_db


@pytest.fixture
def tjsp():
    t, _ = Tribunal.objects.get_or_create(
        sigla='TJSP', defaults={'nome': 'TJSP', 'ativo': True})
    return t


@pytest.fixture
def proc(tjsp):
    return Process.objects.create(
        tribunal=tjsp, numero_cnj='0018347-36.2022.8.26.0576')


#: Ficha como `parsear_incidente` a devolve (campos conferidos contra a página
#: real `tests/fixtures/tjsp/esaj_incidente_precatorio.html`).
FICHA = {
    'codigo_esaj': 'G0000J7130001',
    'tipo': 'PRECATORIO',
    'classe': 'Precatório',
    'sequencial': '01',
    'cnj_principal': '0018347-36.2022.8.26.0576',
    'cnj_proprio': '',
    'situacao': 'Extinto',
    'assunto': 'Indenização por Dano Moral',
    'foro': 'Foro de São José do Rio Preto',
    'vara': 'Anexo do Juizado Especial da Fazenda Publica',
    'controle': '2021/002588',
    'recebido_em': '18/09/2023 às 10:46',
    'valor': '',
    'requerentes': [{'nome': 'Alysson Augusto Faria Maciel', 'papel': 'REQTE'}],
    'ent_devedoras': [{'nome': 'MUNICÍPIO DE SÃO JOSÉ DO RIO PRETO',
                       'papel': 'ENT. DEVEDORA'}],
}


# ------------------------- 1. a ficha vira linha ---------------------------

def test_ficha_vira_linha_completa(proc):
    r = drainer.gravar_incidentes({proc.pk: [FICHA]})
    assert r == {'gravados': 1, 'sem_codigo': 0}

    i = Incidente.objects.get(processo=proc)
    assert i.codigo_esaj == 'G0000J7130001'
    assert i.tipo == Incidente.TIPO_PRECATORIO
    assert i.sequencial == '01'
    assert i.cnj_principal == '0018347-36.2022.8.26.0576'
    assert i.cnj_proprio == '', 'precatório não tem CNJ próprio — e isso é uma afirmação'
    assert i.situacao == 'Extinto'
    assert i.controle == '2021/002588'
    assert i.recebido_em == date(2023, 9, 18)
    assert i.fonte == Incidente.FONTE_ESAJ
    assert [p['nome'] for p in i.requerentes] == ['Alysson Augusto Faria Maciel']
    assert [p['nome'] for p in i.ent_devedoras] == ['MUNICÍPIO DE SÃO JOSÉ DO RIO PRETO']


def test_valor_abstem_e_nunca_vira_zero(proc):
    """A fonte publicou o valor em 1 das 5 páginas da sonda. Ausente é NULL:
    zero seria uma afirmação sobre quanto o beneficiário tem a receber."""
    drainer.gravar_incidentes({proc.pk: [
        dict(FICHA, codigo_esaj='SEM_VALOR', valor=''),
        dict(FICHA, codigo_esaj='COM_VALOR', valor='18.113,27'),
    ]})
    assert Incidente.objects.get(codigo_esaj='SEM_VALOR').valor is None
    assert Incidente.objects.get(codigo_esaj='COM_VALOR').valor == Decimal('18113.27')


def test_data_ilegivel_abstem(proc):
    drainer.gravar_incidentes({proc.pk: [dict(FICHA, recebido_em='sem data')]})
    assert Incidente.objects.get(processo=proc).recebido_em is None


# --------------------- 2. chave natural e idempotência ---------------------

def test_recoleta_atualiza_e_nao_duplica(proc):
    drainer.gravar_incidentes({proc.pk: [FICHA]})
    primeiro = Incidente.objects.get(processo=proc)

    drainer.gravar_incidentes({proc.pk: [dict(FICHA, situacao='Pago',
                                              valor='18.113,27')]})
    assert Incidente.objects.filter(processo=proc).count() == 1, \
        'a chave natural do e-SAJ é o que torna a re-coleta idempotente'
    de_novo = Incidente.objects.get(processo=proc)
    assert de_novo.pk == primeiro.pk
    assert de_novo.situacao == 'Pago'
    assert de_novo.valor == Decimal('18113.27')
    assert de_novo.coletado_em == primeiro.coletado_em, \
        'coletado_em é QUANDO descobrimos — re-coletar não reescreve isso'


def test_ficha_sem_codigo_e_descartada_e_contada(proc, caplog):
    """Chave vazia gravada faria a SEGUNDA ficha sem código colidir com a
    primeira na unique: dois precatórios de beneficiários diferentes virariam
    um. Descarte silencioso seria pior ainda — por isso ERRO com o número."""
    from unittest.mock import patch
    fichas = [dict(FICHA, codigo_esaj=''), dict(FICHA, codigo_esaj=None),
              dict(FICHA)]
    with patch.object(drainer.logger, 'error') as err:
        r = drainer.gravar_incidentes({proc.pk: fichas})
    assert r == {'gravados': 1, 'sem_codigo': 2}
    assert Incidente.objects.filter(processo=proc).count() == 1
    assert err.call_count == 1


def test_mesma_pagina_listada_duas_vezes_nao_estoura(proc):
    r = drainer.gravar_incidentes({proc.pk: [FICHA, dict(FICHA)]})
    assert r['gravados'] == 1


# ------------------- 3. o caminho real: evento do enricher -----------------

def _evento(proc, **extra):
    ev = {
        'v': 1, 'status': 'ok', 'process_id': proc.pk, 'tribunal': 'TJSP',
        'numero_cnj': proc.numero_cnj, 'scraped_at': '2026-09-02T12:00:00+00:00',
        'dados': {'classe': 'Cumprimento Provisório de Sentença'},
        'partes': {
            'ativo': [{'nome': 'Alysson Augusto Faria Maciel', 'papel': 'REQTE',
                       'tipo': 'pf', 'fonte': FONTE_INCIDENTE}],
            'passivo': [{'nome': 'MUNICÍPIO DE SÃO JOSÉ DO RIO PRETO',
                         'papel': 'ENT. DEVEDORA', 'tipo': 'pj',
                         'fonte': FONTE_INCIDENTE}],
            'outros': [],
        },
    }
    ev.update(extra)
    return ev


def test_apply_event_grava_incidente_e_carimba_a_parte(proc):
    drainer.apply_event(_evento(proc, incidentes=[FICHA]))

    assert Incidente.objects.filter(processo=proc).count() == 1
    devedora = ProcessoParte.objects.get(
        processo=proc, parte__nome='MUNICÍPIO DE SÃO JOSÉ DO RIO PRETO')
    assert devedora.polo == ProcessoParte.POLO_PASSIVO, \
        'a tela "Quem deve" filtra por passivo — em outros o ente fica invisível'
    assert devedora.fonte == FONTE_INCIDENTE, \
        'sem carimbo, o ente do requisitório é indistinguível do réu da ação'


def test_apply_batch_grava_incidente_e_carimba_a_parte(proc):
    aplicados, _ = drainer.apply_batch([_evento(proc, incidentes=[FICHA])])
    assert aplicados == 1
    assert Incidente.objects.filter(processo=proc).count() == 1
    devedora = ProcessoParte.objects.get(
        processo=proc, parte__nome='MUNICÍPIO DE SÃO JOSÉ DO RIO PRETO')
    assert devedora.polo == ProcessoParte.POLO_PASSIVO
    assert devedora.fonte == FONTE_INCIDENTE


def test_parte_do_cadastro_do_pai_nao_ganha_carimbo(proc):
    """Controle negativo: quem veio do cadastro do processo continua com
    `fonte IS NULL`, que é o que `sem_parte_de_terceiro` (#118) lê."""
    ev = _evento(proc)
    ev['partes']['ativo'][0].pop('fonte')
    drainer.apply_event(ev)
    ativo = ProcessoParte.objects.get(processo=proc, polo=ProcessoParte.POLO_ATIVO)
    assert ativo.fonte is None


def test_evento_sem_incidentes_nao_apaga_os_ja_colhidos(proc):
    """O payload não distingue "este processo não tem incidente" de "este
    evento veio do caminho em massa com o seguimento desligado". Apagar na
    dúvida faria de todo enriquecimento comum um apagador silencioso do que a
    passada anterior colheu."""
    drainer.gravar_incidentes({proc.pk: [FICHA]})
    proc.refresh_from_db()
    Process.objects.filter(pk=proc.pk).update(enriquecido_em=None)

    drainer.apply_event(_evento(proc))          # sem a chave `incidentes`
    assert Incidente.objects.filter(processo=proc).count() == 1


# ------------------- 4. quem segue incidente, e quem não -------------------

def test_so_o_tjsp_segue_em_massa(settings):
    from enrichers.jobs import seguir_incidentes_de, set_incidentes_desligados
    set_incidentes_desligados(set())
    settings.ESAJ_SEGUIR_INCIDENTES = True
    settings.ESAJ_INCIDENTES_TRIBUNAIS = ['TJSP']

    assert seguir_incidentes_de('TJSP') is True
    assert seguir_incidentes_de('tjsp') is True, 'sigla vem em caixa qualquer'
    # Controle: outro e-SAJ, que tem a MESMA capacidade técnica, fica de fora
    # até alguém medir o retorno dele. Ligar tudo é decisão de orçamento.
    assert seguir_incidentes_de('TJAL') is False
    assert seguir_incidentes_de('TRF1') is False


def test_kill_switch_desliga_sem_deploy(settings):
    from enrichers.jobs import seguir_incidentes_de, set_incidentes_desligados
    settings.ESAJ_SEGUIR_INCIDENTES = True
    settings.ESAJ_INCIDENTES_TRIBUNAIS = ['TJSP']
    try:
        set_incidentes_desligados({'TJSP'})
        assert seguir_incidentes_de('TJSP') is False, \
            'o pool é COMPARTILHADO — estrangular não pode depender de deploy'
    finally:
        set_incidentes_desligados(set())
    assert seguir_incidentes_de('TJSP') is True


def test_master_switch_desliga_todos(settings):
    from enrichers.jobs import seguir_incidentes_de, set_incidentes_desligados
    set_incidentes_desligados(set())
    settings.ESAJ_SEGUIR_INCIDENTES = False
    settings.ESAJ_INCIDENTES_TRIBUNAIS = ['TJSP']
    assert seguir_incidentes_de('TJSP') is False


def test_valor_seco_do_esaj_e_lido_e_texto_solto_nao(proc):
    """O `#valorAcaoProcesso` do incidente vem SEM cifrão (`18.113,27`) — o
    `parse_valor_brl` compartilhado exige `$` e devolvia None para 100% dos
    valores do requisitório. A regex é ancorada: número solto no meio de uma
    frase NÃO vira valor (valor errado no crédito é pior que valor ausente)."""
    assert drainer._valor_do_incidente('18.113,27') == Decimal('18113.27')
    assert drainer._valor_do_incidente('R$ 18.113,27') == Decimal('18113.27')
    assert drainer._valor_do_incidente('1.234.567,89') == Decimal('1234567.89')
    assert drainer._valor_do_incidente('') is None
    assert drainer._valor_do_incidente(None) is None
    assert drainer._valor_do_incidente('processo 18.113,27 de algo') is None
    assert drainer._valor_do_incidente('18113,27x') is None


# ---------- 5. o recorte que paga a requisição onde ela rende ----------

def test_segue_so_onde_ha_sinal_de_precatorio(settings, tjsp):
    """A fila do refill NÃO é o estrato do crédito.

    Medido em produção 25 min depois de ligar (02/09/2026): 58,5% do estrato
    `tem_sinal_precatorio` tem incidente contra **5,3%** do que o refill serve
    de fato (12 de 227 `ok` seguidos) — e a vazão do TJSP caiu de ~74 para
    ~10-15 jobs/min, porque cada job passou a gastar 1+N requisições e um
    processo de 45 incidentes ocupou um worker por 240 s.
    """
    from enrichers.jobs import seguir_incidentes_no, set_incidentes_desligados
    set_incidentes_desligados(set())
    settings.ESAJ_SEGUIR_INCIDENTES = True
    settings.ESAJ_INCIDENTES_TRIBUNAIS = ['TJSP']
    settings.ESAJ_INCIDENTES_SO_COM_SINAL = True

    com = Process.objects.create(tribunal=tjsp, numero_cnj='1111111-11.2022.8.26.0001',
                                 tem_sinal_precatorio=True)
    sem = Process.objects.create(tribunal=tjsp, numero_cnj='2222222-22.2022.8.26.0001',
                                 tem_sinal_precatorio=False)
    # tri-state: NULL = ainda não varremos. Custo certo, retorno desconhecido.
    nulo = Process.objects.create(tribunal=tjsp, numero_cnj='3333333-33.2022.8.26.0001',
                                  tem_sinal_precatorio=None)
    assert seguir_incidentes_no(com) is True
    assert seguir_incidentes_no(sem) is False
    assert seguir_incidentes_no(nulo) is False

    # E o recorte é reversível por setting, sem virar regra escondida.
    settings.ESAJ_INCIDENTES_SO_COM_SINAL = False
    assert seguir_incidentes_no(sem) is True


def test_recorte_nao_salva_tribunal_desligado(settings, tjsp):
    """Controle: sinal de precatório NÃO liga tribunal que não está na lista."""
    from enrichers.jobs import seguir_incidentes_no, set_incidentes_desligados
    set_incidentes_desligados(set())
    settings.ESAJ_SEGUIR_INCIDENTES = True
    settings.ESAJ_INCIDENTES_TRIBUNAIS = ['TJAL']
    p = Process.objects.create(tribunal=tjsp, numero_cnj='4444444-44.2022.8.26.0001',
                               tem_sinal_precatorio=True)
    assert seguir_incidentes_no(p) is False
