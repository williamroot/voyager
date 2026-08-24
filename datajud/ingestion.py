"""Ingestão de movimentações via Datajud (CNJ).

Diferença vs DJEN:
- DJEN é index de publicações em diário oficial — cobre **publicações**
- Datajud é o repositório CNJ do processo — cobre **TODAS** as movs

Conviver: Movimentacao tem `meio` field. DJEN salva com `meio='D'/'E'/etc`,
Datajud salva com `meio='datajud'`. Mesmo Process pode ter movs de ambas
fontes; UI mostra todas na timeline ordenada por data.

Idempotência: external_id = `datajud:<sha1(proc_id+codigo+dataHora)[:24]>`,
único por (tribunal, external_id) garante INSERT seguro com bulk_create
ignore_conflicts.

ENTREGA AO ÍNDICE (24/08/2026): esta porta escreve por DOIS caminhos e, até
esta data, NENHUM dos dois chegava ao Elasticsearch por conta própria —
`bulk_create` não dispara `post_save`, e `.update()` não dispara `post_save`
NEM mexe em `atualizado_em` (o `auto_now` é ignorado por `.update()`, e o
poller `search/sync_incremental.py::sync_processos_atualizados` é keyset por
`atualizado_em`). Ver `_entregar_ao_indice` para os números medidos.
"""
from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Max, Min
from django.utils import timezone

from tribunals.models import ClasseJudicial, Movimentacao, Process

from .client import DatajudClient
from .parser import parse_movimentos

logger = logging.getLogger('voyager.datajud.ingestion')

BATCH_SIZE = 500
#: Tamanho do lote entregue à fila `es_index` — o mesmo que o
#: `search.jobs.indexar_movimentacoes_bulk` consome num `_bulk`.
CHUNK_ES = 500


def _as_dict(x) -> dict:
    """Normaliza um campo do `_source` do Datajud que deveria ser dict mas às
    vezes vem aninhado como lista (lista-de-dict ou lista-de-lista). Desce até
    o primeiro dict; devolve {} se não houver. Evita `AttributeError: 'list'
    object has no attribute 'get'` (visto em ~23% dos failed da fila datajud)."""
    seen = 0
    while isinstance(x, list) and x and seen < 5:
        x = x[0]
        seen += 1
    return x if isinstance(x, dict) else {}


def _meta_updates_from_source(processo: Process, source: dict) -> dict:
    """Extrai metadados do `_source` do Datajud e devolve dict de updates
    para `Process`, respeitando dados já populados (PJe enricher é fonte
    de verdade quando presente — Datajud só preenche lacunas).
    """
    upd: dict = {}

    classe_obj = _as_dict(source.get('classe'))
    classe_codigo = str(classe_obj.get('codigo') or '').strip()
    classe_nome = (classe_obj.get('nome') or '').strip()[:255]
    if classe_codigo and not processo.classe_codigo:
        upd['classe_codigo'] = classe_codigo
        upd['classe_nome'] = classe_nome

    assuntos = source.get('assuntos') or []
    if assuntos and not processo.assunto_codigo:
        a0 = _as_dict(assuntos[0])
        a_cod = str(a0.get('codigo') or '').strip()
        a_nome = (a0.get('nome') or '').strip()[:255]
        if a_cod:
            upd['assunto_codigo'] = a_cod
            upd['assunto_nome'] = a_nome

    orgao = _as_dict(source.get('orgaoJulgador'))
    o_cod = str(orgao.get('codigo') or '').strip()
    o_nome = (orgao.get('nome') or '').strip()[:255]
    if o_cod and not processo.orgao_julgador_codigo:
        upd['orgao_julgador_codigo'] = o_cod
    if o_nome and not processo.orgao_julgador_nome:
        upd['orgao_julgador_nome'] = o_nome

    # Datajud entrega dataAjuizamento como "YYYYMMDDhhmmss"
    dt_ajuiz = source.get('dataAjuizamento')
    if dt_ajuiz and not processo.data_autuacao:
        try:
            upd['data_autuacao'] = datetime.strptime(str(dt_ajuiz)[:8], '%Y%m%d').date()
        except ValueError:
            pass

    # valorCausa pode vir como número ou string; tolera ausência
    vc = source.get('valorCausa')
    if vc is not None and processo.valor_causa is None:
        try:
            upd['valor_causa'] = Decimal(str(vc))
        except (InvalidOperation, ValueError, TypeError):
            pass

    return upd


def _entregar_ao_indice(mov_pks: list[int], processo_pk: int) -> None:
    """Entrega ao índice o que ESTA sincronização escreveu. Propaga erro.

    Por que existe, com o que foi medido em produção em 24/08/2026 (amostra
    aleatória, `_mget` por id — resposta exata, não estimativa):

      MOVIMENTAÇÕES — recorte por janela de `inserido_em` (o índice
      `mov_inserido_tribunal_idx`), filtro `external_id LIKE 'datajud:%'`:

        idade da escrita   linhas na janela   amostra   fora do índice
        0-5 min                     3.088       3.000   3.000 (100,00%)
        5-15 min                    3.927       3.000   1.268 ( 42,27%)
        15-30 min                   4.133       3.000       0
        30-60 min                  15.362       3.000       0

      Vazão da porta: **27.468 linhas/h** na última hora cheia (picos de
      80.000+). O único caminho até o índice era o poller de 10 minutos, e
      isso na hora em que ele estava SAUDÁVEL (atraso medido: 122.604 ids ≈
      1 tick). Com o poller freado por `FILA_ES_ALTA`, desligado por
      `sync_es:off`, ou com a chave de watermark perdida do cache — caso em
      que ele RE-ANCORA NO TOPO — o que ficou abaixo não volta nunca.

      PROCESSO — critério exato: o doc está em dia com esta porta se
      `doc.enriquecido_em >= PG.data_enriquecimento_datajud`.

        janela de `data_enriquecimento_datajud`   amostra   em dia
        30-15 min                                     500   0
        2h-30min                                      500   0
        1d-2h                                         500   8  (1,6%)
        3d-1d                                         500   98 (19,6%)
        30d-7d                                        500   0

      Este segundo buraco não tinha poller NENHUM: `Process.objects.filter(
      pk=...).update(...)` não dispara `post_save` e não mexe em
      `atualizado_em` (o `auto_now` só roda em `Model.save()`), que é
      justamente a chave do keyset de `sync_processos_atualizados`. População:
      22.475.738 processos com `data_enriquecimento_datajud`, 1.703.782 nos
      últimos 30 dias, ~5.000/h.

    Entrega só as movimentações NOVAS, e isso é medição, não descuido: o lote
    inteiro seriam ~73 movimentos por processo (5.000 sincronizações/h x
    73 ≈ 365 mil docs/h contra os 27,5 mil/h que realmente mudaram, 13x).
    Diferente do diário, esta porta não reescreve texto — o `external_id` é
    `sha1(processo, código, dataHora)` e o texto sai do MESMO movimento, então
    re-entregar pré-existente não corrige nada. Quem cobre a entrega que
    falhou é o gate (`datajud/indice.py`), que confere a janela de escrita
    pelos dois lados.

    Propaga a exceção de propósito: fila fora do ar significa que a
    sincronização NÃO foi entregue ao índice. O job morre, o RQ retenta
    (`DATAJUD_RETRY`) e re-sincronizar é idempotente.
    """
    if not getattr(settings, 'DATAJUD_INDEXAR_AO_GRAVAR', True):
        return
    from search.gate import enfileirar_movs, enfileirar_processos

    if mov_pks:
        enfileirar_movs(mov_pks, CHUNK_ES)
    # O doc do processo muda em TODA sincronização, mesmo quando não há
    # movimento novo: `enriquecido_em` do doc é o max() das datas de
    # enriquecimento, e `data_enriquecimento_datajud` acabou de ser gravada.
    enfileirar_processos([processo_pk])


def sync_processo(processo: Process, client: Optional[DatajudClient] = None) -> dict:
    """Busca o processo no Datajud e popula Movimentacao com `meio='datajud'`.

    - 1 request HTTP no Datajud (todos os movimentos vêm em 1 hit)
    - bulk_create idempotente via uniq (tribunal, external_id)
    - Atualiza Process.ultima_sinc_djen_em + total_movimentacoes/datas
    """
    client = client or DatajudClient()
    tribunal = processo.tribunal
    sigla = tribunal.sigla
    source = client.fetch_processo(sigla, processo.numero_cnj)
    if not source:
        # Marca data_enriquecimento_datajud mesmo quando não encontrado:
        # processo passou pelo Datajud, sem hit no índice CNJ. Evita retry
        # infinito a cada bulk re-enqueue.
        now_ts = timezone.now()
        Process.objects.filter(pk=processo.pk).update(
            data_enriquecimento_datajud=now_ts,
            # `atualizado_em` é `auto_now`, e `auto_now` só roda em
            # `Model.save()` — `.update()` o IGNORA. Sem carimbar à mão, a
            # linha muda sem que nada registre que mudou, e o poller
            # `sync_processos_atualizados` (keyset por `atualizado_em`) nunca
            # a enxerga. Ver `_entregar_ao_indice`.
            atualizado_em=now_ts,
        )
        _entregar_ao_indice([], processo.pk)
        return {'cnj': processo.numero_cnj, 'novos': 0, 'duplicados': 0,
                'fonte': 'datajud', 'encontrado': False}

    items = parse_movimentos(source)
    meta_updates = _meta_updates_from_source(processo, source)

    if not items:
        now_ts = timezone.now()
        # `atualizado_em`: ver o comentário do branch "não encontrado" acima.
        update_kwargs = dict(ultima_sinc_djen_em=now_ts, data_enriquecimento_datajud=now_ts,
                             atualizado_em=now_ts)
        update_kwargs.update(meta_updates)
        Process.objects.filter(pk=processo.pk).update(**update_kwargs)
        _entregar_ao_indice([], processo.pk)
        return {'cnj': processo.numero_cnj, 'novos': 0, 'duplicados': 0,
                'fonte': 'datajud', 'encontrado': True}

    ext_ids = [it['external_id'] for it in items]

    with transaction.atomic():
        ja_existem = set(
            Movimentacao.objects
            .filter(tribunal=tribunal, external_id__in=ext_ids)
            .values_list('external_id', flat=True)
        )

        # Catálogo de classes — bulk_create se houver código novo
        novos_classes = {(it['codigo_classe'], it['nome_classe'])
                         for it in items if it.get('codigo_classe') and it.get('nome_classe')}
        if novos_classes:
            ClasseJudicial.objects.bulk_create(
                [ClasseJudicial(codigo=c, nome=n) for c, n in novos_classes],
                ignore_conflicts=True,
                batch_size=BATCH_SIZE,
            )

        movs_to_create = []
        for it in items:
            if it['external_id'] in ja_existem:
                continue
            kwargs = dict(it)
            if kwargs.get('codigo_classe'):
                kwargs['classe_id'] = kwargs['codigo_classe']
            movs_to_create.append(
                Movimentacao(processo_id=processo.pk, tribunal=tribunal, **kwargs)
            )

        novos_ext_ids: list[str] = []
        if movs_to_create:
            Movimentacao.objects.bulk_create(
                movs_to_create, ignore_conflicts=True, batch_size=BATCH_SIZE,
            )
            # `bulk_create(ignore_conflicts=True)` NÃO devolve pk no Postgres,
            # então os pks vêm de um SELECT pelo índice único
            # `uniq_mov_tribunal_extid` — é um index scan por (tribunal,
            # external_id), não uma varredura.
            novos_ext_ids = sorted({m.external_id for m in movs_to_create})
            pks_novos = list(
                Movimentacao.objects
                .filter(tribunal=tribunal, external_id__in=novos_ext_ids)
                .values_list('id', flat=True)
            )
            if len(pks_novos) != len(novos_ext_ids):
                # Gate mecânico e barato: toda linha que este lote diz ter
                # gravado tem que ter pk. Falta aqui é ERRO registrado — nunca
                # um número a menos passando despercebido (regra nº 2).
                logger.error(
                    'datajud %s %s: entrega ao índice INCOMPLETA — %d pks para '
                    '%d external_id gravados',
                    tribunal.sigla, processo.numero_cnj, len(pks_novos),
                    len(novos_ext_ids),
                )
        else:
            pks_novos = []

        # Atualiza resumo do Process (primeira/ultima/total) — única query
        # com aggregates considerando TODAS as fontes (DJEN + Datajud).
        agg = (
            Movimentacao.objects.filter(processo=processo)
            .aggregate(
                primeira=Min('data_disponibilizacao'),
                ultima=Max('data_disponibilizacao'),
                total=Count('id'),
            )
        )
        now_ts = timezone.now()
        update_kwargs = dict(
            primeira_movimentacao_em=agg['primeira'],
            ultima_movimentacao_em=agg['ultima'],
            total_movimentacoes=agg['total'] or 0,
            data_enriquecimento_datajud=now_ts,
            # ultima_sinc_djen_em é compartilhado historicamente; mantém
            # atualizado pra UI/queries antigas continuarem funcionando.
            ultima_sinc_djen_em=now_ts,
            # `atualizado_em`: ver o comentário do branch "não encontrado".
            atualizado_em=now_ts,
        )
        update_kwargs.update(meta_updates)
        Process.objects.filter(pk=processo.pk).update(**update_kwargs)

        # ENTREGA AO ÍNDICE, no COMMIT. Dentro da transação seria entregar pks
        # de linhas que ainda podem sofrer rollback — job enfileirado sobre
        # fantasma. Ver `_entregar_ao_indice` para o que foi medido.
        transaction.on_commit(lambda: _entregar_ao_indice(pks_novos, processo.pk))

    novos = len(movs_to_create)
    duplicados = len(items) - novos
    logger.info('datajud sync %s: novos=%d duplicados=%d',
                processo.numero_cnj, novos, duplicados)

    return {
        'cnj': processo.numero_cnj,
        'novos': novos,
        'duplicados': duplicados,
        'fonte': 'datajud',
        'encontrado': True,
    }
