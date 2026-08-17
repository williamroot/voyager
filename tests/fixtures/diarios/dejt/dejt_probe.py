"""Sonda do DEJT (JSF/Trinidad + RichFaces sobre JBoss 4.3).

Fluxo obrigatório: GET da tela pra pegar JSESSIONID + javax.faces.ViewState,
depois POST no mesmo action com os campos do form. Sem o ViewState o JBoss
devolve a tela em branco (view expired).
"""
import re
import sys
import os

import requests

BASE = 'https://dejt.jt.jus.br/dejt'
UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
      'Chrome/126.0.0.0 Safari/537.36')
OUT = '/home/ubuntu/projetos/voyager/tests/fixtures/diarios/dejt'


def novo_sessao():
    s = requests.Session()
    s.headers.update({'User-Agent': UA, 'Accept-Language': 'pt-BR,pt;q=0.9'})
    return s


def viewstate(html):
    m = re.search(r'name="javax\.faces\.ViewState" value="([^"]*)"', html)
    return m.group(1) if m else None


def consultar(s, tribunal, data_ini, data_fim, caderno='J'):
    tipo = '1' if caderno == 'J' else '0'
    url = f'{BASE}/f/n/diariocon?pesquisacaderno={caderno}&evento=y'
    r = s.get(url, timeout=60)
    vs = viewstate(r.text)
    print(f'[GET] {r.status_code} viewstate={vs} len={len(r.text)}', file=sys.stderr)
    data = {
        'corpo:formulario': 'corpo:formulario',
        'corpo:formulario:tipoCaderno': tipo,
        'corpo:formulario:dataIni': data_ini,
        'corpo:formulario:dataFim': data_fim,
        'corpo:formulario:tribunal': str(tribunal),
        'corpo:formulario:ordenacaoPlc': '',
        'org.apache.myfaces.trinidad.faces.FORM': 'corpo:formulario',
        '_noJavaScript': 'false',
        'javax.faces.ViewState': vs,
        'source': 'corpo:formulario:botaoAcaoPesquisar',
    }
    r2 = s.post(f'{BASE}/f/n/diariocon', data=data, timeout=90,
                headers={'Referer': url,
                         'Content-Type': 'application/x-www-form-urlencoded'})
    print(f'[POST] {r2.status_code} ct={r2.headers.get("content-type")} len={len(r2.text)}',
          file=sys.stderr)
    return r2


if __name__ == '__main__':
    trib, di, df, cad, nome = sys.argv[1:6]
    s = novo_sessao()
    r = consultar(s, trib, di, df, cad)
    p = os.path.join(OUT, nome)
    with open(p, 'w', encoding=r.encoding or 'utf-8') as f:
        f.write(r.text)
    print(p)
