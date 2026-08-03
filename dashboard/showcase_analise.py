"""Análises salvas da Showcase do Extrator — página compartilhável por UUID + listagem.

A ficha (``resultado``) é renderizada client-side pelo MESMO ``renderFicha`` do
showcase (partials ``_showcase_ficha_{css,js}.html``). Login obrigatório —
compartilhável entre usuários da plataforma, não público.
"""
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.template.defaultfilters import filesizeformat

from .models import ShowcaseAnalise


@login_required
def analise_detalhe(request, aid):
    """Página compartilhável de UMA análise (URL por UUID)."""
    a = get_object_or_404(ShowcaseAnalise.objects.select_related('usuario'), uuid=aid)
    tempos = a.tempos or {}
    tsec = tempos.get('total_s') or (a.elapsed_ms / 1000 if a.elapsed_ms else 0)
    def _mil(n):
        return f'{int(n):,}'.replace(',', '.')
    stats = [
        ('Modelo', a.modelo_label or a.versao or '—'),
        ('Tempo', f'{tsec:.1f}s' if tsec else '—'),
        ('Páginas', _mil(a.paginas) if a.paginas else '—'),
        ('Partes', _mil(a.n_partes)),
        ('Documentos', _mil(a.n_docs)),
        ('Tamanho', filesizeformat(a.tamanho_bytes) if a.tamanho_bytes else '—'),
        ('SHA-256', (a.sha256[:12] + '…') if a.sha256 else '—'),
    ]
    return render(request, 'dashboard/showcase_analise.html', {
        'a': a, 'stats': stats,
        # dict CRU — o filtro json_script serializa (passar json.dumps aqui = double-encode)
        'resultado': a.resultado or {},
    })


@login_required
def analise_lista(request):
    """Lista as análises salvas (todas — compartilhadas entre usuários)."""
    qs = ShowcaseAnalise.objects.select_related('usuario').all()[:300]
    return render(request, 'dashboard/showcase_analises.html', {'analises': qs})
