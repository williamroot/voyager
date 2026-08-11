"""Sincroniza `tem_sinal_precatorio=true` dos Process pro Elasticsearch.

Cirúrgico e leve: NÃO reindexa o doc inteiro (não lê partes, não puxa 11M do
Postgres). Lê SÓ os ids dos processos com sinal (via índice composto
proc_trib_sinalprec_idx → seek rápido) e manda um bulk `_update` parcial no ES
setando o campo. Escrita 100% no ES; leitura mínima no Postgres.

Acende a camada "Potencial" do Mapa Comercial após o backfill Fase 0. Só marca os
TRUE — os demais já contam como não-potencial no filtro `tem_sinal_precatorio=true`.

    python manage.py sync_sinal_es --tribunais TJPR,TRF4,TRF6,TRF2
"""
import json
import time

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

from tribunals.models import Process

DATAJUD_ALVO = ['TJPR', 'TRF4', 'TRF6', 'TRF2']


class Command(BaseCommand):
    help = 'Sync tem_sinal_precatorio=true dos Process pro ES (bulk _update parcial, ES-only).'

    def add_arguments(self, p):
        p.add_argument('--tribunais', default=','.join(DATAJUD_ALVO))
        p.add_argument('--batch-size', type=int, default=3000)
        p.add_argument('--sleep', type=float, default=0.0, help='pausa (s) entre bulks')

    def handle(self, *a, **o):
        es = getattr(settings, 'ELASTICSEARCH_URL', 'http://elasticsearch:9200').rstrip('/')
        idx = f"{getattr(settings, 'ELASTICSEARCH_INDEX_PREFIX', 'voyager')}-processos"
        tribs = [s.strip().upper() for s in o['tribunais'].split(',') if s.strip()]
        bs, slp = o['batch_size'], o['sleep']

        # só os ids (values_list) com sinal — usa o índice composto (tribunal, tem_sinal).
        qs = (Process.objects
              .filter(tribunal_id__in=tribs, tem_sinal_precatorio=True)
              .order_by('id').values_list('id', flat=True))
        total = qs.count()
        self.stdout.write(f'Sync sinal→ES: {total:,} docs (tribs={tribs}, batch={bs}) → {idx}')
        self.stdout.flush()

        sess = requests.Session()

        def flush(ids):
            if not ids:
                return 0
            lines = []
            for pid in ids:
                lines.append(json.dumps({'update': {'_index': idx, '_id': pid}}))
                lines.append(json.dumps({'doc': {'tem_sinal_precatorio': True}}))
            body = ('\n'.join(lines) + '\n').encode('utf-8')
            try:
                r = sess.post(f'{es}/_bulk', data=body,
                              headers={'Content-Type': 'application/x-ndjson'}, timeout=180)
                if r.status_code >= 300:
                    self.stderr.write(f'  bulk HTTP {r.status_code}: {r.text[:200]}')
                    return 0
                # conta erros não-404 (404 = doc ainda não indexado; tolerável)
                res = r.json()
                if res.get('errors'):
                    graves = sum(1 for it in res.get('items', [])
                                 if it.get('update', {}).get('status') not in (200, 201, 404))
                    if graves:
                        self.stderr.write(f'  {graves} erros graves no bulk')
            except Exception as e:  # noqa: BLE001
                self.stderr.write(f'  bulk ERRO: {str(e)[:200]}')
                return 0
            return len(ids)

        buf, enviados, t0 = [], 0, time.monotonic()
        for pid in qs.iterator(chunk_size=bs):
            buf.append(pid)
            if len(buf) >= bs:
                flush(buf)
                enviados += len(buf)
                buf = []
                el = time.monotonic() - t0
                rate = enviados / el if el else 0
                eta = (total - enviados) / rate / 60 if rate else 0
                self.stdout.write(f'  {enviados:,}/{total:,} ({100*enviados/max(total,1):.1f}%) '
                                  f'· {rate:.0f}/s · ETA {eta:.0f}min')
                self.stdout.flush()
                if slp:
                    time.sleep(slp)
        flush(buf)
        enviados += len(buf)
        self.stdout.write(self.style.SUCCESS(f'Concluído: {enviados:,} docs marcados no ES.'))
