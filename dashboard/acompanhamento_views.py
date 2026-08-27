"""Acompanhamento — o diário de bordo do produto.

Server-rendered puro (sem fetch): a página é uma lista com filtro, e uma lista
com filtro não precisa de JSON no meio. O estado vive na querystring, então a
visão filtrada é compartilhável por link e o botão voltar funciona — mesmo
padrão da busca.

**Login-gated**: é o histórico interno do produto, com número de acervo e relato
de incidente. Não é página pública.
"""
import datetime

from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_GET

from . import cobertura_nacional
from .models import NotaAcompanhamento

#: janelas prontas. "tudo" existe porque o acervo desta tela é pequeno e o
#: valor dela é justamente ver a série inteira — esconder o começo por padrão
#: seria esconder as descobertas que explicam o resto.
PERIODOS = [
    ('7', 'Últimos 7 dias'),
    ('30', 'Últimos 30 dias'),
    ('90', 'Últimos 90 dias'),
    ('365', 'Último ano'),
    ('tudo', 'Tudo'),
]
PERIODO_PADRAO = '90'


def _janela(periodo: str):
    """(data_inicial, rótulo) da janela pedida. `None` = sem corte."""
    if periodo == 'tudo':
        return None, 'Tudo'
    try:
        dias = int(periodo)
    except (TypeError, ValueError):
        dias = int(PERIODO_PADRAO)
    return timezone.localdate() - datetime.timedelta(days=dias), f'Últimos {dias} dias'


@login_required
@require_GET
def acompanhamento(request):
    """GET /dashboard/acompanhamento/?periodo=&tipo=&area=&q=

    Timeline de descobertas, decisões, incidentes e entregas. O filtro é
    aplicado no banco (a tabela é pequena, mas filtrar em Python envelheceria
    mal) e o resumo por tipo é contado ANTES do filtro de tipo — senão o chip
    "Incidente (3)" viraria "Incidente (3)" mesmo quando só há 3 porque o
    próprio filtro de incidente está ligado, o que é uma contagem circular.
    """
    periodo = request.GET.get('periodo') or PERIODO_PADRAO
    tipo = (request.GET.get('tipo') or '').strip()
    area = (request.GET.get('area') or '').strip()
    busca = (request.GET.get('q') or '').strip()

    desde, rotulo_periodo = _janela(periodo)

    base = NotaAcompanhamento.objects.all()
    if desde:
        base = base.filter(data_evento__gte=desde)
    if area:
        base = base.filter(area=area)
    if busca:
        base = base.filter(titulo__icontains=busca) | base.filter(resumo__icontains=busca)

    # contagem por tipo DENTRO do período, mas ANTES de aplicar o filtro de tipo
    por_tipo = {r['tipo']: r['n'] for r in base.values('tipo').annotate(n=Count('id'))}

    notas = base.filter(tipo=tipo) if tipo else base
    notas = list(notas.select_related('autor'))

    # agrupa por mês pra timeline respirar; o dict do Django template não aceita
    # chave composta, então vai como lista de tuplas
    grupos: list[tuple[str, list]] = []
    for n in notas:
        chave = n.data_evento.strftime('%Y-%m')
        if not grupos or grupos[-1][0] != chave:
            grupos.append((chave, []))
        grupos[-1][1].append(n)

    areas = (NotaAcompanhamento.objects.exclude(area='')
             .values_list('area', flat=True).distinct().order_by('area'))

    return render(request, 'dashboard/acompanhamento.html', {
        'notas': notas,
        'grupos': grupos,
        'total': len(notas),
        'por_tipo': por_tipo,
        'tipos': NotaAcompanhamento.TIPO_CHOICES,
        'periodos': PERIODOS,
        'periodo': periodo,
        'rotulo_periodo': rotulo_periodo,
        'tipo_ativo': tipo,
        'area_ativa': area,
        'areas': list(areas),
        'busca': busca,
        'com_prova': sum(1 for n in notas if n.tem_prova),
        # Só CACHE. Medir aqui custaria 36 s de cardinalidade no caminho da
        # requisição — foi uma medição de rodapé sem teto que derrubou o site
        # em julho (regra nº 7). Sem cache, o card diz que não mediu.
        'cobertura': cobertura_nacional.ler(),
    })


@login_required
@require_GET
def acompanhamento_nota(request, pk: int):
    """Página de uma nota — o relato inteiro, com os números e as referências."""
    nota = get_object_or_404(NotaAcompanhamento.objects.select_related('autor'), pk=pk)
    vizinhas = (NotaAcompanhamento.objects
                .exclude(pk=nota.pk)
                .filter(data_evento__lte=nota.data_evento)[:4])
    return render(request, 'dashboard/acompanhamento_nota.html', {
        'nota': nota, 'vizinhas': vizinhas,
    })
