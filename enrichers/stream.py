"""Stream protocol pra resultados de enriquecimento.

Workers fazem o trabalho pesado (HTTP no PJe, parse) e publicam um payload
neste stream. Um consumer batch único drena e aplica writes em bulk.

Motivo: ~500 workers fazendo get_or_create/insert nas mesmas tabelas hot
(Parte, ProcessoParte) saturam o lock manager do Postgres (BufferMapping
LWLock). Serializando writes por 1 consumer com bulk operations, eliminamos
contenção e fazemos UPSERT em transação única por batch.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import django_rq

logger = logging.getLogger('voyager.enrichers.stream')

# Stream particionado por hash(process_id) % STREAM_PARTITIONS.
#
# Por que: drainer único faz ~1k/min — gargalo. Múltiplas réplicas
# no MESMO stream deadlocavam em tribunals_processoparte porque XREADGROUP
# distribui entries do mesmo process_id entre consumers que então
# competem em DELETE+INSERT do mesmo proc.
#
# Solução sharded: cada process_id sempre cai no mesmo stream. Um drainer
# por shard processa em série; entre shards roda em paralelo. Sem
# cruzamento → sem deadlock.
#
# STREAM_KEY (sem suffix) é o stream legado — fica vivo enquanto o drainer
# legado esvazia entries publicadas antes do deploy do shard.
STREAM_KEY = 'voyager:enrichment:results'
STREAM_KEY_PREFIX = 'voyager:enrichment:results:'
GROUP_NAME = 'enrichment-drainer'
SCHEMA_VERSION = 1

# Quantas partições de stream. Mudar este valor exige drenar o pipeline
# (workers parados + drainers até stream vazio) — senão events publicados
# sob N partições antigas ficam órfãos. Default 4 cobre 4 drainers
# paralelos sem saturar PG (cada um faz ~1k/min, 4k/min total).
STREAM_PARTITIONS = 4

# Cap de tamanho do JSON serializado por entry. Defende contra DoS:
# worker comprometido / parser quebrado pode publicar payload gigante
# que trava o drainer (1 réplica = single point).
MAX_PAYLOAD_BYTES = 256 * 1024  # 256 KB

# Cap do tamanho do stream principal. Defense-in-depth: se o drainer
# travar, o XADD começa a rotacionar entries antigas em vez de crescer
# sem limite (e potencialmente reter PII em RAM por dias).
STREAM_MAXLEN = 1_000_000

STATUS_OK = 'ok'
STATUS_NAO_ENCONTRADO = 'nao_encontrado'
STATUS_ERRO = 'erro'
VALID_STATUSES = frozenset({STATUS_OK, STATUS_NAO_ENCONTRADO, STATUS_ERRO})


def get_redis():
    """Cliente Redis compartilhado com RQ — mesma conexão que os workers
    já usam, evita configurar 2 caminhos."""
    return django_rq.get_connection('default')


def to_redis_fields(payload: dict) -> dict:
    """Serializa o payload como 1 campo 'data' JSON. Stream entries são
    map<bytes,bytes>; manter 1 chave simplifica forward-compat: schema
    vive dentro do JSON, mudar campos não quebra o transporte."""
    return {'data': json.dumps(payload, ensure_ascii=False, default=str)}


def parse_entry(fields: dict) -> Optional[dict]:
    """Decodifica um entry do stream. Aceita keys/values em str ou bytes
    (varia conforme decode_responses do client). Retorna None se schema
    desconhecido ou payload acima do cap — drainer joga na DLQ.
    """
    raw = fields.get('data') or fields.get(b'data')
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8', errors='replace')
    if len(raw) > MAX_PAYLOAD_BYTES:
        logger.warning('stream entry acima do cap', extra={
            'len': len(raw), 'cap': MAX_PAYLOAD_BYTES,
        })
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning('stream entry com JSON invalido', extra={'len': len(raw)})
        return None
    if payload.get('v') != SCHEMA_VERSION:
        logger.warning('stream entry com versao desconhecida', extra={'v': payload.get('v')})
        return None
    if payload.get('status') not in VALID_STATUSES:
        logger.warning('stream entry com status invalido', extra={'status': payload.get('status')})
        return None
    return payload


def build_ok_payload(*, process_id: int, tribunal: str, numero_cnj: str,
                     scraped_at: str, dados: dict, partes: dict) -> dict:
    """Payload pra resultado bem-sucedido. `dados` e `partes` são os mesmos
    dicts produzidos por _extrair_dados/_extrair_partes — drainer não
    precisa reparsear HTML."""
    return {
        'v': SCHEMA_VERSION,
        'status': STATUS_OK,
        'process_id': process_id,
        'tribunal': tribunal,
        'numero_cnj': numero_cnj,
        'scraped_at': scraped_at,
        'dados': dados,
        'partes': partes,
    }


def build_nao_encontrado_payload(*, process_id: int, tribunal: str,
                                  numero_cnj: str, scraped_at: str) -> dict:
    return {
        'v': SCHEMA_VERSION,
        'status': STATUS_NAO_ENCONTRADO,
        'process_id': process_id,
        'tribunal': tribunal,
        'numero_cnj': numero_cnj,
        'scraped_at': scraped_at,
    }


def build_erro_payload(*, process_id: int, tribunal: str, numero_cnj: str,
                       scraped_at: str, erro: str) -> dict:
    return {
        'v': SCHEMA_VERSION,
        'status': STATUS_ERRO,
        'process_id': process_id,
        'tribunal': tribunal,
        'numero_cnj': numero_cnj,
        'scraped_at': scraped_at,
        'erro': (erro or '')[:1000],
    }


def stream_key_for(process_id: int) -> str:
    """Resolve a partição do stream em que `process_id` deve cair.

    Hash determinístico: o mesmo process_id sempre vai pra mesma partição,
    garantindo que UPDATE+DELETE+INSERT em ProcessoParte do mesmo proc
    sejam serializados pelo mesmo drainer (sem deadlock entre drainers).
    """
    if not isinstance(process_id, int) or process_id < 0:
        # Fallback de segurança — payload com process_id inválido cai
        # na partição 0. parse_entry deveria ter rejeitado antes.
        return f'{STREAM_KEY_PREFIX}0'
    return f'{STREAM_KEY_PREFIX}{process_id % STREAM_PARTITIONS}'


def stream_key_partition(partition: int) -> str:
    """Resolve o stream de uma partição específica (drainer)."""
    if not 0 <= partition < STREAM_PARTITIONS:
        raise ValueError(
            f'partition {partition} fora do range [0, {STREAM_PARTITIONS})'
        )
    return f'{STREAM_KEY_PREFIX}{partition}'


def publish(payload: dict, redis_client=None) -> str:
    """Publica um evento de enriquecimento no stream sharded por
    `process_id`. Retorna o ID gerado pelo Redis (timestamp-seq).
    Workers chamam isso em vez de gravar no DB.

    `MAXLEN ~ STREAM_MAXLEN/N` é defense-in-depth: se um drainer travar,
    o stream daquela partição rotaciona entries antigas em vez de
    crescer sem fim. Distribuímos o cap igualmente — total no agregado
    fica em STREAM_MAXLEN.
    """
    r = redis_client or get_redis()
    # stream_key_for já trata pid inválido (None/string/negativo) → p0.
    key = stream_key_for(payload.get('process_id'))
    per_partition_cap = max(1, STREAM_MAXLEN // STREAM_PARTITIONS)
    msg_id = r.xadd(
        key, to_redis_fields(payload),
        maxlen=per_partition_cap, approximate=True,
    )
    return msg_id.decode() if isinstance(msg_id, bytes) else msg_id


def ensure_consumer_group(redis_client=None, stream_key: str | None = None) -> None:
    """Cria o consumer group se ainda não existir, na partição informada
    (ou em todas as partições + stream legado, se omitido).

    `id='0'` faz o group entregar tudo desde o início — útil pra re-deploy
    do drainer sem perder eventos. MKSTREAM cria o stream caso ainda não
    exista (1ª vez em produção).
    """
    r = redis_client or get_redis()
    keys = (
        [stream_key]
        if stream_key
        else [STREAM_KEY] + [stream_key_partition(i) for i in range(STREAM_PARTITIONS)]
    )
    for key in keys:
        try:
            r.xgroup_create(key, GROUP_NAME, id='0', mkstream=True)
        except Exception as exc:
            if 'BUSYGROUP' in str(exc):
                continue
            raise
