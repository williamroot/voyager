#!/usr/bin/env bash
# Loop do coletor da Sala de Controle dos Treinos (ciclo 120s).
#
# 1. roda scripts/coletor_treinos.py (coleta SSH + escreve treinos_status.json);
# 2. deploya o JSON pro container web de prod (scp + docker cp — hot, sem restart).
#
# Lançar (na máquina de operação):
#   setsid nohup bash scripts/coletor_treinos.sh > <scratchpad>/coletor_treinos.log 2>&1 &
set -u

SP="/tmp/claude-1000/-home-ubuntu-projetos-voyager/b7f4c7ca-b394-400b-9efa-3beeb1603c49/scratchpad"
JSON="$SP/treinos_status.json"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CICLO=120

echo "[coletor_treinos] start pid=$$ repo=$REPO"

while true; do
  if python3 "$REPO/scripts/coletor_treinos.py"; then
    if [ -f "$JSON" ]; then
      if scp -o ConnectTimeout=10 -o BatchMode=yes "$JSON" voyager:/tmp/treinos_status.json \
         && ssh -o ConnectTimeout=10 -o BatchMode=yes voyager \
              "docker cp /tmp/treinos_status.json voyager-web-1:/app/.ia/treinos_status.json"; then
        :
      else
        echo "[coletor_treinos] $(date -Is) deploy FALHOU (tenta de novo no próximo ciclo)"
      fi
    fi
  else
    echo "[coletor_treinos] $(date -Is) coleta FALHOU (tenta de novo no próximo ciclo)"
  fi
  sleep "$CICLO"
done
