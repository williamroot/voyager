"""Delegates: lógica de cada tool MCP (reusando código existente)."""
import logging

from django.conf import settings

from tribunals.models import (
    FonteDiario,
    Movimentacao,
    Process,
    ProcessoParte,
    Tribunal,
)

logger = logging.getLogger('voyager.mcp')


def buscar_diarios(query, tribunal=None, data_inicio=None, data_fim=None, size=10):
    """Busca textual em diários oficiais via Elasticsearch."""
    try:
        from search.client import get_es, index_name

        es = get_es()
        must = [{'query_string': {'query': query}}]
        filtros = []
        if tribunal:
            filtros.append({'term': {'tribunal': tribunal}})
        if data_inicio:
            filtros.append({'range': {'publish_date': {'gte': data_inicio}}})
        if data_fim:
            filtros.append({'range': {'publish_date': {'lte': data_fim}}})
        body = {
            'query': {'bool': {'must': must, 'filter': filtros}} if filtros else {'query_string': {'query': query}},
            'size': min(size, 100),
        }
        result = es.search(index=index_name('movimentacoes'), body=body)
        hits = result.get('hits', {}).get('hits', [])
        return [{
            'id': h['_source'].get('id') or int(h['_id']),
            'tribunal': h['_source'].get('tribunal'),
            'publish_date': h['_source'].get('publish_date'),
            'body_snippet': (h['_source'].get('body') or '')[:500],
            'docurl': h['_source'].get('docurl'),
            'proc': h['_source'].get('proc'),
            'score': h.get('_score'),
        } for h in hits]
    except Exception as e:
        logger.error('MCP buscar_diarios erro: %s', e)
        return [{'error': str(e)}]


def get_documento(doc_id):
    """Detalhe de uma publicação de diário oficial."""
    try:
        from search.client import get_es, index_name

        es = get_es()
        result = es.get(index=index_name('movimentacoes'), id=doc_id, ignore=[404])
        if not result.get('found'):
            return {'available': False}
        source = result['_source']
        try:
            mov = Movimentacao.objects.get(pk=doc_id)
            from pdf_storage.cached_docurl import cached_docurl_for

            cached = cached_docurl_for(mov)
            if cached:
                source['cached_docurl'] = cached
        except Movimentacao.DoesNotExist:
            pass
        return source
    except Exception as e:
        logger.error('MCP get_documento erro: %s', e)
        return {'error': str(e)}


def get_processo(cnj):
    """Dados completos de um processo."""
    try:
        proc = Process.objects.select_related('tribunal').get(numero_cnj=cnj)
        return {
            'id': proc.id,
            'numero_cnj': proc.numero_cnj,
            'tribunal': proc.tribunal_id,
            'classe_nome': proc.classe_nome,
            'classe_codigo': proc.classe_codigo,
            'assunto_nome': proc.assunto_nome,
            'orgao_julgador_nome': proc.orgao_julgador_nome,
            'valor_causa': float(proc.valor_causa) if proc.valor_causa else None,
            'total_movimentacoes': proc.total_movimentacoes,
            'ultima_movimentacao_em': proc.ultima_movimentacao_em.isoformat() if proc.ultima_movimentacao_em else None,
            'primeira_movimentacao_em': proc.primeira_movimentacao_em.isoformat() if proc.primeira_movimentacao_em else None,
            'enriquecido_em': proc.enriquecido_em.isoformat() if proc.enriquecido_em else None,
            'enriquecimento_status': proc.enriquecimento_status,
            'classificacao': proc.classificacao,
            'classificacao_score': proc.classificacao_score,
        }
    except Process.DoesNotExist:
        return {'available': False}


def list_movimentacoes(cnj, limit=50, offset=0):
    """Movimentações de um processo."""
    try:
        proc = Process.objects.get(numero_cnj=cnj)
        qs = Movimentacao.objects.filter(processo=proc).order_by('-data_disponibilizacao', '-id')
        qs = qs[offset:offset + limit]
        return [{
            'id': m.id,
            'data_disponibilizacao': m.data_disponibilizacao.isoformat() if m.data_disponibilizacao else None,
            'tipo_comunicacao': m.tipo_comunicacao,
            'nome_orgao': m.nome_orgao,
            'snippet': (m.texto or '')[:500],
        } for m in qs]
    except Process.DoesNotExist:
        return []


def get_partes(cnj):
    """Partes de um processo."""
    try:
        proc = Process.objects.get(numero_cnj=cnj)
        pps = ProcessoParte.objects.filter(processo=proc).select_related('parte')
        return [{
            'nome': pp.parte.nome,
            'documento': pp.parte.documento,
            'tipo': pp.parte.tipo,
            'polo': pp.polo,
            'papel': pp.papel,
            'oab': pp.parte.oab,
        } for pp in pps]
    except Process.DoesNotExist:
        return []


def listar_fontes():
    """Diários/tribunais cobertos."""
    fontes = FonteDiario.objects.select_related('tribunal').all()
    return {str(f.source_id): f.nome for f in fontes}


def status_cobertura(area):
    """Cobertura por área."""
    validas = {'estadual', 'trabalhista', 'federal', 'superior'}
    if area not in validas:
        return {'error': 'area_invalida', 'validas': list(validas)}
    tribunais = Tribunal.objects.filter(ativo=True).order_by('sigla')
    values = [['Tribunal', 'Sistema', 'Publicações']]
    for t in tribunais:
        tem_fonte = FonteDiario.objects.filter(tribunal=t).exists()
        values.append([t.sigla, 'DJEN', 'Sim' if tem_fonte else 'Parcial'])
    return {'majorDimension': 'ROWS', 'range': f'{area.capitalize()}!A1:C{len(values)}', 'values': values}


def monitorar_termo(term, tribunais=None):
    """Cria monitoramento de termo (push webhook)."""
    from monitoring.models import MonitoredTerm

    if len(term) < 3:
        return {'error': 'termo_curto'}
    source_ids = []
    if tribunais:
        for sigla in tribunais:
            fd = FonteDiario.objects.filter(tribunal_id=sigla).first()
            if fd:
                source_ids.append(fd.source_id)
    mt = MonitoredTerm.objects.create(term=term, source_ids=source_ids)
    return {'id': mt.pk, 'status': 'criado'}


def monitorar_processo(cnj):
    """Cria monitoramento de processo."""
    from monitoring.models import MonitoredProcess

    if not cnj or len(cnj) < 20:
        return {'error': 'cnj_invalido'}
    mp = MonitoredProcess.objects.create(cnj=cnj)
    return {'id': mp.pk, 'status': 'criado'}


def listar_detections(desde=None, limit=50):
    """Detecções recentes."""
    from monitoring.models import Detection

    qs = Detection.objects.all().order_by('-detected_at')
    if desde:
        qs = qs.filter(detected_at__gte=desde)
    qs = qs[:limit]
    return [{
        'recorte_id': d.movimentacao_id,
        'target_type': d.target_type,
        'target_id': d.target_id,
        'snippet': d.snippet[:500],
        'detected_at': d.detected_at.isoformat(),
        'entregue': d.entregue_em is not None,
    } for d in qs]


def get_pdf(doc_id):
    """URL do PDF armazenado."""
    try:
        mov = Movimentacao.objects.get(pk=doc_id)
        from pdf_storage.cached_docurl import cached_docurl_for

        url = cached_docurl_for(mov)
        if url:
            from pdf_storage.models import PdfArquivo

            pdf = PdfArquivo.objects.get(movimentacao=mov, status='ok')
            return {
                'cached_docurl': url,
                'tamanho_bytes': pdf.tamanho_bytes,
                'baixado_em': pdf.baixado_em.isoformat() if pdf.baixado_em else None,
            }
        return {'available': False}
    except Movimentacao.DoesNotExist:
        return {'available': False, 'error': 'movimentacao_nao_encontrada'}
    except Exception:
        return {'available': False}


def classificacao_lead(cnj):
    """Score/classificação de um processo (gated)."""
    if not settings.MCP_ENABLE_CLASSIFICACAO:
        return {'error': 'classificacao_desativada', 'detail': 'MCP_ENABLE_CLASSIFICACAO=False'}
    try:
        proc = Process.objects.get(numero_cnj=cnj)
        return {
            'classificacao': proc.classificacao,
            'score': proc.classificacao_score,
            'versao': proc.classificacao_versao,
        }
    except Process.DoesNotExist:
        return {'available': False}