"""Enfileira os dias que o caminho fatiado por UF decapitou, para um tribunal.

    docker exec <worker> sh -c "python manage.py shell < scripts/backfill_dias_capados.py"

Dry-run por padrão; passe --enfileirar pra valer. Trocar SIGLA muda o tribunal.

Critério (assinatura medida pelo general): run de janela de 1 dia, fonte djen,
com >=9.000 itens e razão itens/página < 700. A razão é a assinatura do caminho
por UF: as 27 fatias terminam cada uma numa página parcial, então a média cai
pra ~490-500/pg; o caminho flat dá ~990-1000.

Enfileira com job_id determinístico — re-disparar não duplica.
"""
from django_rq import get_queue
from tribunals.models import IngestionRun as R

SIGLA = 'TJSP'   # ordem da Fase 2: TJSP → TRF3 → TJMG → TJRS → TJGO → TJRJ
Q = get_queue('djen_backfill')

candidatos = {}
qs = (R.objects.filter(fonte='djen', tribunal__sigla=SIGLA)
      .only('janela_inicio', 'janela_fim', 'paginas_lidas', 'movimentacoes_novas',
            'movimentacoes_duplicadas', 'status', 'started_at'))
for r in qs.iterator(chunk_size=2000):
    if r.janela_inicio != r.janela_fim:
        continue
    tot = (r.movimentacoes_novas or 0) + (r.movimentacoes_duplicadas or 0)
    if tot < 9_000 or not r.paginas_lidas:
        continue
    razao = tot / r.paginas_lidas
    # guarda o run MAIS RECENTE por dia — se o último já foi flat, o dia está bom
    ant = candidatos.get(r.janela_inicio)
    if ant is None or r.started_at > ant[0]:
        candidatos[r.janela_inicio] = (r.started_at, razao, tot)

capados = sorted(d for d, (_, razao, _) in candidatos.items() if razao < 700)
ja_flat = [d for d, (_, razao, _) in candidatos.items() if razao >= 700]
print(f'{SIGLA}: {len(candidatos)} dias de 1 dia com >=9k | capados={len(capados)} | já flat={len(ja_flat)}')
if capados:
    print('  faixa:', capados[0], '->', capados[-1])
    perdido = sum(candidatos[d][2] for d in capados)
    print(f'  publicações capturadas nesses dias: {perdido:,} (o recuperável é ~5,4× isso)')

import sys
if '--enfileirar' not in sys.argv:
    print('DRY-RUN. Passe --enfileirar pra valer.')
    raise SystemExit

n = 0
for d in capados:
    Q.enqueue('djen.jobs.reprocessar_janela', SIGLA, d.isoformat(), d.isoformat(),
              job_id=f'f2:{SIGLA}:{d.isoformat()}', job_timeout=21600)
    n += 1
print(f'enfileirados {n} | fila agora: {Q.count}')
