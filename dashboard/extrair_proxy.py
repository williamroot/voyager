"""Proxy fino da tela de extração de autos (Zordon) para dentro do Voyager.

A UI e o pipeline vivem no Zordon (GPU, modelos, doc_classificador). O navegador
do usuário nunca fala direto com o Zordon — todas as telas passam por aqui. Os
paths são espelhados 1:1 (``/extrair``, ``/api/extrair/...``) para que os links
absolutos do HTML servido pelo Zordon resolvam na origem do Voyager.

Somente autenticado. POST é ``csrf_exempt`` porque o form vem do Zordon (sem token
Django) — a proteção efetiva é o ``login_required`` + rede interna ao Zordon.
"""
from __future__ import annotations

import requests
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.views.decorators.csrf import csrf_exempt

_ESTAGIO = {
    "PRECATORIO": ("Precatório expedido", "#22c55e"),
    "PRE_PRECATORIO": ("Pré-precatório (aguardando ofício)", "#f59e0b"),
    "DIREITO_CREDITORIO": ("Direito creditório (em formação)", "#3b82f6"),
}


def _base() -> str:
    return getattr(settings, "ZORDON_URL", "").rstrip("/")


def _resp(r: requests.Response, default_ct: str) -> HttpResponse:
    return HttpResponse(r.content, status=r.status_code,
                        content_type=r.headers.get("content-type", default_ct))


def _sem_zordon() -> HttpResponse:
    return HttpResponse("<h2>Extração indisponível</h2><p>ZORDON_URL não configurado.</p>",
                        status=503, content_type="text/html; charset=utf-8")


@csrf_exempt
@login_required
def extrair(request):
    base = _base()
    if not base:
        return _sem_zordon()
    if request.method == "POST" and request.FILES.get("arquivo"):
        up = request.FILES["arquivo"]
        files = {"arquivo": (up.name, up.file, up.content_type or "application/octet-stream")}
        data = {k: v for k, v in request.POST.items()}
        try:
            r = requests.post(f"{base}/extrair", data=data, files=files,
                              timeout=1200, allow_redirects=False)
        except requests.RequestException:
            return HttpResponse("<h2>Falha ao enviar ao Zordon</h2>", status=502)
        if r.status_code in (301, 302, 303):
            return HttpResponseRedirect(r.headers.get("location", "/extrair"))
        return _resp(r, "text/html; charset=utf-8")
    try:
        r = requests.get(f"{base}/extrair", timeout=30)
    except requests.RequestException:
        return _sem_zordon()
    return _resp(r, "text/html; charset=utf-8")


@login_required
def status(request, job_id):
    base = _base()
    if not base:
        return _sem_zordon()
    try:
        r = requests.get(f"{base}/extrair/{job_id}", timeout=30)
    except requests.RequestException:
        return _sem_zordon()
    return _resp(r, "text/html; charset=utf-8")


@login_required
def api_status(request, job_id):
    try:
        r = requests.get(f"{_base()}/api/extrair/{job_id}", timeout=30)
    except requests.RequestException:
        return HttpResponse('{"erro":"zordon_off"}', status=502, content_type="application/json")
    return _resp(r, "application/json")


@login_required
def api_modelos(request):
    try:
        r = requests.get(f"{_base()}/api/extrair/modelos", timeout=30)
    except requests.RequestException:
        return HttpResponse('{"local":[],"cloud":[]}', status=200, content_type="application/json")
    return _resp(r, "application/json")


@login_required
def arquivo(request, job_id):
    """Stream do arquivo original (ZIP/PDF) que veio do Zordon."""
    try:
        r = requests.get(f"{_base()}/extrair/{job_id}/arquivo", timeout=300, stream=True)
    except requests.RequestException:
        return HttpResponse("arquivo indisponível", status=502)
    resp = HttpResponse(r.raw.read() if r.status_code == 200 else r.content,
                        status=r.status_code,
                        content_type=r.headers.get("content-type", "application/octet-stream"))
    if r.headers.get("content-disposition"):
        resp["Content-Disposition"] = r.headers["content-disposition"]
    return resp


@csrf_exempt
@login_required
def chat(request, job_id):
    """Proxy SSE do chat: repassa o POST e faz STREAM do text/event-stream do Zordon
    token-a-token (sem bufferizar) → o navegador recebe ao vivo."""
    from django.http import StreamingHttpResponse
    base = _base()
    if not base:
        return HttpResponse("indisponível", status=503)

    def gen():
        try:
            with requests.post(f"{base}/extrair/{job_id}/chat", data=request.body,
                               headers={"Content-Type": "application/json"},
                               stream=True, timeout=300) as r:
                for chunk in r.iter_content(chunk_size=None):
                    if chunk:
                        yield chunk
        except requests.RequestException:
            yield b'data: {"tipo": "erro", "erro": "conexao"}\n\n'
    resp = StreamingHttpResponse(gen(), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    return resp


@login_required
def jurimetria(request, job_id):
    """Projeção jurimétrica do processo: lê o estágio+features extraídos (Zordon) e,
    se ainda NÃO é precatório, estima via modelo de sobrevivência DC→precatório
    (KM estratificado, `survival_precatorio.prever`) quando/se sai o ofício."""
    try:
        r = requests.get(f"{_base()}/api/extrair/{job_id}", timeout=30).json()
    except (requests.RequestException, ValueError):
        return JsonResponse({"erro": "zordon_off"}, status=502)
    estagio = r.get("estagio")
    jm = r.get("jm") or {}
    lab, cor = _ESTAGIO.get(estagio, (estagio or "—", "#64748b"))
    est = None
    if estagio and estagio != "PRECATORIO":
        try:
            from dashboard.survival_precatorio import prever
            est = prever(jm.get("tribunal"), jm.get("natureza"), jm.get("ente_devedor"))
        except Exception:  # noqa: BLE001 — serving indisponível não quebra a tela
            est = None
    return JsonResponse({"estagio": estagio, "estagio_label": lab, "estagio_cor": cor,
                         "features": jm, "estimativa": est})


@csrf_exempt
@login_required
def reprocessar(request, job_id):
    try:
        r = requests.post(f"{_base()}/extrair/{job_id}/reprocessar", timeout=30, allow_redirects=False)
    except requests.RequestException:
        return HttpResponseRedirect(f"/extrair/{job_id}")
    return HttpResponseRedirect(r.headers.get("location", f"/extrair/{job_id}"))
