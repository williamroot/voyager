"""Completude do acervo — a única tela que compara os DOIS lados.

Todas as outras telas medem contagem PRÓPRIA: quantos runs, quantas
movimentações, quantos processos. Isso responde "quanto trabalhamos", não
"quanto do acervo temos" — e a diferença entre as duas perguntas custou a este
projeto três perdas medidas (ver a tabela no CLAUDE.md).

Aqui cada linha tem o nosso número ao lado do número da FONTE, com a data em que
a fonte foi medida. Onde não há gabarito externo, a tela **diz que não há** em
vez de inventar um denominador — abster > chutar.

HOT PATH, ZERO QUERY PESADA. Lê do cache preenchido por `warm_completude`; no
miss mostra o estado "medindo" em vez de segurar a requisição. Uma medição de
rodapé sem `request_timeout` já derrubou o site (worker morto pelo gunicorn em
loop) — ver .ia/OPS.md.
"""
import datetime

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.shortcuts import render
from django.views.decorators.http import require_GET

from . import completude_medicoes as M

CACHE_KEY = 'completude:acervo:v1'


def _idade(quando: datetime.date) -> int:
    return (datetime.date.today() - quando).days


@login_required
@require_GET
def completude(request):
    """GET /dashboard/completude/ — quanto do acervo nacional nós temos."""
    dados = cache.get(CACHE_KEY) or {}
    pendente = not dados

    portas = []
    for p in M.PORTAS:
        vivo = (dados.get('portas') or {}).get(p['slug'], {})
        temos = vivo.get('temos')
        declarado = p['declarado']
        portas.append({
            **p,
            'temos': temos,
            'pct': (100.0 * temos / declarado) if (temos and declarado) else None,
            'lacuna': (declarado - temos) if (temos and declarado) else None,
            'idade_medicao': _idade(p['medido_em']),
            # sem gabarito externo a tela NÃO inventa porcentagem
            'sem_gabarito': declarado is None,
        })

    return render(request, 'dashboard/completude.html', {
        'portas': portas,
        'recuperacao': dados.get('recuperacao') or [],
        'resumo_recup': dados.get('resumo_recup') or {},
        'diarios': dados.get('diarios') or [],
        'medido_em': dados.get('medido_em'),
        'pendente': pendente,
        'fase2': M.FASE_2,
    })
