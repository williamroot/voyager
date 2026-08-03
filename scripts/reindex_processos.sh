#!/bin/sh
# Reindex resumível dos PROCESSOS no Elasticsearch, tribunal a tribunal (throttled).
#
# Roda DENTRO de um container do web (tem o código por bind-mount + requests).
# É auto-contido: sobrevive à sessão do Claude cair (rodar detached, ver runbook).
# Resumível: cada tribunal concluído vai pro DONE; re-rodar PULA os já feitos.
# Idempotente: o bulk usa _id=process.id → re-indexar o mesmo processo é upsert.
#
# Progresso: /app/media/reindex_processos.log  (= host ~/voyager/media/…)
#            e o gráfico ao vivo em /dashboard/indexacao/
set -u
LOG=/app/media/reindex_processos.log
DONE=/app/media/reindex_processos_done.txt
BATCH="${BATCH:-2000}"
SLEEP="${SLEEP:-0.2}"

mkdir -p /app/media
touch "$DONE"
echo "=== reindex START $(date -u +%FT%TZ) batch=$BATCH sleep=${SLEEP}s ===" >> "$LOG"

# lista de tribunais (marcador TRIBS:: pra parse robusto apesar dos logs do boot)
TRIBS=$(python manage.py shell -c "from tribunals.models import Tribunal; print('TRIBS::'+','.join(t.sigla for t in Tribunal.objects.order_by('sigla')))" 2>/dev/null | sed -n 's/^TRIBS:://p' | tr ',' ' ')
if [ -z "$TRIBS" ]; then
  echo "ERRO: não consegui listar tribunais — abortando." >> "$LOG"; exit 1
fi
echo "tribunais ($(echo $TRIBS | wc -w)): $TRIBS" >> "$LOG"

for sig in $TRIBS; do
  if grep -qx "$sig" "$DONE" 2>/dev/null; then
    echo "skip $sig (já concluído)" >> "$LOG"; continue
  fi
  echo "--- $sig START $(date -u +%FT%TZ) ---" >> "$LOG"
  python manage.py reindexar_processos --tribunal "$sig" --batch-size "$BATCH" --sleep "$SLEEP" >> "$LOG" 2>&1
  rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "$sig" >> "$DONE"
    echo "--- $sig DONE $(date -u +%FT%TZ) ---" >> "$LOG"
  else
    echo "--- $sig FALHOU rc=$rc (NÃO marcado; re-tenta no próximo run) ---" >> "$LOG"
  fi
done
echo "=== reindex ALL DONE $(date -u +%FT%TZ) ===" >> "$LOG"
