"""Job RQ da extração assíncrona da Showcase do Extrator.

Roda na fila ``manual`` (host ``.103``, mesmo do ``web`` — enxerga o arquivo
montado em ``media/showcase_uploads/<upload_id>/``). Chama o pod via
``showcase_proxy.extrair_no_pod`` (lógica compartilhada, contrato intocado),
atualiza o estado do job no cache Redis (que a view ``job_status`` faz polling)
e apaga o arquivo montado ao terminar.

LOGS RICOS: ``voyager.showcase`` em cada transição — início, latência do pod,
tamanho da resposta, sucesso/erro com stack.
"""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

logger = logging.getLogger("voyager.showcase")


def extrair_job(*, state_job_id: str, versao: str, caminho: str, arquivo: str,
                content_type: str, upload_id: str, limpar_dir: bool = True,
                user_id: int | None = None, sha256: str = "") -> dict:
    """Faz a extração de UM arquivo montado numa versão do modelo.

    Reporta progresso via ``set_job_state`` (cache) em cada etapa. Não levanta
    pra fora sem antes marcar o estado ``erro`` — o polling precisa ver o fim.
    """
    # Import tardio: evita custo no boot do worker e import circular.
    from .showcase_chunks import set_job_state
    from .showcase_proxy import extrair_no_pod

    p = Path(caminho)
    tamanho = p.stat().st_size if p.exists() else 0
    logger.info("[showcase job=%s evt=job_start versao=%s file=%s bytes=%d upload_id=%s]",
                state_job_id, versao, arquivo, tamanho, upload_id)
    set_job_state(state_job_id, status="processando", etapa="enviando ao modelo",
                  progresso=15, bytes=tamanho)

    t0 = time.monotonic()
    try:
        if not p.exists():
            raise FileNotFoundError(f"arquivo montado sumiu: {caminho}")
        with open(p, "rb") as fh:
            set_job_state(state_job_id, status="processando",
                          etapa="modelo lendo e extraindo", progresso=45)
            out, http = extrair_no_pod(versao, fh, arquivo, content_type)
    except Exception as e:  # noqa: BLE001 — qualquer falha vira estado 'erro'
        logger.exception("[showcase job=%s evt=job_error versao=%s file=%s]",
                         state_job_id, versao, arquivo)
        set_job_state(state_job_id, status="erro", etapa="falha na extração",
                      progresso=100, erro=str(e)[:200], versao=versao)
        _talvez_limpar(upload_id, limpar_dir, state_job_id)
        return {"status": "erro", "erro": str(e)[:200]}

    dt = time.monotonic() - t0
    if http == 200:
        set_job_state(state_job_id, status="done", etapa="ficha pronta",
                      progresso=100, resultado=out, versao=versao)
        logger.info("[showcase job=%s evt=job_done versao=%s dt=%.1fs fichas=%d docs=%d]",
                    state_job_id, versao, dt,
                    len(out.get("fichas") or []) if isinstance(out.get("fichas"), list) else 0,
                    len(out.get("docs") or []))
        # persiste a análise (compartilhável por UUID) e devolve o id ao cliente
        analise_id = _persistir_analise(out, versao=versao, arquivo=arquivo,
                                        content_type=content_type, tamanho=tamanho,
                                        sha256=sha256, user_id=user_id, upload_id=upload_id)
        if analise_id:
            set_job_state(state_job_id, analise_id=analise_id)
        ret = {"status": "done", "analise_id": analise_id}
    else:
        # Pod indisponível / resposta inválida — não é crash do job, é erro de negócio.
        set_job_state(state_job_id, status="erro", etapa="modelo indisponível",
                      progresso=100, erro=out.get("erro") or "erro do modelo",
                      resultado=out, versao=versao)
        logger.warning("[showcase job=%s evt=job_pod_fail versao=%s http=%d dt=%.1fs err=%s]",
                       state_job_id, versao, http, dt, (out.get("erro") or "")[:120])
        ret = {"status": "erro", "http": http}

    _talvez_limpar(upload_id, limpar_dir, state_job_id)
    return ret


def _detecta_cessao(fichas: list, docs: list) -> bool:
    """True se a ficha tem cessão de crédito — evento CESSAO na parte, papel
    CEDENTE/CESSIONARIO, ou doc classe CESSAO_CREDITO. Alimenta label + filtro."""
    try:
        for f in (fichas or []):
            if not isinstance(f, dict):
                continue
            if (f.get("papel") or "").upper() in ("CESSIONARIO", "CEDENTE"):
                return True
            for ev in (f.get("eventos") or []):
                if isinstance(ev, dict) and (ev.get("tipo") or "").upper() == "CESSAO":
                    return True
        for d in (docs or []):
            if isinstance(d, dict) and (d.get("classe") or "").upper() == "CESSAO_CREDITO":
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _persistir_analise(out: dict, *, versao: str, arquivo: str, content_type: str,
                       tamanho: int, sha256: str, user_id: int | None, upload_id: str):
    """Salva a análise no DB (compartilhável por UUID). NUNCA derruba o job."""
    try:
        from .models import ShowcaseAnalise
        tempos = out.get("tempos") or {}
        fichas = out.get("fichas") or []
        docs = out.get("docs") or []
        a = ShowcaseAnalise.objects.create(
            usuario_id=user_id,
            arquivo=(arquivo or "autos.pdf")[:255], content_type=(content_type or "")[:120],
            tamanho_bytes=int(tamanho or 0), sha256=(sha256 or "")[:64],
            versao=(versao or "")[:20], modelo_label=(out.get("label") or "")[:120],
            elapsed_ms=int(out.get("elapsed_ms") or 0), tempos=tempos,
            n_partes=len(fichas) if isinstance(fichas, list) else 0,
            n_docs=len(docs) if isinstance(docs, list) else 0,
            paginas=int(tempos.get("n_paginas") or 0),
            tem_cessao=_detecta_cessao(fichas, docs),
            resultado=out, upload_id=(upload_id or "")[:64],
        )
        logger.info("[showcase evt=analise_salva uuid=%s versao=%s partes=%d]",
                    a.uuid, versao, a.n_partes)
        return str(a.uuid)
    except Exception as e:  # noqa: BLE001 — persistir não pode derrubar o job
        logger.warning("[showcase evt=analise_persist_error err=%s]", str(e)[:180])
        return None


def _talvez_limpar(upload_id: str, limpar: bool, job_id: str) -> None:
    """Apaga o diretório do upload (arquivo montado + chunks) após o ÚLTIMO job.

    No modo compare, N jobs leem o MESMO arquivo montado; só o job que fecha o
    lote (``limpar_dir=True``) apaga, pra não puxar o arquivo debaixo dos outros.
    """
    if not limpar:
        return
    from .showcase_chunks import _upl_dir  # noqa: PLC0415
    d = _upl_dir(upload_id)
    try:
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
            logger.info("[showcase job=%s evt=cleanup upload_id=%s dir=%s]",
                        job_id, upload_id, d)
    except Exception as e:  # noqa: BLE001
        logger.warning("[showcase job=%s evt=cleanup_error upload_id=%s err=%s]",
                       job_id, upload_id, str(e)[:120])
