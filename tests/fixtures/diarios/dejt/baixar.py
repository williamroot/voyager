"""Baixa o PDF de um caderno: consulta → clica no link-download (postback JSF)."""
import re, sys, os
from dejt_probe import novo_sessao, consultar, viewstate, BASE

OUT = '/home/ubuntu/projetos/voyager/tests/fixtures/diarios/dejt'

def linhas_resultado(html_):
    """Extrai (data, titulo, source_do_link) da tabela de resultados."""
    out = []
    for tr in re.findall(r'<tr class="linha(?:par|impar)">(.*?)</tr>', html_, re.S):
        campos = re.findall(r'<span class="af_outputLabel">(.*?)</span>', tr, re.S)
        m = re.search(r"source:'([^']+)'", tr)
        if m and len(campos) >= 2:
            out.append((campos[0].strip(), campos[1].strip(), m.group(1)))
    return out

if __name__ == '__main__':
    trib, di, df, cad, idx = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5])
    s = novo_sessao()
    r = consultar(s, trib, di, df, cad)
    rows = linhas_resultado(r.text)
    print('LINHAS:', len(rows))
    for d, t, src in rows:
        print('  ', d, '|', re.sub(r'&\w+;|&#\d+;', '?', t), '|', src)
    if not rows:
        sys.exit(1)
    data, titulo, src = rows[idx]
    vs = viewstate(r.text)
    payload = {
        'corpo:formulario': 'corpo:formulario',
        'corpo:formulario:tipoCaderno': '1' if cad == 'J' else '0',
        'corpo:formulario:dataIni': di,
        'corpo:formulario:dataFim': df,
        'corpo:formulario:tribunal': str(trib),
        'corpo:formulario:ordenacaoPlc': '',
        'org.apache.myfaces.trinidad.faces.FORM': 'corpo:formulario',
        '_noJavaScript': 'false',
        'javax.faces.ViewState': vs,
        'source': src,
    }
    r2 = s.post(f'{BASE}/f/n/diariocon', data=payload, timeout=180, stream=True,
                headers={'Referer': f'{BASE}/f/n/diariocon'})
    print('DOWNLOAD:', r2.status_code, r2.headers.get('content-type'),
          r2.headers.get('content-disposition'), r2.headers.get('content-length'))
    body = r2.content
    print('LEN:', len(body), 'MAGIC:', body[:8])
    nome = sys.argv[6] if len(sys.argv) > 6 else 'caderno.bin'
    p = os.path.join(OUT, nome)
    open(p, 'wb').write(body)
    print(p)
