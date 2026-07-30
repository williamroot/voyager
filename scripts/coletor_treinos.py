#!/usr/bin/env python3
"""Coletor da Sala de Controle dos Treinos (dashboard /dashboard/modelos/treinos/).

Roda na máquina de operação (fora do prod), 1 ciclo por invocação:
  1. coleta o estado de cada run ativo via SSH (llmsv2, pods QuickPod) e arquivos locais;
  2. faz merge com o estado anterior (fonte fora do ar => marca stale, mantém último dado);
  3. escreve treinos_status.json (atômico) no scratchpad.

O deploy do JSON pro container web (scp + docker cp) fica no wrapper
scripts/coletor_treinos.sh, que roda este script em loop de 120s.

Robustez: NENHUMA falha de fonte derruba o ciclo — cada coleta tem timeout
próprio e, em falha, o run anterior é preservado com stale=True.
"""
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone

SCRATCH = "/tmp/claude-1000/-home-ubuntu-projetos-voyager/b7f4c7ca-b394-400b-9efa-3beeb1603c49/scratchpad"
OUT_PATH = os.path.join(SCRATCH, "treinos_status.json")
SSH_TIMEOUT = 25  # s por fonte

SSH_BASE = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
            "-o", "BatchMode=yes"]


def _run(cmd, timeout=SSH_TIMEOUT):
    """Executa comando; retorna (ok, stdout). Nunca levanta exceção."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0, (p.stdout or "")
    except Exception:
        return False, ""


def _parse_bar(texto, total_default):
    """Extrai (step, total, s_per_step, loss) de um tail de log HF/tqdm."""
    step = total = s_it = loss = None
    # barra tqdm: " 23%|██▎ | 710/3045 [8:15:32<27:22:11, 42.20s/it]"
    m = list(re.finditer(r"(\d+)/(\d+)", texto))
    if m:
        step, total = int(m[-1].group(1)), int(m[-1].group(2))
        if total_default and total not in (total_default,) and total < 100:
            # fração espúria (ex.: '2.675' quebrado) — descarta
            step = total = None
    mi = list(re.finditer(r"([0-9]+(?:\.[0-9]+)?)s/it", texto))
    if mi:
        s_it = float(mi[-1].group(1))
    ml = list(re.finditer(r"'loss':\s*([0-9]+(?:\.[0-9]+)?)", texto))
    if ml:
        loss = float(ml[-1].group(1))
    epoch = None
    me = list(re.finditer(r"'epoch':\s*([0-9]+(?:\.[0-9]+)?)", texto))
    if me:
        epoch = float(me[-1].group(1))
    return step, total, s_it, loss, epoch


# ---------------------------------------------------------------- coletas ---

def coleta_v21():
    ok, out = _run(SSH_BASE + ["ubuntu@llmsv2",
                               "tail -c 1500 /mnt/nas-data/voyager-train/logs/train_v21.log"])
    if not ok:
        return None
    step, total, s_it, loss, epoch = _parse_bar(out.replace("\r", "\n"), 3045)
    total = total or 3045
    # a época é sempre confiável; a barra do tqdm às vezes fica fora do tail.
    # deriva o step da época (2 épocas → total steps) e usa o maior dos dois.
    if epoch:
        step = max(step or 0, round(epoch / 2.0 * total))
    return {"step": step, "total": total, "s_per_step": s_it, "loss": loss,
            "epoch": epoch, "done_hint": "train_runtime" in out or "100%|" in out}


def coleta_ab():
    ok, out = _run(SSH_BASE + ["-p", "34800", "-i", os.path.expanduser("~/.ssh/quickpod_ab"),
                               "-o", "IdentitiesOnly=yes",
                               "e100a55d-5247-412b-ba81-7c8c29cd874a@159.48.242.22",
                               "tail -c 1500 /root/train/train_ab.log"])
    if not ok:
        return None
    step, total, s_it, loss, epoch = _parse_bar(out.replace("\r", "\n"), 3045)
    total = total or 3045
    if epoch:
        step = max(step or 0, round(epoch / 2.0 * total))
    return {"step": step, "total": total, "s_per_step": s_it, "loss": loss,
            "epoch": epoch, "done_hint": "train_runtime" in out or "100%|" in out}


def coleta_esp():
    key = os.path.join(SCRATCH, "pod_esp.key")
    ok, out = _run(SSH_BASE + ["-p", "23700", "-i", key, "-o", "IdentitiesOnly=yes",
                               "4ab71868-5c96-4676-afe3-83bb45f50e80@107.222.215.224",
                               "grep -c 'TAREFA=.*END' /root/train/train_esp.log; "
                               "grep -oE 'TAREFA=[a-z_]+ START' /root/train/train_esp.log | tail -1"])
    if not ok:
        return None
    linhas = [l.strip() for l in out.splitlines() if l.strip()]
    feitas = None
    tarefa = None
    for l in linhas:
        if re.fullmatch(r"\d+", l):
            feitas = int(l)
        m = re.search(r"TAREFA=([a-z_]+) START", l)
        if m:
            tarefa = m.group(1)
    if feitas is None:
        return None
    return {"step": feitas, "total": 7, "s_per_step": None, "loss": None,
            "tarefa_atual": tarefa, "done_hint": feitas >= 7}


def coleta_dapt():
    # spec original: grep -oE '[0-9]+/3814' — o log real mostra N/3814; parseamos
    # a barra genericamente (local) pra sobreviver a mudança de total.
    ok, out = _run(SSH_BASE + ["-p", "23280", "-i", os.path.expanduser("~/.ssh/quickpod_dapt"),
                               "-o", "IdentitiesOnly=yes",
                               "7609c6d1-4089-4269-8218-5e2aa510c4ff@107.222.215.224",
                               "tail -c 300 /root/dapt/train.log | tr '\\r' '\\n' | tail -3"])
    if not ok:
        return None
    step, total, s_it, loss, _epoch = _parse_bar(out, 3814)
    if step is None:
        return None
    return {"step": step, "total": total or 3814, "s_per_step": s_it, "loss": loss,
            "done_hint": "100%|" in out}


def coleta_shadow():
    path = "/home/ubuntu/projetos/precatorio-ai-analyzer/dataset/shadow_oficio_results.jsonl"
    try:
        with open(path, "rb") as f:
            n = sum(1 for _ in f)
    except OSError:
        return None
    return {"step": n, "total": 30, "s_per_step": None, "loss": None,
            "done_hint": n >= 30}


# ------------------------------------------------------------- definições ---

RUNS_ATIVOS = [
    {
        "id": "v21", "coleta": coleta_v21,
        "nome": "Retreino v2.1 — Qwen2.5-7B QLoRA",
        "desc": "Retreino Ficha da Parte v2.1 — janelamento de docs longos + gold melhorado (211k exemplos).",
        "onde": "llmsv2 · RTX 3090 24GB (local)", "custo": "R$ luz",
        "total_default": 3045, "s_step_default": 42.0, "gpu": True,
        "custo_hora_usd": 0.0,
    },
    {
        "id": "ab_qwen3", "coleta": coleta_ab,
        "nome": "A/B de base — Qwen3-8B",
        "desc": "A/B de modelo base: Qwen3-8B vs Qwen2.5 no MESMO dataset — decide a base da próxima geração.",
        "onde": "QuickPod · RTX 4090 24GB", "custo": "$0,31/h",
        "total_default": 3045, "s_step_default": 18.0, "gpu": True,
        "custo_hora_usd": 0.31,
    },
    {
        "id": "especialistas", "coleta": coleta_esp,
        "nome": "7 Especialistas LoRA",
        "desc": "1 adapter especialista por tipo de documento — testa se especialização vence multi-task nas classes fracas.",
        "onde": "QuickPod · RTX 3090 24GB", "custo": "$0,186/h",
        "total_default": 7, "s_step_default": None, "gpu": True,
        "custo_hora_usd": 0.186, "unidade": "tarefas",
    },
    {
        "id": "dapt", "coleta": coleta_dapt,
        "nome": "DAPT — pré-treino continuado",
        "desc": "Pré-treino continuado no NOSSO corpus de 279M tokens de autos — o modelo aprende o dialeto do precatório antes da tarefa.",
        "onde": "QuickPod · RTX 3090 24GB", "custo": "$0,186/h",
        "total_default": 3814, "s_step_default": 72.5, "gpu": True,
        "custo_hora_usd": 0.186,
    },
    {
        "id": "shadow_oficio", "coleta": coleta_shadow,
        "nome": "Shadow ofício — Analista A2",
        "desc": "Nosso extrator vs GPT do JuriscopeIA nos mesmos ofícios — prova do swap sem custo OpenAI.",
        "onde": "CPU local (sem GPU)", "custo": "R$ 0",
        "total_default": 30, "s_step_default": None, "gpu": False,
        "custo_hora_usd": 0.0, "unidade": "ofícios",
    },
]

RUNS_CONCLUIDOS = [
    {"id": "extrator_v1", "nome": "Extrator v1", "status": "done",
     "desc": "Primeiro fine-tune do extrator de precatórios (Qwen2.5-7B QLoRA).",
     "conclusao": "macro 87,9% TEST cego (+9,9pp vs base). Natureza 99,1 · valor 91,8 · cessão 100. GGUF 4,68GB serve em 6GB VRAM. 2,4s/extração."},
    {"id": "extrator_v2", "nome": "Extrator v2 — Ficha da Parte", "status": "done",
     "desc": "Extração entity-centric: ficha por parte (valores, pagamentos, juiz).",
     "conclusao": "gate PARCIAL: pagamentos 100%, juiz 99%, partes 54→75. Fracas por dado (docs longos) → originou o v2.1. GGUF v2 empacotado."},
    {"id": "dataset_v21", "nome": "Dataset v2.1", "status": "done",
     "desc": "Regeração do dataset de treino com janelamento de documentos longos.",
     "conclusao": "211.928 exemplos, 37k docs longos recuperados via janelamento, cessão ×4,3, pagamento ×4,5. Corpus inteiro em 56min."},
    {"id": "sdk", "nome": "SDK standalone", "status": "done",
     "desc": "Extrator empacotado como SDK: pdf → ficha sem depender da infra Voyager/Zordon.",
     "conclusao": "pdf→ficha sem infra: 53 testes, 0 JSON inválido, 192 docs/h em CPU (≈6.000/h em GPU)."},
    {"id": "merger", "nome": "Merger/Ledger", "status": "done",
     "desc": "Consolidação das fichas por processo: entidades, saldo e jurimetria derivada.",
     "conclusao": "resolução de entidades + saldo derivado + jurimetria, 24/24 testes."},
]

RUNS_FILA = [
    {"id": "dpo_kappa", "nome": "DPO-κ", "status": "queued",
     "desc": "Preference tuning com os 2.688 pares da validação humana (κ) já prontos."},
    {"id": "destil_3b", "nome": "Destilação 3B", "status": "queued",
     "desc": "Destilar o extrator 7B num 3B — mesma qualidade alvo, metade da VRAM e 2× a vazão."},
    {"id": "bakeoff", "nome": "Bake-off professores", "status": "queued",
     "desc": "gpt-oss:120b vs glm-5.2 vs kimi-k3 vs deepseek-v4-pro como professor do próximo gold."},
    {"id": "analista_ia", "nome": "Analista IA", "status": "queued",
     "desc": "Substituir o GPT do JuriscopeIA pelo nosso stack (extrator + analista) — corta o custo OpenAI."},
]


# ------------------------------------------------------------------ ciclo ---

def _eta_humana(segundos):
    if segundos is None or segundos <= 0:
        return None
    d, r = divmod(int(segundos), 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


def _estado_anterior():
    try:
        with open(OUT_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return {r["id"]: r for r in data.get("runs", [])}
    except Exception:
        return {}


def ciclo():
    agora = time.time()
    agora_iso = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    anterior = _estado_anterior()
    runs = []

    for spec in RUNS_ATIVOS:
        prev = anterior.get(spec["id"], {})
        dado = spec["coleta"]()
        run = {
            "id": spec["id"], "nome": spec["nome"], "desc": spec["desc"],
            "onde": spec["onde"], "custo": spec["custo"],
            "unidade": spec.get("unidade", "steps"), "gpu": spec["gpu"],
            "custo_hora_usd": spec["custo_hora_usd"],
        }
        if dado is None:
            # fonte fora do ar: preserva último dado conhecido, marca stale
            run.update({k: prev.get(k) for k in
                        ("step", "total", "s_per_step", "loss", "pct", "eta_s",
                         "eta", "tarefa_atual", "last_ok_at", "status")})
            run["total"] = run.get("total") or spec["total_default"]
            run["status"] = prev.get("status") or "running"
            # run já concluído com a fonte fora do ar é esperado (o processo
            # terminou e o log parou) — não é stale, só terminou. Stale só vale
            # pra run *rodando* que perdeu o feed ao vivo.
            run["stale"] = run["status"] != "done"
        else:
            step = dado.get("step")
            if step is None:  # barra não estava no tail — carrega o último visto
                step = prev.get("step")
            total = dado.get("total") or prev.get("total") or spec["total_default"]
            if step is None and dado.get("epoch") is not None and 0 < dado["epoch"] <= 1:
                # fallback: treinos de 1 epoch — estima o step pela fração da epoch
                step = int(dado["epoch"] * total)
            s_step = dado.get("s_per_step") or prev.get("s_per_step") or spec["s_step_default"]
            loss = dado.get("loss") if dado.get("loss") is not None else prev.get("loss")
            done = bool(dado.get("done_hint")) or (step is not None and step >= total)
            eta_s = None
            if not done and step is not None and s_step:
                eta_s = (total - step) * s_step
            run.update({
                "step": step, "total": total, "s_per_step": s_step, "loss": loss,
                "pct": round(100.0 * step / total, 1) if step is not None and total else None,
                "eta_s": eta_s, "eta": _eta_humana(eta_s),
                "tarefa_atual": dado.get("tarefa_atual"),
                "status": "done" if done else "running",
                "stale": False, "last_ok_at": agora,
            })
        if run.get("pct") is None and run.get("step") is not None and run.get("total"):
            run["pct"] = round(100.0 * run["step"] / run["total"], 1)
        runs.append(run)

    for seed in RUNS_CONCLUIDOS + RUNS_FILA:
        r = dict(seed)
        r.setdefault("stale", False)
        runs.append(r)

    rodando = [r for r in runs if r.get("status") == "running"]
    concluidos = [r for r in runs if r.get("status") == "done"]
    gpus = sum(1 for r in rodando if r.get("gpu"))
    custo_h = round(sum(r.get("custo_hora_usd") or 0 for r in rodando), 3)

    payload = {
        "generated_at": agora_iso,
        "generated_at_ts": agora,
        "ciclo_s": 120,
        "contadores": {
            "rodando": len(rodando),
            "concluidos": len(concluidos),
            "na_fila": sum(1 for r in runs if r.get("status") == "queued"),
            "gpus_em_uso": gpus,
            "custo_hora_usd": custo_h,
            "stale": sum(1 for r in runs if r.get("stale")),
        },
        "runs": runs,
    }

    os.makedirs(SCRATCH, exist_ok=True)
    tmp = OUT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT_PATH)
    return payload


if __name__ == "__main__":
    p = ciclo()
    c = p["contadores"]
    print(f"[{p['generated_at']}] ok — rodando={c['rodando']} done={c['concluidos']} "
          f"fila={c['na_fila']} gpus={c['gpus_em_uso']} stale={c['stale']}")
