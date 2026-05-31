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
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

from django.db import transaction
from django.db.models import Count, Max, Min
from django.utils import timezone

from tribunals.models import ClasseJudicial, Movimentacao, Process

from .client import DatajudClient
from .parser import parse_movimentos

logger = logging.getLogger('voyager.datajud.ingestion')

BATCH_SIZE = 500


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
    meta_updates = _meta_updates_from_source(processo, source)

    if not items:
        now_ts = timezone.now()
        update_kwargs = dict(ultima_sinc_djen_em=now_ts, data_enriquecimento_datajud=now_ts)
        update_kwargs.update(meta_updates)
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
        update_kwargs.update(meta_updates)
        Process.objects.filter(pk=processo.pk).update(**update_kwargs)

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
