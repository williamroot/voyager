"""Delegates: lógica de cada tool MCP (reusando código existente)."""
import logging

from django.conf import settings

from tribunals.models import (
    FonteDiario,
    Movimentacao,
    Parte,
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


def buscar_entidades(nome=None, documento=None, oab=None, tribunal=None, size=20):
    """Busca processos por entidade (parte/advogado) via nome, documento ou OAB.

    Usa ORM (ProcessoParte + Parte) porque as partes não estão indexadas no ES
    (backfill --sem-partes ainda rodando). Retorna processos + estatísticas.
    """
    if not any([nome, documento, oab]):
        return {'error': 'informe_nome_ou_documento_ou_oab'}
    try:
        partes_qs = Parte.objects.all()
        if documento:
            partes_qs = partes_qs.filter(documento__icontains=documento)
        elif oab:
            partes_qs = partes_qs.filter(oab__icontains=oab)
        elif nome:
            partes_qs = partes_qs.filter(nome__icontains=nome)
        partes_ids = list(partes_qs.values_list('id', flat=True)[:500])
        if not partes_ids:
            return {'processos': [], 'total': 0}
        pps = ProcessoParte.objects.filter(parte_id__in=partes_ids).select_related('processo', 'processo__tribunal', 'parte')
        if tribunal:
            pps = pps.filter(processo__tribunal_id=tribunal)
        # Agrupa por processo.
        procs = {}
        for pp in pps:
            proc = pp.processo
            if proc.id not in procs:
                procs[proc.id] = {
                    'cnj': proc.numero_cnj,
                    'tribunal': proc.tribunal_id,
                    'classe_nome': proc.classe_nome,
                    'assunto_nome': proc.assunto_nome,
                    'valor_causa': float(proc.valor_causa) if proc.valor_causa else None,
                    'total_movimentacoes': proc.total_movimentacoes,
                    'ultima_movimentacao_em': proc.ultima_movimentacao_em.isoformat() if proc.ultima_movimentacao_em else None,
                    'classificacao': proc.classificacao,
                    'partes': [],
                }
            procs[proc.id]['partes'].append({
                'nome': pp.parte.nome,
                'polo': pp.polo,
                'papel': pp.papel,
                'tipo': pp.parte.tipo,
                'oab': pp.parte.oab,
            })
        resultado = list(procs.values())[:size]
        # Estatísticas.
        total = len(procs)
        por_tribunal = {}
        por_classe = {}
        for p in procs.values():
            t = p['tribunal']
            por_tribunal[t] = por_tribunal.get(t, 0) + 1
            c = p['classe_nome'] or 'N/A'
            por_classe[c] = por_classe.get(c, 0) + 1
        return {
            'processos': resultado,
            'total': total,
            'estatisticas': {
                'por_tribunal': dict(sorted(por_tribunal.items(), key=lambda x: -x[1])[:10]),
                'por_classe': dict(sorted(por_classe.items(), key=lambda x: -x[1])[:10]),
            },
        }
    except Exception as e:
        logger.error('MCP buscar_entidades erro: %s', e)
        return {'error': str(e)}


def buscar_valores(tribunal=None, classe=None, valor_min=None, valor_max=None, size=20):
    """Busca processos por faixa de valor da causa."""
    try:
        qs = Process.objects.exclude(valor_causa__isnull=True).select_related('tribunal')
        if tribunal:
            qs = qs.filter(tribunal_id=tribunal)
        if classe:
            qs = qs.filter(classe_nome__icontains=classe)
        if valor_min is not None:
            qs = qs.filter(valor_causa__gte=valor_min)
        if valor_max is not None:
            qs = qs.filter(valor_causa__lte=valor_max)
        qs = qs.order_by('-valor_causa')[:size]
        resultado = [{
            'cnj': p.numero_cnj,
            'tribunal': p.tribunal_id,
            'classe_nome': p.classe_nome,
            'assunto_nome': p.assunto_nome,
            'valor_causa': float(p.valor_causa) if p.valor_causa else None,
            'orgao_julgador': p.orgao_julgador_nome,
            'total_movimentacoes': p.total_movimentacoes,
            'classificacao': p.classificacao,
        } for p in qs]
        # Estatísticas de valor.
        valores = [r['valor_causa'] for r in resultado if r['valor_causa']]
        stats = {}
        if valores:
            stats = {
                'count': len(valores),
                'soma': sum(valores),
                'media': sum(valores) / len(valores),
                'mediana': sorted(valores)[len(valores) // 2],
                'min': min(valores),
                'max': max(valores),
            }
        return {'processos': resultado, 'estatisticas_valores': stats}
    except Exception as e:
        logger.error('MCP buscar_valores erro: %s', e)
        return {'error': str(e)}


def jurimetria(tribunal=None, classe=None, ano=None, metrica='volume'):
    """Estatísticas de jurimetria: volume, distribuição, tendência temporal.

    metrica: 'volume' (processos por tribunal/classe/ano), 'valores' (soma/média),
             'classificacao' (distribuição de leads), 'andamentos' (tipos mais comuns).
    """
    try:
        from django.db.models import Count, Sum, Avg, Q
        from django.db import connection

        if metrica == 'volume':
            qs = Process.objects.all()
            if tribunal:
                qs = qs.filter(tribunal_id=tribunal)
            if classe:
                qs = qs.filter(classe_nome__icontains=classe)
            if ano:
                qs = qs.filter(ano_cnj=ano)
            # Distribuição por tribunal.
            por_tribunal = list(
                qs.values('tribunal_id').annotate(count=Count('id'))
                .order_by('-count')[:20]
            )
            # Distribuição por classe.
            por_classe = list(
                qs.values('classe_nome').annotate(count=Count('id'))
                .order_by('-count')[:20]
            )
            # Distribuição por ano.
            por_ano = list(
                qs.values('ano_cnj').annotate(count=Count('id'))
                .order_by('ano_cnj')[:30]
            )
            total = qs.count()
            return {
                'metrica': 'volume',
                'total_processos': total,
                'por_tribunal': por_tribunal,
                'por_classe': por_classe,
                'por_ano': por_ano,
            }

        elif metrica == 'valores':
            qs = Process.objects.exclude(valor_causa__isnull=True)
            if tribunal:
                qs = qs.filter(tribunal_id=tribunal)
            if classe:
                qs = qs.filter(classe_nome__icontains=classe)
            if ano:
                qs = qs.filter(ano_cnj=ano)
            por_tribunal = list(
                qs.values('tribunal_id').annotate(
                    count=Count('id'),
                    soma=Sum('valor_causa'),
                    media=Avg('valor_causa'),
                ).order_by('-soma')[:20]
            )
            return {
                'metrica': 'valores',
                'por_tribunal': [
                    {
                        'tribunal': r['tribunal_id'],
                        'count': r['count'],
                        'soma': float(r['soma']) if r['soma'] else 0,
                        'media': float(r['media']) if r['media'] else 0,
                    }
                    for r in por_tribunal
                ],
            }

        elif metrica == 'classificacao':
            qs = Process.objects.exclude(classificacao__isnull=True)
            if tribunal:
                qs = qs.filter(tribunal_id=tribunal)
            if ano:
                qs = qs.filter(ano_cnj=ano)
            por_classificacao = list(
                qs.values('classificacao').annotate(count=Count('id'))
                .order_by('-count')
            )
            return {
                'metrica': 'classificacao',
                'distribuicao': por_classificacao,
            }

        elif metrica == 'andamentos':
            # Tipos de andamento mais comuns (top 20).
            qs = Movimentacao.objects.all()
            if tribunal:
                qs = qs.filter(tribunal_id=tribunal)
            if ano:
                qs = qs.filter(data_disponibilizacao__year=ano)
            por_tipo = list(
                qs.values('tipo_comunicacao').annotate(count=Count('id'))
                .order_by('-count')[:20]
            )
            return {
                'metrica': 'andamentos',
                'tipos_mais_comuns': por_tipo,
            }

        return {'error': 'metrica_invalida', 'validas': ['volume', 'valores', 'classificacao', 'andamentos']}
    except Exception as e:
        logger.error('MCP jurimetria erro: %s', e)
        return {'error': str(e)}


def contexto_processo(cnj):
    """Dossiê completo de um processo: dados, partes, valores, movimentações recentes, classificação."""
    try:
        proc = Process.objects.select_related('tribunal', 'classe', 'assunto').get(numero_cnj=cnj)
    except Process.DoesNotExist:
        return {'available': False, 'error': 'processo_nao_encontrado'}

    # Partes.
    pps = ProcessoParte.objects.filter(processo=proc).select_related('parte')
    partes = {
        'ativo': [],
        'passivo': [],
        'outros': [],
    }
    for pp in pps:
        polo = pp.polo or 'outros'
        if polo not in partes:
            polo = 'outros'
        partes[polo].append({
            'nome': pp.parte.nome,
            'documento': pp.parte.documento or None,
            'tipo': pp.parte.tipo,
            'papel': pp.papel,
            'oab': pp.parte.oab or None,
            'representa': None,  # TODO: resolver representa se necessário
        })

    # Últimas 10 movimentações.
    movs = Movimentacao.objects.filter(processo=proc).order_by('-data_disponibilizacao', '-id')[:10]
    ultimas_movs = [{
        'id': m.id,
        'data': m.data_disponibilizacao.isoformat() if m.data_disponibilizacao else None,
        'tipo': m.tipo_comunicacao,
        'orgao': m.nome_orgao,
        'snippet': (m.texto or '')[:300],
        'assunto_norm': m.assunto_norm or [],
    } for m in movs]

    # Timeline (anos com movimentações).
    from django.db.models import Count
    timeline = list(
        Movimentacao.objects.filter(processo=proc)
        .extra(select={'ano': "EXTRACT(year FROM data_disponibilizacao)"})
        .values('ano').annotate(movs=Count('id')).order_by('ano')
    )

    return {
        'processo': {
            'id': proc.id,
            'numero_cnj': proc.numero_cnj,
            'tribunal': proc.tribunal_id,
            'tribunal_nome': proc.tribunal.nome,
            'classe_nome': proc.classe_nome,
            'classe_codigo': proc.classe_codigo,
            'assunto_nome': proc.assunto_nome,
            'assunto_codigo': proc.assunto_codigo,
            'data_autuacao': proc.data_autuacao.isoformat() if proc.data_autuacao else None,
            'valor_causa': float(proc.valor_causa) if proc.valor_causa else None,
            'orgao_julgador': proc.orgao_julgador_nome,
            'juizo': proc.juizo,
            'segredo_justica': proc.segredo_justica,
            'total_movimentacoes': proc.total_movimentacoes,
            'primeira_movimentacao_em': proc.primeira_movimentacao_em.isoformat() if proc.primeira_movimentacao_em else None,
            'ultima_movimentacao_em': proc.ultima_movimentacao_em.isoformat() if proc.ultima_movimentacao_em else None,
            'enriquecimento_status': proc.enriquecimento_status,
            'enriquecido_em': proc.enriquecido_em.isoformat() if proc.enriquecido_em else None,
            'classificacao': proc.classificacao,
            'classificacao_score': proc.classificacao_score,
            'classificacao_versao': proc.classificacao_versao,
            'ano_cnj': proc.ano_cnj,
        },
        'partes': partes,
        'ultimas_movimentacoes': ultimas_movs,
        'timeline': timeline,
    }


def buscar_por_parte(nome, tribunal=None, size=20):
    """Busca processos por nome de parte (pessoa/empresa) + estatísticas da parte."""
    if not nome or len(nome) < 3:
        return {'error': 'nome_curto'}
    try:
        # Busca partes pelo nome.
        partes_qs = Parte.objects.filter(nome__icontains=nome)
        if not partes_qs.exists():
            return {'partes': [], 'processos': [], 'total': 0}
        partes_data = []
        for parte in partes_qs[:10]:
            pps = ProcessoParte.objects.filter(parte=parte).select_related('processo', 'processo__tribunal')
            if tribunal:
                pps = pps.filter(processo__tribunal_id=tribunal)
            procs = []
            polos = {'ativo': 0, 'passivo': 0, 'outros': 0}
            for pp in pps:
                polo = pp.polo or 'outros'
                if polo in polos:
                    polos[polo] += 1
                procs.append({
                    'cnj': pp.processo.numero_cnj,
                    'tribunal': pp.processo.tribunal_id,
                    'classe': pp.processo.classe_nome,
                    'polo': polo,
                    'papel': pp.papel,
                    'valor_causa': float(pp.processo.valor_causa) if pp.processo.valor_causa else None,
                    'classificacao': pp.processo.classificacao,
                })
            partes_data.append({
                'parte_id': parte.id,
                'nome': parte.nome,
                'documento': parte.documento,
                'tipo': parte.tipo,
                'oab': parte.oab,
                'total_processos': parte.total_processos,
                'polos': polos,
                'processos': procs[:size],
            })
        return {'partes': partes_data, 'total': sum(len(p['processos']) for p in partes_data)}
    except Exception as e:
        logger.error('MCP buscar_por_parte erro: %s', e)
        return {'error': str(e)}