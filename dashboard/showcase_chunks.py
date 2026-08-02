"""Showcase do Extrator — UPLOAD EM CHUNKS + EXTRAÇÃO ASSÍNCRONA.

Por que existe: o upload síncrono antigo (``showcase_proxy.showcase_extrair``)
passa o corpo inteiro pelo Cloudflare Tunnel (``voyager.was.dev.br``), que impõe
**100 MB de body** e **~100s de timeout** por request. Um PDF de ~1 GB dos autos
é impossível assim. Aqui o arquivo é fatiado no cliente em **chunks de 8 MB**
(cada um << 100 MB), enviados em paralelo; o servidor remonta em disco, valida a
integridade e **enfileira** a extração (async), devolvendo um ``job_id`` na hora.
O cliente faz *polling* até o job terminar e então usa o MESMO ``renderFicha``.

O CONTRATO DO RESULTADO é idêntico ao do endpoint síncrono — só muda o
transporte. O job reusa a lógica de proxy pro pod (``_extrair_no_pod``,
compartilhada com ``showcase_proxy``).

Fluxo (URLs sob ``/dashboard/api/showcase/upload/``):

    POST  .../init/                     {filename,size,total_chunks,content_type}
                                        → {upload_id}
    POST  .../chunk/<upload_id>/<idx>/  corpo = bytes do chunk (+ X-Chunk-MD5)
                                        → {ok, idx, bytes, recebidos}
    POST  .../finish/<upload_id>/       {versao} ou {versoes:[...]} (compare)
                                        → {jobs:{<versao>:<job_id>}}  (rápido, <2s)
    GET   .../job/<job_id>/             → {status, etapa, progresso, resultado?}

Armazenamento: ``SHOWCASE_UPLOAD_DIR/<upload_id>/chunk_<idx>`` (streaming, nunca
carrega o arquivo em RAM). ``web`` e ``worker_manual`` rodam no MESMO host
(.103) e bind-montam o repo em ``/app`` → o worker enxerga os chunks escritos
pelo web. O arquivo montado é apagado após a extração (ou por TTL).

Estado do job: hash Redis (via cache) keyed por ``job_id`` — sobrevive à limpeza
do registry do RQ e dá polling barato sem tocar o DB.

LOGS RICOS: logger ``voyager.showcase`` em cada passo, formato greppável
``[showcase upload_id=… evt=chunk idx=3/128 bytes=8388608 dt=0.42s]`` — abrir
``docker logs web`` (ou ``worker_manual``) e ver exatamente onde travou.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path

import django_rq
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_GET

logger = logging.getLogger("voyager.showcase")

# ── Configuração (com defaults sãos; overridable em settings) ────────────────
# Diretório raiz dos uploads em montagem. media/ já é gitignored.
UPLOAD_DIR = Path(getattr(settings, "SHOWCASE_UPLOAD_DIR", None)
                  or (Path(settings.BASE_DIR) / "media" / "showcase_uploads"))
# Teto de tamanho total do arquivo montado (bytes). Default 2 GB.
MAX_TOTAL_BYTES = int(getattr(settings, "SHOWCASE_MAX_UPLOAD_BYTES", 2 * 1024**3))
# Máximo de chunks (proteção contra flood de metadados). 8 MB * 4096 = 32 GB teto duro.
MAX_CHUNKS = int(getattr(settings, "SHOWCASE_MAX_CHUNKS", 4096))
# TTL do estado do job no cache (segundos).
JOB_TTL = int(getattr(settings, "SHOWCASE_JOB_TTL", 3600))
# TTL do meta do upload no cache (segundos) — janela pra o cliente terminar de subir.
UPLOAD_TTL = int(getattr(settings, "SHOWCASE_UPLOAD_TTL", 6 * 3600))
# Timeout do job RQ (segundos) — o pod pode demorar em doc grande.
JOB_TIMEOUT = int(getattr(settings, "SHOWCASE_JOB_TIMEOUT", 3600))
# Fila RQ. 'manual' roda no .103 (mesmo host do web) — vê os chunks montados.
QUEUE_NAME = getattr(settings, "SHOWCASE_QUEUE", "manual")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers de estado (cache Redis)
# ─────────────────────────────────────────────────────────────────────────────

def _upl_key(upload_id: str) -> str:
    return f"showcase:upl:{upload_id}"


def _job_key(job_id: str) -> str:
    return f"showcase:job:{job_id}"


def _upl_dir(upload_id: str) -> Path:
    return UPLOAD_DIR / upload_id


def _valid_id(s: str) -> bool:
    """Aceita só UUID hex/tracejado — barra path traversal em <upload_id>."""
    try:
        uuid.UUID(str(s))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def set_job_state(job_id: str, **fields) -> dict:
    """Merge de campos no estado do job (status/etapa/progresso/resultado/erro)."""
    st = cache.get(_job_key(job_id)) or {}
    st.update(fields)
    st["atualizado_em"] = time.time()
    cache.set(_job_key(job_id), st, JOB_TTL)
    return st


# ─────────────────────────────────────────────────────────────────────────────
# View: INIT — cria o upload_id e o diretório
# ─────────────────────────────────────────────────────────────────────────────

@csrf_exempt
@login_required
@require_POST
def upload_init(request):
    try:
        body = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"erro": "JSON inválido"}, status=400)

    filename = str(body.get("filename") or "autos.pdf")[:255]
    try:
        size = int(body.get("size") or 0)
        total_chunks = int(body.get("total_chunks") or 0)
    except (ValueError, TypeError):
        return JsonResponse({"erro": "size/total_chunks inválidos"}, status=400)
    content_type = str(body.get("content_type") or "application/octet-stream")[:120]

    if size <= 0 or total_chunks <= 0:
        return JsonResponse({"erro": "size e total_chunks são obrigatórios (>0)"}, status=400)
    if size > MAX_TOTAL_BYTES:
        logger.warning("[showcase evt=init_reject motivo=too_big size=%d max=%d file=%s]",
                       size, MAX_TOTAL_BYTES, filename)
        return JsonResponse({"erro": f"arquivo excede o teto de {MAX_TOTAL_BYTES // 1024**2} MB",
                             "max_bytes": MAX_TOTAL_BYTES}, status=413)
    if total_chunks > MAX_CHUNKS:
        return JsonResponse({"erro": f"total_chunks excede o teto ({MAX_CHUNKS})"}, status=400)

    upload_id = str(uuid.uuid4())
    d = _upl_dir(upload_id)
    d.mkdir(parents=True, exist_ok=True)

    meta = {
        "upload_id": upload_id, "filename": filename, "size": size,
        "total_chunks": total_chunks, "content_type": content_type,
        "user_id": request.user.id, "criado_em": time.time(),
        "recebidos": [],  # índices já gravados (dedupe de retry)
    }
    cache.set(_upl_key(upload_id), meta, UPLOAD_TTL)
    logger.info("[showcase upload_id=%s evt=init file=%s size=%d chunks=%d ct=%s user=%s]",
                upload_id, filename, size, total_chunks, content_type, request.user.id)
    return JsonResponse({"upload_id": upload_id, "chunk_size_hint": 8 * 1024**2})


# ─────────────────────────────────────────────────────────────────────────────
# View: CHUNK — grava um chunk em disco (streaming) + valida md5 opcional
# ─────────────────────────────────────────────────────────────────────────────

@csrf_exempt
@login_required
@require_POST
def upload_chunk(request, upload_id: str, index: int):
    t0 = time.monotonic()
    if not _valid_id(upload_id):
        return JsonResponse({"erro": "upload_id inválido"}, status=400)
    meta = cache.get(_upl_key(upload_id))
    if not meta:
        logger.warning("[showcase upload_id=%s evt=chunk_reject motivo=expired idx=%s]",
                       upload_id, index)
        return JsonResponse({"erro": "upload expirado ou inexistente — reinicie", "expirado": True},
                            status=410)
    if meta.get("user_id") != request.user.id:
        return JsonResponse({"erro": "upload de outro usuário"}, status=403)

    try:
        idx = int(index)
    except (ValueError, TypeError):
        return JsonResponse({"erro": "index inválido"}, status=400)
    if idx < 0 or idx >= meta["total_chunks"]:
        return JsonResponse({"erro": "index fora do intervalo"}, status=400)

    d = _upl_dir(upload_id)
    d.mkdir(parents=True, exist_ok=True)
    dest = d / f"chunk_{idx:06d}"
    tmp = d / f".chunk_{idx:06d}.part"

    # Lê o corpo do chunk. Com o nginx BUFFERANDO o request (proxy_request_buffering
    # on — default), o gunicorn recebe os 8MB COMPLETOS antes de a view rodar, então
    # não há leitura interrompida no meio (a causa do 500: cliente/tunnel resetava a
    # conexão durante request.read()). 8MB < DATA_UPLOAD_MAX_MEMORY_SIZE (16MB) → o
    # corpo fica em RAM sem spill. Se a conexão cair mesmo assim, devolve erro
    # RETENTÁVEL (503) — o cliente reenvia o chunk (idempotente por índice).
    try:
        data = request.body
    except Exception as e:  # noqa: BLE001 — conexão resetada / corpo incompleto
        logger.warning("[showcase upload_id=%s evt=chunk_read_reset idx=%d err=%s]",
                       upload_id, idx, str(e)[:120])
        return JsonResponse({"erro": "conexão interrompida ao receber o chunk — reenvie",
                             "retryable": True}, status=503)
    n = len(data)
    if n > MAX_TOTAL_BYTES:  # guarda contra chunk gigante forjado
        return JsonResponse({"erro": "chunk excede o teto"}, status=413)

    # Integridade opcional por chunk (o cliente manda o md5 hex).
    expected = (request.headers.get("X-Chunk-MD5") or request.GET.get("md5") or "").strip().lower()
    got = hashlib.md5(data).hexdigest()
    if expected and expected != got:
        logger.warning("[showcase upload_id=%s evt=chunk_md5_mismatch idx=%d exp=%s got=%s bytes=%d]",
                       upload_id, idx, expected, got, n)
        return JsonResponse({"erro": "md5 do chunk não confere — reenvie",
                             "idx": idx, "esperado": expected, "recebido": got}, status=422)

    # Grava atômico: escreve no .part e só então renomeia (chunk válido = arquivo final).
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
    except Exception as e:  # noqa: BLE001 — disco cheio / IO
        tmp.unlink(missing_ok=True)
        logger.error("[showcase upload_id=%s evt=chunk_write_error idx=%d bytes=%d err=%s]",
                     upload_id, idx, n, str(e)[:120])
        return JsonResponse({"erro": "falha ao gravar o chunk", "retryable": True,
                             "detalhe": str(e)[:120]}, status=500)

    # marca recebido (idempotente — retry do mesmo idx não duplica)
    meta = cache.get(_upl_key(upload_id)) or meta
    recs = set(meta.get("recebidos") or [])
    recs.add(idx)
    meta["recebidos"] = sorted(recs)
    cache.set(_upl_key(upload_id), meta, UPLOAD_TTL)

    dt = time.monotonic() - t0
    mbps = (n / 1024**2 / dt) if dt > 0 else 0.0
    logger.info("[showcase upload_id=%s evt=chunk idx=%d/%d bytes=%d dt=%.2fs %.1fMB/s recebidos=%d/%d]",
                upload_id, idx, meta["total_chunks"], n, dt, mbps,
                len(meta["recebidos"]), meta["total_chunks"])
    return JsonResponse({"ok": True, "idx": idx, "bytes": n, "md5": got,
                         "recebidos": len(meta["recebidos"]), "total": meta["total_chunks"]})


# ─────────────────────────────────────────────────────────────────────────────
# View: FINISH — remonta, valida integridade, enfileira a extração
# ─────────────────────────────────────────────────────────────────────────────

@csrf_exempt
@login_required
@require_POST
def upload_finish(request, upload_id: str):
    if not _valid_id(upload_id):
        return JsonResponse({"erro": "upload_id inválido"}, status=400)
    meta = cache.get(_upl_key(upload_id))
    if not meta:
        return JsonResponse({"erro": "upload expirado ou inexistente", "expirado": True}, status=410)
    if meta.get("user_id") != request.user.id:
        return JsonResponse({"erro": "upload de outro usuário"}, status=403)

    try:
        body = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        body = {}
    versoes = body.get("versoes")
    if not versoes:
        v = body.get("versao")
        versoes = [v] if v else []
    versoes = [str(x) for x in versoes if x][:8]  # sanidade: <=8 versões no compare
    if not versoes:
        return JsonResponse({"erro": "informe 'versao' ou 'versoes'"}, status=400)

    # ── valida contagem de chunks ──
    d = _upl_dir(upload_id)
    total = meta["total_chunks"]
    faltando = [i for i in range(total) if not (d / f"chunk_{i:06d}").exists()]
    if faltando:
        logger.warning("[showcase upload_id=%s evt=finish_incomplete faltam=%d de=%d primeiros=%s]",
                       upload_id, len(faltando), total, faltando[:10])
        return JsonResponse({"erro": "faltam chunks — reenvie",
                             "faltando": faltando[:50], "total_faltando": len(faltando)}, status=409)

    # ── monta o arquivo único (streaming) + valida tamanho + hash ──
    montado = d / meta["filename"]
    t0 = time.monotonic()
    sha = hashlib.sha256()
    escrito = 0
    try:
        with open(montado, "wb") as out:
            for i in range(total):
                cp = d / f"chunk_{i:06d}"
                with open(cp, "rb") as cf:
                    while True:
                        piece = cf.read(1024 * 1024)
                        if not piece:
                            break
                        out.write(piece)
                        sha.update(piece)
                        escrito += len(piece)
    except Exception as e:  # noqa: BLE001
        logger.error("[showcase upload_id=%s evt=assembly_error escrito=%d err=%s]",
                     upload_id, escrito, str(e)[:160])
        return JsonResponse({"erro": "falha ao montar o arquivo", "detalhe": str(e)[:160]}, status=500)

    dt = time.monotonic() - t0
    esperado = meta["size"]
    hash_hex = sha.hexdigest()
    tamanho_ok = (escrito == esperado)
    logger.info("[showcase upload_id=%s evt=assembly final=%d esperado=%d ok=%s sha256=%s dt=%.2fs]",
                upload_id, escrito, esperado, tamanho_ok, hash_hex[:16], dt)
    if not tamanho_ok:
        montado.unlink(missing_ok=True)
        logger.error("[showcase upload_id=%s evt=assembly_size_mismatch final=%d esperado=%d]",
                     upload_id, escrito, esperado)
        return JsonResponse({"erro": "tamanho montado difere do esperado — reenvie",
                             "montado": escrito, "esperado": esperado}, status=422)

    # hash de arquivo inteiro opcional (o cliente pode mandar sha256 do File).
    exp_sha = (body.get("sha256") or "").strip().lower()
    if exp_sha and exp_sha != hash_hex:
        montado.unlink(missing_ok=True)
        logger.warning("[showcase upload_id=%s evt=file_sha_mismatch exp=%s got=%s]",
                       upload_id, exp_sha, hash_hex)
        return JsonResponse({"erro": "sha256 do arquivo não confere — reenvie"}, status=422)

    # ── enfileira um job por versão (compare = N jobs) ──
    from . import showcase_jobs  # local: evita import circular no boot
    queue = django_rq.get_queue(QUEUE_NAME)
    jobs: dict[str, str] = {}
    for ver in versoes:
        job_id = str(uuid.uuid4())
        set_job_state(job_id, status="pending", etapa="na fila", progresso=0,
                      versao=ver, arquivo=meta["filename"], upload_id=upload_id)
        try:
            queue.enqueue(
                showcase_jobs.extrair_job,
                job_id=job_id,          # RQ usa esse id; casamos com nosso estado
                kwargs={
                    "state_job_id": job_id, "versao": ver,
                    "caminho": str(montado), "arquivo": meta["filename"],
                    "content_type": meta["content_type"], "upload_id": upload_id,
                    # limpa o dir só depois do ÚLTIMO job (o que fecha o compare)
                    "limpar_dir": (ver == versoes[-1]),
                },
                job_timeout=JOB_TIMEOUT,
                result_ttl=JOB_TTL,
                failure_ttl=JOB_TTL,
            )
        except Exception as e:  # noqa: BLE001 — Redis fora, fila indisponível
            logger.error("[showcase upload_id=%s evt=enqueue_error versao=%s err=%s]",
                         upload_id, ver, str(e)[:160])
            set_job_state(job_id, status="erro", etapa="falha ao enfileirar",
                          erro="fila indisponível — tente de novo")
            return JsonResponse({"erro": "não foi possível enfileirar (fila fora do ar)",
                                 "detalhe": str(e)[:120]}, status=503)
        jobs[ver] = job_id
        logger.info("[showcase upload_id=%s evt=enqueued versao=%s job_id=%s fila=%s]",
                    upload_id, ver, job_id, QUEUE_NAME)

    # o upload_id não é mais necessário no cache (chunks viram o arquivo montado)
    cache.delete(_upl_key(upload_id))
    return JsonResponse({"jobs": jobs, "arquivo": meta["filename"], "sha256": hash_hex})


# ─────────────────────────────────────────────────────────────────────────────
# View: JOB — polling do status/resultado
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@require_GET
def job_status(request, job_id: str):
    if not _valid_id(job_id):
        return JsonResponse({"erro": "job_id inválido"}, status=400)
    st = cache.get(_job_key(job_id))
    if not st:
        # Estado sumiu (TTL) — sinaliza terminado desconhecido pro cliente parar.
        return JsonResponse({"status": "desconhecido",
                             "etapa": "estado expirado ou inexistente"}, status=404)
    return JsonResponse(st, json_dumps_params={"default": str})
