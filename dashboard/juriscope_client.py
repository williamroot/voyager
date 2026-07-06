"""Cliente read-only pro banco do Juriscope/Falcon (precatórios).

O Juriscope já tem os dados estruturados de precatório (natureza, valor, ente
devedor, ordem cronológica, datas) pra ~2,3M processos. Aqui buscamos por CNJ
(`datamodel_process.numero_autos`) pra enriquecer o dossiê de jurimetria.

Leitura pura, on-demand (1 processo por CNJ). Degrada em silêncio: se o DSN não
está configurado ou o banco está indisponível, devolve {} — o dossiê segue sem
o bloco precatório do Juriscope. Nunca escreve.
"""
from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger('voyager.juriscope')

_SQL = """
    SELECT p.natureza, p.valor_acao, p.valor_acao_corrigido, p.devedora,
           p.ordem_orcamentaria, p.ano_ordem_orcamentaria, p.seq_ordem_orcamentaria,
           p.data_oficio, p.data_protocolo, p.tribunal, p.codigo_requisitorio,
           p.is_extinto, p.sem_expedicao, p.not_found, p.files_downloaded,
           p.cessao_credito, e.name AS ente_nome, e.cnpj AS ente_cnpj,
           e.process_count AS ente_process_count
    FROM datamodel_process p
    LEFT JOIN datamodel_entity e ON e.id = p.entity_id
    WHERE p.numero_autos = %s
    ORDER BY p.updated_at DESC NULLS LAST
    LIMIT 1
"""


def disponivel() -> bool:
    return bool(getattr(settings, 'JURISCOPE_DB_DSN', ''))


def dados_precatorio(cnj: str) -> dict:
    """Busca dados de precatório do Juriscope por CNJ. {} se indisponível/sem match."""
    dsn = getattr(settings, 'JURISCOPE_DB_DSN', '')
    if not dsn or not cnj:
        return {}
    try:
        import psycopg
        with psycopg.connect(dsn, connect_timeout=8) as conn:
            conn.read_only = True
            with conn.cursor() as cur:
                cur.execute("SET statement_timeout='8000'")
                cur.execute(_SQL, [cnj])
                row = cur.fetchone()
                if not row:
                    return {'encontrado': False}
                cols = [d[0] for d in cur.description]
                d = dict(zip(cols, row))
    except Exception as exc:  # noqa: BLE001 — degrada
        logger.warning('juriscope: falha ao buscar %s: %s', cnj, str(exc)[:120])
        return {'erro': str(exc)[:120]}

    d['encontrado'] = True
    d['fonte'] = 'juriscope/falcon (datamodel_process)'
    return d
