"""Probe dinâmico de enricher (dev/recon — base descartável, read-only no código).

Monta uma subclasse de BasePjeEnricher OU BaseEsajEnricher a partir de env vars,
semeia Tribunal+Process mínimos e roda enriquecer() via Cortex com direct_apply,
imprimindo status + dados + partes. Permite testar viabilidade de um tribunal SEM
editar enrichers/jobs.py (seguro pra N agentes em paralelo: cada um usa SIGLA/CNJ
distintos).

Env:
  KIND        = pje | esaj
  SIGLA       = ex TJBA
  CNJ         = ex 8002640-67.2021.8.05.0039
  BASE_URL    = ex https://pje.tjba.jus.br
  LIST_URL    = (pje) ex https://pje.tjba.jus.br/pje/ConsultaPublica/listView.seam
  DETALHE_PATH= (pje) ex /pje/ConsultaPublica/DetalheProcessoConsultaPublica
  CPOSG_PATH  = (esaj, opcional) ex cposg / cposg5

Uso: python manage.py shell -c "exec(open('scripts/probe_dynamic.py').read())"
"""
import os

from tribunals.models import Tribunal, Process, ProcessoParte

KIND = os.environ['KIND']
SIGLA = os.environ['SIGLA']
CNJ = os.environ['CNJ']
BASE_URL = os.environ['BASE_URL']

trib, _ = Tribunal.objects.get_or_create(
    sigla=SIGLA, defaults={'nome': SIGLA, 'sigla_djen': SIGLA, 'ativo': True})
proc, _ = Process.objects.get_or_create(tribunal=trib, numero_cnj=CNJ)
ProcessoParte.objects.filter(processo=proc).delete()

if KIND == 'pje':
    from enrichers.pje import BasePjeEnricher

    class _Dyn(BasePjeEnricher):
        pass
    _Dyn.BASE_URL = BASE_URL
    _Dyn.LIST_URL = os.environ['LIST_URL']
    _Dyn.DETALHE_PATH = os.environ['DETALHE_PATH']
    _Dyn.TRIBUNAL_SIGLA = SIGLA
    _Dyn.LOG_NAME = f'voyager.enrichers.{SIGLA.lower()}'
elif KIND == 'esaj':
    from enrichers.esaj import BaseEsajEnricher

    class _Dyn(BaseEsajEnricher):
        pass
    _Dyn.BASE_URL = BASE_URL
    _Dyn.TRIBUNAL_SIGLA = SIGLA
    _Dyn.LOG_NAME = f'voyager.enrichers.{SIGLA.lower()}'
    if os.environ.get('CPOSG_PATH'):
        _Dyn.CPOSG_PATH = os.environ['CPOSG_PATH']
else:
    raise SystemExit(f'KIND inválido: {KIND}')

enricher = _Dyn(prefer_cortex=True)
res = enricher.enriquecer(proc, direct_apply=True)
print('RESULT:', res)
proc.refresh_from_db()
pp = ProcessoParte.objects.filter(processo=proc).select_related('parte')
print('CLASSE:', proc.classe_id, '| PARTES:', pp.count())
for x in pp[:10]:
    print(f'  [{x.polo}/{x.papel}] {x.parte.nome!r} doc={x.parte.documento!r} oab={x.parte.oab!r}')
