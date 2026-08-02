"""API CRUD de monitoramento (Jusbrasil/Digesto-compat)."""
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from api.permissions import HasAPIKeyOrBearer
from monitoring.models import (
    Detection, MonitoredPerson, MonitoredProcess, MonitoredTerm, WebhookConfig,
)


# --- MonitoredTerm ---

@api_view(['POST', 'GET'])
@permission_classes([HasAPIKeyOrBearer])
def monitored_term_list(request):
    if request.method == 'POST':
        term = request.data.get('term', '')
        source_ids = request.data.get('source_ids', [])
        if len(term) < 3:
            return Response({'error': 'termo_curto'}, status=status.HTTP_400_BAD_REQUEST)
        for ch in (',', '!', '='):
            if ch in term:
                return Response({'error': f'caractere_{ch}_proibido'}, status=status.HTTP_400_BAD_REQUEST)
        mt = MonitoredTerm.objects.create(term=term, source_ids=source_ids)
        return Response({'id': mt.pk, 'term': mt.term, 'is_active': mt.is_active}, status=status.HTTP_201_CREATED)

    terms = MonitoredTerm.objects.filter(is_active=True)
    return Response([{
        'id': t.pk, 'term': t.term, 'source_ids': t.source_ids,
        'is_active': t.is_active, 'created_at': t.created_at.isoformat(),
    } for t in terms])


@api_view(['GET', 'PATCH', 'DELETE'])
@permission_classes([HasAPIKeyOrBearer])
def monitored_term_detail(request, pk: int):
    try:
        mt = MonitoredTerm.objects.get(pk=pk)
    except MonitoredTerm.DoesNotExist:
        return Response({'error': 'nao_encontrado'}, status=status.HTTP_404_NOT_FOUND)

    if request.method == 'GET':
        return Response({'id': mt.pk, 'term': mt.term, 'source_ids': mt.source_ids, 'is_active': mt.is_active})
    if request.method == 'PATCH':
        if 'term' in request.data:
            mt.term = request.data['term']
        if 'source_ids' in request.data:
            mt.source_ids = request.data['source_ids']
        if 'is_active' in request.data:
            mt.is_active = request.data['is_active']
        mt.save()
        return Response({'id': mt.pk, 'term': mt.term, 'is_active': mt.is_active})
    if request.method == 'DELETE':
        mt.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# --- MonitoredPerson ---

@api_view(['POST', 'GET'])
@permission_classes([HasAPIKeyOrBearer])
def monitored_person_list(request):
    if request.method == 'POST':
        mp = MonitoredPerson.objects.create(
            nome=request.data.get('nome', ''),
            documento=request.data.get('documento', ''),
            oab=request.data.get('oab', ''),
            tribunais=request.data.get('tribunais', []),
            is_advogado=request.data.get('is_advogado', False),
        )
        return Response({'id': mp.pk, 'nome': mp.nome, 'is_active': mp.is_active}, status=status.HTTP_201_CREATED)

    persons = MonitoredPerson.objects.filter(is_active=True)
    return Response([{
        'id': p.pk, 'nome': p.nome, 'documento': p.documento, 'oab': p.oab,
        'tribunais': p.tribunais, 'is_active': p.is_active,
    } for p in persons])


# --- MonitoredProcess ---

@api_view(['POST', 'GET'])
@permission_classes([HasAPIKeyOrBearer])
def monitored_process_list(request):
    if request.method == 'POST':
        cnj = request.data.get('cnj', '')
        if not cnj or len(cnj) < 20:
            return Response({'error': 'cnj_invalido'}, status=status.HTTP_400_BAD_REQUEST)
        mp = MonitoredProcess.objects.create(
            cnj=cnj,
            tribunais=request.data.get('tribunais', []),
        )
        return Response({'id': mp.pk, 'cnj': mp.cnj, 'is_active': mp.is_active}, status=status.HTTP_201_CREATED)

    procs = MonitoredProcess.objects.filter(is_active=True)
    return Response([{
        'id': p.pk, 'cnj': p.cnj, 'tribunais': p.tribunais, 'is_active': p.is_active,
    } for p in procs])


# --- Detections ---

@api_view(['GET'])
@permission_classes([HasAPIKeyOrBearer])
def detection_list(request):
    desde = request.GET.get('desde')
    limit = int(request.GET.get('limit', 50))
    qs = Detection.objects.all().order_by('-detected_at')
    if desde:
        qs = qs.filter(detected_at__gte=desde)
    qs = qs[:limit]
    return Response([{
        'id': d.pk, 'target_type': d.target_type, 'target_id': d.target_id,
        'movimentacao_id': d.movimentacao_id, 'snippet': d.snippet[:500],
        'detected_at': d.detected_at.isoformat(),
        'entregue_em': d.entregue_em.isoformat() if d.entregue_em else None,
    } for d in qs])