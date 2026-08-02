"""Jobs RQ de monitoramento (fila monitoring)."""
import hashlib
import hmac
import json
import logging
from datetime import timedelta

import django_rq
import requests
from django.utils import timezone

from tribunals.models import Movimentacao

from .models import (
    Detection, MonitoredPerson, MonitoredProcess, MonitoredTerm, WebhookConfig,
)
from .payload import build_recorte_payload

logger = logging.getLogger('voyager.monitoring.jobs')

RETRY_LIMIT = 5
WEBHOOK_TIMEOUT = 30


def varredura_diaria():
    """Varredura diária: pra cada target ativo, busca ES nos últimos 2 dias
    e cria Detections. Idempotente via constraint unique."""
    agora = timezone.now()
    desde = agora - timedelta(days=2)

    total = 0
    # 1. Termos
    for term in MonitoredTerm.objects.filter(is_active=True):
        total += _varredura_termo(term, desde)

    # 2. Pessoas (por nome/documento)
    for person in MonitoredPerson.objects.filter(is_active=True, is_monitored_diario=True):
        total += _varredura_person(person, desde)

    # 3. Processos (por CNJ)
    for proc in MonitoredProcess.objects.filter(is_active=True):
        total += _varredura_proc(proc, desde)

    logger.info('Varredura diária: %d detections criadas', total)
    return total


def _varredura_termo(term: MonitoredTerm, desde) -> int:
    """Busca ES por termo nos últimos dias e cria Detections."""
    try:
        from search.client import get_es, index_name
        es = get_es()
        body = {
            'query': {
                'bool': {
                    'must': [{'query_string': {'query': term.term}}],
                    'filter': [{'range': {'publish_date': {'gte': desde.isoformat()}}}],
                }
            },
            'size': 1000,
        }
        if term.source_ids:
            body['query']['bool']['filter'].append({'terms': {'source': term.source_ids}})

        result = es.search(index=index_name('movimentacoes'), body=body)
        return _criar_detections(result, 'term', term.pk)
    except Exception as e:
        logger.error('Erro na varredura do termo %s: %s', term.pk, e)
        return 0


def _varredura_person(person: MonitoredPerson, desde) -> int:
    """Busca ES por nome/documento nos últimos dias."""
    try:
        from search.client import get_es, index_name
        es = get_es()
        # Busca por nome, documento ou OAB no campo partes/advs.
        query_parts = []
        if person.nome:
            query_parts.append(f'partes:"{person.nome}"')
        if person.documento:
            query_parts.append(f'partes:"{person.documento}"')
        if person.oab:
            query_parts.append(f'advs:"{person.oab}"')
        if not query_parts:
            return 0

        body = {
            'query': {
                'bool': {
                    'must': [{'query_string': {'query': ' OR '.join(query_parts)}}],
                    'filter': [{'range': {'publish_date': {'gte': desde.isoformat()}}}],
                }
            },
            'size': 1000,
        }
        result = es.search(index=index_name('movimentacoes'), body=body)
        return _criar_detections(result, 'person', person.pk)
    except Exception as e:
        logger.error('Erro na varredura da person %s: %s', person.pk, e)
        return 0


def _varredura_proc(proc: MonitoredProcess, desde) -> int:
    """Busca movimentações por CNJ nos últimos dias."""
    try:
        qs = Movimentacao.objects.filter(
            processo__numero_cnj=proc.cnj,
            data_disponibilizacao__gte=desde,
        ).select_related('processo', 'tribunal')
        count = 0
        for mov in qs:
            _, created = _criar_detection('proc', proc.pk, mov)
            if created:
                count += 1
        return count
    except Exception as e:
        logger.error('Erro na varredura do proc %s: %s', proc.pk, e)
        return 0


def _criar_detections(es_result, target_type: str, target_id: int) -> int:
    """Cria Detections a partir do resultado ES. Idempotente via constraint."""
    count = 0
    for hit in es_result.get('hits', {}).get('hits', []):
        mov_id = hit['_source'].get('id') or int(hit['_id'])
        try:
            mov = Movimentacao.objects.get(pk=mov_id)
            det, created = _criar_detection(target_type, target_id, mov)
            if created:
                count += 1
        except Movimentacao.DoesNotExist:
            continue
    return count


def _criar_detection(target_type: str, target_id: int, mov) -> tuple:
    """Cria uma Detection se não existe. Retorna (detection, created)."""
    snippet = (mov.texto or '')[:2000]
    det, created = Detection.objects.get_or_create(
        target_type=target_type,
        target_id=target_id,
        movimentacao=mov,
        defaults={'snippet': snippet},
    )
    if created:
        # Enfileira entrega.
        q = django_rq.get_queue('monitoring')
        q.enqueue('monitoring.jobs.entregar_detection', det.pk)
    return det, created


def entregar_detection(det_pk: int):
    """Entrega uma Detection via webhook (HMAC-signed). Retry 5x com backoff."""
    try:
        det = Detection.objects.select_related('movimentacao').get(pk=det_pk)
    except Detection.DoesNotExist:
        logger.warning('Detection %s não encontrada', det_pk)
        return

    # Busca webhooks ativos do cliente associado.
    # Se não há ApiClient associado (monitoramento global), pega todos ativos.
    webhooks = WebhookConfig.objects.filter(is_active=True)
    if not webhooks.exists():
        logger.debug('Sem webhooks ativos pra Detection %s', det_pk)
        return

    payload = build_recorte_payload(det.movimentacao, det.target_type, det.target_id)
    payload_json = json.dumps(payload, default=str)

    for wh in webhooks:
        if det.target_type not in (wh.evento_types or ['term', 'person', 'proc']):
            continue
        _tentar_entrega(wh, det, payload_json)


def _tentar_entrega(wh: WebhookConfig, det: Detection, payload_json: str):
    """Tenta entregar com retry. HMAC signing."""
    signature = hmac.new(
        wh.secret.encode(), payload_json.encode(), hashlib.sha256,
    ).hexdigest()

    for tentativa in range(1, RETRY_LIMIT + 1):
        try:
            resp = requests.post(
                wh.url,
                data=payload_json,
                headers={
                    'Content-Type': 'application/json',
                    'X-Voyager-Signature': f'sha256={signature}',
                },
                timeout=WEBHOOK_TIMEOUT,
            )
            if 200 <= resp.status_code < 300:
                det.entregue_em = timezone.now()
                det.save(update_fields=['entregue_em'])
                logger.info('Detection %s entregue (tentativa %d)', det.pk, tentativa)
                return
            logger.warning('Webhook %s retornou %s (tentativa %d)', wh.url, resp.status_code, tentativa)
        except Exception as e:
            logger.warning('Erro entregando webhook (tentativa %d): %s', tentativa, e)

    # Esgotou retries.
    det.erro_entrega = f'Esgotado {RETRY_LIMIT} tentativas'
    det.save(update_fields=['erro_entrega'])
    logger.error('Detection %s não entregue após %d tentativas', det.pk, RETRY_LIMIT)