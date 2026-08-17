"""Mede o tamanho (Content-Length) de todos os cadernos de um dia, sem baixar o corpo.

Só os headers: `stream=True` + fechar a conexão. É o jeito de estimar volume sem
puxar gigabytes do servidor do CSJT.
"""
import re, sys, html, time
from dejt_probe import novo_sessao, consultar, viewstate, BASE
from baixar import linhas_resultado

di = df = sys.argv[1]
cad = sys.argv[2] if len(sys.argv) > 2 else 'J'
s = novo_sessao()
r = consultar(s, '', di, df, cad)
rows = linhas_resultado(r.text)
print('cadernos:', len(rows))
total = 0
t0 = time.time()
for i, (data, titulo, src) in enumerate(rows):
    vs = viewstate(r.text)
    p = {'corpo:formulario': 'corpo:formulario',
         'corpo:formulario:tipoCaderno': '1' if cad == 'J' else '0',
         'corpo:formulario:dataIni': di, 'corpo:formulario:dataFim': df,
         'corpo:formulario:tribunal': '', 'corpo:formulario:ordenacaoPlc': '',
         'org.apache.myfaces.trinidad.faces.FORM': 'corpo:formulario',
         '_noJavaScript': 'false', 'javax.faces.ViewState': vs, 'source': src}
    t1 = time.time()
    with s.post(f'{BASE}/f/n/diariocon', data=p, timeout=180, stream=True) as resp:
        cl = int(resp.headers.get('content-length') or 0)
        ct = resp.headers.get('content-type')
        cd = resp.headers.get('content-disposition')
    total += cl
    print(f'{i:2d} {html.unescape(titulo)[:60]:60s} {ct:20s} {cl/1e6:9.2f} MB  {time.time()-t1:.1f}s {cd}')
print(f'TOTAL {total/1e6:.1f} MB em {time.time()-t0:.0f}s')
