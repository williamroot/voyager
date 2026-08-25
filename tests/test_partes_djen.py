"""Promoção dos destinatários da DJEN a `Parte`/`ProcessoParte`.

O defeito que estes testes travam (auditoria 24-25/08/2026,
`.ia/ENRICHMENT.md` §Achado 1): `djen/parser.py` grava
`Movimentacao.destinatarios` e `destinatario_advogados` desde sempre, e
NENHUM caminho de código promovia isso a entidade. `grep -rn destinatario
--include='*.py'` só achava o parser e os coletores de `diarios/` escrevendo.
Resultado medido em 11.160 processos: 90,0% sem nenhuma `ProcessoParte`, e
94,2% desses COM destinatário gravado — 84,8% do acervo (≈ 86,7 M processos)
ganhava parte sem uma única requisição.

Números independentes desta missão (produção, semente 20260825):

  · 2.311 de 3.584 processos amostrados (6 âncoras × 600 pks uniformes em
    `id ∈ [1, 104.602.261]`) sem `ProcessoParte`; **2.172 = 94,0%** deles com
    destinatário — confirma os 94,2% da auditoria por outro desenho de amostra.
  · 23.771 destinatários e 26.569 advogados (24 âncoras × 400 pks): `polo` em
    100%, mas com QUATRO valores (`A` 13.003 · `P` 10.519 · `T` 228 · `D` 21);
    `papel` em 10 (0,04%); `uf_oab`+`numero_oab` em 100% dos advogados.
  · 12.592 destinatários distintos por (processo, nome, polo): 598 (4,75%) com
    marcador textual de segredo e 840 (6,67%) só com iniciais.
"""
import pytest

from tribunals.models import Movimentacao, Parte, Process, ProcessoParte, Tribunal
from tribunals.services.partes_djen import (
    FONTE,
    PAPEL,
    formatar_oab,
    nome_de_segredo,
    polo_de,
    promover_lote,
    sem_processoparte,
    specs_do_processo,
)

# --------------------------------------------------------------------------
# Guarda de segredo — nomes REAIS colhidos em produção (semente 20260825)
# --------------------------------------------------------------------------
#: Todos apareceram no banco. Parte inventada em processo sob segredo é PIOR
#: que processo sem parte.
NOMES_DE_SEGREDO = [
    # marcador textual — 598 ocorrências em 12.592 (4,75%)
    'SIGILO', 'SIGILO1', 'SIGILO2', 'SIGILO 1', 'SIGILO 2', 'SIGILOSO',
    'EM SEGREDO DE JUSTIÇA', 'EM SEGREDO DE JUSTIçA',
    'SEGREDO DE JUSTIÃ§A',            # mojibake, como está gravado
    'SEGREDO DE JUSTIçA',
    'PROCESSO ESTÁ EM SEGREDO DE JUSTIÇA - 1',
    'PROCESSO ESTÁ EM SEGREDO DE JUSTIÇA - 4',
    'PARTE/PROCESSO SIGILOSO OU PROCESSO ESTá EM SEGREDO DE JUSTIçA.1',
    # só iniciais — 840 ocorrências (6,67%)
    'E.O.', 'M.P.E.M.G.', 'P.J.R.A.F.A.', 'W.V.D.', 'M.S.D.', 'F.P.B.',
    'E. S. D. J.', 'M. P. D. D. F. E. D. T.', 'A.A.', 'D.N.',
    # iniciais com conectivo
    'A. H. DOS S.', 'F. S. O. DO B. L.', 'H. A. DA S.', 'J. A. DE O.',
    'E. DE L.', 'M. DA S. P. R.', 'C. DOS S. C.', 'U. N. -. C. C.',
    # nome de UMA letra
    'I', 'M', 'N.', 'B.', 'A.',
    # vazio / ausente
    '', '   ', None,
]

#: Controle negativo: nomes REAIS que a guarda NÃO pode descartar. O último é o
#: motivo de a regra `[Xx]{3,}` ("máscara") ter sido REJEITADA — o único nome
#: dos 12.592 com 3 X seguidos é uma incorporadora com algarismo romano.
NOMES_REAIS = [
    'EDILBERTO LUCIO DA COSTA OLIVEIRA JUNIOR',
    'WANDERSON AMANCIO VIEIRA',
    'BRASIL NORTE COM. REPRESENTACOES COMERCIAIS LTDA',
    'CTC - CENTRO DE TECNOLOGIA CANAVIEIRA S.A.',
    'SOCIEDADE BENEFICENTE SAO CAMILO',
    'MINISTÉRIO PÚBLICO DO ESTADO DE SÃO PAULO',
    'CAPITAL ADMINISTRADORA JUDICIAL LTDA.',
    'UNIAO NACIONAL DOS CONSUMIDORES E PROPRIETARIOS DE VEICULOS - UNICOON',
    'ESTEFANI SA SOCIEDADE INDIVIDUAL DE ADVOCACIA',
    'A. P. CARNAUBA-ME',
    'MRV MRL XXXVIII INCORPORACOES SPE LTDA',
]


@pytest.mark.parametrize('nome', NOMES_DE_SEGREDO)
def test_nome_de_segredo_descarta(nome):
    assert nome_de_segredo(nome) is True, (
        f'{nome!r} viraria uma Parte. "SIGILO" sozinho aparece 358 vezes na '
        f'amostra — vira UMA entidade colando 358 processos que não têm relação'
    )


@pytest.mark.parametrize('nome', NOMES_REAIS)
def test_nome_de_segredo_nao_descarta_nome_real(nome):
    """Controle negativo. Sem ele a guarda "funciona" descartando tudo."""
    assert nome_de_segredo(nome) is False, (
        f'{nome!r} é uma entidade real e foi descartada — a guarda virou censura'
    )


# --------------------------------------------------------------------------
# OAB — `Parte.oab` tem unique constraint parcial: formatar diferente do resto
# do sistema = Parte duplicada em 17,7 M de linhas
# --------------------------------------------------------------------------
@pytest.mark.parametrize('numero,uf,esperado', [
    ('6094', 'AP', 'AP6094'),          # 89,7% dos 26.569 medidos
    ('46601', 'BA', 'BA46601'),
    ('46601A', 'BA', 'BA46601A'),      # 9,3% — sufixo de uma letra, colado
    ('209212A', 'RJ', 'RJ209212A'),    # forma REAL em tribunals_parte
    ('4960', 'MT', 'MT4960'),
    ('MT4960', 'MT', 'MT4960'),        # UF repetida no número (103 casos)
    ('4960MT', 'MT', 'MT4960'),        # … e como sufixo
    ('27467/O', 'CE', 'CE27467'),      # o `/` corta, igual a parse_oab
    ('1.234', 'SP', 'SP1234'),         # ponto de milhar sai, igual a parse_oab
    ('679-A', 'PI', 'PI679A'),
    ('6094', 'ap', 'AP6094'),          # uf minúscula
])
def test_formatar_oab(numero, uf, esperado):
    assert formatar_oab(numero, uf) == esperado


@pytest.mark.parametrize('numero,uf', [
    ('R', 'AM'), ('D', 'AM'), ('A1608', 'AM'),   # lixo real, medido
    ('6094', ''), ('6094', 'BRASIL'), ('6094', None), (None, 'SP'), ('', 'SP'),
])
def test_formatar_oab_se_abstem(numero, uf):
    """Abster > chutar: sem OAB provável o advogado entra pelo nome, não com
    uma OAB inventada que colidiria com a de outra pessoa."""
    assert formatar_oab(numero, uf) == ''


def test_formatar_oab_bate_com_parse_oab_do_resto_do_sistema():
    """Régua contra a fonte que já gravou 17,7 M de linhas."""
    from enrichers.parsers import parse_oab

    for numero, uf in [('6094', 'AP'), ('46601A', 'BA'), ('1.234', 'SP')]:
        assert formatar_oab(numero, uf) == parse_oab(f'OAB {uf} {numero}')


# --------------------------------------------------------------------------
# polo — QUATRO valores medidos, não dois
# --------------------------------------------------------------------------
@pytest.mark.parametrize('bruto,esperado', [
    ('A', 'ativo'), ('P', 'passivo'),
    ('T', 'outros'),      # 228 medidos — terceiro (MP, administradora judicial)
    ('D', 'outros'),      # 21 medidos
    ('', 'outros'),       # coletores de diarios/ gravam polo vazio
    (None, 'outros'), ('x', 'outros'),
])
def test_polo_de(bruto, esperado):
    assert polo_de(bruto) == esperado


# --------------------------------------------------------------------------
# Extração das specs
# --------------------------------------------------------------------------
MOV_TRT8 = (
    # formato REAL de produção (Movimentacao id=1324979039, TRT8)
    [{'nome': 'EDILBERTO LUCIO DA COSTA OLIVEIRA JUNIOR', 'polo': 'A',
      'comunicacao_id': 641602628},
     {'nome': 'BRASIL NORTE COM. REPRESENTACOES COMERCIAIS LTDA', 'polo': 'P',
      'comunicacao_id': 641602628}],
    [{'id': 1183172997, 'advogado_id': 3413154, 'comunicacao_id': 641602628,
      'advogado': {'id': 3413154, 'nome': 'EDUARDO DELGADO FREIRE',
                   'uf_oab': 'AP', 'numero_oab': '6094'}}],
)


def test_specs_do_processo_le_o_formato_real():
    specs = specs_do_processo([MOV_TRT8])
    assert sorted(specs.por_polo) == ['ativo', 'outros', 'passivo']
    assert specs.por_polo['ativo'][0]['nome'] == 'EDILBERTO LUCIO DA COSTA OLIVEIRA JUNIOR'
    assert specs.por_polo['passivo'][0]['nome'] == 'BRASIL NORTE COM. REPRESENTACOES COMERCIAIS LTDA'
    adv = specs.por_polo['outros'][0]
    assert (adv['nome'], adv['oab'], adv['tipo']) == ('EDUARDO DELGADO FREIRE', 'AP6094', 'advogado')
    # Sem CPF/CNPJ no payload: o campo fica VAZIO e a tela diz que está vazio.
    assert all(s['documento'] == '' for lista in specs.por_polo.values() for s in lista)


def test_specs_aguenta_o_formato_dos_coletores_de_diarios():
    """`diarios/fontes/*` gravam nas MESMAS colunas com `papel` e polo ''."""
    specs = specs_do_processo([(
        [{'nome': 'FULANO DE TAL', 'polo': 'A', 'papel': 'Agravante'},
         {'nome': 'SICRANA DE TAL', 'polo': '', 'papel': 'Agravado'}],
        [{'advogado': {'nome': 'ADVOGADA X', 'numero_oab': '192562', 'uf_oab': 'SP'},
          'oabs': [{'numero_oab': '192562', 'uf_oab': 'SP'}], 'polo': 'A'}],
    )])
    assert specs.por_polo['ativo'][0]['nome'] == 'FULANO DE TAL'
    # `outros` junta duas coisas: o destinatário de polo desconhecido e o
    # advogado. Buscar por nome, não por índice.
    por_nome = {s['nome']: s for s in specs.por_polo['outros']}
    assert por_nome['ADVOGADA X']['oab'] == 'SP192562'
    assert por_nome['ADVOGADA X']['tipo'] == 'advogado'
    # polo '' cai em outros, não estoura
    assert por_nome['SICRANA DE TAL']['tipo'] == 'desconhecido'


def test_specs_le_jsonb_como_texto():
    """Regressão da sonda quebrada: psycopg devolve `jsonb` como `str`, e
    `bool('[]')` é `True`. A primeira medição desta missão reportou "100% dos
    processos têm destinatário" em 6 de 6 âncoras — o valor real era 94,0%.
    Verificação com input vazio reporta SUCESSO."""
    cheio = specs_do_processo([('[{"nome": "FULANO DE TAL", "polo": "A"}]', '[]')])
    assert cheio.por_polo['ativo'][0]['nome'] == 'FULANO DE TAL'
    vazio = specs_do_processo([('[]', '[]')])
    assert not vazio, 'a sonda tratou [] como preenchido — é o defeito de novo'


def test_specs_deduplica_a_mesma_parte_nas_3_movimentacoes():
    """Lemos as 3 movimentações mais recentes e elas repetem o destinatário."""
    specs = specs_do_processo([MOV_TRT8, MOV_TRT8, MOV_TRT8])
    assert len(specs.por_polo['ativo']) == 1
    assert len(specs.por_polo['outros']) == 1


def test_specs_conta_o_processo_que_so_tem_segredo():
    specs = specs_do_processo([([{'nome': 'SIGILO', 'polo': 'A'},
                                 {'nome': 'E.O.', 'polo': 'P'}], [])])
    assert not specs, 'criou Parte a partir de nome de segredo'
    assert specs.descartados_segredo == 2
    assert specs.marcador_segredo is True, 'a fonte AFIRMOU segredo e não registramos'


def test_marcador_de_segredo_nao_e_afirmado_por_iniciais():
    """Iniciais também são usadas em família sem segredo decretado — não dá
    pra provar, então não afirmamos (regra nº 6)."""
    specs = specs_do_processo([([{'nome': 'W.V.D.', 'polo': 'A'}], [])])
    assert specs.descartados_segredo == 1
    assert specs.marcador_segredo is False


# --------------------------------------------------------------------------
# Fim a fim, contra o banco
# --------------------------------------------------------------------------
@pytest.fixture
def cenario(db):
    from django.utils import timezone

    trib = Tribunal.objects.create(sigla='TRT8X', nome='TRT8 de teste')
    procs = []
    for i in range(4):
        p = Process.objects.create(numero_cnj=f'000000{i}-11.2024.5.08.0001',
                                   tribunal=trib)
        procs.append(p)
        dest, advs = MOV_TRT8
        if i == 3:      # processo sob segredo: só nomes mascarados
            dest = [{'nome': 'SIGILO', 'polo': 'A'}, {'nome': 'W.V.D.', 'polo': 'P'}]
            advs = []
        Movimentacao.objects.create(
            processo=p, tribunal=trib, external_id=f'ext-{i}',
            data_disponibilizacao=timezone.now(),
            destinatarios=dest, destinatario_advogados=advs,
        )
    # procs[2] JÁ TEM parte, vinda do enricher (fonte NULL, papel preenchido)
    parte_enricher = Parte.objects.create(nome='PARTE DO ENRICHER',
                                          documento='12.345.678/0001-90',
                                          tipo_documento='CNPJ', tipo='pj')
    ProcessoParte.objects.create(processo=procs[2], parte=parte_enricher,
                                 polo='ativo', papel='AUTOR')
    return trib, procs


def test_backfill_cria_parte_para_quem_nao_tinha(cenario):
    """O defeito: processo com destinatário gravado e ZERO ProcessoParte."""
    _, procs = cenario
    alvo = sem_processoparte([p.id for p in procs])
    assert procs[2].id not in alvo, 'processo com parte do enricher não foi pulado'

    res = promover_lote(alvo)

    assert res.linhas_confirmadas > 0
    for p in (procs[0], procs[1]):
        polos = set(ProcessoParte.objects.filter(processo=p).values_list('polo', flat=True))
        assert polos == {'ativo', 'passivo', 'outros'}, (
            f'processo {p.id} tem destinatário no JSONB e ficou sem parte — '
            f'é exatamente o defeito de 84,8% do acervo'
        )
    adv = ProcessoParte.objects.get(processo=procs[0], polo='outros')
    assert adv.parte.oab == 'AP6094'
    assert adv.parte.tipo == 'advogado'


def test_backfill_marca_a_procedencia(cenario):
    """Sem `fonte`, a tela não pode dizer que a parte veio da publicação (sem
    CPF/CNPJ) e o rollback por faixa não existe."""
    _, procs = cenario
    promover_lote(sem_processoparte([p.id for p in procs]))
    assert ProcessoParte.objects.filter(processo=procs[0]).exclude(fonte=FONTE).count() == 0
    # e o papel fica VAZIO — o DJEN não dá papel (10 em 23.771 = 0,04%)
    assert set(ProcessoParte.objects.filter(processo=procs[0])
               .values_list('papel', flat=True)) == {PAPEL}


def test_backfill_nao_apaga_nem_altera_o_que_o_enricher_escreveu(cenario):
    """É PROIBIDO usar o caminho do apply_event (DELETE do processo inteiro):
    ele apagaria dado do enricher, que tem CPF/CNPJ e papel."""
    _, procs = cenario
    antes = list(ProcessoParte.objects.filter(fonte__isnull=True)
                 .values('id', 'processo_id', 'parte_id', 'polo', 'papel'))
    assert antes, 'fixture inválida: não havia linha de enricher para proteger'
    promover_lote(sem_processoparte([p.id for p in procs]))
    depois = list(ProcessoParte.objects.filter(fonte__isnull=True)
                  .values('id', 'processo_id', 'parte_id', 'polo', 'papel'))
    assert depois == antes, 'linha do enricher foi apagada ou alterada'


def test_backfill_e_idempotente(cenario):
    """2ª execução tem que criar ZERO linha — a idempotência vem da constraint
    parcial `uniq_processo_parte_polo_papel_principal`, não de um delete."""
    _, procs = cenario
    ids = [p.id for p in procs]
    promover_lote(sem_processoparte(ids))
    total_1 = ProcessoParte.objects.count()

    # sem_processoparte agora exclui todos — força o caminho de reescrita
    res2 = promover_lote([procs[0].id, procs[1].id])
    total_2 = ProcessoParte.objects.count()

    assert total_2 == total_1, (
        f'2ª execução criou {total_2 - total_1} linhas; tem que criar 0'
    )
    assert res2.linhas_confirmadas == ProcessoParte.objects.filter(
        processo_id__in=[procs[0].id, procs[1].id], fonte=FONTE).count()


def test_backfill_nao_cria_parte_em_processo_de_segredo(cenario):
    """Parte inventada em processo sob segredo é PIOR que processo sem parte."""
    _, procs = cenario
    res = promover_lote(sem_processoparte([p.id for p in procs]))
    assert ProcessoParte.objects.filter(processo=procs[3]).count() == 0
    assert res.so_segredo == 1, 'o processo de segredo sumiu no resto em vez de ser contado'
    assert res.com_marcador_segredo == 1
    assert not Parte.objects.filter(nome__in=['SIGILO', 'W.V.D.']).exists()


def test_dry_run_nao_escreve_nem_processoparte_nem_parte(cenario):
    """Regressão de um defeito REAL desta missão: `_bulk_upsert_partes` do
    drainer ESCREVE `Parte`, e o `--dry-run` o chamava. O primeiro dry-run do
    piloto criou **39.303 `Parte` órfãs** em produção (20.609 `desconhecido` +
    18.694 `advogado`, zero com `ProcessoParte`). Controle que provou a
    autoria: na hora anterior a mesma consulta devolveu 0 órfã de qualquer
    tipo — o drainer cria `Parte` e `ProcessoParte` na mesma transação.

    Um `--dry-run` que escreve é pior que não ter `--dry-run`: é usado
    exatamente por quem ainda não decidiu rodar."""
    _, procs = cenario
    pp_antes = ProcessoParte.objects.count()
    partes_antes = set(Parte.objects.values_list('pk', flat=True))

    res = promover_lote(sem_processoparte([p.id for p in procs]), dry_run=True)

    assert res.linhas_tentadas > 0, 'o dry-run não contou nada — virou no-op'
    assert ProcessoParte.objects.count() == pp_antes
    novas = set(Parte.objects.values_list('pk', flat=True)) - partes_antes
    assert not novas, (
        f'--dry-run criou {len(novas)} Parte: '
        f'{list(Parte.objects.filter(pk__in=novas).values_list("nome", flat=True))[:5]}'
    )


def test_dry_run_conta_o_mesmo_que_a_execucao_real(cenario):
    """Controle positivo do contador do dry-run: se ele contar diferente do
    que a execução real cria, o dry-run mente sobre o tamanho do trabalho."""
    _, procs = cenario
    alvo = sem_processoparte([p.id for p in procs])
    seco = promover_lote(alvo, dry_run=True)
    real = promover_lote(alvo)
    assert seco.linhas_tentadas == real.linhas_tentadas
