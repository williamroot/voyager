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

import logging
import time

import requests
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt

logger = logging.getLogger("voyager.showcase")


def extrair_no_pod(versao: str, fileobj, filename: str, content_type: str | None,
                   *, timeout: int = 900) -> tuple[dict, int]:
    """Chama o pod ``{url}/extrair`` da versão e devolve ``(payload_normalizado,
    http_status)`` — a MESMA forma que o endpoint síncrono retornava (contrato
    intocado). ``fileobj`` é um file-like aberto em modo binário (streaming, não
    carrega em RAM). Compartilhado entre o proxy síncrono e o job assíncrono de
    chunks — ponto único de verdade da chamada ao pod.

    Nunca levanta: erros viram ``({"erro": ...}, status)`` pra o chamador
    repassar/registrar. Loga latência, tamanho da resposta e nº de fichas/docs.
    """
    cfg = (getattr(settings, "SHOWCASE_MODELOS", {}) or {}).get(versao)
    if not cfg or not cfg.get("url"):
        return ({"erro": f"modelo '{versao}' indisponível (pod não configurado)",
                 "versao": versao, "indisponivel": True}, 503)
    base = cfg["url"].rstrip("/")
    files = {"files": (filename, fileobj, content_type or "application/pdf")}
    t0 = time.monotonic()
    try:
        r = requests.post(f"{base}/extrair", files=files, timeout=timeout)
    except requests.RequestException as e:
        logger.error("[showcase evt=pod_error versao=%s file=%s err=%s]",
                     versao, filename, str(e)[:160])
        return ({"erro": "pod do modelo fora do ar", "detalhe": str(e)[:120],
                 "versao": versao, "indisponivel": True}, 502)
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    resp_bytes = len(r.content or b"")
    try:
        payload = r.json()
    except ValueError:
        logger.error("[showcase evt=pod_bad_json versao=%s http=%s bytes=%d]",
                     versao, r.status_code, resp_bytes)
        return ({"erro": "resposta inválida do modelo", "versao": versao}, 502)
    _p = payload if isinstance(payload, dict) else {}
    out = {
        "versao": versao,
        "label": cfg.get("label", versao),
        "elapsed_ms": elapsed_ms,                       # round-trip (rede + modelo)
        "tempos": _p.get("tempos") or {},               # tempo REAL do modelo
        "fichas": _p.get("fichas", payload if isinstance(payload, list) else []),
        "docs": _p.get("docs") or [],
        "contexto": _p.get("contexto") or {},
        "avisos": _p.get("avisos") or [],
        "alvaras_orfaos": _p.get("alvaras_orfaos") or [],
        "estagio": _p.get("estagio") or {},
        "arquivo": filename,
    }
    logger.info("[showcase evt=pod_ok versao=%s file=%s http=%s dt=%dms resp_bytes=%d fichas=%d docs=%d]",
                versao, filename, r.status_code, elapsed_ms, resp_bytes,
                len(out["fichas"]) if isinstance(out["fichas"], list) else 0,
                len(out["docs"]))
    return (out, 200)


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
    # cards explicativos: specs técnicas por versão + a cor/disponibilidade da versão
    info = {}
    for ver, spec in (getattr(settings, "SHOWCASE_MODELO_INFO", {}) or {}).items():
        info[ver] = {**spec, "cor": modelos.get(ver, {}).get("cor", "#3b82f6"),
                     "disponivel": modelos.get(ver, {}).get("disponivel", False)}
    return render(request, "dashboard/showcase.html", {
        "modelos": modelos,
        "info": info,
        "tem_modelo": any(m["disponivel"] for m in modelos.values()),
    })


@csrf_exempt
@login_required
def showcase_extrair(request, versao: str):
    """Recebe o PDF e faz proxy pro pod da versão escolhida. Retorna a ficha do
    MODELO (sem consulta externa) + o tempo de processamento. Tudo na hora."""
    if request.method != "POST" or not request.FILES.get("arquivo"):
        return JsonResponse({"erro": "envie um PDF em 'arquivo'"}, status=400)
    up = request.FILES["arquivo"]
    out, status = extrair_no_pod(versao, up.file, up.name, up.content_type)
    return JsonResponse(out, status=status, json_dumps_params={"default": str})


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
