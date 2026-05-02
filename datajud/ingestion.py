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
"""
from __future__ import annotations

import logging
from typing import Optional

from django.db import transaction
from django.db.models import Count, Max, Min
from django.utils import timezone

from tribunals.models import ClasseJudicial, Movimentacao, Process

from .client import DatajudClient
from .parser import parse_movimentos

logger = logging.getLogger('voyager.datajud.ingestion')

BATCH_SIZE = 500


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
        )
        return {'cnj': processo.numero_cnj, 'novos': 0, 'duplicados': 0,
                'fonte': 'datajud', 'encontrado': False}

    items = parse_movimentos(source)

    # Datajud retorna metadata do processo (classe + assunto) — popula
    # Process.classe_codigo/nome quando o tribunal não tem PJe enricher
    # próprio (ex: TJMG/TJSP onde DJEN também não traz classe).
    classe_obj = source.get('classe') or {}
    classe_codigo_src = str(classe_obj.get('codigo') or '').strip()
    classe_nome_src = (classe_obj.get('nome') or '').strip()[:255]

    if not items:
        now_ts = timezone.now()
        update_kwargs = dict(ultima_sinc_djen_em=now_ts, data_enriquecimento_datajud=now_ts)
        if classe_codigo_src and not processo.classe_codigo:
            update_kwargs['classe_codigo'] = classe_codigo_src
            update_kwargs['classe_nome'] = classe_nome_src
        Process.objects.filter(pk=processo.pk).update(**update_kwargs)
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

        if movs_to_create:
            Movimentacao.objects.bulk_create(
                movs_to_create, ignore_conflicts=True, batch_size=BATCH_SIZE,
            )

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
        )
        if classe_codigo_src and not processo.classe_codigo:
            update_kwargs['classe_codigo'] = classe_codigo_src
            update_kwargs['classe_nome'] = classe_nome_src
        Process.objects.filter(pk=processo.pk).update(**update_kwargs)

    novos = len(movs_to_create)
    duplicados = len(items) - novos
    logger.info('datajud sync %s: novos=%d duplicados=%d',
                processo.numero_cnj, novos, duplicados)

    # Re-classifica após mov nova/atualizada. Só vale a pena recalcular
    # se houve mov nova; movs duplicadas não mudam features.
    if novos > 0 or processo.classificacao_em is None:
        try:
            from tribunals.classificador import classificar_e_persistir
            classificar_e_persistir(processo)
        except Exception as exc:
            logger.warning('falha ao classificar %s: %s', processo.numero_cnj, exc)

    return {
        'cnj': processo.numero_cnj,
        'novos': novos,
        'duplicados': duplicados,
        'fonte': 'datajud',
        'encontrado': True,
    }
