"""SEGUNDO EIXO do gate: inventário do que a fonte imprime (`diarios/inventario.py`).

O eixo antigo mede PROPORÇÃO — dos CNJs impressos, quantos caíram dentro de um
bloco — e reprova abaixo de 95%. Ele é estruturalmente cego para a perda
pequena, e a cegueira foi medida em produção: a pauta numerada do caderno 19 do
DJE/TJSP passou calada em **22 de 22** edições verdes, com **7.917** registros,
todas entre 0,60% e 4,54% dos CNJs.

A prova ao vivo, na edição REAL `4148-19` (19/02/2025) — a de menor fração,
73 registros em 12.091 CNJs = 0,60% —, rodando o segmentador no estado em que
ela de fato fechou `ok`:

    eixo 1 (proporção) .... 99,0%  → PASSA        ← foi o que aconteceu
    eixo 2 (inventário) ... ACUSA por DOIS caminhos:
        perna A: 73 registros impressos × 0 blocos do formato `pauta`
        perna B: 53 CNJs órfãos com a MESMA forma de linha
    com o segmentador ATUAL: 99,6% e nada a acusar  ← sem falso positivo
"""

import re

import pytest

from diarios.inventario import (
    PISO_ASSINATURA,
    TETO_ASSINATURAS,
    Inventario,
    MarcadorRegistro,
    assinatura_de_linha,
)

MARC = (
    MarcadorRegistro(nome='depre', padrao=re.compile(r'^N[ºo°]?\s*de ordem cronol', re.I),
                     formato='precatorio'),
)


# ── a forma da linha ─────────────────────────────────────────────────────────
def test_assinatura_apaga_o_que_varia_e_guarda_o_que_e_formato():
    a = assinatura_de_linha('3 - 0000239-66.2022.8.26.0120 - Processo Digital. Petições')
    b = assinatura_de_linha('117 - 1009999-11.2024.8.26.0100 - Processo Digital. Petições')
    assert a == b, 'duas entradas da MESMA pauta têm que ter a mesma forma'
    assert '#' in a and '0000239' not in a
    # …e formatos diferentes NÃO colidem.
    assert a != assinatura_de_linha('Processo de origem: 0001538-65.2023.8.26.0210/0001')
    assert assinatura_de_linha('  a   b  ') == 'a b'
    assert assinatura_de_linha('') == ''


# ── perna A: inventário por marcador ─────────────────────────────────────────
def test_perna_A_acusa_com_os_DOIS_numeros():
    inv = Inventario(marcadores=MARC)
    for _ in range(27):
        inv.ver_linha('Nº de ordem cronológica: 278/2026', [])
    # o segmentador não produziu nenhum bloco desse formato
    d = inv.conferir()
    assert len(d) == 1
    assert d[0].tipo == 'marcador'
    assert (d[0].impresso, d[0].segmentado) == (27, 0)
    assert '27 registros' in str(d[0]) and '0 blocos' in str(d[0])


def test_perna_A_e_MAIOR_OU_IGUAL_e_nao_igualdade():
    """Um balde de formato pode receber registros de mais de um marcador — o
    excesso é legítimo, a falta não."""
    inv = Inventario(marcadores=MARC)
    for _ in range(10):
        inv.ver_linha('Nº de ordem cronológica: 1/2026', [])
    for _ in range(40):
        inv.ver_bloco('precatorio')
    assert inv.conferir() == []
    inv2 = Inventario(marcadores=MARC)
    for _ in range(10):
        inv2.ver_linha('Nº de ordem cronológica: 1/2026', [])
    for _ in range(9):
        inv2.ver_bloco('precatorio')
    assert len(inv2.conferir()) == 1


def test_marcador_ausente_no_caderno_nao_vira_divergencia():
    """Edição sem aquele formato não pode acusar falta dele."""
    inv = Inventario(marcadores=MARC)
    inv.ver_linha('Processo 1005255-88.2015.8.26.0100 - Vistos.', [])
    assert inv.conferir() == []


def test_fonte_sem_marcador_ABSTEM_e_abstencao_nao_e_aprovacao():
    """Sem marcador declarado o eixo não mede — e tem que DIZER que não mediu.

    Se `mede` fosse True com marcadores vazios, uma fonte sem inventário
    apareceria no log igual a uma fonte medida e aprovada. É exatamente a
    doença que este eixo trata.
    """
    inv = Inventario()
    assert inv.mede is False
    assert inv.conferir() == []
    assert inv.total_impresso() == 0


# ── independência do segmentador ─────────────────────────────────────────────
def test_o_inventario_conta_LINHA_nunca_BLOCO():
    """Contar o que o parser produziu e comparar consigo mesmo é circular — o
    erro que a régua do nicho 12078 quase cometeu ao usar `codigo_classe` como
    prova de si mesmo. `ver_bloco` recebe só o NOME do formato, nunca texto."""
    import inspect

    inv = Inventario(marcadores=MARC)
    inv.ver_bloco('precatorio')
    assert inv.impresso == {}, 'bloco não pode alimentar o lado da FONTE'
    params = list(inspect.signature(inv.ver_bloco).parameters)
    assert params == ['formato'], (
        f'`ver_bloco` recebe {params}: se ele receber o TEXTO do bloco, alguém '
        f'vai contar marcador ali dentro e o eixo vira circular'
    )


# ── perna B: assinatura dos órfãos ───────────────────────────────────────────
def test_perna_B_nomeia_o_formato_desconhecido_sem_conhecer_o_formato():
    """A perna que pega o que a perna A não pode conhecer.

    Não precisa de marcador declarado: precisa só que o formato seja
    REPETITIVO — e um formato é, por definição, repetitivo. Foi assim, à mão,
    que os dois formatos do #118 apareceram: 100% dos 6.170 órfãos de
    `4155-11` caíram em duas formas.
    """
    inv = Inventario(marcadores=MARC)
    orfaos = []
    for i in range(PISO_ASSINATURA + 5):
        cnj = f'{i:07d}-11.2024.8.26.0100'
        inv.ver_linha(f'{i} - {cnj} - Processo Digital. Petições para juntada', [cnj])
        orfaos.append(cnj)
    d = inv.conferir(orfaos)
    assert [x.tipo for x in d] == ['assinatura']
    assert d[0].impresso == PISO_ASSINATURA + 5
    assert 'Processo Di' in d[0].detalhe and '#' in d[0].detalhe


def test_perna_B_nao_confunde_residuo_legitimo_com_formato():
    """Citação de jurisprudência e CNJ partido entre linhas são órfãos
    legítimos. Medido em 02/09/2026: 10 e 7 ocorrências, contra 1.170 e 6.170
    das famílias reais. Abaixo do piso, o eixo cala."""
    inv = Inventario(marcadores=MARC)
    orfaos = []
    for i in range(PISO_ASSINATURA - 1):
        cnj = f'{i:07d}-11.2024.8.26.0100'
        inv.ver_linha(f'parcialmente provido. (TJSP; Apelação Cível {cnj}; Relator)', [cnj])
        orfaos.append(cnj)
    assert inv.conferir(orfaos) == []


def test_teto_de_assinaturas_e_alerta_declarado_nunca_corte_mudo():
    """Regra nº 2 da casa. Atingir o teto vira BANDEIRA, não silêncio."""
    inv = Inventario(marcadores=MARC)
    inv._assinatura_por_cnj = {f'x{i}': 'f' for i in range(TETO_ASSINATURAS)}
    inv.ver_linha('3 - 0000001-11.2024.8.26.0100 - Processo Digital', ['0000001-11.2024.8.26.0100'])
    assert inv.assinaturas_truncadas is True
    assert inv.resumo()['assinaturas_truncadas'] is True


# ── o gate do coletor: proporção PASSA e o inventário ACUSA ──────────────────
@pytest.mark.parametrize('cego', [True, False])
def test_gate_do_coletor_pega_a_perda_que_a_proporcao_deixa_passar(cego, monkeypatch):
    """A situação real de `4148-19`: 73 registros perdidos em 12.091 CNJs.

    Montado com `Pagina`/`Linha` reais: muitos atos normais (que segmentam) e
    poucas entradas de pauta. Com o segmentador cego, a cobertura fica ACIMA do
    piso — e é o eixo novo que acusa, nomeando formato e números.
    """
    from diarios.base import ColetorError
    from diarios.fontes.tjsp_dje import segmentador as S
    from diarios.fontes.tjsp_dje.coletor import ColetorDjeTjsp
    from diarios.fontes.tjsp_dje.pdf import Linha, Pagina

    if cego:   # o estado em que a edição de fato fechou `ok`
        monkeypatch.setattr(S, '_RE_ANCORA_PAUTA', re.compile(r'(?!)'))

    linhas = []
    for i in range(400):                       # ruído normal, que segmenta bem
        cnj = f'{1000000 + i}-11.2024.8.26.0100'
        linhas.append(Linha(pagina=1, tamanho=8.0,
                            texto=f'Processo {cnj} - Procedimento Comum - Vistos. Fls. 10.'))
    for i in range(12):                        # a pauta, 12 de 412 = 2,9%
        cnj = f'{2000000 + i}-11.2024.8.26.0000'
        linhas.append(Linha(pagina=1, tamanho=8.0,
                            texto=f'{i + 1} - {cnj} - Processo Digital. Petições - Agravo'))

    col = ColetorDjeTjsp()
    inv = Inventario(marcadores=tuple(col.MARCADORES_DE_REGISTRO))
    from diarios.base import achar_cnjs
    no_texto, em_bloco = set(), set()
    for ln in linhas:
        no_texto.update(achar_cnjs(ln.texto))
        inv.ver_linha(ln.texto, achar_cnjs(ln.texto))
    blocos = list(S.segmentar([Pagina(numero=1, linhas=linhas)], tamanho_corpo=8.0))
    for b in blocos:
        inv.ver_bloco(b.formato)
        em_bloco.update(achar_cnjs(b.texto_corrido))

    unidade = type('U', (), {'chave': '4148-19'})()
    contagem = {'blocos': len(blocos), 'itens': len(blocos), 'sem_cnj': 0, 'outro_tribunal': 0}
    cobertura = len(no_texto & em_bloco) / len(no_texto)

    if not cego:
        assert cobertura == 1.0
        col._aferir_cobertura(unidade, no_texto, em_bloco, contagem, inv)   # não levanta
        return

    # CEGO: a proporção passa (a perda cabe na folga dos 5%)…
    assert cobertura > 0.95, f'cobertura {cobertura:.1%} — o teste precisa que ela PASSE'
    # …e o eixo novo é quem acusa, com o formato e os dois números.
    with pytest.raises(ColetorError) as e:
        col._aferir_cobertura(unidade, no_texto, em_bloco, contagem, inv)
    msg = str(e.value)
    assert 'inventário divergente' in msg
    assert 'pauta numerada' in msg and '12 registros' in msg and '0 blocos' in msg
    assert 'ACIMA do piso' in msg, 'a mensagem tem que dizer que a proporção não veria isto'
