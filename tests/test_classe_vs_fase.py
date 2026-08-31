"""Classe (o que o CNJ CADASTRA) × Fase (o que o TRIBUNAL PUBLICA) — #105.

A MEDIÇÃO QUE MOTIVOU (31/08/2026, amostra uniforme por pk, semente 20260831)
----------------------------------------------------------------------------
40.000 pks sorteados, 39.101 existiam, 861 com `classe_codigo = '12078'`
(2,20% — bate com os 2.337.739 do banco sobre 104,1 M linhas). Cruzando por
CNJ contra o `voyager-acervo`:

    concorda (o CNJ também diz 12078) ..... 608
    discorda (o CNJ diz outra classe) ..... 222   = 26,7% dos conferíveis
    o CNJ não tem o CNJ .................... 31   =  3,6%

Veredito, por canais INDEPENDENTES do campo que gerou o rótulo (usar o
`codigo_classe` da movimentação como prova seria circular):

    o TEXTO da publicação diz verbatim "CUMPRIMENTO DE SENTENÇA CONTRA
    A FAZENDA PÚBLICA (12078) Nº <o CNJ do processo>" ......... 207 (93,2%)
    partes do PJe: EXEQUENTE × ente público no polo passivo .... 10  (4,5%)
    movimento do Datajud de fase de execução ...................  2  (0,9%)
    SEM evidência (abstenção declarada) ........................  3  (1,4%)

⇒ 98,6% das discordâncias são COLISÃO DE CAMPO. Controle negativo (400
processos que NÃO rotulamos 12078): o canal verbatim dispara em 6 (1,5%).

O QUE ESTES TESTES TRAVAM
-------------------------
 1. `escolher_classe` respeita o grau de ORIGEM e ABSTÉM quando a fonte não
    resolve — 29,3% dos CNJs têm 2+ classes distintas no acervo;
 2. a porta do Datajud escreve `classe_cnj_*` MESMO com `classe_codigo` já
    preenchido (era o "só preenche lacuna" que congelava o cadastro);
 3. a porta do Datajud NÃO escreve `fase_*` (senão a colisão volta com outro
    nome);
 4. a fase só SOBE: recoleta de dia antigo não rebaixa fase mais nova;
 5. `backfill_fase` ignora movimento do Datajud — ele carrega a classe
    cadastral copiada, não uma publicação;
 6. o doc do ES leva os cinco campos novos, e o model exigido pelo builder
    inclui os dois códigos (guarda de worker com código velho).
"""
from datetime import UTC, datetime
from io import StringIO
from types import SimpleNamespace

import pytest
from django.core.management import call_command

# ---------------------------------------------------------------- 1. regra --

def _docs(*trios):
    return list(trios)


def test_um_unico_codigo_nao_precisa_de_grau():
    from datajud.management.commands.backfill_classe_cnj import escolher_classe
    cod, nome, motivo = escolher_classe(_docs(('G1', '12078', 'Cumprimento'),
                                              ('G2', '12078', 'Cumprimento')))
    assert (cod, motivo) == ('12078', '')


def test_multi_classe_vale_o_grau_de_origem():
    """O caso REAL, do índice, que a medição de 30/08 leu como "rótulo errado":

        5005545-67.2020.4.03.6103
          G1   7    Procedimento Comum Cível
          JE   436  Procedimento do Juizado Especial Cível
          TR   460  Recurso Inominado Cível

    A origem é o Juizado (`JE`, e a Turma Recursal PROVA o juizado), então a
    classe cadastral é 436 — não a do primeiro documento que a página devolveu.
    """
    from datajud.management.commands.backfill_classe_cnj import escolher_classe
    cod, nome, motivo = escolher_classe(_docs(
        ('G1', '7', 'Procedimento Comum Cível'),
        ('JE', '436', 'Procedimento do Juizado Especial Cível'),
        ('TR', '460', 'Recurso Inominado Cível')))
    assert cod == '436', 'pegou a classe do grau errado'
    assert nome == 'Procedimento do Juizado Especial Cível'
    assert motivo == ''


def test_abstem_quando_a_fonte_se_contradiz():
    """`G1 + JE` sem `TR`/`TRU` é a fonte se contradizendo sobre a MESMA vara
    adjunta (0,64% do acervo, medido em 25/08/2026). Abster > chutar."""
    from datajud.management.commands.backfill_classe_cnj import escolher_classe
    cod, _, motivo = escolher_classe(_docs(
        ('G1', '7', 'Procedimento Comum Cível'),
        ('JE', '436', 'Procedimento do Juizado Especial Cível')))
    assert cod == '', 'escolheu no chute onde a fonte não decide'
    assert 'G1 x JE' in motivo


def test_abstem_quando_o_mesmo_grau_traz_duas_classes():
    from datajud.management.commands.backfill_classe_cnj import escolher_classe
    cod, _, motivo = escolher_classe(_docs(
        ('G1', '7', 'Procedimento Comum Cível'),
        ('G1', '198', 'Apelação Cível'),
        ('G2', '198', 'Apelação Cível')))
    assert cod == ''
    assert 'divergentes' in motivo


# ------------------------------------------------- 2 e 3. porta do Datajud --

def _proc_ns(**kw):
    base = dict(numero_cnj='0000001-11.2020.8.26.0100', grau='',
                classe_codigo='', classe_nome='',
                classe_cnj_codigo='', classe_cnj_nome='',
                fase_codigo='', fase_nome='', fase_em=None,
                assunto_codigo='', orgao_julgador_codigo='',
                orgao_julgador_nome='', data_autuacao=None, valor_causa=None)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def _colunas_existem(monkeypatch):
    from datajud import ingestion
    monkeypatch.setattr(ingestion, '_COLUNAS_CONFERIDAS',
                        {'grau': True, 'classe_cnj_codigo': True})


def test_datajud_escreve_classe_cnj_mesmo_com_classe_codigo_preenchida(_colunas_existem):
    """O `classe_codigo` já vem preenchido pela publicação em 26,7% dos casos
    conferíveis. Se `classe_cnj_*` herdasse o "só preenche lacuna" dele, o
    cadastro do CNJ nunca seria gravado justamente nos processos onde os dois
    DIVERGEM — ou seja, exatamente nos que motivaram a coluna."""
    from datajud.ingestion import _meta_updates_from_source
    upd = _meta_updates_from_source(
        _proc_ns(classe_codigo='12078', classe_nome='Cumprimento…'),
        {'classe': {'codigo': 436, 'nome': 'Procedimento do Juizado Especial Cível'}})
    assert upd['classe_cnj_codigo'] == '436'
    assert upd['classe_cnj_nome'] == 'Procedimento do Juizado Especial Cível'
    assert 'classe_codigo' not in upd, 'sobrescreveu o campo de compatibilidade'


def test_datajud_atualiza_classe_cnj_quando_o_cadastro_muda(_colunas_existem):
    """O próprio Datajud publica `Retificação de Classe Processual` (172 em 209
    processos TRF3 amostrados). Congelar a primeira leitura seria guardar um
    fato que a fonte já desmentiu."""
    from datajud.ingestion import _meta_updates_from_source
    upd = _meta_updates_from_source(
        _proc_ns(classe_cnj_codigo='436', classe_cnj_nome='Juizado'),
        {'classe': {'codigo': 12078, 'nome': 'Cumprimento contra a Fazenda'}})
    assert upd['classe_cnj_codigo'] == '12078'


def test_datajud_nao_escreve_fase(_colunas_existem):
    """Movimento/classe do Datajud é CADASTRO. Se ele escrevesse `fase_*`, a
    colisão voltaria com outro nome de campo."""
    from datajud.ingestion import _meta_updates_from_source
    upd = _meta_updates_from_source(
        _proc_ns(), {'classe': {'codigo': 436, 'nome': 'Juizado'}})
    assert not any(k.startswith('fase_') for k in upd), upd


# ------------------------------------------------------------- 4. só sobe ----

@pytest.mark.django_db(transaction=True)
def test_fase_so_sobe_recoleta_antiga_nao_rebaixa():
    """O backfill de histórico roda o tempo todo nesta casa. Sem a guarda de
    recência, uma recoleta de 2023 sobrescreveria a fase de 2026 — que é
    exatamente o dado que o produto vende."""
    from djen.ingestion import _flush_resumo
    from tribunals.models import Movimentacao, Process, Tribunal

    trib, _ = Tribunal.objects.get_or_create(
        sigla='TRF3', defaults={'nome': 'TRF3', 'sigla_djen': 'TRF3'})
    p = Process.objects.create(tribunal=trib, numero_cnj='0000001-11.2024.4.03.6100')
    novo = datetime(2026, 5, 2, tzinfo=UTC)
    velho = datetime(2023, 1, 5, tzinfo=UTC)
    Movimentacao.objects.create(
        processo=p, tribunal=trib, external_id='a', data_disponibilizacao=novo,
        codigo_classe='12078', nome_classe='Cumprimento contra a Fazenda', meio='D')

    _flush_resumo(trib, [p.numero_cnj], None, None,
                  {p.numero_cnj: ('12078', 'Cumprimento contra a Fazenda', novo)})
    p.refresh_from_db()
    assert (p.fase_codigo, p.fase_em) == ('12078', novo)

    _flush_resumo(trib, [p.numero_cnj], None, None,
                  {p.numero_cnj: ('436', 'Procedimento do Juizado', velho)})
    p.refresh_from_db()
    assert p.fase_codigo == '12078', 'a recoleta antiga rebaixou a fase'
    assert p.fase_em == novo


@pytest.mark.django_db(transaction=True)
def test_fase_sobe_com_publicacao_mais_nova_e_toca_a_campainha():
    from djen.ingestion import _flush_resumo
    from tribunals.models import Movimentacao, Process, Tribunal

    trib, _ = Tribunal.objects.get_or_create(
        sigla='TRF3', defaults={'nome': 'TRF3', 'sigla_djen': 'TRF3'})
    p = Process.objects.create(tribunal=trib, numero_cnj='0000002-11.2024.4.03.6100',
                               fase_codigo='436', fase_nome='Juizado',
                               fase_em=datetime(2024, 1, 1, tzinfo=UTC))
    antes = Process.objects.values_list('atualizado_em', flat=True).get(pk=p.pk)
    dt = datetime(2026, 6, 1, tzinfo=UTC)
    Movimentacao.objects.create(
        processo=p, tribunal=trib, external_id='b', data_disponibilizacao=dt,
        codigo_classe='12078', nome_classe='Cumprimento', meio='D')

    _flush_resumo(trib, [p.numero_cnj], None, None,
                  {p.numero_cnj: ('12078', 'Cumprimento', dt)})
    p.refresh_from_db()
    assert (p.fase_codigo, p.fase_nome, p.fase_em) == ('12078', 'Cumprimento', dt)
    assert p.atualizado_em > antes, 'campainha não tocou — a busca fica velha'


# -------------------------------------------------------- 5. backfill_fase --

@pytest.mark.django_db(transaction=True)
def test_backfill_fase_ignora_movimento_do_datajud():
    """`datajud/parser.py` copia a classe CADASTRAL do processo em CADA
    movimento. Se o backfill lesse esses, `fase_*` viraria `classe_cnj_*` com
    outro nome — a mesma colisão, um campo adiante."""
    from tribunals.models import Movimentacao, Process, Tribunal

    trib, _ = Tribunal.objects.get_or_create(
        sigla='TRF3', defaults={'nome': 'TRF3', 'sigla_djen': 'TRF3'})
    p = Process.objects.create(tribunal=trib, numero_cnj='0000003-11.2024.4.03.6100')
    Movimentacao.objects.create(
        processo=p, tribunal=trib, external_id='pub', meio='D',
        data_disponibilizacao=datetime(2026, 2, 1, tzinfo=UTC),
        codigo_classe='12078', nome_classe='Cumprimento contra a Fazenda')
    # o movimento do Datajud é MAIS NOVO e carrega a classe cadastral
    Movimentacao.objects.create(
        processo=p, tribunal=trib, external_id='datajud:x', meio='datajud',
        data_disponibilizacao=datetime(2026, 7, 1, tzinfo=UTC),
        codigo_classe='436', nome_classe='Procedimento do Juizado Especial Cível')

    call_command('backfill_fase', de=p.pk - 1, ate=p.pk, sem_checkpoint=True,
                 sleep=0, stdout=StringIO())
    p.refresh_from_db()
    assert p.fase_codigo == '12078', (
        'o backfill leu a classe cadastral do movimento Datajud como se fosse '
        'publicação')


@pytest.mark.django_db(transaction=True)
def test_backfill_fase_sem_reparo_nao_escreve():
    from tribunals.models import Movimentacao, Process, Tribunal

    trib, _ = Tribunal.objects.get_or_create(
        sigla='TRF3', defaults={'nome': 'TRF3', 'sigla_djen': 'TRF3'})
    p = Process.objects.create(tribunal=trib, numero_cnj='0000004-11.2024.4.03.6100')
    Movimentacao.objects.create(
        processo=p, tribunal=trib, external_id='pub', meio='D',
        data_disponibilizacao=datetime(2026, 2, 1, tzinfo=UTC),
        codigo_classe='12078', nome_classe='Cumprimento')
    antes = Process.objects.values('fase_codigo', 'atualizado_em').get(pk=p.pk)

    call_command('backfill_fase', de=p.pk - 1, ate=p.pk, sem_checkpoint=True,
                 sem_reparo=True, sleep=0, stdout=StringIO())
    assert Process.objects.values('fase_codigo', 'atualizado_em').get(pk=p.pk) == antes


def test_backfill_fase_toca_a_campainha_no_mesmo_update():
    """SQL cru não roda o `auto_now`, e `sync_processos_atualizados` é keyset
    por `atualizado_em`: sem a campainha o campo fica certo no banco e ausente
    na busca (foi assim que 2,6 M de `ProcessoParte` ficaram invisíveis)."""
    fonte = open('tribunals/management/commands/backfill_fase.py').read()
    i = fonte.find('UPDATE tribunals_process p SET fase_codigo')
    assert i > 0, 'o UPDATE da fase sumiu — teste desatualizado'
    assert 'atualizado_em = now()' in fonte[i:i + 300]


def test_backfill_classe_cnj_toca_a_campainha_no_mesmo_update():
    fonte = open('datajud/management/commands/backfill_classe_cnj.py').read()
    i = fonte.find('UPDATE tribunals_process p SET classe_cnj_codigo')
    assert i > 0, 'o UPDATE da classe do CNJ sumiu — teste desatualizado'
    assert 'atualizado_em = now()' in fonte[i:i + 300]


# ------------------------------------------------------------- 6. índice ----

@pytest.mark.django_db(transaction=True)
def test_doc_do_es_leva_os_cinco_campos():
    from search.documents import processo_to_doc
    from tribunals.models import Process, Tribunal

    trib, _ = Tribunal.objects.get_or_create(
        sigla='TRF3', defaults={'nome': 'TRF3', 'sigla_djen': 'TRF3'})
    p = Process.objects.create(
        tribunal=trib, numero_cnj='0000005-11.2024.4.03.6100',
        classe_codigo='12078', classe_nome='Cumprimento',
        classe_cnj_codigo='436', classe_cnj_nome='Juizado',
        fase_codigo='12078', fase_nome='Cumprimento',
        fase_em=datetime(2026, 6, 1, tzinfo=UTC))
    doc = processo_to_doc(p)
    assert doc['classe_cnj_codigo'] == '436'
    assert doc['classe_cnj_nome'] == 'Juizado'
    assert doc['fase_codigo'] == '12078'
    assert doc['fase_nome'] == 'Cumprimento'
    assert doc['fase_em'].startswith('2026-06-01')
    # o campo de compatibilidade continua onde estava — quem lê hoje não quebra
    assert doc['codigo_classe'] == '12078'


def test_mapping_do_indice_de_processos_cobre_os_campos_novos():
    from search.mappings import MOV_MAPPING, PROC_MAPPING
    props = PROC_MAPPING['mappings']['properties']
    for campo in ('classe_cnj_codigo', 'classe_cnj_nome', 'fase_codigo',
                  'fase_nome', 'fase_em'):
        assert campo in props, f'{campo} no builder e fora do mapping'
    assert props['fase_em']['type'] == 'date'
    # e NÃO no índice de movimentações: publicação não tem "fase do processo"
    assert 'fase_codigo' not in MOV_MAPPING['mappings']['properties']


def test_guarda_de_modelo_velho_exige_os_campos_novos():
    """`exigir_modelo_em_dia` existe porque um worker com `models.py` velho
    grava valor de enchimento em TODO doc que passa (foi assim que `grau` ficou
    em 1,03% do índice contra 78,57% do banco)."""
    from search.documents import CAMPOS_EXIGIDOS
    assert 'classe_cnj_codigo' in CAMPOS_EXIGIDOS['Process']
    assert 'fase_codigo' in CAMPOS_EXIGIDOS['Process']


# ---------------------------------------------------- 7. porta do enricher --

def test_drainer_escreve_fase_a_partir_da_classe_do_pje():
    """A classe do detalhe do PJe/e-SAJ é a classe ATUAL no sistema do tribunal
    — é fase, não cadastro. Foi este canal que provou 10 das 12 discordâncias
    que o texto do diário não alcançava."""
    fonte = open('enrichers/drainer.py').read()
    i = fonte.find("processo.fase_codigo = dados_norm['classe_codigo']")
    assert i > 0, 'o drainer parou de gravar a fase'
    trecho = fonte[max(0, i - 600):i + 400]
    assert 'scraped_at' in trecho, (
        'a fase tem que ser datada pela LEITURA da fonte; com `now()` um evento '
        'antigo drenado tarde rebaixaria uma fase mais nova')
    assert 'processo.fase_em is None or fase_dt > processo.fase_em' in trecho
