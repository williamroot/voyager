"""Probe de validação live de enricher (uso em dev — base descartável).

Uso: python manage.py shell -c "exec(open('scripts/live_enrich_probe.py').read())"
com env SIGLA e CNJ. Cria Tribunal+Process mínimos, roda o enricher via Cortex
com direct_apply, e imprime status + dados + contagem de partes por polo.
"""
import os
import datetime as _dt

from django.utils import timezone

from tribunals.models import Tribunal, Process, ProcessoParte
from enrichers.jobs import _ENRICHERS

SIGLA = os.environ['SIGLA']
CNJ = os.environ['CNJ']

trib, _ = Tribunal.objects.get_or_create(
    sigla=SIGLA,
    defaults={'nome': SIGLA, 'sigla_djen': SIGLA, 'ativo': True},
)
proc, _ = Process.objects.get_or_create(
    tribunal=trib, numero_cnj=CNJ,
)
# limpa partes anteriores de uma rerun
ProcessoParte.objects.filter(processo=proc).delete()

cls = _ENRICHERS[SIGLA]
enricher = cls(prefer_cortex=True)  # dev: pool vazio → força Cortex
res = enricher.enriquecer(proc, direct_apply=True)

print('RESULT:', res)
proc.refresh_from_db()
pp = ProcessoParte.objects.filter(processo=proc).select_related('parte')
print('CLASSE:', proc.classe_id, '| ASSUNTO:', proc.assunto_id, '| ORGAO:', getattr(proc, 'orgao_julgador', None))
print('PARTES no banco:', pp.count())
for x in pp[:12]:
    print(f'  [{x.polo}/{x.papel}] {x.parte.nome!r} doc={x.parte.documento!r} oab={x.parte.oab!r} tipo={x.parte.tipo}')
