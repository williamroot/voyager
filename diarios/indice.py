"""Gate de completude do ÍNDICE — a edição coletada tem que virar edição BUSCÁVEL.

Por que este arquivo existe, com o número que o motivou (medido em 21/08/2026):

    TJSP, 12/03/2025 — os 8 cadernos do DJE recém-coletados
      Postgres ....... 283.393
      Elasticsearch .. 255.709
      FORA do índice .  27.684   (9,8% do dia)

A causa foi reconstruída à unidade, e NÃO era corrupção nem perda: era ESPERA.
`diarios/base.py::persistir_movimentacoes` grava por `bulk_create`, que não
dispara `post_save`, logo o write-through de `search/signals.py` nunca é
acionado. A única coisa que levava essas linhas ao índice era o poller
`search/sync_incremental.py` (10 em 10 minutos, keyset por `id > watermark`).
No instante da medição, o último tick antes do fim da coleta (21:41:38 -03)
tinha deixado o watermark em `id=1.663.688.937`; a coleta terminou às 21:44:43
com `id` máximo 1.664.109.049. Linhas do diário ACIMA daquele watermark:
**27.619** — mais 65 de resíduo antigo do DJEN no mesmo dia = **27.684**, o
número relatado. O tick seguinte (21:53:01) as enfileirou e o buraco fechou
sozinho. Quem mediu duas vezes com poucos minutos de intervalo viu o mesmo
número porque ENTRE TICKS nada se move: é um poller de 10 minutos, não um
pipeline.

Isso é "run verde, log limpo, número redondo" outra vez: a edição fecha `ok`,
`itens_gravados=29.033`, e NADA no sistema afirma que aquelas 29.033 linhas são
buscáveis. Se o poller estiver desligado (`sync_es:off`), freado
(`FILA_ES_ALTA`), ou se a chave do watermark sumir do cache (ele re-ancora no
TOPO e o que ficou abaixo nunca mais é lido), a edição continua dizendo `ok`.

Este módulo é a régua que faltava, e ela mede OS DOIS LADOS (regra nº 5 do
CLAUDE.md): conta no Postgres e conta no Elasticsearch, com a MESMA janela, e a
diferença é achado — nunca ruído.

Escopo da régua é o (tribunal, DIA), não a edição, e isso é medição, não
preguiça: para recortar UMA edição seria preciso filtrar por prefixo do
`external_id`, e o EXPLAIN em produção mostrou 29,2 s e 65.846 blocos lidos do
disco para um único caderno (o `LIKE` não usa índice; o planner varre a fatia
`(tribunal, data)` inteira e joga 254.360 linhas fora no filtro). Contar o dia
inteiro pelos dois lados custa milissegundos no ES e um índice-scan no PG. Por
isso os campos gravados em `EdicaoDiario` têm `_no_dia` no nome: quem lê não
pode confundir "faltam N no dia" com "faltam N nesta edição".
"""
import datetime as dt
import logging

from django.conf import settings
from django.db import OperationalError, connection, transaction
from django.utils import timezone

from tribunals.models import Movimentacao

logger = logging.getLogger('voyager.diarios.indice')

#: Teto de espera do lado do ES. Régua NUNCA segura escrita (regra nº 7).
ES_TIMEOUT = 60
#: Teto de espera do lado do Postgres, para a CONTAGEM. Medido em produção em
#: 21/08/2026: contar o dia 12/03/2025 do TJSP (277.110 linhas) leva **3,23 s**
#: pelo `mov_tribunal_data_disp_idx`. 120 s é folga de 37x.
PG_TIMEOUT = '120s'
#: Teto de espera da LEITURA DOS PKS, que é outra ordem de grandeza: os mesmos
#: 277.110 ids levaram **45,02 s** (a contagem usa o índice, a leitura do `id`
#: precisa do heap). O banco de produção não tem `statement_timeout` global
#: (medido: `SHOW statement_timeout` = 0), então sem isto aqui a consulta do
#: reparo pode pendurar indefinidamente segurando conexão do pgbouncer.
PG_TIMEOUT_LEITURA = '300s'

#: Quantos ids por pergunta ao ES. Medido: 220.544 ids em 23 perguntas = 1 s
#: (o `terms` sobre o campo `id`, que é `long` e indexado). O default do ES
#: para `index.max_terms_count` é 65.536 — 10.000 fica com folga larga.
BLOCO_TERMS = 10_000
#: Bloco fino do `_mget`, usado só quando um bloco grosso não bate. `_mget` é
#: realtime GET por `_id`: dá a lista EXATA de ausentes, o `terms` só dá o total.
BLOCO_MGET = 1_000
#: Tamanho do lote enfileirado (o mesmo do `search.jobs.indexar_movimentacoes_bulk`).
CHUNK_ENFILEIRA = 500

#: Teto de linhas re-enfileiradas por chamada do reparo. Atingi-lo é ERRO
#: registrado, jamais um `return` discreto (regra nº 2): a edição fica SEM o
#: carimbo de conferida e a próxima passada continua de onde parou.
TETO_REPARO = 200_000

#: Teto de pks lidos do Postgres numa passada de reparo, e ele existe por
#: MEMÓRIA, não por elegância. O reparo lê os ids do dia numa consulta só e
#: fatia em Python — a alternativa (keyset por `id`) obrigaria o planner a
#: ordenar por `id` uma fatia selecionada por `(tribunal, data)`, ou seja a
#: reordenar as 283 mil linhas do dia A CADA bloco. 500 mil ids em lista Python
#: são ~20 MB; o `worker_diarios` tem `mem_limit 1g`. Atingir o teto é ERRO
#: registrado e o dia continua SEM carimbo — dívida visível, não silêncio.
TETO_LEITURA = 500_000


def janela_do_dia(dia: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """O dia CIVIL (America/Sao_Paulo) como um par de instantes absolutos.

    Os dois lados TÊM que usar exatamente estes instantes. O `publish_date` do
    doc do ES é `Movimentacao.data_disponibilizacao.isoformat()`
    (`search/documents.py`), ou seja o MESMO instante — mas escrito com fuso.
    Comparar "o dia 12/03 em UTC" de um lado com "o dia 12/03 em -03" do outro
    produz diferença de 1.029 linhas num dia do TJSP que não é buraco nenhum,
    é o deslocamento das 3 horas. Já custou um alarme falso.
    """
    ini = timezone.make_aware(dt.datetime.combine(dia, dt.time.min))
    return ini, ini + dt.timedelta(days=1)


def _es():
    from search.client import get_es
    return get_es()


def _indice() -> str:
    from search.client import index_name
    return index_name('movimentacoes')


def contar_no_pg(tribunal_id: str, dia: dt.date) -> int | None:
    """Linhas do (tribunal, dia) no Postgres. `None` = não deu pra medir.

    Ancorado em `(tribunal, data_disponibilizacao)` — o `mov_tribunal_data_disp_idx`.
    NUNCA por `external_id__startswith`: isso é Seq Scan em 1,39 bilhão de linhas.
    """
    ini, fim = janela_do_dia(dia)
    try:
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute('SET LOCAL statement_timeout = %s', [PG_TIMEOUT])
            return (Movimentacao.objects
                    .filter(tribunal_id=tribunal_id,
                            data_disponibilizacao__gte=ini,
                            data_disponibilizacao__lt=fim)
                    .count())
    except OperationalError:
        logger.warning('gate índice: contagem no PG estourou %s (%s %s) — abstendo',
                       PG_TIMEOUT, tribunal_id, dia)
        return None


def contar_no_es(tribunal_id: str, dia: dt.date) -> int | None:
    """Docs do (tribunal, dia) no Elasticsearch. `None` = não deu pra medir.

    Abster é obrigatório aqui: devolver 0 porque o ES não respondeu faria a
    régua gritar "o dia inteiro está fora do índice" e o reparo re-enfileirar
    283 mil linhas por nada.
    """
    ini, fim = janela_do_dia(dia)
    try:
        r = _es().count(index=_indice(), query={'bool': {'filter': [
            {'term': {'tribunal': tribunal_id}},
            {'range': {'publish_date': {'gte': ini.isoformat(), 'lt': fim.isoformat()}}},
        ]}}, request_timeout=ES_TIMEOUT)
        return int(r['count'])
    except Exception:      # ES fora não pode derrubar a coleta
        logger.warning('gate índice: contagem no ES falhou (%s %s) — abstendo',
                       tribunal_id, dia, exc_info=True)
        return None


def conferir_dia(tribunal_id: str, dia: dt.date) -> dict:
    """Mede os DOIS lados do (tribunal, dia). Não escreve nada, não repara nada.

    Devolve `{'pg': int|None, 'es': int|None, 'faltando': int|None}`.
    `faltando=None` significa NÃO SEI — nunca 0.
    """
    pg = contar_no_pg(tribunal_id, dia)
    es = contar_no_es(tribunal_id, dia)
    faltando = None if (pg is None or es is None) else max(pg - es, 0)
    return {'pg': pg, 'es': es, 'faltando': faltando,
            'tribunal': tribunal_id, 'dia': dia.isoformat()}


def _ausentes_no_bloco(ids: list[int]) -> list[int]:
    """Quais destes ids NÃO estão no índice. Duas perguntas, da grossa pra fina.

    Primeiro um `terms` count (1 requisição por 10.000 ids): se bater, o bloco
    inteiro está lá e não se paga mais nada. Só o bloco que NÃO bate desce pro
    `_mget`, que devolve o `found` de cada id. É a diferença entre 23 perguntas
    e 221 para um dia de 220 mil linhas.
    """
    es = _es()
    idx = _indice()
    n = es.count(index=idx, query={'terms': {'id': ids}}, request_timeout=ES_TIMEOUT)['count']
    if n == len(ids):
        return []
    faltam: list[int] = []
    for i in range(0, len(ids), BLOCO_MGET):
        fino = ids[i:i + BLOCO_MGET]
        r = es.mget(index=idx, ids=[str(x) for x in fino], source=False,
                    request_timeout=ES_TIMEOUT)
        faltam.extend(int(d['_id']) for d in r['docs'] if not d.get('found'))
    return faltam


def _enfileirar(pks: list[int]) -> int:
    """Enfileira `indexar_movimentacoes_bulk` na fila `es_index`. Propaga erro.

    Propagar é de propósito: fila fora do ar durante um reparo significa que o
    reparo NÃO aconteceu, e engolir a exceção aqui deixaria a edição carimbada
    como conferida sem ter sido consertada — que é o buraco original com um
    carimbo de qualidade em cima.
    """
    import django_rq
    q = django_rq.get_queue('es_index')
    for i in range(0, len(pks), CHUNK_ENFILEIRA):
        q.enqueue('search.jobs.indexar_movimentacoes_bulk', pks[i:i + CHUNK_ENFILEIRA])
    return len(pks)


def reparar_dia(tribunal_id: str, dia: dt.date, teto: int = TETO_REPARO) -> dict:
    """Acha as linhas do (tribunal, dia) que faltam no índice e as re-enfileira.

    Caro por construção (lê os pks do dia no Postgres), então só roda quando o
    `conferir_dia` acusou diferença — e é por isso que o gate barato vem antes.

    A leitura é UMA consulta com teto, fatiada em Python. Keyset por `id` foi
    recusado: a fatia é selecionada por `(tribunal, data_disponibilizacao)` e
    pedir `ORDER BY id` a cada bloco faria o planner reordenar as 283 mil
    linhas do dia 28 vezes. `.iterator()` também não serve — este projeto roda
    com `DISABLE_SERVER_SIDE_CURSORS=True` (pgbouncer), então ele traz tudo do
    mesmo jeito. Quem segura a memória é o `TETO_LEITURA`, e estourá-lo é ERRO
    registrado.
    """
    ini, fim = janela_do_dia(dia)
    # UMA consulta, ancorada em `(tribunal, data_disponibilizacao)` — o
    # `mov_tribunal_data_disp_idx`. Nunca `external_id__startswith`, que é Seq
    # Scan em 1,39 bilhão de linhas, e nunca keyset por `id` (o planner teria
    # de reordenar a fatia do dia a cada bloco). O teto é o que segura a
    # memória; ler `TETO_LEITURA + 1` é como se descobre que ele foi atingido.
    try:
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute('SET LOCAL statement_timeout = %s', [PG_TIMEOUT_LEITURA])
            ids = list(Movimentacao.objects
                       .filter(tribunal_id=tribunal_id,
                               data_disponibilizacao__gte=ini,
                               data_disponibilizacao__lt=fim)
                       .order_by('id')
                       .values_list('id', flat=True)[:TETO_LEITURA + 1])
    except OperationalError:
        # Abstenção, não zero: o dia fica SEM carimbo e volta na próxima passada.
        logger.error('gate índice: leitura dos pks de %s %s estourou %s — reparo '
                     'ADIADO, o dia continua em dívida.',
                     tribunal_id, dia, PG_TIMEOUT_LEITURA)
        return {'lidos': 0, 'faltando': 0, 'enfileiradas': 0, 'teto_atingido': True,
                'abstido': True, 'tribunal': tribunal_id, 'dia': dia.isoformat()}
    teto_leitura = len(ids) > TETO_LEITURA
    if teto_leitura:
        ids = ids[:TETO_LEITURA]

    lidos = enfileiradas = 0
    faltando: list[int] = []
    teto_enfileira = False
    for i in range(0, len(ids), BLOCO_TERMS):
        bloco = ids[i:i + BLOCO_TERMS]
        lidos += len(bloco)
        ausentes = _ausentes_no_bloco(bloco)
        if ausentes:
            faltando.extend(ausentes)
            if len(faltando) >= teto:
                teto_enfileira = True
                break

    if faltando:
        enfileiradas = _enfileirar(faltando)

    if teto_enfileira or teto_leitura:
        # Teto é ALERTA, nunca corte mudo (regra nº 2). A edição NÃO recebe o
        # carimbo de conferida; a próxima passada recomeça e vai adiante.
        logger.error(
            'gate índice: TETO atingido em %s %s — lidos=%d, faltando=%d, '
            'enfileiradas=%d (leitura=%s, reparo=%s). O dia NÃO fechou; '
            'a conferência tem que rodar de novo.',
            tribunal_id, dia, lidos, len(faltando), enfileiradas,
            teto_leitura, teto_enfileira,
        )
    return {'lidos': lidos, 'faltando': len(faltando), 'enfileiradas': enfileiradas,
            'teto_atingido': teto_enfileira or teto_leitura,
            'tribunal': tribunal_id, 'dia': dia.isoformat()}


def gate_ativo() -> bool:
    """`DIARIOS_GATE_INDICE_ENABLED` — desligável sem deploy, ligado por padrão.

    Ligado por padrão de propósito, e FORA do `DIARIOS_SCHEDULER_ENABLED`: hoje
    a coleta de diário acontece por `manage.py diarios_coletar`, à mão, com o
    agendamento desligado. Um gate que só roda quando o agendamento está ligado
    não teria pego o caso que o criou.
    """
    return bool(getattr(settings, 'DIARIOS_GATE_INDICE_ENABLED', True))
