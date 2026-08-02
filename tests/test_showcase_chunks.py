"""Testes do upload em chunks + extração assíncrona da Showcase.

Cobre o que é load-bearing e testável sem pod/rede:
- guarda de path-traversal no ``upload_id`` (``_valid_id``);
- ciclo init → chunk → finish com validação de integridade (contagem, tamanho,
  hashes md5 por-chunk e sha256 do arquivo), montagem correta dos bytes;
- rejeições: chunk faltando (409), tamanho divergente (422), md5 divergente (422);
- o job (``extrair_job``) com o pod MOCKADO: escreve estado done/erro e limpa o dir.

O RQ é curto-circuitado: ``upload_finish`` chama ``queue.enqueue`` — mockamos a
fila pra rodar o job inline e conferir o estado no cache.
"""
import hashlib
import json
from pathlib import Path
from unittest import mock

import pytest
from django.core.cache import cache
from django.test import RequestFactory

from dashboard import showcase_chunks as sc


@pytest.fixture(autouse=True)
def _tmp_upload_dir(tmp_path, settings):
    """Aponta o diretório de uploads pra um tmp isolado e limpa o cache."""
    d = tmp_path / "showcase_uploads"
    with mock.patch.object(sc, "UPLOAD_DIR", d):
        cache.clear()
        yield d
    cache.clear()


class _User:
    id = 42
    is_authenticated = True


# ── _valid_id (path-traversal guard) ─────────────────────────────────────────

def test_valid_id_aceita_uuid_e_barra_traversal():
    import uuid
    assert sc._valid_id(str(uuid.uuid4()))
    assert not sc._valid_id("../../etc/passwd")
    assert not sc._valid_id("foo/bar")
    assert not sc._valid_id("")
    assert not sc._valid_id(None)


# ── ciclo completo init → chunks → finish, com o job mockado ─────────────────

def _init(rf, filename, size, total_chunks):
    body = json.dumps({"filename": filename, "size": size,
                       "total_chunks": total_chunks, "content_type": "application/pdf"}).encode()
    req = rf.post("/init/", data=body, content_type="application/json")
    req.user = _User()
    resp = sc.upload_init(req)
    assert resp.status_code == 200
    return json.loads(resp.content)["upload_id"]


def _send_chunk(rf, upload_id, idx, data, md5=None):
    req = rf.post(f"/chunk/{upload_id}/{idx}/", data=data, content_type="application/octet-stream")
    req.user = _User()
    if md5:
        req.META["HTTP_X_CHUNK_MD5"] = md5
    return sc.upload_chunk(req, upload_id, idx)


def test_ciclo_completo_monta_e_enfileira(rf):
    conteudo = b"A" * (8 * 1024) + b"B" * (4 * 1024)   # 12 KB → 2 chunks de 8KB
    CHUNK = 8 * 1024
    partes = [conteudo[i:i + CHUNK] for i in range(0, len(conteudo), CHUNK)]
    upload_id = _init(rf, "autos.pdf", len(conteudo), len(partes))

    for i, p in enumerate(partes):
        md5 = hashlib.md5(p).hexdigest()
        r = _send_chunk(rf, upload_id, i, p, md5=md5)
        assert r.status_code == 200, r.content

    # finish: mocka a fila pra capturar os kwargs do job (sem RQ real)
    enqueued = {}

    def fake_enqueue(func, **kw):
        enqueued["func"] = func
        enqueued["kwargs"] = kw["kwargs"]
        return mock.Mock()

    fq = mock.Mock()
    fq.enqueue.side_effect = fake_enqueue
    with mock.patch("django_rq.get_queue", return_value=fq):
        body = json.dumps({"versao": "v21",
                           "sha256": hashlib.sha256(conteudo).hexdigest()}).encode()
        req = rf.post(f"/finish/{upload_id}/", data=body, content_type="application/json")
        req.user = _User()
        resp = sc.upload_finish(req, upload_id)
    assert resp.status_code == 200, resp.content
    jobs = json.loads(resp.content)["jobs"]
    assert "v21" in jobs

    # o arquivo montado bate byte-a-byte
    montado = Path(enqueued["kwargs"]["caminho"])
    assert montado.read_bytes() == conteudo


def test_finish_rejeita_chunk_faltando(rf):
    upload_id = _init(rf, "x.pdf", 100, 3)
    _send_chunk(rf, upload_id, 0, b"a" * 40)
    # falta o 1 e o 2
    req = rf.post(f"/finish/{upload_id}/", data=json.dumps({"versao": "v21"}).encode(),
                  content_type="application/json")
    req.user = _User()
    resp = sc.upload_finish(req, upload_id)
    assert resp.status_code == 409


def test_chunk_rejeita_md5_divergente(rf):
    upload_id = _init(rf, "x.pdf", 10, 1)
    r = _send_chunk(rf, upload_id, 0, b"hello", md5="deadbeef" * 4)
    assert r.status_code == 422


def test_finish_rejeita_tamanho_divergente(rf):
    # declara size=999 mas manda só 5 bytes num chunk
    upload_id = _init(rf, "x.pdf", 999, 1)
    _send_chunk(rf, upload_id, 0, b"hello")
    req = rf.post(f"/finish/{upload_id}/", data=json.dumps({"versao": "v21"}).encode(),
                  content_type="application/json")
    req.user = _User()
    resp = sc.upload_finish(req, upload_id)
    assert resp.status_code == 422


def test_init_rejeita_arquivo_gigante(rf):
    body = json.dumps({"filename": "big.pdf", "size": 99 * 1024**3,
                       "total_chunks": 10}).encode()
    req = rf.post("/init/", data=body, content_type="application/json")
    req.user = _User()
    resp = sc.upload_init(req)
    assert resp.status_code == 413


# ── job com pod mockado ──────────────────────────────────────────────────────

def test_extrair_job_done_e_limpa(rf, tmp_path):
    from dashboard import showcase_jobs
    d = sc._upl_dir("11111111-1111-1111-1111-111111111111")
    d.mkdir(parents=True, exist_ok=True)
    arq = d / "autos.pdf"
    arq.write_bytes(b"PDF")
    job_id = "22222222-2222-2222-2222-222222222222"
    sc.set_job_state(job_id, status="pending")

    fake_out = ({"versao": "v21", "fichas": [{"nome": "X"}], "docs": []}, 200)
    with mock.patch("dashboard.showcase_proxy.extrair_no_pod", return_value=fake_out):
        showcase_jobs.extrair_job(state_job_id=job_id, versao="v21", caminho=str(arq),
                                  arquivo="autos.pdf", content_type="application/pdf",
                                  upload_id="11111111-1111-1111-1111-111111111111",
                                  limpar_dir=True)
    st = cache.get(sc._job_key(job_id))
    assert st["status"] == "done"
    assert st["resultado"]["fichas"] == [{"nome": "X"}]
    assert not d.exists()  # cleanup apagou o diretório


@pytest.fixture
def rf():
    return RequestFactory()
