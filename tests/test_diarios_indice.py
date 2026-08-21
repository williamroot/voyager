"""Gate de completude do ÍNDICE — a edição coletada tem que virar edição BUSCÁVEL.

O incidente que estes testes travam, medido em produção em 21/08/2026:

    TJSP, 12/03/2025 — os 8 cadernos do DJE recém-coletados
      Postgres ....... 283.393
      Elasticsearch .. 255.709
      FORA do índice .  27.684   (9,8% do dia)

A causa foi reconstruída à unidade e NÃO era corrupção: era ESPERA. As linhas
do diário chegavam ao Elasticsearch APENAS pelo poller de 10 minutos
(`search/sync_incremental.py`), porque `persistir_movimentacoes` grava por
`bulk_create` e `bulk_create` não dispara `post_save`. O último tick antes do
fim da coleta (21:41:38 -03) deixou o watermark em `id=1.663.688.937`; a coleta
terminou às 21:44:43 com `id` máximo 1.664.109.049. Linhas do diário acima
daquele watermark: **27.619** — mais 65 de resíduo antigo do DJEN no mesmo dia
= **27.684**, o número relatado. Quem mediu duas vezes com poucos minutos entre
as medições viu o mesmo número porque ENTRE TICKS de um poller nada se move.

E o pior: as 8 edições estavam `status=ok`, `itens_gravados=220.544`. Nada no
sistema afirmava que aquelas linhas eram buscáveis. "Run verde, log limpo,
número redondo" — os três da tabela do CLAUDE.md ao mesmo tempo.

O outro buraco que apareceu na mesma medição, e que estes testes também travam:
**91 jobs `indexar_movimentacoes_bulk` parados no `FailedJobRegistry`**, 83
deles com `ApiError(413)` (corpo acima do `http.max_content_length` do ES),
referenciando 45.500 publicações — das quais **45.313 estavam fora do índice**.
Ninguém reprocessa o FailedJobRegistry: o lote contado em DOCUMENTOS e não em
BYTES era um corte mudo do nosso lado de um teto que existia do lado do ES.
"""

import datetime as dt
from unittest import mock

import pytest
from django.utils import timezone


# ── 1. a entrega ao índice acontece na GRAVAÇÃO, não no próximo poller ───────
@pytest.mark.django_db(transaction=True)
def test_persistir_entrega_o_lote_ao_indice_no_commit():
    """A regressão principal: gravar sem entregar ao índice.

    Antes, `persistir_movimentacoes` terminava sem falar com a fila `es_index`
    e a docstring dizia que "a indexação é feita depois, em lote, por
    `reindexar_*`" — comando que ninguém rodava. O resultado medido foram
    27.619 linhas do dia 12/03/2025 do TJSP fora da busca com a edição `ok`.
    """
    from django.db import transaction

    from diarios.base import id_bloco_impresso, persistir_movimentacoes
    from djen.parser import ParsedItem
    from tribunals.models import Movimentacao, Tribunal

    t, _ = Tribunal.objects.get_or_create(
        sigla='TJSP', defaults={'nome': 'TJSP', 'sigla_djen': 'TJSP'})
    Movimentacao.objects.filter(tribunal=t).delete()

    quando = dt.datetime(2025, 3, 12, 3, 0, tzinfo=dt.UTC)
    itens = []
    for i in range(3):
        texto = f'Vistos. Ato nº {i} do caderno 12.'
        itens.append(ParsedItem(
            cnj=f'100966{i}-22.2025.8.26.0100',
            external_id=id_bloco_impresso('tjsp-dje', 4161, 12, i, texto=texto),
            data_disponibilizacao=quando, texto=texto, meio='D',
        ))

    fila = mock.MagicMock()
    with mock.patch('django_rq.get_queue', return_value=fila):
        with transaction.atomic():
            novas, dup = persistir_movimentacoes(itens, t, None)
            # dentro da transação NADA foi enfileirado: entregar pks de linhas
            # que podem sofrer rollback é enfileirar fantasma.
            assert fila.enqueue.call_count == 0
        # ...e no commit, exatamente um job de lote com os 3 pks.
        assert fila.enqueue.call_count == 1

    assert (novas, dup) == (3, 0)
    nome, args = fila.enqueue.call_args[0][0], fila.enqueue.call_args[0][1]
    assert nome == 'search.jobs.indexar_movimentacoes_bulk'
    assert sorted(args) == sorted(
        Movimentacao.objects.filter(tribunal=t).values_list('id', flat=True))


@pytest.mark.django_db(transaction=True)
def test_recoleta_reentrega_ao_indice_mesmo_sem_linha_nova():
    """Re-coletar uma edição já coletada tem que RE-ENTREGAR ao índice.

    Motivo concreto: a troca de extrator (ADR-031) muda a quebra de linha e
    portanto o `texto`. E, principalmente, a re-coleta é o único caminho de
    reparo que um operador tem à mão — se ela devolvesse `novas=0` e não
    falasse com o índice, "recoletei e continua fora da busca" seria o
    comportamento correto do sistema.
    """
    from django.db import transaction

    from diarios.base import id_bloco_impresso, persistir_movimentacoes
    from djen.parser import ParsedItem
    from tribunals.models import Movimentacao, Tribunal

    t, _ = Tribunal.objects.get_or_create(
        sigla='TJSP', defaults={'nome': 'TJSP', 'sigla_djen': 'TJSP'})
    Movimentacao.objects.filter(tribunal=t).delete()
    texto = 'Ato único, re-coletado.'
    item = ParsedItem(
        cnj='1009999-22.2025.8.26.0100',
        external_id=id_bloco_impresso('tjsp-dje', 4161, 12, 7, texto=texto),
        data_disponibilizacao=dt.datetime(2025, 3, 12, 3, 0, tzinfo=dt.UTC),
        texto=texto, meio='D',
    )
    fila = mock.MagicMock()
    with mock.patch('django_rq.get_queue', return_value=fila), transaction.atomic():
        persistir_movimentacoes([item], t, None)
    with mock.patch('django_rq.get_queue', return_value=fila), transaction.atomic():
        novas, dup = persistir_movimentacoes([item], t, None)
    assert (novas, dup) == (0, 1), 'a segunda passada não grava linha nova'
    assert fila.enqueue.call_count == 2, 'mas ENTREGA ao índice nas duas'


@pytest.mark.django_db(transaction=True)
def test_fila_fora_do_ar_derruba_a_coleta_em_vez_de_calar():
    """Fila fora ⇒ a edição NÃO foi entregue ao índice ⇒ a coleta falha alto.

    Engolir aqui produziria exatamente o estado que este arquivo existe para
    impedir: `status=ok`, `itens_gravados` cheio, e nada no índice.
    """
    from django.db import transaction

    from diarios.base import id_bloco_impresso, persistir_movimentacoes
    from djen.parser import ParsedItem
    from tribunals.models import Movimentacao, Tribunal

    t, _ = Tribunal.objects.get_or_create(
        sigla='TJSP', defaults={'nome': 'TJSP', 'sigla_djen': 'TJSP'})
    Movimentacao.objects.filter(tribunal=t).delete()
    texto = 'Ato com a fila morta.'
    item = ParsedItem(
        cnj='1008888-22.2025.8.26.0100',
        external_id=id_bloco_impresso('tjsp-dje', 4161, 12, 9, texto=texto),
        data_disponibilizacao=dt.datetime(2025, 3, 12, 3, 0, tzinfo=dt.UTC),
        texto=texto, meio='D',
    )
    with mock.patch('django_rq.get_queue', side_effect=RuntimeError('redis fora')), \
         pytest.raises(RuntimeError), transaction.atomic():
        persistir_movimentacoes([item], t, None)


# ── 2. a régua mede OS DOIS LADOS, com a MESMA janela ────────────────────────
def test_janela_do_dia_e_o_dia_civil_e_fecha_exatamente_24h():
    """O fuso já produziu um alarme falso: comparar "12/03 em UTC" de um lado
    com "12/03 em -03" do outro desloca 3 horas e inventa 1.029 linhas de
    diferença num dia do TJSP. Os dois lados usam ESTES instantes."""
    from diarios.indice import janela_do_dia

    ini, fim = janela_do_dia(dt.date(2025, 3, 12))
    assert fim - ini == dt.timedelta(days=1)
    assert timezone.localtime(ini).hour == 0
    assert timezone.localtime(ini).date() == dt.date(2025, 3, 12)


@pytest.mark.django_db
def test_gate_acusa_diferenca_entre_os_dois_lados():
    from diarios import indice

    with mock.patch.object(indice, 'contar_no_pg', return_value=283_393), \
         mock.patch.object(indice, 'contar_no_es', return_value=255_709):
        m = indice.conferir_dia('TJSP', dt.date(2025, 3, 12))
    assert m['faltando'] == 27_684, 'a régua tem que devolver o buraco medido'


@pytest.mark.django_db
def test_es_mudo_vira_abstencao_e_nunca_zero():
    """Abster > chutar (regra nº 6). Devolver 0 quando o ES não responde faria a
    régua dizer "o dia fechou" sem ter olhado — e, pior, o reparo leria a
    diferença como "o dia inteiro está fora" e re-enfileiraria 283 mil linhas."""
    from diarios import indice

    with mock.patch.object(indice, 'contar_no_pg', return_value=283_393), \
         mock.patch.object(indice, 'contar_no_es', return_value=None):
        m = indice.conferir_dia('TJSP', dt.date(2025, 3, 12))
    assert m['faltando'] is None
    assert m['faltando'] != 0


@pytest.mark.django_db
def test_edicao_sem_conferencia_nao_recebe_carimbo_e_e_retentada():
    """Abstenção não carimba. Se carimbasse, o dia sairia da fila do gate para
    sempre — um "conferido" que nunca foi conferido é pior que nada."""
    from diarios.jobs import conferir_indice
    from diarios.models import EdicaoDiario
    from tribunals.models import Tribunal

    t, _ = Tribunal.objects.get_or_create(
        sigla='TJSP', defaults={'nome': 'TJSP', 'sigla_djen': 'TJSP'})
    e = EdicaoDiario.objects.create(
        fonte='tjsp-dje', chave='4161-12', data=dt.date(2025, 3, 12), tribunal=t,
        status=EdicaoDiario.OK, itens_gravados=29_033,
        coletado_em=timezone.now() - dt.timedelta(hours=1),
    )
    with mock.patch('diarios.indice.contar_no_pg', return_value=283_393), \
         mock.patch('diarios.indice.contar_no_es', return_value=None):
        saida = conferir_indice(carencia_min=0)
    e.refresh_from_db()
    assert saida['abstidos'] == 1
    assert e.indice_conferido_em is None
    assert e.indice_faltando_no_dia is None


@pytest.mark.django_db
def test_gate_carimba_as_8_edicoes_do_dia_com_uma_medicao():
    """Um dia do DJE/TJSP são 8 cadernos. Medir 8 vezes o mesmo (tribunal, dia)
    custaria 8x e devolveria o mesmo número — o erro dos "8 cartões iguais".
    O gate agrupa por (tribunal, dia) e carimba as 8 edições de uma vez."""
    from diarios.jobs import conferir_indice
    from diarios.models import EdicaoDiario
    from tribunals.models import Tribunal

    t, _ = Tribunal.objects.get_or_create(
        sigla='TJSP', defaults={'nome': 'TJSP', 'sigla_djen': 'TJSP'})
    for chave in ('4161-10', '4161-11', '4161-12', '4161-13',
                  '4161-15', '4161-18', '4161-19', '4161-20'):
        EdicaoDiario.objects.create(
            fonte='tjsp-dje', chave=chave, data=dt.date(2025, 3, 12), tribunal=t,
            status=EdicaoDiario.OK, itens_gravados=1,
            coletado_em=timezone.now() - dt.timedelta(hours=1),
        )
    pg = mock.MagicMock(return_value=283_393)
    es = mock.MagicMock(return_value=283_393)
    with mock.patch('diarios.indice.contar_no_pg', pg), \
         mock.patch('diarios.indice.contar_no_es', es):
        saida = conferir_indice(carencia_min=0)
    assert pg.call_count == 1 and es.call_count == 1, 'uma medição, não oito'
    assert saida['conferidos'] == 1 and saida['com_buraco'] == 0
    assert EdicaoDiario.objects.filter(indice_conferido_em__isnull=False).count() == 8
    assert set(EdicaoDiario.objects.values_list('indice_faltando_no_dia', flat=True)) == {0}


@pytest.mark.django_db
def test_gate_repara_o_que_falta_e_registra_o_buraco():
    """Achar o buraco e não consertá-lo seria trocar uma perda silenciosa por
    uma perda documentada. O gate re-enfileira."""
    from diarios.jobs import conferir_indice
    from diarios.models import EdicaoDiario
    from tribunals.models import Tribunal

    t, _ = Tribunal.objects.get_or_create(
        sigla='TJSP', defaults={'nome': 'TJSP', 'sigla_djen': 'TJSP'})
    EdicaoDiario.objects.create(
        fonte='tjsp-dje', chave='4161-12', data=dt.date(2025, 3, 12), tribunal=t,
        status=EdicaoDiario.OK, itens_gravados=29_033,
        coletado_em=timezone.now() - dt.timedelta(hours=1),
    )
    reparo = {'lidos': 283_393, 'faltando': 27_684, 'enfileiradas': 27_684,
              'teto_atingido': False, 'tribunal': 'TJSP', 'dia': '2025-03-12'}
    with mock.patch('diarios.indice.contar_no_pg', return_value=283_393), \
         mock.patch('diarios.indice.contar_no_es', return_value=255_709), \
         mock.patch('diarios.indice.reparar_dia', return_value=reparo) as rep:
        saida = conferir_indice(carencia_min=0)
    assert rep.call_count == 1
    assert saida['com_buraco'] == 1 and saida['reenfileiradas'] == 27_684
    e = EdicaoDiario.objects.get()
    assert e.indice_faltando_no_dia == 27_684
    assert e.indice_reenfileiradas == 27_684


@pytest.mark.django_db
def test_teto_do_reparo_e_erro_e_deixa_o_dia_em_divida():
    """Regra nº 2 do CLAUDE.md: teto é alerta, nunca corte mudo. Bateu o teto,
    o dia NÃO recebe carimbo de conferido e a próxima passada continua."""
    from diarios.jobs import conferir_indice
    from diarios.models import EdicaoDiario
    from tribunals.models import Tribunal

    t, _ = Tribunal.objects.get_or_create(
        sigla='TJSP', defaults={'nome': 'TJSP', 'sigla_djen': 'TJSP'})
    e = EdicaoDiario.objects.create(
        fonte='tjsp-dje', chave='4161-12', data=dt.date(2025, 3, 12), tribunal=t,
        status=EdicaoDiario.OK, itens_gravados=29_033,
        coletado_em=timezone.now() - dt.timedelta(hours=1),
    )
    reparo = {'lidos': 2_000_000, 'faltando': 200_000, 'enfileiradas': 200_000,
              'teto_atingido': True, 'tribunal': 'TJSP', 'dia': '2025-03-12'}
    with mock.patch('diarios.indice.contar_no_pg', return_value=1_000_000), \
         mock.patch('diarios.indice.contar_no_es', return_value=1), \
         mock.patch('diarios.indice.reparar_dia', return_value=reparo):
        conferir_indice(carencia_min=0)
    e.refresh_from_db()
    assert e.indice_conferido_em is None, 'teto atingido não vira "conferido"'


@pytest.mark.django_db
def test_reparo_so_reenfileira_o_que_falta():
    """O reparo é caro (lê os pks do dia no Postgres). Ele não pode re-enfileirar
    o dia inteiro por causa de 62 linhas ausentes — a fila `es_index` já foi a
    1,68 milhão uma vez."""
    from diarios import indice
    from tribunals.models import Movimentacao, Tribunal

    t, _ = Tribunal.objects.get_or_create(
        sigla='TJSP', defaults={'nome': 'TJSP', 'sigla_djen': 'TJSP'})
    Movimentacao.objects.filter(tribunal=t).delete()
    from tribunals.models import Process
    quando = timezone.make_aware(dt.datetime(2025, 3, 12, 0, 0))
    ids = []
    for i in range(5):
        p = Process.objects.create(tribunal=t, numero_cnj=f'100777{i}-22.2025.8.26.0100')
        m = Movimentacao.objects.create(
            processo=p, tribunal=t, external_id=f'tjsp-dje:4161-12-{i}',
            data_disponibilizacao=quando, texto='x')
        ids.append(m.id)

    ausentes = [ids[1], ids[3]]
    with mock.patch.object(indice, '_ausentes_no_bloco', return_value=ausentes), \
         mock.patch.object(indice, '_enfileirar', side_effect=lambda p: len(p)) as enf:
        r = indice.reparar_dia('TJSP', dt.date(2025, 3, 12))
    assert r['lidos'] == 5
    assert r['faltando'] == 2 and r['enfileiradas'] == 2
    assert enf.call_args[0][0] == ausentes


# ── 3. o teto do `_bulk` que virava 45.313 publicações fora do índice ────────
def test_bulk_413_divide_o_lote_em_vez_de_morrer_no_registry():
    """Medido em 21/08/2026: 83 dos 91 jobs mortos no `FailedJobRegistry` eram
    `ApiError(413)` — corpo acima do `http.max_content_length` do ES. Os 91
    referenciavam 45.500 publicações, 45.313 delas FORA do índice, e ninguém
    reprocessa o registry. 413 não é erro de dado, é erro de tamanho: a mesma
    lista dividida ao meio passa."""
    from elasticsearch import ApiError

    from search import jobs as sj

    chamadas = []

    class ESFalso:
        def bulk(self, operations):
            chamadas.append(len(operations) // 2)
            if len(operations) // 2 > 2:
                raise ApiError('413', meta=mock.MagicMock(status=413), body=None)
            return {'errors': False, 'items': []}

    ops = []
    for i in range(8):
        ops.append({'index': {'_index': 'voyager-movimentacoes', '_id': i}})
        ops.append({'body': 'x'})
    with mock.patch.object(sj, 'get_es', return_value=ESFalso()):
        aceitos = sj._enviar_bulk(ops)
    assert aceitos == 8, 'nenhum documento pode ser perdido na divisão'
    assert max(chamadas) == 8 and min(chamadas) == 2, 'dividiu até caber'


def test_bulk_fecha_o_lote_por_bytes_e_nao_so_por_documento():
    """O lote era contado em DOCUMENTOS (500) e o texto de uma publicação não
    tem tamanho fixo. Um caderno com atos longos estourava o teto do ES no meio
    da fila, e o job inteiro morria."""
    from search import jobs as sj

    tamanhos = []

    class ESFalso:
        def bulk(self, operations):
            tamanhos.append(len(operations) // 2)
            return {'errors': False, 'items': []}

    class MovFalsa:
        def __init__(self, i):
            self.id = i

    grande = 'x' * (sj.BULK_MAX_BYTES // 4)
    with mock.patch.object(sj, 'get_es', return_value=ESFalso()), \
         mock.patch.object(sj, 'indices_espelho', return_value=['voyager-movimentacoes']), \
         mock.patch.object(sj, 'movimentacao_to_doc', side_effect=lambda m: {'body': grande}), \
         mock.patch.object(sj.Movimentacao, 'objects') as objs:
        objs.filter.return_value.select_related.return_value = [MovFalsa(i) for i in range(10)]
        sj.indexar_movimentacoes_bulk(list(range(10)))
    assert len(tamanhos) > 1, 'dez documentos de 5 MB não podem sair num _bulk só'
    assert sum(tamanhos) == 10, 'e nenhum pode ficar para trás'


# ── 4. o poller: watermark que anda sem ter enfileirado é perda definitiva ───
@pytest.mark.django_db
def test_watermark_nao_anda_quando_o_enqueue_falha():
    """O keyset só anda pra frente e ninguém revisita. A versão anterior
    engolia a exceção do enqueue, devolvia 0 e avançava o watermark logo em
    seguida — um soluço do Redis apagava do índice, para sempre, todas as
    publicações daquela leva, com um WARNING no log."""
    from django.core.cache import cache

    from search import sync_incremental as si
    from tribunals.models import Movimentacao, Process, Tribunal

    t, _ = Tribunal.objects.get_or_create(
        sigla='TJSP', defaults={'nome': 'TJSP', 'sigla_djen': 'TJSP'})
    p = Process.objects.create(tribunal=t, numero_cnj='1006666-22.2025.8.26.0100')
    m = Movimentacao.objects.create(
        processo=p, tribunal=t, external_id='tjsp-dje:wm-1',
        data_disponibilizacao=timezone.now(), texto='x')
    cache.set(si._WM_MOV_ID, m.id - 1, None)
    try:
        with mock.patch.object(si, '_enfileirar_movs', side_effect=RuntimeError('redis fora')):
            out = si.sync_movimentacoes_novas()
        assert out.get('erro_enqueue') is True
        assert cache.get(si._WM_MOV_ID) == m.id - 1, 'o watermark NÃO pode andar'
    finally:
        cache.delete(si._WM_MOV_ID)


@pytest.mark.django_db
def test_recoletar_zera_o_carimbo_do_gate():
    """Edição recoletada é edição POR CONFERIR.

    O texto pode ter mudado — a troca de extrator da ADR-031 muda a quebra de
    linha e portanto o documento inteiro. Manter o "conferido" antigo seria
    carregar um selo de qualidade emitido sobre outro conteúdo, e o gate nunca
    mais olharia para aquele dia.
    """
    from diarios.models import EdicaoDiario
    from tribunals.models import Tribunal

    t, _ = Tribunal.objects.get_or_create(
        sigla='TJSP', defaults={'nome': 'TJSP', 'sigla_djen': 'TJSP'})
    e = EdicaoDiario.objects.create(
        fonte='tjsp-dje', chave='4161-10', data=dt.date(2025, 3, 12), tribunal=t,
        status=EdicaoDiario.OK, itens_gravados=11)
    e.carimbar_indice(no_es=277_110, faltando=0, reenfileiradas=None)
    assert e.indice_conferido_em is not None

    e.marcar(EdicaoDiario.OK, itens_gravados=11, itens_duplicados=11)
    e.refresh_from_db()
    assert e.indice_conferido_em is None
    assert e.indice_no_es_no_dia is None
    assert e.indice_faltando_no_dia is None
