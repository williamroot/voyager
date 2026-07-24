#!/usr/bin/env python3
"""gpu-bridge — expõe a telemetria de GPU que já existe (hash Redis) como /metrics.

Reuso puro: NÃO reinstrumenta nada. Lê os hashes que o `vet_gpu_report.py` do
Zordon já escreve no Redis (DB 3):

  vetor:gpu     HASH  field=<host>  value=JSON {name, util, mem_used, mem_total, ts}
  extracao:gpu  HASH  field=<host>  value=JSON {name, util, mem_used, mem_total, ts}

Expõe em texto no formato Prometheus, com label `role` (vetorizacao|extracao),
`host` e `gpu` (nome do modelo). `ts` > STALE_S segundos => marca stale (não
zera o util, expõe `voyager_gpu_stale` = 1 pra alerta de GPU hang).

Sem dependências pesadas: só `redis` + stdlib http.server. Read-only no Redis.
"""
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import redis

REDIS_HOST = os.environ.get("GPU_BRIDGE_REDIS_HOST", "192.168.30.100")
REDIS_PORT = int(os.environ.get("GPU_BRIDGE_REDIS_PORT", "6379"))
REDIS_DB = int(os.environ.get("GPU_BRIDGE_REDIS_DB", "3"))
REDIS_PASSWORD = os.environ.get("GPU_BRIDGE_REDIS_PASSWORD") or None
STALE_S = float(os.environ.get("GPU_BRIDGE_STALE_S", "180"))
PORT = int(os.environ.get("GPU_BRIDGE_PORT", "9835"))

# RQ queues do Zordon (vetorizar/vetorizar_write/extract/ingest) vivem no MESMO
# redis DB da telemetria de GPU: DB 3 (verificado ao vivo — o comentário do
# settings.py está desatualizado; DB 0 tem as filas de enrich do Voyager). Expomos
# a profundidade pra o alerta composto "fila_write runaway AND autovacuum".
# rq guarda a fila como Redis LIST na chave rq:queue:<name>.
RQ_REDIS_DB = int(os.environ.get("GPU_BRIDGE_RQ_REDIS_DB", "3"))
RQ_QUEUES = [q.strip() for q in os.environ.get(
    "GPU_BRIDGE_RQ_QUEUES", "vetorizar_write,vetorizar,extract,ingest"
).split(",") if q.strip()]

# hash key -> role label
HASHES = {
    os.environ.get("GPU_BRIDGE_HASH_VETOR", "vetor:gpu"): "vetorizacao",
    os.environ.get("GPU_BRIDGE_HASH_EXTRACAO", "extracao:gpu"): "extracao",
}

_pool = redis.ConnectionPool(
    host=REDIS_HOST,
    port=REDIS_PORT,
    db=REDIS_DB,
    password=REDIS_PASSWORD,
    decode_responses=True,
    socket_connect_timeout=3,
    socket_timeout=3,
)
_rq_pool = redis.ConnectionPool(
    host=REDIS_HOST,
    port=REDIS_PORT,
    db=RQ_REDIS_DB,
    password=REDIS_PASSWORD,
    decode_responses=True,
    socket_connect_timeout=3,
    socket_timeout=3,
)


def _esc(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"')


def _labels(role: str, host: str, gpu: str) -> str:
    return f'role="{_esc(role)}",host="{_esc(host)}",gpu="{_esc(gpu)}"'


def collect() -> str:
    now = time.time()
    lines = [
        "# HELP voyager_gpu_util GPU utilization percent (0-100), from Redis vetor:gpu/extracao:gpu.",
        "# TYPE voyager_gpu_util gauge",
        "# HELP voyager_gpu_mem_used_bytes GPU memory used in bytes.",
        "# TYPE voyager_gpu_mem_used_bytes gauge",
        "# HELP voyager_gpu_mem_total_bytes GPU memory total in bytes.",
        "# TYPE voyager_gpu_mem_total_bytes gauge",
        "# HELP voyager_gpu_age_seconds Seconds since this host last reported telemetry.",
        "# TYPE voyager_gpu_age_seconds gauge",
        "# HELP voyager_gpu_stale 1 if telemetry older than stale threshold (GPU hang / reporter dead).",
        "# TYPE voyager_gpu_stale gauge",
        "# HELP voyager_gpu_hosts_total Number of GPU hosts seen per role.",
        "# TYPE voyager_gpu_hosts_total gauge",
        "# HELP voyager_gpu_bridge_up 1 if the bridge could read Redis on last scrape.",
        "# TYPE voyager_gpu_bridge_up gauge",
        "# HELP voyager_rq_queue_depth Pending jobs in an RQ queue (Redis LIST rq:queue:<name>).",
        "# TYPE voyager_rq_queue_depth gauge",
    ]
    up = 1
    per_role_count: dict[str, int] = {r: 0 for r in HASHES.values()}
    try:
        r = redis.Redis(connection_pool=_pool)
        for hash_key, role in HASHES.items():
            raw = r.hgetall(hash_key) or {}
            for host, blob in raw.items():
                try:
                    d = json.loads(blob)
                except (ValueError, TypeError):
                    continue
                per_role_count[role] += 1
                gpu = str(d.get("name", "unknown"))
                lbl = _labels(role, host, gpu)
                util = d.get("util")
                mem_used = d.get("mem_used")
                mem_total = d.get("mem_total")
                ts = d.get("ts")
                if util is not None:
                    lines.append(f"voyager_gpu_util{{{lbl}}} {float(util)}")
                if mem_used is not None:
                    lines.append(f"voyager_gpu_mem_used_bytes{{{lbl}}} {float(mem_used) * 1024 * 1024}")
                if mem_total is not None:
                    lines.append(f"voyager_gpu_mem_total_bytes{{{lbl}}} {float(mem_total) * 1024 * 1024}")
                if ts is not None:
                    age = max(0.0, now - float(ts))
                    lines.append(f"voyager_gpu_age_seconds{{{lbl}}} {age:.1f}")
                    lines.append(f"voyager_gpu_stale{{{lbl}}} {1 if age > STALE_S else 0}")
    except Exception:  # noqa: BLE001 - bridge must never 500; expose bridge_up=0
        up = 0
    for role, cnt in per_role_count.items():
        lines.append(f'voyager_gpu_hosts_total{{role="{_esc(role)}"}} {cnt}')
    # RQ queue depths (best-effort; failure just omits the series, keeps bridge_up).
    try:
        rq = redis.Redis(connection_pool=_rq_pool)
        for q in RQ_QUEUES:
            depth = rq.llen(f"rq:queue:{q}")
            lines.append(f'voyager_rq_queue_depth{{queue="{_esc(q)}"}} {int(depth)}')
    except Exception:  # noqa: BLE001
        pass
    lines.append(f"voyager_gpu_bridge_up {up}")
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") in ("/metrics", ""):
            body = collect().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.rstrip("/") == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok\n")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_args):  # silence access log
        return


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"gpu-bridge on :{PORT} -> redis {REDIS_HOST}:{REDIS_PORT}/{REDIS_DB} "
          f"hashes={list(HASHES)} stale={STALE_S}s", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
