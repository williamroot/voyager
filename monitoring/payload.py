"""Monta o payload de recorte (schema Jusbrasil) pra entrega via webhook."""
from tribunals.models import FonteDiario, ProcessoParte


def build_recorte_payload(mov, target_type: str, target_id: int) -> dict:
    """Monta o payload de recorte no formato Jusbrasil/Digesto."""
    proc = mov.processo
    fd = FonteDiario.objects.filter(tribunal_id=mov.tribunal_id).first()

    # Serializa advs e partes.
    pps = ProcessoParte.objects.filter(processo=proc).select_related('parte')
    advs = []
    partes = []
    for pp in pps:
        if pp.parte.tipo == 'advogado' or 'ADVOGADO' in (pp.papel or ''):
            nome = pp.parte.nome
            if pp.parte.oab:
                nome = f'{nome} (OAB {pp.parte.oab})'
            advs.append(nome)
        partes.append(pp.parte.nome)

    from pdf_storage.cached_docurl import cached_docurl_for

    return {
        'doc_id': mov.id,
        'snippet': (mov.texto or '')[:20000],
        'texto': (mov.texto or '')[:10000],
        'proc': proc.numero_cnj,
        'proc_alt': None,
        'proc_apens': None,
        'advs': ', '.join(advs)[:20000] if advs else None,
        'partes': ', '.join(partes)[:8000] if partes else None,
        'assunto': proc.assunto_nome or None,
        'assunto_norm': mov.assunto_norm or [],
        'detected_at': {'$date': int(mov.inserido_em.timestamp() * 1000)} if mov.inserido_em else None,
        'published_at': {'$date': int(mov.data_disponibilizacao.timestamp() * 1000)} if mov.data_disponibilizacao else None,
        'available_at': {'$date': int(mov.inserido_em.timestamp() * 1000)} if mov.inserido_em else None,
        'docurl': mov.link or None,
        'cached_docurl': cached_docurl_for(mov),
        'recorte_id': mov.id,
        'source_id': fd.source_id if fd else None,
        'periodico_diario_slug': fd.diario_slug if fd else mov.tribunal_id.lower(),
        'periodico_orgao_slug': fd.orgao_slug if fd else mov.tribunal_id.lower(),
        'periodico_caderno_slug': fd.caderno_slug if fd else '',
        'secao_diario': mov.nome_orgao or None,
        'num_pag_original': None,
        'processo_id': proc.id,
        'sections': [],
        # Metadados do monitoramento
        'target_type': target_type,
        'target_id': target_id,
    }