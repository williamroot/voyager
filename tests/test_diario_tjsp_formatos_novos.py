"""Os dois formatos que o segmentador do DJE/TJSP não conhecia (#118).

Material REAL, recortado do `dje.tjsp.jus.br` em 02/09/2026 — 4 páginas de cada
caderno, para caber no git (92 KB e 114 KB) sem virar as dezenas de MB dos
cadernos inteiros:

  · `caderno11_depre_20250310_p3-6.pdf` — a relação da **DEPRE** (Diretoria de
    Execuções de Precatórios), formato 6.
  · `caderno19_pauta_20250313_p1240-1243.pdf` — a **pauta numerada** de
    julgamento, formato 4b.

POR QUE ESTES DOIS FORMATOS TÊM ARQUIVO PRÓPRIO
------------------------------------------------
Não foram descobertos lendo o caderno: apareceram porque **doze edições
reprovaram o gate de cobertura** entre 02/09/2026 00:00 e 06:00 UTC, seis de
cada caderno, e 100% dos CNJs órfãos estavam num dos dois. As do caderno 19
reprovaram entre **92,6% e 94,5%** — todas a menos de dois pontos e meio do
piso de 95%, que é a assinatura de "existe a mesma perda menor, passando por
baixo do gate, num dia que fechou verde".

A RÉGUA DESTES TESTES VEM DA FONTE, NÃO DE MIM
-----------------------------------------------
A relação da DEPRE imprime QUATRO campos obrigatórios por registro —
`Nº de ordem cronológica`, `Processo`, `Processo de origem` e
`Entidade devedora`. Na relação inteira de 10/03/2025 eles aparecem **2.568
vezes cada**. Isso é gabarito mecânico de graça: se o número de blocos não
bater com a contagem dos quatro marcadores NO PDF, o parser errou — e o teste
mede exatamente isso, em vez de conferir um número que eu escrevi à mão.
"""

import os
import re
from collections import Counter

import pytest

from diarios.base import achar_cnjs
from diarios.fontes.tjsp_dje import pdf, segmentador

FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures', 'diarios', 'tjsp_esaj')
DEPRE = os.path.join(FIXTURES, 'caderno11_depre_20250310_p3-6.pdf')
PAUTA = os.path.join(FIXTURES, 'caderno19_pauta_20250313_p1240-1243.pdf')

#: Os quatro campos obrigatórios do registro da DEPRE, como o caderno imprime.
MARCADORES_DEPRE = {
    'ordem': re.compile(r'^N[ºo°]?\s*de\s+ordem\s+cronol[óo]gica\s*:', re.I),
    'processo': re.compile(r'^Processo\s*:\s*\d'),
    'origem': re.compile(r'^Processo de origem\s*:', re.I),
    'entidade': re.compile(r'^Entidade devedora\s*:', re.I),
}

pytestmark = pytest.mark.skipif(
    not (os.path.exists(DEPRE) and os.path.exists(PAUTA)),
    reason='fixtures dos formatos novos ausentes',
)


def _segmentar(caminho):
    """Roda o segmentador de verdade e devolve (blocos, cnjs_no_texto, marcadores).

    Os marcadores são contados na MESMA passada das páginas — não numa leitura
    separada — para que a contagem e a segmentação vejam byte a byte o mesmo
    texto extraído. Duas leituras seriam duas réguas.
    """
    with open(caminho, 'rb') as fh:
        corpo = fh.read()
    tamanho = pdf.tamanho_do_corpo(pdf.abrir(corpo))
    no_texto: set[str] = set()
    marcadores: Counter = Counter()

    def _paginas():
        for pagina in pdf.paginas(corpo):
            no_texto.update(achar_cnjs(pagina.texto))
            for linha in pagina.linhas:
                texto = linha.texto.strip()
                for nome, regex in MARCADORES_DEPRE.items():
                    if regex.match(texto):
                        marcadores[nome] += 1
            yield pagina

    blocos = list(segmentador.segmentar(_paginas(), tamanho_corpo=tamanho))
    return blocos, no_texto, marcadores


# ─────────────────────────────────────────────────────────────────────────────
# Formato 6 — relação da DEPRE
# ─────────────────────────────────────────────────────────────────────────────
def test_depre_o_quadruplo_pareado_e_a_regua_da_propria_fonte():
    """Blocos == cada um dos quatro marcadores. A fonte confere o parser."""
    blocos, _, marcadores = _segmentar(DEPRE)
    precatorios = [b for b in blocos if b.formato == segmentador.FORMATO_PRECATORIO]

    # Controle positivo: sonda que devolve zero não prova nada.
    assert marcadores['ordem'] > 0, 'a sonda não achou marcador nenhum no PDF'
    assert len(set(marcadores.values())) == 1, (
        f'os quatro campos obrigatórios do registro NÃO pareiam no PDF: '
        f'{dict(marcadores)} — ou a fonte mudou de layout, ou a extração de '
        f'texto perdeu linha'
    )
    assert len(precatorios) == marcadores['ordem'], (
        f'{len(precatorios)} blocos para {marcadores["ordem"]} registros '
        f'impressos. O parser errou — e quem disse isso foi a fonte, não eu'
    )


def test_depre_cobre_todo_cnj_impresso():
    """Era 68,4% e 83,1% nas duas edições que reprovaram. Aqui é o recorte."""
    blocos, no_texto, _ = _segmentar(DEPRE)
    em_bloco: set[str] = set()
    for bloco in blocos:
        em_bloco.update(achar_cnjs(bloco.texto_corrido))
    assert no_texto, 'nenhum CNJ impresso — fixture errada'
    fora = no_texto - em_bloco
    assert not fora, f'{len(fora)} de {len(no_texto)} CNJs fora de bloco: {sorted(fora)[:5]}'


def test_depre_entrega_o_ente_devedor_como_parte_do_polo_passivo():
    """O motivo de existir do formato: `ENT. DEVEDORA` existia em DUAS
    `ProcessoParte` no acervo inteiro contra 4.550.742 de `AUTOR`."""
    blocos, _, _ = _segmentar(DEPRE)
    pubs = [segmentador.interpretar(b) for b in blocos
            if b.formato == segmentador.FORMATO_PRECATORIO]
    assert pubs

    com_devedora = [p for p in pubs
                    if any(pt['papel'] == 'ENTIDADE DEVEDORA' and pt['polo'] == 'P'
                           for pt in p.destinatarios)]
    assert len(com_devedora) == len(pubs), (
        f'só {len(com_devedora)} de {len(pubs)} registros trouxeram o ente '
        f'devedor no polo passivo'
    )

    primeiro = pubs[0]
    papeis = {pt['papel']: (pt['nome'], pt['polo']) for pt in primeiro.destinatarios}
    assert papeis['REQTE'] == ('VERA LÚCIA DA SILVA E SILVA', 'A')
    assert papeis['ENTIDADE DEVEDORA'] == ('SPPREV - SÃO PAULO PREVIDÊNCIA', 'P')
    # A agrupadora é o ente por trás da autarquia — passivo por construção.
    assert papeis['ENTIDADE AGRUPADORA'] == ('FAZENDA DO ESTADO DE SÃO PAULO', 'P')


def test_depre_o_processo_de_origem_fica_no_texto_e_nao_vira_o_cnj_do_bloco():
    """O par precatório↔origem é o dado, e ele tem que ficar do lado certo.

    O CNJ do bloco é o PRECATÓRIO (foro 0500, o da DEPRE); o de origem fica
    verbatim no `texto`, de onde o `search/entidades_texto.py` o colhe para
    `cnjs_citados` — o campo de incidente vinculado que já existe no índice.
    Trocar os dois atribuiria o ato ao processo errado.
    """
    blocos, _, _ = _segmentar(DEPRE)
    pubs = [segmentador.interpretar(b) for b in blocos
            if b.formato == segmentador.FORMATO_PRECATORIO]
    primeiro = pubs[0]
    assert primeiro.cnj == '0156916-80.2024.8.26.0500'
    assert 'Processo de origem: 0001538-65.2023.8.26.0210/0001' in primeiro.texto
    assert '0001538-65.2023.8.26.0210' in achar_cnjs(primeiro.texto)
    assert primeiro.cnj != '0001538-65.2023.8.26.0210'

    # Todos os registros do recorte trazem a origem impressa.
    sem_origem = [p for p in pubs if 'Processo de origem' not in p.texto]
    assert not sem_origem, f'{len(sem_origem)} registros sem o processo de origem'


def test_depre_advogado_sai_com_oab_inclusive_o_da_linha_de_continuacao():
    """`Advogados:` continua na linha SEGUINTE, sem rótulo. Ler linha a linha
    perderia o segundo nome — medido: 1.041 ocorrências de uma continuação só
    na relação de 10/03/2025."""
    blocos, _, _ = _segmentar(DEPRE)
    pubs = [segmentador.interpretar(b) for b in blocos
            if b.formato == segmentador.FORMATO_PRECATORIO]
    advs = [a['advogado'] for a in pubs[0].destinatario_advogados]
    nomes = {a['nome'] for a in advs}
    assert 'MARINA MACHIAVELI BRUNHARA' in nomes           # linha com rótulo
    assert 'WLADIMIR RIBEIRO JUNIOR' in nomes              # linha SEM rótulo
    assert all(a['numero_oab'] and a['uf_oab'] for a in advs), (
        f'advogado sem inscrição: {advs}'
    )
    # E nenhum advogado virou parte.
    papeis = {pt['papel'] for pt in pubs[0].destinatarios}
    assert not papeis & {'ADVOGADO', 'ADVOGADA', 'ADVOGADOS', 'ADVOGADAS'}


def test_depre_abstem_da_classe_e_guarda_a_natureza_verbatim():
    """A relação não imprime classe da TPU. Escrever 'Precatório' aqui
    contaminaria `Process.classe_nome` com um rótulo que a fonte não disse.

    Já a NATUREZA (alimentícia × comum) o caderno IMPRIME, e ela decide em qual
    fila de pagamento o crédito entra — vai verbatim em `tipo_comunicacao`.
    """
    blocos, _, _ = _segmentar(DEPRE)
    pubs = [segmentador.interpretar(b) for b in blocos
            if b.formato == segmentador.FORMATO_PRECATORIO]
    assert all(p.nome_classe == '' for p in pubs)
    assert pubs[0].tipo_comunicacao == 'NATUREZA ALIMENTÍCIA'
    # A vara do registro (a de ORIGEM) vale mais que a seção, que é a DEPRE e
    # é a mesma para todos os registros.
    assert pubs[0].nome_orgao.startswith('JUIZADO ESPECIAL CÍVEL E CRIMINAL')
    assert 'FORO DE GUAÍRA' in pubs[0].nome_orgao


def test_depre_nao_rouba_o_bloco_do_formato_de_distribuicao():
    """A relação da DEPRE também tem uma linha `Processo:`. Hoje o formato 1
    só não a rouba porque exige o rótulo em CAIXA ALTA; o teste trava isso para
    a próxima pessoa não relaxar a âncora sem perceber o efeito colateral."""
    assert segmentador._ancora('PROCESSO :1099645-98.2025.8.26.0100') == \
        segmentador.FORMATO_DISTRIBUICAO
    assert segmentador._ancora('Processo: 0156916-80.2024.8.26.0500') is None
    assert segmentador._ancora('Nº de ordem cronológica: 278/2026') == \
        segmentador.FORMATO_PRECATORIO
    assert segmentador._ancora('Nº de ordem cronológica: 278/2026 (CANCELADO)') == \
        segmentador.FORMATO_PRECATORIO


# ─────────────────────────────────────────────────────────────────────────────
# Formato 4b — pauta numerada
# ─────────────────────────────────────────────────────────────────────────────
def test_pauta_numerada_cobre_todo_cnj_impresso():
    """Era 92,6% a 94,5% nas seis edições do caderno 19 que reprovaram."""
    blocos, no_texto, _ = _segmentar(PAUTA)
    em_bloco: set[str] = set()
    for bloco in blocos:
        em_bloco.update(achar_cnjs(bloco.texto_corrido))
    assert no_texto
    fora = no_texto - em_bloco
    assert not fora, f'{len(fora)} de {len(no_texto)} CNJs fora: {sorted(fora)[:5]}'


def test_pauta_numerada_abre_bloco_e_extrai_classe_partes_e_oab():
    blocos, _, _ = _segmentar(PAUTA)
    pubs = [segmentador.interpretar(b) for b in blocos
            if b.formato == segmentador.FORMATO_NUMERO]
    assert len(pubs) >= 30, f'só {len(pubs)} blocos de pauta'
    primeiro = pubs[0]
    assert primeiro.cnj == '2376731-90.2024.8.26.0000'
    # O sufixo de incidente ('/50000') é o vínculo impresso e não se perde.
    assert primeiro.numero_impresso == '2376731-90.2024.8.26.0000/50000'
    assert primeiro.nome_classe == 'Agravo Interno Cível'
    papeis = {pt['papel']: pt['polo'] for pt in primeiro.destinatarios}
    assert papeis.get('Agravante') == 'A'
    assert papeis.get('Agravado') == 'P'
    assert any(a['advogado']['numero_oab'] == '301591'
               for a in primeiro.destinatario_advogados)


def test_pauta_numerada_todos_os_blocos_saem_com_classe():
    """Regressão da ladainha do artigo 7º: ela vem COLADA em 'Processo
    Digital', tem 137 caracteres, e com igualdade em vez de prefixo ela virava
    a classe, estourava o teto de 120 e a função abstinha — em TODA a pauta,
    inclusive na que já era segmentada antes desta mudança."""
    blocos, _, _ = _segmentar(PAUTA)
    pubs = [segmentador.interpretar(b) for b in blocos
            if b.formato == segmentador.FORMATO_NUMERO]
    sem_classe = [p for p in pubs if not p.nome_classe]
    assert not sem_classe, f'{len(sem_classe)} de {len(pubs)} blocos sem classe'


def test_ancora_da_pauta_exige_o_marcador_de_processo():
    """O ordinal sozinho NÃO abre bloco. Sem o `Processo Digital|Físico` a
    âncora dispararia dentro de citação de jurisprudência quebrada de linha —
    o erro que PARTE um ato ao meio e o atribui ao processo citado."""
    a = segmentador._ancora
    assert a('3 - 0000239-66.2022.8.26.0120 - Processo Digital. Petições') == \
        segmentador.FORMATO_NUMERO
    assert a('12 - 0004389-73.2012.8.26.0045 - Processo Físico - Apelação') == \
        segmentador.FORMATO_NUMERO
    assert a('3 - 0000239-66.2022.8.26.0120 - Apelação Cível - Cândido Mota') is None
    assert a('fls. 3 - 0000239-66.2022.8.26.0120 - Processo Digital') is None


def test_classe_do_cabecalho_descarta_a_ladainha_da_res_551():
    """Unidade, sem PDF: o segmento do marcador é prefixo, não igualdade."""
    corrido = ('1 - 2376731-90.2024.8.26.0000/50000 - Processo Digital. Petições para '
               'juntada devem ser apresentadas exclusivamente por meio eletrônico, nos '
               'termos do artigo 7º da Res. 551/2011 - Agravo Interno Cível - São Paulo '
               '- Relator João Camillo de Almeida Prado Costa - Agravante: Fulano')
    classe, comarca = segmentador._classe_do_cabecalho(corrido)
    assert classe == 'Agravo Interno Cível'
    assert comarca == 'São Paulo'


def test_natureza_nao_engole_linha_de_despacho():
    """O cabeçalho de natureza só é lido FORA de bloco.

    O caderno cível imprime 'NATUREZA DA CAUSA', 'NATUREZA JURÍDICA' dentro do
    corpo de despachos. Sem a guarda de bloco aberto, essas linhas sumiriam do
    `texto` verbatim — perda silenciosa dentro do conserto de uma perda
    silenciosa. Este teste é a guarda, montado com o `Linha`/`Pagina` reais.
    """
    from diarios.fontes.tjsp_dje.pdf import Linha, Pagina

    def ln(texto, tamanho=8.0):
        return Linha(pagina=1, tamanho=tamanho, texto=texto)

    pagina = Pagina(numero=1, linhas=[
        ln('Processo 1005255-88.2015.8.26.0100 - Procedimento Ordinário - Vistos.'),
        ln('NATUREZA DA CAUSA: indenizatória. Defiro a gratuidade.'),
        ln('- ADV: CLAUDIO ROBERTO VIEIRA (OAB 186323/SP)'),
    ])
    blocos = list(segmentador.segmentar([pagina], tamanho_corpo=8.0))
    assert len(blocos) == 1
    assert 'NATUREZA DA CAUSA: indenizatória' in blocos[0].texto, (
        'a linha de despacho foi engolida pelo cabeçalho de natureza'
    )
    assert blocos[0].natureza == ''
