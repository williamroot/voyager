#!/bin/sh
# Supervisor da passada de entidades.
#
# A passada é RETOMÁVEL (carimbo `ents_v`), então "morreu" e "acabou" se tratam
# igual: relança. O que ela NÃO pode é morrer calada — daí um log por faixa e o
# eco do motivo da saída. Ver .ia/OPS.md.
#
#   uso: entidades.sh "<faixas separadas por espaço>"
FAIXAS_N=32
CD=/home/ubuntu/voyager
for FX in $1; do
  (
    LOG=/tmp/entidades_f${FX}.log
    echo "=== faixa $FX iniciada $(date -Is) ===" >> $LOG
    TENTATIVA=0
    while [ $TENTATIVA -lt 200 ]; do
      TENTATIVA=$((TENTATIVA + 1))
      cd $CD && docker compose -f docker-compose-prod.yml exec -T web \
        python manage.py es_movs_v2 --entidades --faixa $FX --faixas $FAIXAS_N \
        --lote 1000 >> $LOG 2>&1
      RC=$?
      FALTA=$(grep -o 'faltam [0-9,]* docs' $LOG | tail -1 | tr -d 'faltm docs,' )
      echo "--- tentativa $TENTATIVA rc=$RC faltam=$FALTA $(date -Is)" >> $LOG
      [ "$RC" = "0" ] && [ "$FALTA" = "0" ] && break
      sleep 30
    done
    echo "=== faixa $FX encerrada $(date -Is) ===" >> $LOG
  ) &
done
wait
