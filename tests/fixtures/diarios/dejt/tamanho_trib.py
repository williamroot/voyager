import sys, html, re
from dejt_probe import novo_sessao, consultar, viewstate, BASE
from baixar import linhas_resultado

def medir(trib, dia, cad='J'):
    s = novo_sessao()
    r = consultar(s, trib, dia, dia, cad)
    rows = linhas_resultado(r.text)
    if not rows:
        return None, None
    data, titulo, src = rows[0]
    p = {'corpo:formulario': 'corpo:formulario',
         'corpo:formulario:tipoCaderno': '1' if cad == 'J' else '0',
         'corpo:formulario:dataIni': dia, 'corpo:formulario:dataFim': dia,
         'corpo:formulario:tribunal': str(trib), 'corpo:formulario:ordenacaoPlc': '',
         'org.apache.myfaces.trinidad.faces.FORM': 'corpo:formulario',
         '_noJavaScript': 'false', 'javax.faces.ViewState': viewstate(r.text), 'source': src}
    with s.post(f'{BASE}/f/n/diariocon', data=p, timeout=180, stream=True) as resp:
        return int(resp.headers.get('content-length') or 0), html.unescape(titulo)

if __name__ == '__main__':
    for dia in sys.argv[2:]:
        cl, tit = medir(sys.argv[1], dia)
        print(f'{dia}  {"(vazio)" if cl is None else f"{cl/1e6:8.2f} MB"}  {tit or ""}')
