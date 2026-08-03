"""Análises salvas da Showcase do Extrator — página compartilhável por UUID + listagem.

A ficha (``resultado``) é renderizada client-side pelo MESMO ``renderFicha`` do
showcase (partials ``_showcase_ficha_{css,js}.html``). Login obrigatório —
compartilhável entre usuários da plataforma, não público.
"""
import os
import shutil
import uuid as _uuid
from pathlib import Path

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.template.defaultfilters import filesizeformat
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import ShowcaseAnalise


@login_required
def analise_detalhe(request, aid):
    """Página compartilhável de UMA análise (URL por UUID)."""
    a = get_object_or_404(ShowcaseAnalise.objects.select_related('usuario'), uuid=aid)
    tempos = a.tempos or {}
    tsec = tempos.get('total_s') or (a.elapsed_ms / 1000 if a.elapsed_ms else 0)
    res = a.resultado or {}

    # valor total a receber (soma dos valor_a_receber das partes) + estágio — o
    # "cartão de visita" da análise mostra o que decide, não só metadados.
    def _num(v):
        try:
            s = str(v).replace('.', '').replace(',', '.')
            import re as _re
            s = _re.sub(r'[^\d.-]', '', s)
            return float(s) if s not in ('', '-', '.') else None
        except Exception:
            return None
    total = 0.0
    tem_valor = False
    for f in (res.get('fichas') or []):
        if not isinstance(f, dict):
            continue
        va = f.get('valor_a_receber')
        v = _num(va.get('valor')) if isinstance(va, dict) else None
        if v is not None:
            total += v
            tem_valor = True
    valor_txt = ('R$ ' + f'{total:,.2f}'.replace(',', '§').replace('.', ',').replace('§', '.')) if tem_valor else '—'
    estagio = ((res.get('estagio') or {}).get('estagio_rotulo')
               or (res.get('estagio') or {}).get('estagio') or '—')

    def _mil(n):
        return f'{int(n):,}'.replace(',', '.')
    stats = [
        ('Valor a receber', valor_txt),
        ('Estágio', estagio),
        ('Modelo', a.modelo_label or a.versao or '—'),
        ('Tempo', f'{tsec:.1f}s' if tsec else '—'),
        ('Páginas', _mil(a.paginas) if a.paginas else '—'),
        ('Tamanho', filesizeformat(a.tamanho_bytes) if a.tamanho_bytes else '—'),
        ('SHA-256', (a.sha256[:12] + '…') if a.sha256 else '—'),
    ]
    return render(request, 'dashboard/showcase_analise.html', {
        'a': a, 'stats': stats,
        # dict CRU — o filtro json_script serializa (passar json.dumps aqui = double-encode)
        'resultado': a.resultado or {},
    })


@csrf_exempt
@login_required
@require_POST
def analise_reprocessar(request, aid):
    """Roda a MESMA análise de novo (mesmo arquivo + mesma versão) — pega melhorias
    do modelo/SDK sem reenviar. Usa o arquivo preservado (``arquivo_path``); se ele
    não existe mais, orienta a reenviar. Devolve ``{job_id}`` pro cliente pollar."""
    import django_rq
    from . import showcase_jobs
    from .showcase_chunks import (JOB_TIMEOUT, JOB_TTL, QUEUE_NAME, UPLOAD_DIR,
                                  set_job_state)

    a = get_object_or_404(ShowcaseAnalise, uuid=aid)
    if not a.arquivo_path:
        return JsonResponse({"erro": "o arquivo original desta análise não foi preservado "
                                     "(análise antiga) — reenvie pela Showcase."}, status=409)
    src = Path(settings.MEDIA_ROOT) / a.arquivo_path
    if not src.exists():
        return JsonResponse({"erro": "o arquivo original não está mais disponível — "
                                     "reenvie pela Showcase."}, status=409)

    # staging: novo upload_id, hardlink/copia o arquivo pro dir de uploads (o worker lê de lá)
    upload_id = str(_uuid.uuid4())
    d = UPLOAD_DIR / upload_id
    d.mkdir(parents=True, exist_ok=True)
    montado = d / (a.arquivo or "autos.pdf")
    try:
        os.link(src, montado)
    except OSError:
        shutil.copy2(src, montado)

    job_id = str(_uuid.uuid4())
    set_job_state(job_id, status="pending", etapa="na fila (reprocessar)", progresso=0,
                  versao=a.versao, arquivo=a.arquivo, upload_id=upload_id)
    try:
        django_rq.get_queue(QUEUE_NAME).enqueue(
            showcase_jobs.extrair_job, job_id=job_id,
            kwargs={"state_job_id": job_id, "versao": a.versao or "v21",
                    "caminho": str(montado), "arquivo": a.arquivo or "autos.pdf",
                    "content_type": a.content_type or "application/pdf",
                    "upload_id": upload_id, "limpar_dir": True,
                    "user_id": request.user.id, "sha256": a.sha256},
            job_timeout=JOB_TIMEOUT, result_ttl=JOB_TTL, failure_ttl=JOB_TTL)
    except Exception as e:  # noqa: BLE001 — Redis/fila fora
        set_job_state(job_id, status="erro", etapa="falha ao enfileirar",
                      erro="fila indisponível — tente de novo")
        return JsonResponse({"erro": "não foi possível enfileirar (fila fora do ar)",
                             "detalhe": str(e)[:120]}, status=503)
    return JsonResponse({"job_id": job_id})


@login_required
def analise_lista(request):
    """Lista as análises salvas (todas — compartilhadas entre usuários).
    Filtro opcional ``?cessao=1`` → só as que têm cessão de crédito."""
    so_cessao = request.GET.get('cessao') in ('1', 'true', 'sim')
    qs = ShowcaseAnalise.objects.select_related('usuario')
    if so_cessao:
        qs = qs.filter(tem_cessao=True)
    total = ShowcaseAnalise.objects.count()
    n_cessao = ShowcaseAnalise.objects.filter(tem_cessao=True).count()
    return render(request, 'dashboard/showcase_analises.html', {
        'analises': qs[:300], 'so_cessao': so_cessao,
        'total': total, 'n_cessao': n_cessao,
    })
