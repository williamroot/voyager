"""Showcase do Extrator — tela investidor: sobe um PDF, o(s) modelo(s) extraem a
ficha 100% ON-DEVICE (SEM consulta externa — só o modelo local + OCR, via o SDK
standalone servido num pod), e mostra o resultado com riqueza.

O navegador nunca fala direto com o pod: tudo passa por este proxy fino
(``login_required``). As URLs por versão de modelo vivem em
``settings.SHOWCASE_MODELOS`` (preenchidas quando o pod servidor sobe):

    SHOWCASE_MODELOS = {
      "v1":  {"url": "http://IP:PORT", "label": "Geração 1", "cor": "#71717a"},
      "v2":  {"url": "http://IP:PORT", "label": "v2 Ficha da Parte", "cor": "#3b82f6"},
      "v21": {"url": "http://IP:PORT", "label": "v2.1 (campeão)", "cor": "#22c55e"},
      "v22": {"url": "http://IP:PORT", "label": "v2.2 (herdeiros)", "cor": "#a855f7"},
    }

Cada ``url`` é a raiz do SDK (FastAPI): ``POST {url}/extrair`` (multipart
``files=@arquivo.pdf``) → ``{"fichas": [...]}``; ``POST {url}/explicar`` (opcional)
→ justificativa por campo. Versão sem ``url`` = indisponível (a UI mostra assim).
"""
from __future__ import annotations

import time

import requests
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt


def _modelos() -> dict:
    """Dict de versões configuradas (só as que têm url viram 'disponível')."""
    cfg = getattr(settings, "SHOWCASE_MODELOS", {}) or {}
    out = {}
    for ver, m in cfg.items():
        out[ver] = {
            "label": m.get("label", ver),
            "cor": m.get("cor", "#3b82f6"),
            "disponivel": bool(m.get("url")),
            "explicavel": bool(m.get("explicavel")),
        }
    return out


@login_required
def showcase(request):
    """Página nativa do Voyager (chrome: topo+sidebar) da tela de showcase."""
    modelos = _modelos()
    return render(request, "dashboard/showcase.html", {
        "modelos": modelos,
        "tem_modelo": any(m["disponivel"] for m in modelos.values()),
    })


@csrf_exempt
@login_required
def showcase_extrair(request, versao: str):
    """Recebe o PDF e faz proxy pro pod da versão escolhida. Retorna a ficha do
    MODELO (sem consulta externa) + o tempo de processamento. Tudo na hora."""
    if request.method != "POST" or not request.FILES.get("arquivo"):
        return JsonResponse({"erro": "envie um PDF em 'arquivo'"}, status=400)
    cfg = (getattr(settings, "SHOWCASE_MODELOS", {}) or {}).get(versao)
    if not cfg or not cfg.get("url"):
        return JsonResponse({"erro": f"modelo '{versao}' indisponível (pod não configurado)",
                             "versao": versao, "indisponivel": True}, status=503)
    up = request.FILES["arquivo"]
    files = {"files": (up.name, up.file, up.content_type or "application/pdf")}
    base = cfg["url"].rstrip("/")
    t0 = time.monotonic()
    try:
        r = requests.post(f"{base}/extrair", files=files, timeout=900)
    except requests.RequestException as e:
        return JsonResponse({"erro": "pod do modelo fora do ar", "detalhe": str(e)[:120],
                             "versao": versao, "indisponivel": True}, status=502)
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    try:
        payload = r.json()
    except ValueError:
        return JsonResponse({"erro": "resposta inválida do modelo", "versao": versao},
                            status=502)
    return JsonResponse({
        "versao": versao,
        "label": cfg.get("label", versao),
        "elapsed_ms": elapsed_ms,
        "fichas": payload.get("fichas", payload if isinstance(payload, list) else []),
        "arquivo": up.name,
    }, json_dumps_params={"default": str})


@csrf_exempt
@login_required
def showcase_explicar(request, versao: str):
    """Passe de EXPLICABILIDADE (2º passe no MESMO modelo): justifica cada campo
    apontando o trecho do doc. On-device. Se o pod não expõe /explicar, 501."""
    if request.method != "POST":
        return JsonResponse({"erro": "POST"}, status=400)
    cfg = (getattr(settings, "SHOWCASE_MODELOS", {}) or {}).get(versao)
    if not cfg or not cfg.get("url"):
        return JsonResponse({"erro": "modelo indisponível", "versao": versao}, status=503)
    if not cfg.get("explicavel"):
        return JsonResponse({"erro": "explicabilidade não disponível nesta versão",
                             "versao": versao, "sem_explicacao": True}, status=501)
    base = cfg["url"].rstrip("/")
    try:
        r = requests.post(f"{base}/explicar", json=(request.body and __import__("json").loads(request.body)) or {},
                          timeout=300)
        return JsonResponse(r.json() if r.content else {}, json_dumps_params={"default": str})
    except requests.RequestException as e:
        return JsonResponse({"erro": "pod fora do ar", "detalhe": str(e)[:120]}, status=502)
