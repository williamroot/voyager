"""O ente devedor tem que ATERRISSAR em `ProcessoParte` (#118, 02/09/2026).

O parser da relação da DEPRE passou a extrair `Entidade devedora` no polo
passivo, e o JSONB provou que chegou ao banco: **2.568 de 2.568** movimentações
da relação de 10/03/2025 com `papel='ENTIDADE DEVEDORA'` e `polo='P'` em
`Movimentacao.destinatarios`. E `ProcessoParte` desses processos: **ZERO**.

Extraído sem aterrissar é "coletado pela metade" — a tela "Quem deve" do
Overview lê `ProcessoParte`, não o JSONB. O vínculo se perdia em DOIS pontos
independentes, os dois medidos antes de mexer numa linha de código:

  A. **Ninguém promovia.** A promoção existe (`tribunals/services/partes_djen.py`)
     mas é backfill por FAIXA DE PK disparado à mão. Conferido no Redis: zero
     checkpoints de shard; e as 218.068 `ProcessoParte` criadas nas 24 h
     anteriores eram TODAS do enricher (`fonte IS NULL`), nenhuma de
     `fonte='djen'`. Processo que nasce hoje de uma coleta tem pk acima de
     qualquer faixa já varrida — nunca é alcançado.

  B. **O papel morria no caminho.** `specs_do_processo` lia o `polo` do JSONB
     (por isso o passivo sobrevivia) mas tirava o `papel` só de uma tabela HTML
     no texto (`papeis_do_texto`), pensada para o eproc. O `destinatarios[].papel`
     — que o DJEN quase nunca traz (0,04%) e que os coletores de `diarios/`
     SEMPRE trazem — não era lido. Rodando `specs_do_processo` sobre o JSONB
     real de produção, os três saíam com `papel=''`.
"""

import datetime as dt
from unittest import mock

import pytest

from tribunals.models import ProcessoParte
from tribunals.services.partes_djen import papel_da_fonte, specs_do_processo

#: O JSONB REAL da movimentação 2018538324 (processo 105929723), coletada em
#: 02/09/2026 da relação da DEPRE de 10/03/2025, caderno 4159-11.
DEPRE_REAL = [
    {'nome': 'VERA LÚCIA DA SILVA E SILVA', 'polo': 'A', 'papel': 'REQTE'},
    {'nome': 'SPPREV - SÃO PAULO PREVIDÊNCIA', 'polo': 'P', 'papel': 'ENTIDADE DEVEDORA'},
    {'nome': 'FAZENDA DO ESTADO DE SÃO PAULO', 'polo': 'P', 'papel': 'ENTIDADE AGRUPADORA'},
]


# ── B. o papel que a FONTE imprimiu ──────────────────────────────────────────
def test_papel_da_fonte_le_o_rotulo_do_registro():
    assert papel_da_fonte({'papel': 'ENTIDADE DEVEDORA'}) == 'ENTIDADE DEVEDORA'
    assert papel_da_fonte({'papel': '  Reqte \n'}) == 'Reqte'
    # Ausente, vazio ou não-dict: abstém, nunca inventa.
    assert papel_da_fonte({}) == ''
    assert papel_da_fonte({'papel': None}) == ''
    assert papel_da_fonte({'nome': 'FULANO'}) == ''
    # Cabe na coluna (varchar 120): corta aqui, não no `DataError` do banco no
    # meio de um lote de 500.
    assert len(papel_da_fonte({'papel': 'X' * 400})) == 120


def test_o_ente_devedor_sai_no_polo_passivo_COM_o_papel():
    """A regressão que motivou o arquivo: o polo sobrevivia, o papel morria."""
    specs = specs_do_processo([(DEPRE_REAL, [], 'Nº de ordem cronológica: 278/2026')])
    passivo = {i['nome']: i['papel'] for i in specs.por_polo[ProcessoParte.POLO_PASSIVO]}
    assert passivo == {
        'SPPREV - SÃO PAULO PREVIDÊNCIA': 'ENTIDADE DEVEDORA',
        'FAZENDA DO ESTADO DE SÃO PAULO': 'ENTIDADE AGRUPADORA',
    }, 'o ente devedor precisa chegar no polo PASSIVO e com o papel impresso'
    ativo = {i['nome']: i['papel'] for i in specs.por_polo[ProcessoParte.POLO_ATIVO]}
    assert ativo == {'VERA LÚCIA DA SILVA E SILVA': 'REQTE'}


def test_o_djen_continua_tirando_o_papel_do_texto():
    """Controle negativo: quem NÃO traz papel no JSONB não pode regredir.

    O DJEN traz `papel` em 0,04% dos destinatários; o rótulo dele vem da tabela
    do cabeçalho. Se a precedência nova tivesse desligado esse caminho, 79,2%
    das publicações eproc perderiam o papel de uma vez.
    """
    cabecalho = ('<table><tr><td>AUTOR</td><td>: Fulano de Tal</td></tr>'
                 '<tr><td>RÉU</td><td>: Banco Beltrano S/A</td></tr></table>')
    destinatarios = [{'nome': 'Fulano de Tal', 'polo': 'A'},
                     {'nome': 'Banco Beltrano S/A', 'polo': 'P'}]
    specs = specs_do_processo([(destinatarios, [], cabecalho)])
    assert specs.por_polo[ProcessoParte.POLO_ATIVO][0]['papel'] == 'AUTOR'
    assert specs.por_polo[ProcessoParte.POLO_PASSIVO][0]['papel'] == 'RÉU'


def test_a_fonte_vence_o_texto_quando_os_dois_falam():
    """Precedência escrita: quem ROTULOU o campo foi o diário; o texto é
    inferência sobre uma tabela HTML e só entra quando a fonte cala."""
    cabecalho = '<table><tr><td>AUTOR</td><td>: SPPREV - SÃO PAULO PREVIDÊNCIA</td></tr></table>'
    specs = specs_do_processo([([DEPRE_REAL[1]], [], cabecalho)])
    assert specs.por_polo[ProcessoParte.POLO_PASSIVO][0]['papel'] == 'ENTIDADE DEVEDORA'


# ── A. a promoção acontece na GRAVAÇÃO ───────────────────────────────────────
@pytest.mark.django_db(transaction=True)
def test_gravar_enfileira_a_promocao_a_parte_no_commit():
    """O buraco A: o dado entrava e ninguém o promovia.

    Mesmo remédio do §12 da `DIARIOS.md` (entrega ao índice) num campo
    diferente — `on_commit`, em lote, e nunca um job por publicação.
    """
    from django.db import transaction

    from diarios.base import id_bloco_impresso, persistir_movimentacoes
    from djen.parser import ParsedItem
    from tribunals.models import Movimentacao, Process, Tribunal

    t, _ = Tribunal.objects.get_or_create(
        sigla='TJSP', defaults={'nome': 'TJSP', 'sigla_djen': 'TJSP'})
    Movimentacao.objects.filter(tribunal=t).delete()

    texto = ('Nº de ordem cronológica: 278/2026\n'
             'Processo: 0156916-80.2024.8.26.0500\n'
             'Entidade devedora: SPPREV - SÃO PAULO PREVIDÊNCIA')
    item = ParsedItem(
        cnj='0156916-80.2024.8.26.0500',
        external_id=id_bloco_impresso('tjsp-dje', 4159, 11, 3, texto=texto),
        data_disponibilizacao=dt.datetime(2025, 3, 10, 3, 0, tzinfo=dt.UTC),
        texto=texto, meio='D', destinatarios=DEPRE_REAL,
    )

    fila = mock.MagicMock()
    with mock.patch('django_rq.get_queue', return_value=fila):
        with transaction.atomic():
            persistir_movimentacoes([item], t, None)
            promocoes = [c for c in fila.enqueue.call_args_list
                         if c.args and getattr(c.args[0], '__name__', '') == 'promover_partes']
            assert not promocoes, 'nada pode ser enfileirado ANTES do commit'
        promocoes = [c for c in fila.enqueue.call_args_list
                     if c.args and getattr(c.args[0], '__name__', '') == 'promover_partes']

    assert len(promocoes) == 1, 'a promoção tem que ser enfileirada no commit'
    pk = Process.objects.get(tribunal=t, numero_cnj=item.cnj).pk
    assert promocoes[0].args[1] == [pk]


@pytest.mark.django_db(transaction=True)
def test_fila_fora_do_ar_nao_derruba_a_coleta_da_movimentacao():
    """Assimetria deliberada em relação à entrega ao índice.

    Índice ausente torna a edição INÚTIL (coletada e não buscável) e por isso
    derruba a coleta. Parte ausente é enriquecimento que o
    `backfill_partes_djen` recupera depois — e a movimentação, que é o acervo,
    já está gravada. Derrubar a edição inteira por causa disso seria trocar um
    dado a menos por muitos dados a menos.
    """
    from django.db import transaction

    from diarios.base import id_bloco_impresso, persistir_movimentacoes
    from djen.parser import ParsedItem
    from tribunals.models import Movimentacao, Tribunal

    t, _ = Tribunal.objects.get_or_create(
        sigla='TJSP', defaults={'nome': 'TJSP', 'sigla_djen': 'TJSP'})
    Movimentacao.objects.filter(tribunal=t).delete()
    texto = 'Entidade devedora: MUNICÍPIO DE AMERICANA'
    item = ParsedItem(
        cnj='0313356-07.2024.8.26.0500',
        external_id=id_bloco_impresso('tjsp-dje', 4159, 11, 4, texto=texto),
        data_disponibilizacao=dt.datetime(2025, 3, 10, 3, 0, tzinfo=dt.UTC),
        texto=texto, meio='D', destinatarios=DEPRE_REAL,
    )

    fila = mock.MagicMock()

    def _get_queue(nome, *a, **k):
        if nome == 'default':
            raise ConnectionError('redis fora do ar')
        return fila

    with mock.patch('django_rq.get_queue', side_effect=_get_queue):
        with transaction.atomic():
            novas, dup = persistir_movimentacoes([item], t, None)

    assert (novas, dup) == (1, 0)
    assert Movimentacao.objects.filter(tribunal=t, external_id=item.external_id).exists(), (
        'a movimentação tem que estar gravada mesmo sem a promoção'
    )


def test_a_procedencia_diz_de_onde_a_parte_veio():
    """`fonte` existe para não mentir sobre procedência — e desde 02/09/2026 o
    DJEN não é o único a escrever em `Movimentacao.destinatarios`.

    Carimbar `'djen'` numa parte que veio do DJE/TJSP faria a coluna mentir
    justamente onde ela existe para não mentir. E a contagem independente TEM
    que acompanhar o carimbo: contar por `'djen'` uma passada que gravou
    `'diario'` devolveria 0, e o job reportaria "não gravei" tendo gravado.
    """
    import inspect

    from tribunals.services import partes_djen as svc

    assert svc.FONTE_DIARIO == 'diario' and svc.FONTE == 'djen'
    assert len(svc.FONTE_DIARIO) <= 16, 'ProcessoParte.fonte é varchar(16)'
    assert inspect.signature(svc.promover_lote).parameters['fonte'].default == svc.FONTE
    assert inspect.signature(svc._contar_linhas_djen).parameters['fonte'].default == svc.FONTE

    # E o job dos diários passa a procedência certa.
    codigo = inspect.getsource(__import__('diarios.jobs', fromlist=['x']).promover_partes)
    assert 'fonte=FONTE_DIARIO' in codigo


@pytest.mark.django_db(transaction=True)
def test_promocao_le_as_movs_do_LOTE_e_nao_as_3_mais_recentes():
    """O teto invisível que sobrou na primeira passada, medido em produção.

    `promover_lote` lê as `JANELA_MOVS=3` movimentações mais recentes de cada
    processo — heurística correta para quem varre por faixa de pk e não sabe o
    que procura. O coletor SABE: ele acabou de gravar as linhas. Medido na
    relação da DEPRE de 10/03/2025: dos 2.568 processos, **823 (32%)** têm mais
    de 3 movimentações, e ZERO deles ganhou o ente devedor; os 1.445 que
    ganharam estão TODOS na faixa de até 3.
    """
    from django.db import transaction

    from diarios.base import id_bloco_impresso, persistir_movimentacoes
    from djen.parser import ParsedItem
    from tribunals.models import Movimentacao, Tribunal

    t, _ = Tribunal.objects.get_or_create(
        sigla='TJSP', defaults={'nome': 'TJSP', 'sigla_djen': 'TJSP'})
    Movimentacao.objects.filter(tribunal=t).delete()
    texto = 'Entidade devedora: SPPREV - SÃO PAULO PREVIDÊNCIA'
    item = ParsedItem(
        cnj='0156916-80.2024.8.26.0500',
        external_id=id_bloco_impresso('tjsp-dje', 4159, 11, 9, texto=texto),
        data_disponibilizacao=dt.datetime(2025, 3, 10, 3, 0, tzinfo=dt.UTC),
        texto=texto, meio='D', destinatarios=DEPRE_REAL,
    )

    fila = mock.MagicMock()
    with mock.patch('django_rq.get_queue', return_value=fila):
        with transaction.atomic():
            persistir_movimentacoes([item], t, None)

    promocoes = [c for c in fila.enqueue.call_args_list
                 if c.args and getattr(c.args[0], '__name__', '') == 'promover_partes']
    assert len(promocoes) == 1
    mov_ids = promocoes[0].args[2]
    esperado = list(Movimentacao.objects.filter(tribunal=t).values_list('id', flat=True))
    assert mov_ids == esperado, (
        'a promoção precisa receber os pks das movimentações do LOTE — sem '
        'eles ela cai na janela das 3 mais recentes e perde 32% dos casos'
    )


def test_ler_movimentacoes_por_pk_nao_tem_janela():
    """Controle de contrato: a função nova não aceita `janela`, de propósito."""
    import inspect

    from tribunals.services.partes_djen import ler_movimentacoes_por_pk, promover_lote

    assert 'janela' not in inspect.signature(ler_movimentacoes_por_pk).parameters
    assert ler_movimentacoes_por_pk([]) == {}
    # E `promover_lote` aceita a injeção sem perder o caminho antigo.
    par = inspect.signature(promover_lote).parameters['movs_por_processo']
    assert par.default is None, 'sem injeção, o comportamento antigo é o default'
