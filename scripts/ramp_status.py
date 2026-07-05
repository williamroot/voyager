"""Status da rampa de ingestão (trabalhista + superiores) — 2026-07.

Uso: docker compose exec web python scripts/ramp_status.py
Emite 1 linha RAMP com capacidade (Redis/filas) + volume por segmento, e
ALERTA=... quando algum limiar de capacidade é cruzado (pra o monitor decidir
se manda push).
"""
import os

import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

import django_rq  # noqa: E402
from django.db.models import Q  # noqa: E402

from tribunals.models import Process  # noqa: E402

c = django_rq.get_connection('datajud')
mem = c.info('memory')
mem_h = mem['used_memory_human']
mem_bytes = mem['used_memory']
maxmem = mem.get('maxmemory') or 0

bf = len(django_rq.get_queue('djen_backfill'))
ing = len(django_rq.get_queue('djen_ingestion'))

# Capacidade (Redis noeviction em 48G no incidente 2026-07-02) — SEMPRE reportada.
alertas = []
if maxmem and mem_bytes > 0.85 * maxmem:
    alertas.append(f'REDIS {mem_h}/{round(maxmem/1e9)}G (>85%)')
elif mem_bytes > 40e9:
    alertas.append(f'REDIS {mem_h} (>40G)')
if bf > 1_000_000:
    alertas.append(f'BACKFILL {bf:,} (>1M)')

# Volume (informativo) — os count() em 57M+ ficam lentos sob carga de backfill;
# guarda com statement_timeout pra o monitor NUNCA pendurar. n/a se estourar.
def _vol():
    from django.db import connection, transaction
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '20000'")
        trab = Process.objects.filter(Q(tribunal__sigla__startswith='TRT') | Q(tribunal__sigla='TST')).count()
        sup = Process.objects.filter(tribunal__sigla__in=['STJ', 'STF']).count()
        tot = Process.objects.count()
    return f'{trab:,}', f'{sup:,}', f'{tot:,}'

try:
    trab, sup, tot = _vol()
except Exception:
    trab = sup = tot = 'n/a(timeout)'

print(f'RAMP mem={mem_h} backfill={bf:,} ingestion={ing:,} '
      f'trabalhista={trab} superiores={sup} total={tot}')
print('ALERTA=' + ('; '.join(alertas) if alertas else 'nenhum'))
