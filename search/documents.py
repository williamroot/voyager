"""Serialização ORM → documento Elasticsearch (formato Jusbrasil/Digesto)."""
from typing import Optional

from tribunals.models import FonteDiario, Movimentacao, Process, ProcessoParte

# Cache em memória das FonteDiario (tabela pequena, ~14 rows, não muda em runtime).
_FONTE_CACHE: dict[str, FonteDiario] = {}


def _get_fonte(tribunal_id: str) -> Optional[FonteDiario]:
    if tribunal_id not in _FONTE_CACHE:
        fd = FonteDiario.objects.filter(tribunal_id=tribunal_id).first()
        _FONTE_CACHE[tribunal_id] = fd
    return _FONTE_CACHE[tribunal_id]


def _serialize_partes(processo: Process) -> tuple[str, str, bool]:
    """Retorna (advs_str, partes_str, tem_ente_publico_passivo).

    tem_ente_publico_passivo = devedor público no polo passivo (RE_ENTE_PUBLICO,
    a mesma regex do Estágio do Crédito) — derivado aqui porque as participações
    JÁ estão carregadas (zero query extra).
    """
    from tribunals.estagio import RE_ENTE_PUBLICO
    # relação reversa: se o queryset veio com prefetch_related('participacoes'),
    # NÃO dispara query por processo — essencial pro reindex em massa (71M) não cair
    # em N+1. No caminho single-doc (write-through) faz 1 query, igual antes.
    pps = processo.participacoes.all()
    advs = []
    partes = []
    tem_ente = False
    for pp in pps:
        if pp.parte.tipo == 'advogado' or 'ADVOGADO' in (pp.papel or ''):
            nome = pp.parte.nome
            if pp.parte.oab:
                nome = f'{nome} (OAB {pp.parte.oab})'
            advs.append(nome)
        partes.append(pp.parte.nome)
        if not tem_ente and pp.polo == 'passivo' and RE_ENTE_PUBLICO.search(pp.parte.nome or ''):
            tem_ente = True
    return ', '.join(advs), ', '.join(partes), tem_ente


def _source_id_for(tribunal_id: str) -> Optional[int]:
    fd = _get_fonte(tribunal_id)
    return fd.source_id if fd else None


def _periodico_slugs(tribunal_id: str) -> dict:
    fd = _get_fonte(tribunal_id)
    if fd:
        return {
            'periodico_diario_slug': fd.diario_slug,
            'periodico_orgao_slug': fd.orgao_slug,
            'periodico_caderno_slug': fd.caderno_slug,
        }
    return {
        'periodico_diario_slug': tribunal_id.lower(),
        'periodico_orgao_slug': tribunal_id.lower(),
        'periodico_caderno_slug': '',
    }


def movimentacao_to_doc(mov: Movimentacao) -> dict:
    """Monta o documento ES no formato Jusbrasil/Digesto."""
    proc = mov.processo
    advs, partes, _ = _serialize_partes(proc)
    source_id = _source_id_for(mov.tribunal_id)
    slugs = _periodico_slugs(mov.tribunal_id)
    return {
        'id': mov.id,
        'tribunal': mov.tribunal_id,
        'source': source_id,
        'publish_date': mov.data_disponibilizacao.isoformat() if mov.data_disponibilizacao else None,
        'available_at': mov.inserido_em.isoformat() if mov.inserido_em else None,
        'detected_at': mov.inserido_em.isoformat() if mov.inserido_em else None,
        'body': mov.texto,
        'docurl': mov.link,
        'cached_docurl': None,  # populado por pdf_storage se disponível
        'proc': proc.numero_cnj,
        'proc_alt': None,
        'proc_apens': None,
        'advs': advs,
        'partes': partes,
        'assunto': proc.assunto_nome or '',
        'assunto_norm': mov.assunto_norm or [],
        'processo_id': proc.id,
        'classe_nome': mov.nome_classe or proc.classe_nome or '',
        'codigo_classe': mov.codigo_classe or proc.classe_codigo or '',
        'secao_diario': mov.nome_orgao,
        'ativo': mov.ativo,
        'recorte_id': mov.id,
        'tipo_comunicacao': mov.tipo_comunicacao,
        'nome_orgao': mov.nome_orgao,
        **slugs,
    }


def movimentacao_to_doc_sem_partes(mov: Movimentacao) -> dict:
    """Versão sem query de ProcessoParte — mais rápida pra backfill inicial.

    As partes podem ser reindexadas depois via reindex sem --sem-partes.
    """
    proc = mov.processo
    source_id = _source_id_for(mov.tribunal_id)
    slugs = _periodico_slugs(mov.tribunal_id)
    return {
        'id': mov.id,
        'tribunal': mov.tribunal_id,
        'source': source_id,
        'publish_date': mov.data_disponibilizacao.isoformat() if mov.data_disponibilizacao else None,
        'available_at': mov.inserido_em.isoformat() if mov.inserido_em else None,
        'detected_at': mov.inserido_em.isoformat() if mov.inserido_em else None,
        'body': mov.texto,
        'docurl': mov.link,
        'cached_docurl': None,
        'proc': proc.numero_cnj,
        'proc_alt': None,
        'proc_apens': None,
        'advs': '',
        'partes': '',
        'assunto': proc.assunto_nome or '',
        'assunto_norm': mov.assunto_norm or [],
        'processo_id': proc.id,
        'classe_nome': mov.nome_classe or proc.classe_nome or '',
        'codigo_classe': mov.codigo_classe or proc.classe_codigo or '',
        'secao_diario': mov.nome_orgao,
        'ativo': mov.ativo,
        'recorte_id': mov.id,
        'tipo_comunicacao': mov.tipo_comunicacao,
        'nome_orgao': mov.nome_orgao,
        **slugs,
    }


def processo_to_doc(proc: Process) -> dict:
    """Monta o documento ES do processo (index processos).

    Schema de NEGÓCIO completo — todo campo agregável que o comercial/leads/
    jurimetria precisa mora aqui (ver PROC_MAPPING). Não adicionar campo sem
    atualizar o mapping + reindexar.
    """
    from .geo import uf_do_tribunal
    advs, partes, tem_ente = _serialize_partes(proc)
    source_id = _source_id_for(proc.tribunal_id)
    # "validado" = passou por QUALQUER enriquecimento (tribunal/djen/datajud)
    enriquecido_em = (proc.data_enriquecimento_datajud or proc.data_enriquecimento_tribunal
                      or proc.data_enriquecimento_djen or proc.enriquecido_em)
    return {
        'id': proc.id,
        'tribunal': proc.tribunal_id,
        'uf': uf_do_tribunal(proc.tribunal_id),            # mapa comercial: agrega por estado
        'tem_sinal_precatorio': proc.tem_sinal_precatorio,  # Fase 0: possível precatório (sinal DJEN)
        'source': source_id,
        'proc': proc.numero_cnj,
        'classe_nome': proc.classe_nome or '',
        'codigo_classe': proc.classe_codigo or '',
        'assunto': proc.assunto_nome or '',
        'assunto_codigo': proc.assunto_codigo or '',
        'advs': advs,
        'partes': partes,
        'orgao_julgador': proc.orgao_julgador_nome or '',
        'valor_causa': float(proc.valor_causa) if proc.valor_causa else None,
        'ano_cnj': proc.ano_cnj,
        'data_autuacao': proc.data_autuacao.isoformat() if proc.data_autuacao else None,
        'primeira_movimentacao_em': proc.primeira_movimentacao_em.isoformat() if proc.primeira_movimentacao_em else None,
        'total_movimentacoes': proc.total_movimentacoes,
        'ultima_movimentacao_em': proc.ultima_movimentacao_em.isoformat() if proc.ultima_movimentacao_em else None,
        'inserido_em': proc.inserido_em.isoformat() if proc.inserido_em else None,
        'segredo_justica': proc.segredo_justica,
        'classificacao': proc.classificacao or '',
        'classificacao_score': proc.classificacao_score,
        'classificacao_em': proc.classificacao_em.isoformat() if proc.classificacao_em else None,
        'enriquecido': enriquecido_em is not None,
        'enriquecido_em': enriquecido_em.isoformat() if enriquecido_em else None,
        'tem_ente_publico_passivo': tem_ente,
    }