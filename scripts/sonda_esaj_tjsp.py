"""SONDA AO VIVO do e-SAJ TJSP: classifica a resposta REAL para
processos hoje marcados `nao_encontrado`, estratificado por (prefixo do
sequencial CNJ, ano_cnj).

Amostra: /tmp/e3_amostra_tjsp.json (semente 20260825, 2500 ancoras, blocos de
40 pks — desenho por conglomerado, o mesmo da auditoria de 24/08).
Sub-amostragem por estrato com random.Random(20260825).

Poucas requisicoes, espacadas: PAUSA_S entre elas, direto (sem proxy), pela
mesma rota de rede dos workers. Somente leitura; nao escreve no banco nem
publica no stream.

Desfechos: os cinco de `enrichers.esaj.classificar_resposta` (DETALHE_PARTES,
DETALHE_VAZIO, NAO_EXISTE, SEGREDO, LISTA, AMBIGUO) mais BLOQUEIO (403/429/WAF)
e ERRO_HTTP (5xx/transporte).

Resultado de 25/08/2026, 62 requisicoes (matriz completa em .ia/OPS.md
§"TJSP com 0,6% de `ok`"): prefixo 4 + ano >= 2025 = 16/16 NAO_EXISTE (estao no
eproc, nao no e-SAJ); prefixos 0/1 = 30/32 com cadastro ou lista AGORA (falsos-
negativos do bug de 2026-07-06); segredo real = 3 de 62.

Como rodar (de um container com Django, pela rota de rede dos workers):
    docker exec -w /app -e POR_ESTRATO=8 -e PAUSA_S=3 \
        voyager-worker_default-1 python /app/scripts/sonda_esaj_tjsp.py
"""
import json
import os
import random
import re
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, '/app')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
import django

django.setup()

import requests  # noqa: E402

from enrichers.esaj import (  # noqa: E402
    DEFAULT_HEADERS,
    BaseEsajEnricher,
    _format_cnj,
    classificar_resposta,
)

SEMENTE = 20260825
POR_ESTRATO = int(os.environ.get('POR_ESTRATO', '8'))
PAUSA_S = float(os.environ.get('PAUSA_S', '3'))
BASE = 'https://esaj.tjsp.jus.br'
OUT = os.environ.get('OUT', '/tmp/e3_sonda_html')
AMOSTRA = os.environ.get('AMOSTRA', '/tmp/e3_amostra_tjsp.json')
os.makedirs(OUT, exist_ok=True)

rnd = random.Random(SEMENTE)
with open(AMOSTRA) as _f:
    d = json.load(_f)

# ---- estratos ----
ESTRATOS = [
    ('ne_4_2026', 'nao_encontrado', lambda x: x['cnj'][0] == '4' and x['ano'] == 2026),
    ('ne_4_2025', 'nao_encontrado', lambda x: x['cnj'][0] == '4' and x['ano'] == 2025),
    ('ne_1_2026', 'nao_encontrado', lambda x: x['cnj'][0] == '1' and x['ano'] == 2026),
    ('ne_1_2024', 'nao_encontrado', lambda x: x['cnj'][0] == '1' and x['ano'] == 2024),
    ('ne_1_2021', 'nao_encontrado', lambda x: x['cnj'][0] == '1' and x['ano'] == 2021),
    ('ne_0_2025', 'nao_encontrado', lambda x: x['cnj'][0] == '0' and x['ano'] == 2025),
    ('ne_2_2025', 'nao_encontrado', lambda x: x['cnj'][0] == '2' and x['ano'] == 2025),
    ('CTRL_ok',   'ok',             lambda x: True),
]

alvos = []
for nome, chave, filtro in ESTRATOS:
    pool = [x for x in d[chave] if filtro(x)]
    rnd.shuffle(pool)
    n = POR_ESTRATO if nome != 'CTRL_ok' else 6
    for x in pool[:n]:
        alvos.append((nome, x))
    print(f'{nome}: {len(pool)} disponiveis -> {min(n, len(pool))} sondados')

print(f'\nTOTAL {len(alvos)} requisicoes, {PAUSA_S}s entre elas '
      f'(~{len(alvos)*PAUSA_S/60:.1f} min)\n')

# A classificação é a MESMA de produção (`enrichers.esaj.classificar_resposta`)
# — a sonda não pode ter um classificador próprio, senão ela mede outra coisa.
# Ver `tests/test_esaj_segredo.py` para o porquê de ela ser ESTRUTURAL: a frase
# de segredo mora num popup escondido em toda página de detalhe, e
# `class="classeProcesso"` da página de lista casa o substring `classeProcesso`.


def classificar(resp):
    if resp.status_code in (403, 429):
        return 'BLOQUEIO', f'http {resp.status_code}'
    if resp.status_code >= 500:
        return 'ERRO_HTTP', f'http {resp.status_code}'
    if re.search(r'awsWafCookie|challenge\.js|Request unsuccessful', resp.text, re.I):
        return 'BLOQUEIO', 'challenge/WAF no corpo'
    return classificar_resposta(resp.text).upper(), f'{len(resp.text)} bytes'


sess = requests.Session()
sess.headers.update(DEFAULT_HEADERS)
res = []
t0 = time.time()
for i, (estrato, x) in enumerate(alvos, 1):
    cnj_fmt = _format_cnj(x['cnj'])
    grau = BaseEsajEnricher._grau(x['cnj'])
    path = 'cposg' if grau == '2g' else 'cpopg'
    params = BaseEsajEnricher._build_search_params(cnj_fmt, grau)
    sess.cookies.clear()
    try:
        sess.get(f'{BASE}/{path}/open.do', timeout=(10, 60))
        r = sess.get(f'{BASE}/{path}/search.do', params=params,
                     timeout=(10, 60), allow_redirects=True)
        desfecho, detalhe = classificar(r)
        info = {'estrato': estrato, 'id': x['id'], 'cnj': cnj_fmt, 'ano': x['ano'],
                'grau': grau, 'http': r.status_code, 'redirect': bool(r.history),
                'bytes': len(r.text), 'desfecho': desfecho, 'detalhe': detalhe}
        fn = f'{OUT}/{estrato}_{x["id"]}_{desfecho}.html'
        with open(fn, 'w') as f:
            f.write(r.text)
    except Exception as exc:
        info = {'estrato': estrato, 'id': x['id'], 'cnj': cnj_fmt, 'ano': x['ano'],
                'grau': grau, 'http': None, 'redirect': None, 'bytes': 0,
                'desfecho': 'ERRO_HTTP', 'detalhe': str(exc)[:120]}
    res.append(info)
    print(f'[{i:3d}/{len(alvos)}] {estrato:11s} {info["cnj"]} ano={x["ano"]} '
          f'http={info["http"]} redir={info["redirect"]} {info["bytes"]:6d}B '
          f'=> {info["desfecho"]:15s} {info["detalhe"]}', flush=True)
    time.sleep(PAUSA_S)

print(f'\n{len(res)} requisicoes em {time.time()-t0:.0f}s\n')

print('== MATRIZ estrato x desfecho ==')
m = defaultdict(Counter)
for r in res:
    m[r['estrato']][r['desfecho']] += 1
desf = sorted({r['desfecho'] for r in res})
print(f'{"estrato":12s} ' + ' '.join(f'{x:>15s}' for x in desf))
for e, _, _ in ESTRATOS:
    if e in m:
        print(f'{e:12s} ' + ' '.join(f'{m[e][x]:>15d}' for x in desf))

print('\n== MATRIZ ano x desfecho (so nao_encontrado) ==')
ma = defaultdict(Counter)
for r in res:
    if r['estrato'].startswith('ne_'):
        ma[r['ano']][r['desfecho']] += 1
print(f'{"ano":6s} ' + ' '.join(f'{x:>15s}' for x in desf))
for ano in sorted(ma):
    print(f'{ano!s:6s} ' + ' '.join(f'{ma[ano][x]:>15d}' for x in desf))

with open('/tmp/e3_sonda_result.json', 'w') as f:
    json.dump({'semente': SEMENTE, 'por_estrato': POR_ESTRATO,
               'pausa_s': PAUSA_S, 'n_requisicoes': len(res), 'res': res}, f, indent=1)
print('\ngravado /tmp/e3_sonda_result.json e HTMLs em', OUT)
