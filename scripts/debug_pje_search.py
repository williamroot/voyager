"""Debug do PJe: reproduz _buscar_processo e salva o HTML pós-busca pra inspeção."""
import os
from bs4 import BeautifulSoup
from enrichers.pje import BasePjeEnricher, CAMPO_NUM

SIGLA = os.environ['SIGLA']; CNJ = os.environ['CNJ']

class _Dyn(BasePjeEnricher):
    pass
_Dyn.BASE_URL = os.environ['BASE_URL']
_Dyn.LIST_URL = os.environ['LIST_URL']
_Dyn.DETALHE_PATH = os.environ['DETALHE_PATH']
_Dyn.TRIBUNAL_SIGLA = SIGLA
_Dyn.LOG_NAME = f'voyager.enrichers.{SIGLA.lower()}'

e = _Dyn(prefer_cortex=True)
resp = e._get(e.LIST_URL)
soup = BeautifulSoup(resp.text, 'html.parser')
vs = soup.find('input', {'name': 'javax.faces.ViewState'})
print('LISTVIEW status:', resp.status_code, 'ViewState?', bool(vs and vs.get('value')))
fields = e._extract_form_fields(soup)
search_id = e._find_search_script_id(soup) or 'fPP:j_id268'
print('search_id:', search_id, '| CAMPO_NUM:', CAMPO_NUM)
payload = dict(fields)
payload[CAMPO_NUM] = CNJ
payload['fPP'] = 'fPP'
payload['AJAXREQUEST'] = '_viewRoot'
payload['javax.faces.ViewState'] = vs['value']
payload[search_id] = search_id
payload['AJAX:EVENTS_COUNT'] = '1'
resp = e._post(e.LIST_URL, payload)
out = f'/tmp/pje_{SIGLA}_search.html'
open(out, 'w').write(resp.text)
print('POST status:', resp.status_code, 'len:', len(resp.text), '->', out)
low = resp.text.lower()
for m in ['detalheprocesso', 'detalhe', 'idprocesso', 'captcha', 'hcaptcha', 'recaptcha',
          'nenhum registro', 'não foram encontrados', 'consultapublica', 'href=']:
    print(f'  marker {m!r}:', low.count(m))
import re
for pat in [r'/[A-Za-z/]*Detalhe[A-Za-z]*', r'idProcesso\w*["\']?\s*[:=]\s*["\']?\d+']:
    hits = re.findall(pat, resp.text)[:3]
    print(f'  regex {pat} ->', hits)
