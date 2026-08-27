"""O papel processual está no TEXTO da publicação — e nós jogávamos fora.

CONTEXTO (medido em 27/08/2026). O JSONB do DJEN (`destinatarios`) só traz
`polo: "A"/"P"` — POSIÇÃO. O corpo da publicação traz a FUNÇÃO, numa tabela:

    <b>PROCEDIMENTO DO JUIZADO ESPECIAL CÍVEL Nº 5012262-48.2025.4.02.5101/RJ</b>
    <table>
      <tr><td>AUTOR</td>       <td>: CONDOMINIO RESIDENCIAL VILLAGGIO FLORENCA</td></tr>
      <tr><td>ADVOGADO(A)</td> <td>: JOAO PAULO SARDINHA DOS SANTOS (OAB RJ250427)</td></tr>
      <tr><td>RÉU</td>         <td>: CAIXA ECONÔMICA FEDERAL - CEF</td></tr>
    </table>

Medido em 720 publicações eproc, amostradas por 60 âncoras espalhadas pelo
espaço de id: **79,2% trazem a tabela**. E ela resolve o que o JSONB não
resolve — naquela publicação o JSONB lista TRÊS advogados sem dizer de quem é
qual; o texto amarra o `RJ250427` ao autor.

Régua da casa: o texto **NUNCA cria parte**, só rotula quem o JSONB já trouxe.
Assim um cabeçalho estranho no máximo deixa de rotular — abstém, não inventa.
"""
from tribunals.services.partes_djen import (
    _chave_nome, papeis_do_texto, specs_do_processo,
)

# publicação REAL, `tribunals_movimentacao id=512523291`, TRF2, 08/07/2025
CABECALHO_REAL = (
    '<b>PROCEDIMENTO DO JUIZADO ESPECIAL CÍVEL  Nº 5012262-48.2025.4.02.5101/RJ</b>'
    '</br><b><table border="0">'
    '<tr><td>AUTOR</td><td>: CONDOMINIO RESIDENCIAL VILLAGGIO FLORENCA</td></tr>'
    '<tr><td>ADVOGADO(A)</td><td>: JOAO PAULO SARDINHA DOS SANTOS (OAB RJ250427)</td></tr>'
    '<tr><td>RÉU</td><td>: CAIXA ECONÔMICA FEDERAL - CEF</td></tr>'
    '</table></b></br><p align="center">SENTENÇA</p></br>Em face do exposto…'
)


def test_le_os_papeis_da_publicacao_real():
    p = papeis_do_texto(CABECALHO_REAL)
    assert p[_chave_nome('CONDOMINIO RESIDENCIAL VILLAGGIO FLORENCA')] == 'AUTOR'
    assert p[_chave_nome('CAIXA ECONÔMICA FEDERAL - CEF')] == 'RÉU'
    # o `(OAB RJ250427)` sai do nome — senão nunca casaria com o JSONB
    assert p[_chave_nome('JOAO PAULO SARDINHA DOS SANTOS')] == 'ADVOGADO'


def test_relator_nao_vira_parte():
    """`RELATOR` apareceu 75× na amostra. É o desembargador, não parte."""
    txt = ('<table><tr><td>RELATOR</td><td>: Des. FULANO DE TAL</td></tr>'
           '<tr><td>AGRAVANTE</td><td>: EMPRESA X LTDA</td></tr></table>')
    p = papeis_do_texto(txt)
    assert _chave_nome('Des. FULANO DE TAL') not in p
    assert p[_chave_nome('EMPRESA X LTDA')] == 'AGRAVANTE'


def test_rotulo_desconhecido_abstem():
    txt = '<table><tr><td>SEI LA O QUE</td><td>: BELTRANO</td></tr></table>'
    assert papeis_do_texto(txt) == {}


def test_mesmo_nome_com_dois_papeis_abstem():
    """Fonte se contradizendo não vira chute — vira campo vazio (regra nº 6)."""
    txt = ('<table><tr><td>AUTOR</td><td>: FULANO</td></tr>'
           '<tr><td>RÉU</td><td>: FULANO</td></tr></table>')
    assert papeis_do_texto(txt) == {}


def test_sem_tabela_devolve_vazio_sem_levantar():
    assert papeis_do_texto('') == {}
    assert papeis_do_texto(None) == {}
    assert papeis_do_texto('SENTENÇA. Julgo procedente. Int.') == {}


def test_acento_e_pontuacao_nao_impedem_o_casamento():
    """O JSONB grava `CAIXA ECONÔMICA FEDERAL - CEF`; o texto idem, mas o
    casamento não pode depender de acento nem de hífen."""
    assert _chave_nome('CAIXA ECONÔMICA FEDERAL - CEF') == \
           _chave_nome('caixa economica federal  cef')


def test_specs_do_processo_rotula_quem_veio_do_jsonb():
    """O caminho inteiro: JSONB traz QUEM, texto diz QUAL FUNÇÃO."""
    dest = [{'nome': 'CONDOMINIO RESIDENCIAL VILLAGGIO FLORENCA', 'polo': 'A'},
            {'nome': 'CAIXA ECONÔMICA FEDERAL - CEF', 'polo': 'P'}]
    advs = [{'advogado': {'nome': 'JOAO PAULO SARDINHA DOS SANTOS',
                          'uf_oab': 'RJ', 'numero_oab': 'RJ250427'}}]
    specs = specs_do_processo([(dest, advs, CABECALHO_REAL)])
    por_nome = {s['nome']: s for lista in specs.por_polo.values() for s in lista}
    assert por_nome['CONDOMINIO RESIDENCIAL VILLAGGIO FLORENCA']['papel'] == 'AUTOR'
    assert por_nome['CAIXA ECONÔMICA FEDERAL - CEF']['papel'] == 'RÉU'
    assert por_nome['JOAO PAULO SARDINHA DOS SANTOS']['papel'] == 'ADVOGADO'


def test_texto_NAO_cria_parte_que_o_jsonb_nao_trouxe():
    """A trava que impede o texto de inventar gente."""
    dest = [{'nome': 'CONDOMINIO RESIDENCIAL VILLAGGIO FLORENCA', 'polo': 'A'}]
    specs = specs_do_processo([(dest, [], CABECALHO_REAL)])
    nomes = {s['nome'] for lista in specs.por_polo.values() for s in lista}
    assert nomes == {'CONDOMINIO RESIDENCIAL VILLAGGIO FLORENCA'}, (
        'o texto criou parte por conta própria — a CEF veio só do cabeçalho')


def test_sem_papel_no_texto_o_campo_fica_vazio():
    """Abster > chutar: sem cabeçalho, `papel` continua sendo o vazio de antes."""
    dest = [{'nome': 'ALGUEM', 'polo': 'A'}]
    specs = specs_do_processo([(dest, [], None)])
    s = next(s for lista in specs.por_polo.values() for s in lista)
    assert s['papel'] == ''


def test_tupla_antiga_de_2_ainda_funciona():
    """Chamador que não passa cabeçalho não pode quebrar — abstenção."""
    specs = specs_do_processo([([{'nome': 'ALGUEM', 'polo': 'A'}], [])])
    assert specs
