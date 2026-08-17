"""Caderno (linhas) → blocos de publicação. É aqui que mora o trabalho.

O HTTP do e-SAJ é trivial (GET sem login, sem captcha, sem sessão). O risco
real é este arquivo: o caderno é um FLUXO CONTÍNUO de texto que atravessa
páginas, e errar o recorte significa colar o texto de um processo no do
vizinho — o pior erro possível, porque o dado fica plausível e a extração
downstream aprende errado.

CINCO FORMATOS DE BLOCO, todos conferidos em caderno real de 2009, 2012, 2015 e
2025 — e nenhum deles foi descoberto lendo o caderno: cada um apareceu quando a
medição de cobertura acusou CNJ impresso que não caía dentro de bloco nenhum.

1. `distribuicao` — relação de feitos distribuídos, um campo por linha:
       PROCESSO :1099645-98.2025.8.26.0100
       CLASSE :EMBARGOS À EXECUÇÃO
       EMBARGTE : Laura Cristina Lopes Cardoso
       ADVOGADO : 293286/SP - Luiz Fernando Vian Espeiorin
       EMBARGDO : Marcelo Guimarães Sarti
       VARA :15ª VARA CÍVEL
   Aqui parte, papel, classe, vara e advogado vêm ROTULADOS — extrair é ler.

2. `ato` — relação de intimações da 1ª instância, tudo numa corrida de texto:
       Processo 1005255-88.2015.8.26.0100 - Procedimento Ordinário -
       Inadimplemento - RISSATO CONSTRUÇÕES LTDA. - Contestação retro: ... -
       ADV: CLAUDIO ROBERTO VIEIRA (OAB 186323/SP)
   Classe e advogados são extraíveis com confiança; as PARTES **não** — não há
   marcador entre "última parte" e "início do despacho", e chutar produziria
   "Vistos" como nome de parte. A casa manda abster: o nome fica no `texto`
   verbatim, que é íntegro, e quem resolve isso é o extrator de autos.

3. `segunda_instancia` — 2ª instância / colégio recursal, com papéis rotulados:
       Nº 2217577-02.2025.8.26.0000 - Processo Digital - Agravo de Instrumento -
       São Paulo - Agravante: Banco Rodobens S/A - Agravado: Rev do Brasil ... -
       Advs: Andréia Regina Viola (OAB: 163205/SP)

4. `numero_primeiro` — pauta de julgamento virtual e resultado de julgamento:
   igual ao 3, mas SEM o 'Nº' na frente, e às vezes com ';' no lugar do ' - '.

5. `lista` — a grade de distribuição da 2ª instância: só números, em duas
   colunas, sob 'Ao Des. Fulano' + a classe. Não tem ato; tem o fato de o
   processo existir e ter sido distribuído. Cada número vira uma publicação com
   o cabeçalho da grade reconstruído — é o único formato cujo `texto` não é uma
   corrida contígua do PDF, porque a fonte é uma TABELA. Está dito aqui para
   ninguém achar que é verbatim de despacho.

COMO O BLOCO SABE ONDE ACABAR (e por que não é só regex de início)
------------------------------------------------------------------
Três eventos fecham um bloco: (a) a âncora do bloco seguinte; (b) um TÍTULO DE
SEÇÃO, reconhecido pelo tamanho da fonte (ver `pdf.py`) — é o que separa uma
vara da próxima; (c) uma LINHA ESTRUTURAL da relação ('JUÍZO DE DIREITO DA 1ª
VARA CÍVEL', 'RELAÇÃO Nº 0186/2015'), que aparece entre relações da mesma vara,
onde não há título para socorrer. Sem (b) e (c), o cabeçalho da relação
seguinte seria engolido como se fosse o final do despacho anterior.
"""

import logging
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from diarios.base import CNJ_TOLERANTE, achar_cnjs

from .pdf import Linha, Pagina

logger = logging.getLogger('voyager.diarios.tjsp_dje')

FORMATO_DISTRIBUICAO = 'distribuicao'
FORMATO_ATO = 'ato'
FORMATO_2INST = 'segunda_instancia'
FORMATO_NUMERO = 'numero_primeiro'
FORMATO_LISTA = 'lista'

_CNJ = CNJ_TOLERANTE.pattern
#: âncora 1 — 'PROCESSO :<numero>'. Só o rótulo PROCESSO abre bloco; CLASSE,
#: VARA e os papéis são campos DENTRO dele.
_RE_ANCORA_DISTRIBUICAO = re.compile(r'^PROCESSO\s*:\s*\S')
#: âncora 2 — 'Processo <numero> - ...'. O `\d` logo depois é obrigatório: sem
#: ele, toda linha de continuação que começa com "Processo Civil, a parte..."
#: (e há centenas) abriria um bloco fantasma.
_RE_ANCORA_ATO = re.compile(r'^Processo\s+n?[ºo°]?\s*\d')
#: âncora 3 — 'Nº <numero> - ...' da 2ª instância. Não colide com o
#: 'Nº ORDEM:01.05.2009/001449' dos cadernos antigos (lá vem letra, não dígito).
_RE_ANCORA_2INST = re.compile(r'^N[ºo°]\s*\d')
#: âncora 4 — bloco que ABRE NO NÚMERO, sem 'Processo'/'Nº' na frente. São dois
#: usos, com separador diferente e a mesma anatomia:
#:   pauta de julgamento virtual do Colégio Recursal (separador ';')
#:     '0110390-43.2025.8.26.9061; Processo Digital; Agravo de Instrumento; ...'
#:   resultado de julgamento da 2ª instância (separador ' - ')
#:     '0004389-73.2012.8.26.0045 - Processo Físico - Apelação - Santa Isabel -
#:      Relator: Des.: Roberto Midolla - Apelante: ... - Negaram provimento...'
#: Achadas medindo a cobertura, não lendo o caderno: sem a primeira, 789 dos
#: 4.101 CNJs do caderno 12 de 21/07/2025 (19%) ficavam fora de qualquer bloco;
#: sem a segunda, 1.534 dos 16.339 do caderno 11 de 15/07/2015 (9%).
#:
#: O 'Processo Digital/Físico' logo depois do número NÃO é decoração: sem ele a
#: âncora dispara também dentro de citação de jurisprudência
#: ('(TJSP; ...; 0106620-42.2025.8.26.9061; Relator (a): ...)'), que o PDF
#: quebra de linha bem no CNJ. Isso PARTE um ato ao meio e atribui a segunda
#: metade ao processo CITADO — erro pior que não achar, porque o resultado
#: parece dado bom. Medido no caderno 12 de 2025: a âncora frouxa criava 791
#: blocos, a estrita cria 697 — com a MESMA cobertura de CNJ (99,1%). Os 94
#: blocos de diferença eram só ato picado no meio.
_RE_ANCORA_NUMERO = re.compile(
    rf'^{_CNJ}(?:\s*/\s*\d{{2,6}})?\s*(?:;|-)\s*Processo\s+(Digital|F[íi]sico)', re.I)
#: âncora 5 — GRADE de distribuição da 2ª instância: linha que é SÓ número, em
#: tabela de duas colunas, sob 'Ao Des. Fulano' + a classe.
#:
#:     Ao Des. Leonel Costa
#:     Apelação
#:     1001466-73.2015.8.26.0038 1006402-49.2014.8.26.0565
#:
#: Não tem texto de ato — é a lista dos processos distribuídos ao relator
#: naquele dia. Ignorá-la custa caro: 3.495 dos 16.339 CNJs (21%) do caderno 11
#: de 15/07/2015 estão SÓ aí, e para uma missão cujo problema é ter 6% do
#: acervo do TJSP, "o processo existe e foi distribuído ao Des. X como Apelação"
#: é informação, não ruído.
_RE_LINHA_SO_NUMEROS = re.compile(rf'^\s*(?:{_CNJ}\s*)+$')
#: linha de contexto que pode virar classe da grade ('Apelação / Reexame
#: Necessário'). 'Apelação 44' tem dígito e é contagem, não classe.
_RE_CLASSE_DA_GRADE = re.compile(r'^[A-Za-zÀ-ú][A-Za-zÀ-ú /]{2,58}$')

#: linhas de cabeçalho de relação — fecham o bloco aberto e NÃO entram em
#: bloco nenhum. São o único delimitador quando duas relações da mesma vara se
#: sucedem sem título no meio.
_RE_ESTRUTURAL = re.compile(
    r'^(JU[ÍI]ZO\s+DE\s+DIREITO|JU[ÍI]Z(A|\(A\))?\s+DE\s+DIREITO|JUIZ\(A\)|'
    r'ESCRIV[ÃA][ON]?|ESCREVENTE|DIRETOR|CHEFE\s+DE\s+SE|'
    r'EDITAL\s+DE\s+INTIMA|RELA[ÇC][ÃA]O\b|LISTA\s+DE\s+PUBLICA)', re.I)

#: o número como o caderno imprime: CNJ, o sufixo de incidente ('/01') e —
#: nos cadernos do interior — o número ANTIGO entre parênteses
#: ('0000003-43.2011.8.26.0236 (236.01.2011.000003)'). O de-para CNJ↔numeração
#: pré-2010 é de graça aí e não existe em nenhum outro lugar do sistema.
_RE_NUMERO_IMPRESSO = re.compile(
    rf'(?P<numero>{_CNJ}(?:\s*/\s*\d{{2,6}})?)(?:\s*(?P<antigo>\(\s*[\d./-]{{10,30}}\s*\)))?')
#: segmento que é só o número antigo entre parênteses — não é classe.
_RE_SO_NUMERO = re.compile(r'^\(?\s*[\d./\-]{6,}\s*\)?$')
#: OAB em três grafias, todas reais: '(OAB 186323/SP)' (2015),
#: '(OAB: 163205/SP)' (2025) e '23784-A/PA' (advogado de outra seccional).
#: o ':' fica FORA do nome de propósito: sem isso o rótulo entra no nome e o
#: advogado é cadastrado como 'ogado: Carlos Roberto Vissechi' (o lazy quantifier
#: começa no meio de 'Advogado:'). Pego pelo teste, não pela leitura.
_RE_ADV_PARENTESES = re.compile(
    r'(?P<nome>[^\-(),;:]{3,90}?)\s*\(\s*OAB:?\s*(?P<num>\d{1,6}[-\s]?[A-Za-z]?)\s*/?\s*(?P<uf>[A-Za-z]{2})\s*\)')
_RE_OAB_PREFIXO = re.compile(
    r'^(?P<num>\d{1,6}(?:\s*-\s*[A-Za-z])?)\s*/?\s*(?P<uf>[A-Za-z]{2})\s*-\s*(?P<nome>.+)$')

_RE_CAMPO = re.compile(
    r'^(?P<rotulo>[A-ZÀ-Ú][A-ZÀ-Úa-zà-ú/º°\.\(\) ]{1,28}?)\s*:\s*(?P<valor>.*)$')
#: rótulos do formato 1 que NÃO são parte. Tudo que sobrar é papel de parte —
#: a lista de papéis do TJSP é longa demais para enumerar (REQTE, REQDO,
#: EMBARGTE, INVTARDO, HERDEIRA, RECLAMANTE, ALIMENTANTE, QUERELANTE...), então
#: a lista fechada é a dos NÃO-papéis. Os códigos curtos (IP, CF, TC, PORT, BO,
#: PP) são referências de peça na distribuição criminal — inquérito policial,
#: carta precatória, termo circunstanciado, portaria, boletim de ocorrência —
#: e vinham virando "parte" com número no lugar do nome (medido: 1.593
#: ocorrências no caderno 12 de 15/07/2015).
_CAMPOS_NAO_PARTE = frozenset({
    'PROCESSO', 'CLASSE', 'ASSUNTO', 'VARA', 'ADVOGADO', 'ADVOGADA', 'FORO',
    'CONTROLE', 'DISTRIBUICAO', 'DISTRIBUIÇÃO', 'DATA', 'Nº ORDEM', 'N ORDEM',
    'NUMERO DE ORDEM', 'JUIZ', 'JUIZA', 'LOCAL',
    'ORIGEM', 'JUÍZO DEPREC.', 'JUIZO DEPREC.', 'IP', 'CF', 'TC', 'PORT', 'BO', 'PP',
})
#: papéis abreviados do DIÁRIO que a tabela do `enrichers/esaj.py` não cobre —
#: lá os nomes vêm da consulta pública ('Embte'), aqui vêm do diário
#: ('EMBARGTE'). Complemento, não fork: o `_polo` só cai aqui quando o esaj
#: devolve 'outros'.
_PAPEIS_ATIVO_DIARIO = ('embargte', 'embargant', 'imptte', 'querelante')
_PAPEIS_PASSIVO_DIARIO = ('embargd', 'imptdo', 'querelad')
_RE_TEM_LETRA = re.compile(r'[A-Za-zÀ-ú]')
_MARCADORES_DIGITAL = ('Processo Digital', 'Processo Físico', 'Processo Fisico')
#: o cabeçalho dos formatos 2/3 separa campos por ' - ' e o da pauta (formato 4)
#: por ';'. Um separador só não serve para os dois.
_RE_SEPARADOR = re.compile(r'\s+-\s+|\s*;\s*')
_RE_PREFIXO_ADV = re.compile(r'^(?:-\s*)?(?:ADVS?|ADVOGADOS?|ADVOGADAS?)\s*[:.]?\s*', re.I)


@dataclass
class Bloco:
    """Uma publicação recortada do caderno.

    `texto` é VERBATIM: as quebras de linha são as do PDF e nada é "consertado".
    Os campos estruturados são o que o próprio diário rotula — quando ele não
    rotula, o campo fica vazio (abster > chutar).
    """
    formato: str
    pagina: int
    linhas: list[str] = field(default_factory=list)
    #: seção hierárquica vigente (foro/vara) no momento em que o bloco abriu
    orgao_secao: str = ''
    #: 'Distribuição' quando o cabeçalho da relação declara feitos distribuídos
    tipo_comunicacao: str = ''

    @property
    def texto(self) -> str:
        return '\n'.join(self.linhas).strip()

    @property
    def texto_corrido(self) -> str:
        """Só para PARSE de campo — nunca para gravar. A quebra de linha do PDF
        é coluna de página, não conteúdo, e ela parte nomes e OABs no meio."""
        return re.sub(r'\s+', ' ', ' '.join(self.linhas)).strip()

    # -- campos derivados --------------------------------------------------
    @property
    def numero_impresso(self) -> str:
        """O número COMO IMPRESSO, com o sufixo de incidente ('/01') que a
        normalização CNJ descarta. No TJSP o crédito de um precatório se
        espalha por conhecimento → cumprimento → precatório, e esse sufixo é a
        única pista impressa do vínculo — por isso não é jogado fora."""
        m = _RE_NUMERO_IMPRESSO.search(self.texto_corrido[:140])
        if not m:
            return ''
        numero = re.sub(r'\s+', '', m.group('numero'))
        antigo = re.sub(r'\s+', '', m.group('antigo') or '')
        return f'{numero} {antigo}'.strip()

    @property
    def cnj(self) -> str:
        """CNJ do bloco: o primeiro número do CABEÇALHO do bloco.

        A janela dos 140 primeiros caracteres é deliberada — o corpo do
        despacho cita outros processos ("Agravo 2113576-63.2025.8.26.0000;
        Relator...") e pegar o primeiro CNJ do bloco inteiro atribuiria o ato
        ao processo errado. Na grade de distribuição não há corpo: o bloco tem
        um número só, e o cabeçalho ('Ao Des. Fulano', a classe) vem antes dele.
        """
        alvo = self.texto_corrido if self.formato == FORMATO_LISTA else self.texto_corrido[:140]
        achados = achar_cnjs(alvo)
        return achados[0] if achados else ''


def _classificar(linha: Linha, corpo: float) -> str:
    """mobília | titulo | corpo, pelo tamanho da fonte (ver `pdf.py`)."""
    if linha.tamanho < corpo - 0.4:
        return 'mobilia'
    if linha.tamanho > corpo + 0.4:
        return 'titulo'
    return 'corpo'


def _ancora(texto: str) -> str | None:
    if _RE_ANCORA_DISTRIBUICAO.match(texto):
        return FORMATO_DISTRIBUICAO
    if _RE_ANCORA_ATO.match(texto):
        return FORMATO_ATO
    if _RE_ANCORA_2INST.match(texto):
        return FORMATO_2INST
    if _RE_ANCORA_NUMERO.match(texto):
        return FORMATO_NUMERO
    return None


class _Hierarquia:
    """Pilha de títulos por tamanho de fonte.

    Um título de tamanho S descarta todos os títulos de tamanho <= S (entrou uma
    seção do mesmo nível ou mais alto). Sobra o caminho vigente; o mais
    específico é o menor tamanho. Não é convenção inventada: é como o caderno é
    diagramado desde 2007 (16 = agrupamento, 12 = foro, 11 = vara).
    """

    def __init__(self):
        self._niveis: dict[float, str] = {}

    def empilhar(self, tamanho: float, texto: str) -> None:
        for t in [t for t in self._niveis if t <= tamanho]:
            del self._niveis[t]
        self._niveis[tamanho] = texto.strip()

    def caminho(self, niveis: int = 3) -> str:
        """Os `niveis` títulos mais específicos, do mais geral para o mais
        específico ('IBITINGA - Cível - 1ª Vara Cível').

        Três níveis, não dois: nos cadernos do interior o nível 16 é a COMARCA
        ('IBITINGA'), e cortar em dois devolveria 'Cível - 1ª Vara Cível' —
        órgão sem cidade, que num estado com 645 municípios não identifica nada.
        """
        ordenados = [self._niveis[t] for t in sorted(self._niveis, reverse=True)]
        return ' - '.join(ordenados[-niveis:]) if ordenados else ''


def segmentar(paginas: Iterable[Pagina], tamanho_corpo: float) -> Iterator[Bloco]:  # noqa: PLR0912
    """Recorta o caderno inteiro em blocos, atravessando páginas.

    Streaming de propósito: um caderno de 2.001 páginas dá ~16 MB de texto e a
    coleta escreve em lotes de 500 — nada disso precisa caber na memória de uma
    vez.

    O `noqa: PLR0912` é consciente: isto é uma máquina de estados de uma
    passada só, e cada ramo é um evento do caderno (mobília, título, linha
    estrutural, âncora, grade, continuação). Quebrar em funções esconderia a
    ordem em que os eventos têm que ser testados — que é o que a torna correta.
    """
    hierarquia = _Hierarquia()
    tipo_relacao = ''
    atual: Bloco | None = None
    #: últimas linhas soltas de corpo — é o cabeçalho da grade de distribuição
    #: ('Ao Des. Leonel Costa' / 'Apelação'), que fica ACIMA da tabela.
    contexto: list[str] = []

    for pagina in paginas:
        for linha in pagina.linhas:
            especie = _classificar(linha, tamanho_corpo)
            texto = linha.texto.strip()
            if especie == 'mobilia' or not texto:
                continue

            if especie == 'titulo':
                if atual is not None:
                    yield atual
                    atual = None
                hierarquia.empilhar(linha.tamanho, texto)
                # Título novo = relação nova: o tipo da relação anterior não
                # vale mais. Já a seção 'Subseção III - Processos Distribuídos'
                # declara o tipo dela no próprio título.
                tipo_relacao = 'Distribuição' if 'DISTRIBU' in texto.upper() else ''
                contexto = []
                continue

            if _RE_ESTRUTURAL.match(texto):
                if atual is not None:
                    yield atual
                    atual = None
                if 'DISTRIBU' in texto.upper():
                    tipo_relacao = 'Distribuição'
                contexto = []
                continue

            formato = _ancora(texto)
            if formato:
                if atual is not None:
                    yield atual
                atual = Bloco(
                    formato=formato, pagina=pagina.numero,
                    orgao_secao=hierarquia.caminho(),
                    tipo_comunicacao=tipo_relacao,
                )
                atual.linhas.append(linha.texto)
                continue

            # Grade de distribuição: uma linha que é SÓ número, e só quando não
            # há bloco aberto. A condição do bloco aberto é o que impede roubar
            # a linha de continuação de um despacho que por acaso quebrou
            # deixando só o número na linha.
            if atual is None and _RE_LINHA_SO_NUMEROS.match(texto):
                for numero in achar_cnjs(texto):
                    grade = Bloco(
                        formato=FORMATO_LISTA, pagina=pagina.numero,
                        orgao_secao=hierarquia.caminho(),
                        tipo_comunicacao=tipo_relacao or 'Distribuição',
                    )
                    grade.linhas = [*contexto, numero]
                    yield grade
                continue

            if atual is not None:
                atual.linhas.append(linha.texto)
            else:
                # Linha de corpo sem bloco aberto: é o cabeçalho da grade ou
                # texto de expediente. Guarda as duas últimas — só elas viram
                # contexto da grade; o resto é descartado de propósito, porque
                # sem processo não há `Movimentacao` para pendurar.
                contexto = [*contexto, texto][-2:]

    if atual is not None:
        yield atual


# ─────────────────────────────────────────────────────────────────────────────
# Extração de campos DENTRO do bloco
# ─────────────────────────────────────────────────────────────────────────────
def _polo(papel: str) -> str:
    """Papel impresso ('REQTE', 'Agravado') → polo no vocabulário do DJEN ('A'/'P').

    Reusa o `_polo_para_tipo` do `enrichers/esaj.py` em vez de reescrever a
    tabela: são os MESMOS papéis do mesmo tribunal (o e-SAJ do TJSP alimenta a
    consulta pública e o diário), e duas cópias da mesma tabela viram duas
    tabelas diferentes na primeira correção — aquele arquivo já pagou o bug do
    'exequente' por extenso que caía em 'outros'. Chamada com a classe no lugar
    de `self` porque o método só lê constantes de classe; não instanciamos o
    enricher, que abriria sessão HTTP.
    """
    from enrichers.esaj import BaseEsajEnricher
    resultado = BaseEsajEnricher._polo_para_tipo(BaseEsajEnricher, papel)
    if resultado == 'outros':
        t = (papel or '').strip().lower()
        if t.startswith(_PAPEIS_ATIVO_DIARIO):
            return 'A'
        if t.startswith(_PAPEIS_PASSIVO_DIARIO):
            return 'P'
    return {'ativo': 'A', 'passivo': 'P'}.get(resultado, '')


def _advogado(nome: str, numero: str, uf: str) -> dict:
    """Mesmo formato do DJEN (`destinatarioadvogados[].advogado`), para que a
    API, o índice e a UI leiam as duas portas com o mesmo código."""
    return {'advogado': {
        'nome': _RE_PREFIXO_ADV.sub('', re.sub(r'\s+', ' ', nome).strip()).strip(),
        'numero_oab': re.sub(r'\s+', '', numero).upper(),
        'uf_oab': uf.upper(),
    }}


def _advogados_com_oab(texto: str) -> list[dict]:
    """Advogados no formato '<Nome> (OAB 186323/SP)' / '(OAB: 163205/SP)'."""
    saida, vistos = [], set()
    for m in _RE_ADV_PARENTESES.finditer(texto):
        adv = _advogado(m.group('nome'), m.group('num'), m.group('uf'))
        chave = (adv['advogado']['nome'].lower(), adv['advogado']['numero_oab'])
        if adv['advogado']['nome'] and chave not in vistos:
            vistos.add(chave)
            saida.append(adv)
    return saida


def _campos_rotulados(bloco: Bloco) -> tuple[dict, list[dict], list[dict]]:
    """Formato 1: um campo por linha, tudo rotulado."""
    campos: dict[str, str] = {}
    partes: list[dict] = []
    advogados: list[dict] = []
    for linha in bloco.linhas:
        m = _RE_CAMPO.match(linha.strip())
        if not m:
            continue
        rotulo = re.sub(r'\s+', ' ', m.group('rotulo')).strip().upper()
        valor = m.group('valor').strip()
        if not valor:
            continue
        if rotulo in ('ADVOGADO', 'ADVOGADA'):
            oab = _RE_OAB_PREFIXO.match(valor)
            if oab:
                advogados.append(_advogado(oab.group('nome'), oab.group('num'), oab.group('uf')))
            else:
                # Sem OAB legível: grava o nome e ABSTÉM da inscrição em vez de
                # inventar seccional.
                advogados.append(_advogado(valor, '', ''))
            continue
        if rotulo in _CAMPOS_NAO_PARTE or not _RE_TEM_LETRA.search(valor):
            # Valor sem uma única letra é número de peça (IP, precatória,
            # portaria), não nome de gente. Guarda genérica para o rótulo novo
            # que a lista fechada ainda não conhece.
            campos[rotulo] = valor
            continue
        partes.append({'nome': valor, 'polo': _polo(rotulo), 'papel': rotulo})
    return campos, partes, advogados


def _partes_rotuladas(texto: str) -> list[dict]:
    """Formatos 3 e 4: papéis rotulados na corrida de texto.

    O separador é ' - ' na 2ª instância ('- Agravante: Fulano -') e ';' na
    pauta ('; Agravante: Fulano;'). Só entra parte cujo papel está ROTULADO —
    é o que separa este caso do formato 2, onde ninguém sabe onde a parte
    termina e o despacho começa.
    """
    partes = []
    for m in re.finditer(r'(?:^|\s-\s|;\s*)(?P<papel>[A-ZÀ-Ú][A-Za-zÀ-ú/\.\(\) ]{2,28}?):\s*'
                         r'(?P<nome>[^\-;]{2,120}?)(?=\s-\s|;|$)', texto):
        papel = m.group('papel').strip()
        nome = m.group('nome').strip(' .;')
        polo = _polo(papel)
        # SÓ entra quem tem polo reconhecido. Na corrida de texto da pauta, o
        # mesmo padrão "<Rótulo>: <valor>" também casa 'Relator (a): Fulano',
        # 'Órgão Julgador: 3ª Turma Recursal' e 'Data do Julgamento: 07/05/2025'
        # — cadastrar isso como PARTE seria exatamente o chute que a casa
        # proíbe. Sem polo, o nome fica onde já está: no `texto` verbatim.
        if not polo or not nome or not _RE_TEM_LETRA.search(nome):
            continue
        partes.append({'nome': nome, 'polo': polo, 'papel': papel})
    return partes


def _classe_do_cabecalho(texto: str) -> tuple[str, str]:
    """Classe (e comarca, quando houver) do cabeçalho dos formatos 2, 3 e 4.

    Layout do e-SAJ: `<numero> [(nº antigo)] [- Processo Digital] - <classe> -
    <assunto|comarca> - ...`. Devolve ('', '') quando o cabeçalho não tem os
    separadores esperados — abster é melhor que rotular o começo do despacho
    como classe.
    """
    m = _RE_NUMERO_IMPRESSO.search(texto[:140])
    if not m:
        return '', ''
    resto = texto[m.end():]
    partes = [p.strip() for p in _RE_SEPARADOR.split(resto)]
    # 'Processo Digital' e o número antigo entre parênteses vêm ANTES da classe
    # nos cadernos do interior — sem descartá-los, a classe de 31 mil linhas do
    # caderno 13 de 15/07/2015 virava '(236.01.2011.000003)'.
    #
    # E descartamos QUALQUER segmento que ABRE com '(', não só o puramente
    # numérico: o `_RE_SO_NUMERO` exige ausência de letra, e a verificação
    # adversarial de 16/08/2026 mediu o custo disso no mesmo caderno — 963 de
    # 31.408 itens (3,1%) gravavam `nome_classe='(processo principal
    # 0000359-67.2013.8.26)'` ou `'(apensado ao processo ...)'`, a classe
    # VERDADEIRA ('Impugnação de Assistência Judiciária', 'Divórcio Litigioso')
    # se perdia, e o vocabulário de classes inflava de 267 para 1.090 valores.
    # Nenhuma classe da TPU começa com parêntese: o que abre com '(' é sempre
    # aposto sobre o processo (apensamento, principal, número antigo).
    partes = [p for p in partes
              if p and p not in _MARCADORES_DIGITAL
              and not p.startswith('(') and not _RE_SO_NUMERO.match(p)]
    if not partes:
        return '', ''
    classe = partes[0]
    if len(classe) > 120 or ':' in classe:
        return '', ''
    segunda = partes[1] if len(partes) > 1 else ''
    comarca = segunda if (segunda and len(segunda) <= 40 and ':' not in segunda) else ''
    return classe, comarca


@dataclass
class Publicacao:
    """O bloco já com os campos que o diário rotulou — pronto para virar
    `ParsedItem`. O que a fonte não rotula fica vazio, e é vazio de propósito."""
    cnj: str
    numero_impresso: str
    texto: str
    pagina: int
    formato: str
    nome_classe: str = ''
    nome_orgao: str = ''
    tipo_comunicacao: str = ''
    destinatarios: list[dict] = field(default_factory=list)
    destinatario_advogados: list[dict] = field(default_factory=list)


def interpretar(bloco: Bloco) -> Publicacao:
    """Bloco → `Publicacao` com os campos extraídos por formato."""
    corrido = bloco.texto_corrido
    pub = Publicacao(
        cnj=bloco.cnj, numero_impresso=bloco.numero_impresso, texto=bloco.texto,
        pagina=bloco.pagina, formato=bloco.formato,
        nome_orgao=bloco.orgao_secao, tipo_comunicacao=bloco.tipo_comunicacao,
    )

    if bloco.formato == FORMATO_LISTA:
        # A grade não tem ato: tem o número, o relator e a classe. Tudo o mais
        # fica vazio — e o `texto` avisa que é entrada de grade, não despacho.
        classe = bloco.linhas[-2] if len(bloco.linhas) > 1 else ''
        pub.nome_classe = classe if _RE_CLASSE_DA_GRADE.match(classe) else ''
        return pub

    if bloco.formato == FORMATO_DISTRIBUICAO:
        campos, partes, advogados = _campos_rotulados(bloco)
        pub.nome_classe = campos.get('CLASSE', '')
        # A vara do próprio bloco vale mais que o título da seção: na relação de
        # distribuídos a seção é o 'Distribuidor Cível' e a vara é por processo.
        pub.nome_orgao = campos.get('VARA') or bloco.orgao_secao
        pub.destinatarios = partes
        pub.destinatario_advogados = advogados
        return pub

    classe, comarca = _classe_do_cabecalho(corrido)
    pub.nome_classe = classe
    pub.destinatario_advogados = _advogados_com_oab(corrido)
    if bloco.formato in (FORMATO_2INST, FORMATO_NUMERO):
        pub.destinatarios = _partes_rotuladas(corrido)
        if comarca:
            pub.nome_orgao = f'{bloco.orgao_secao} - {comarca}' if bloco.orgao_secao else comarca
    # FORMATO_ATO: partes NÃO são extraídas — ver docstring do módulo.
    return pub
