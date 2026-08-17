"""Sonda da 'Pesquisa Avançada' → /dejt/f/n/materiapublicadacon (matéria publicada).

Diferente da consulta de cadernos: devolve a PUBLICAÇÃO individual (não o PDF do
dia inteiro). Aceita filtro por número de processo e nome de advogado.
"""
import re, sys, html
from dejt_probe import novo_sessao, consultar, viewstate, BASE


def abrir_avancada(s, cad='J'):
    """Chega em materiapublicadacon pelo botão da tela de cadernos (Seam
    conversation: entrar por URL direta perde o conversationId)."""
    r = consultar(s, '', '10/07/2024', '10/07/2024', cad)
    vs = viewstate(r.text)
    payload = {
        'corpo:formulario': 'corpo:formulario',
        'corpo:formulario:tipoCaderno': '1' if cad == 'J' else '0',
        'corpo:formulario:dataIni': '10/07/2024', 'corpo:formulario:dataFim': '10/07/2024',
        'corpo:formulario:tribunal': '', 'corpo:formulario:ordenacaoPlc': '',
        'org.apache.myfaces.trinidad.faces.FORM': 'corpo:formulario',
        '_noJavaScript': 'false', 'javax.faces.ViewState': vs,
        'source': 'corpo:formulario:botaoPesquisaAvancada',
    }
    r2 = s.post(f'{BASE}/f/n/diariocon', data=payload, timeout=90)
    return r2


def pesquisar(s, r_form, *, di='', df='', tribunal='', processo='', adv='', cad='J'):
    vs = viewstate(r_form.text)
    url = r_form.url
    payload = {
        'corpo:formulario': 'corpo:formulario',
        'corpo:formulario:tipoCaderno': '1' if cad == 'J' else '0',
        'corpo:formulario:dataPublicacaoINI': di,
        'corpo:formulario:dataPublicacaoFIM': df,
        'corpo:formulario:tribunal': str(tribunal),
        'corpo:formulario:cmbUnidadePublicadora': 'org.jboss.seam.ui.NoSelectionConverter.noSelectionValue',
        'corpo:formulario:cmbTipoMateria': '',
        'corpo:formulario:numeroProcesso': processo,
        'corpo:formulario:adv': adv,
        'corpo:formulario:orderByUsuario': 'disponibilizacao',
        'corpo:formulario:ordenacaoPlc': '', 'navDe': '',
        'org.apache.myfaces.trinidad.faces.FORM': 'corpo:formulario',
        '_noJavaScript': 'false', 'javax.faces.ViewState': vs,
        'source': 'corpo:formulario:botaoAcaoPesquisar',
    }
    return s.post(url, data=payload, timeout=120, headers={'Referer': url})


def texto(h):
    t = re.sub(r'(?is)<script.*?</script>', '', h)
    t = re.sub(r'(?is)<style.*?</style>', '', t)
    t = re.sub(r'(?s)<[^>]+>', '|', t)
    t = html.unescape(t)
    t = re.sub(r'\|+', '|', t)
    return re.sub(r'[ \t]+', ' ', t)


if __name__ == '__main__':
    s = novo_sessao()
    rf = abrir_avancada(s)
    print('avancada url:', rf.url)
    r = pesquisar(s, rf, processo=sys.argv[1] if len(sys.argv) > 1 else '0010177-81.2023.5.03.0010')
    print('status', r.status_code, len(r.text), r.url)
    open('/home/ubuntu/projetos/voyager/tests/fixtures/diarios/dejt/materia_por_cnj.html', 'w').write(r.text)
    t = texto(r.text)
    i = t.find('Pesquisas > Di')
    print(t[i:i+4000])
