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
    ORDER BY p.valor_acao DESC NULLS LAST, p.updated_at DESC NULLS LAST
"""

# Um mesmo numero_autos tem N linhas (1 por precatório/RPV do processo). Campos
# escalares (natureza/ente/ordem) vêm da linha mais rica; VALORES são SOMADOS
# (o processo tem vários ofícios). Pegar só 1 linha (LIMIT 1) pegava a casca vazia.
_ESCALARES = ('natureza', 'devedora', 'ordem_orcamentaria', 'ano_ordem_orcamentaria',
              'seq_ordem_orcamentaria', 'data_oficio', 'data_protocolo', 'tribunal',
              'codigo_requisitorio', 'ente_nome', 'ente_cnpj', 'ente_process_count')


def _brl(v) -> str | None:
    """Decimal/float → '1.234.567,89' (pt-BR). None passa direto."""
    if v is None:
        return None
    return f'{v:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')


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
                rows = cur.fetchall()
                if not rows:
                    return {'encontrado': False}
                cols = [d[0] for d in cur.description]
                linhas = [dict(zip(cols, r)) for r in rows]
    except Exception as exc:  # noqa: BLE001 — degrada
        logger.warning('juriscope: falha ao buscar %s: %s', cnj, str(exc)[:120])
        return {'erro': str(exc)[:120]}

    # Agrega as N linhas (1 por precatório/RPV): SOMA os valores; escalares vêm da
    # 1ª linha que os tiver (ordenadas por valor DESC → a mais rica primeiro).
    def _soma(campo):
        vs = [l.get(campo) for l in linhas if l.get(campo) is not None]
        return sum(vs) if vs else None

    def _primeiro(campo):
        return next((l.get(campo) for l in linhas if l.get(campo) is not None), None)

    d = {campo: _primeiro(campo) for campo in _ESCALARES}
    d['valor_acao'] = _soma('valor_acao')
    d['valor_acao_corrigido'] = _soma('valor_acao_corrigido')
    d['valores_individuais'] = [l['valor_acao'] for l in linhas if l.get('valor_acao') is not None]
    d['n_precatorios'] = len(d['valores_individuais'])
    # strings prontas em pt-BR (o template não tem filtro de moeda)
    d['valor_acao_fmt'] = _brl(d['valor_acao'])
    d['valor_acao_corrigido_fmt'] = _brl(d['valor_acao_corrigido'])
    d['valores_individuais_fmt'] = [_brl(v) for v in d['valores_individuais']]
    d['n_requisitorios'] = len(linhas)
    d['files_downloaded'] = any(l.get('files_downloaded') for l in linhas)
    d['cessao_credito'] = any(l.get('cessao_credito') for l in linhas)
    d['is_extinto'] = all(l.get('is_extinto') for l in linhas)
    d['sem_expedicao'] = all(l.get('sem_expedicao') for l in linhas)
    d['not_found'] = all(l.get('not_found') for l in linhas)
    d['encontrado'] = True
    d['fonte'] = 'juriscope/falcon (datamodel_process, agregado)'
    return d
