"""Promove `Movimentacao.destinatarios` / `destinatario_advogados` a
`Parte` + `ProcessoParte` — o dado que a DJEN entrega em toda comunicação e
que nunca virou entidade.

O buraco (auditoria de 24-25/08/2026, `.ia/ENRICHMENT.md` §Achado 1): o parser
da DJEN grava os dois JSONB desde sempre, e `grep -rn destinatario` mostra que
NINGUÉM os lê para virar entidade. O grafo `Parte`/`ProcessoParte` — que
alimenta a ficha, a tela de partes, o "quem deve" e os campos `partes`/`advs`
do índice — é populado só pelos enrichers. Medido em 11.160 processos:

    sem nenhuma ProcessoParte .............. 10.046 / 11.160 = 90,0%
       … mas com destinatário DJEN gravado .  9.467 / 10.046 = 94,2%
    ⇒ ganham parte sem UMA requisição ......  9.467 / 11.160 = 84,8% ≈ 86,7 M

Confirmado de forma independente aqui (semente 20260825, 6 âncoras × 600 pks
uniformes em `id ∈ [1, 104.602.261]`): 2.311 dos 3.584 processos amostrados não
têm `ProcessoParte`, e 2.172 deles (**94,0%**) têm destinatário gravado.

O que este módulo NÃO faz, de propósito (regra nº 6 — abster > chutar):

* **`representa` fica NULL.** O DJEN entrega `destinatarios` e
  `destinatario_advogados` como duas listas irmãs, sem nenhum vínculo
  advogado→representado. Inventar o vínculo seria chute.
* **`documento` fica vazio.** Não há CPF/CNPJ no payload.
* **O `polo` é abstido quando a janela se contradiz.** O JSONB diz o polo da
  COMUNICAÇÃO, não o da ação: num processo com recurso, o autor aparece como
  RECORRIDO (polo P) numa publicação e como AUTOR (polo A) noutra, e as duas
  estão certas sobre a sua fase. Como o dedupe é por `(nome, polo)`, isso
  gravava a MESMA pessoa no ativo E no passivo do mesmo processo — um fato
  impossível. Agora ela vai inteira para `outros`. Ver `_polos_contraditorios`.
* **`papel` fica vazio.** Medido: `papel` existe em 10 de 23.771 destinatários
  (0,04%) — e só nos que vêm dos coletores de `diarios/`, não do DJEN.
* **Não substitui o enricher.** Destinatário é quem foi intimado NAQUELA
  comunicação, não o cadastro de partes. Cobertura ampla e rasa; o enricher é
  estreita e profunda. Por isso `fonte='djen'` fica gravado na linha: a tela
  precisa poder dizer de onde a parte veio.

Formato real do dado, medido em produção (24 âncoras × 400 pks, semente
20260825 — 23.771 destinatários e 26.569 advogados):

    destinatarios[]           polo 23.771 · nome 23.771 · comunicacao_id 23.761 · papel 10
    destinatario_advogados[]  advogado · id · advogado_id · comunicacao_id · created_at · updated_at
    …[].advogado              nome · numero_oab · uf_oab (100%) · id

`polo` tem QUATRO valores, não dois: `A` 13.003 · `P` 10.519 · `T` 228 · `D` 21.
`T`/`D` são terceiro/custos legis (Ministério Público, administradora judicial)
— mapeiam para `outros`, junto com os advogados.

E o polo tem uma régua, medida em 02/09/2026 contra o enricher (24 âncoras,
900 processos que têm as DUAS origens): **96,6% dos destinatários que batem
por nome saem com o polo idêntico** ao que o PJe/eSAJ gravou. O nome bate em
93,7% e a OAB em 94,5% dos pares comparáveis — em 256 outros o enricher NÃO
tinha OAB e nós acrescentamos. Ver `.ia/ENRICHMENT.md`.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

from django.db import connection, transaction

from tribunals.models import ProcessoParte
from tribunals.services.oab import canonizar_oab

logger = logging.getLogger('voyager.partes_djen')

#: Procedência gravada em toda linha criada aqui. NULL = legado/enricher.
#: `'djen'` é o default histórico e quer dizer "promovida de
#: `Movimentacao.destinatarios`" — e desde 02/09/2026 o DJEN não é o único a
#: escrever nesse JSONB: os coletores de `diarios/` escrevem no MESMO campo,
#: com dado que o DJEN não tem (papel rotulado, ente devedor). Carimbar as duas
#: com `'djen'` faria a coluna de PROCEDÊNCIA mentir justamente onde ela existe
#: para não mentir, então quem chama diz de onde veio (`FONTE_DIARIO`).
FONTE = 'djen'
#: Promovida a partir de uma coleta de diário próprio (`diarios/`).
FONTE_DIARIO = 'diario'

#: `papel` do DJEN é vazio de propósito — ver docstring. Não é `'DESTINATARIO'`
#: porque isso inventaria um papel processual que a fonte não deu, e porque a
#: constraint `uniq_processo_parte_polo_papel_principal` inclui `papel`: um
#: rótulo nosso criaria linha paralela à do enricher em vez de colidir com ela.
PAPEL = ''

POLO_DJEN = {'A': ProcessoParte.POLO_ATIVO, 'P': ProcessoParte.POLO_PASSIVO}


# --------------------------------------------------------------------------
# Guarda de segredo de justiça
# --------------------------------------------------------------------------
# Parte inventada em processo sob segredo é PIOR que processo sem parte. As
# duas famílias abaixo saíram de medição, não de suposição — 12.592
# destinatários distintos por (processo, nome, polo), semente 20260825:
#
#   marcador explícito ....  598 = 4,75%   'SIGILO', 'SIGILO1', 'SIGILO 2',
#                                          'SIGILOSO', 'EM SEGREDO DE JUSTIÇA',
#                                          'SEGREDO DE JUSTIÃ§A' (mojibake),
#                                          'PROCESSO ESTÁ EM SEGREDO DE JUSTIÇA - 1',
#                                          'PARTE/PROCESSO SIGILOSO OU …JUSTIçA.4'
#   só iniciais ...........  840 = 6,67%   'E.O.', 'W.V.D.', 'E. S. D. J.',
#                                          'A. H. DOS S.', 'F. S. O. DO B. L.',
#                                          e nomes de UMA letra: 'I', 'M', 'N.'
#
# O que NÃO virou regra, e o motivo: `[Xx]{3,}` parecia óbvio para "máscara" e
# teria DESCARTADO 'MRV MRL XXXVIII INCORPORACOES SPE LTDA' — o único nome da
# amostra inteira com 3 X seguidos é uma incorporadora com algarismo romano.
# Máscara por `*` ou `X` não existe neste campo: 0 ocorrências em 12.592.
_MARCADORES_SEGREDO = (
    'SEGREDO DE JUSTI',      # cobre 'EM SEGREDO DE JUSTIÇA', mojibake e o
                             # 'PROCESSO ESTÁ EM SEGREDO DE JUSTIÇA - 1'
    'PROCESSO SIGILOSO',
    'NOME OMITIDO',
    'NOME NAO INFORMADO',
    'NOME NÃO INFORMADO',
)
#: Casados por igualdade (não por substring) para não matar uma empresa que
#: tenha a palavra no nome — ex. uma hipotética "SIGILO SEGURANÇA LTDA".
_NOMES_SEGREDO_EXATOS = frozenset({'SIGILO', 'SIGILOSO', 'SIGILOSA', 'SEGREDO'})

#: Conectivos que não contam como "palavra real" ao decidir se o nome é só
#: iniciais — 'A. H. DOS S.' é máscara, não nome.
_CONECTIVOS = frozenset({'DA', 'DE', 'DO', 'DAS', 'DOS', 'E', 'DI', 'DU', 'D'})

_SO_DIGITOS_FINAL = re.compile(r'[\s\-.,;:/#nº°]*\d+$')
_NAO_ALFANUM = re.compile(r'[^0-9A-Za-zÀ-ÿ ]+')


def nome_de_segredo(nome: str | None) -> bool:
    """`True` quando o nome NÃO identifica ninguém e não pode virar `Parte`.

    Conservador de propósito: na dúvida devolve `False` (mantém a parte). O
    custo dos dois erros não é simétrico — descartar um nome bom perde uma
    linha; criar uma `Parte` a partir de 'SIGILO' publica uma entidade que não
    existe e cola 358 processos distintos numa só (medido: 'SIGILO' aparece 358
    vezes na amostra, sempre como o mesmo texto).
    """
    if not nome:
        return True
    limpo = ' '.join(str(nome).split())
    if not limpo:
        return True

    up = limpo.upper()
    for marcador in _MARCADORES_SEGREDO:
        if marcador in up:
            return True
    # 'SIGILO', 'SIGILO1', 'SIGILO 2' — sufixo numérico é enumeração de parte,
    # não parte do nome.
    nu = _SO_DIGITOS_FINAL.sub('', up).strip()
    if nu in _NOMES_SEGREDO_EXATOS:
        return True

    # Só iniciais: todo token é uma letra (com ou sem ponto) ou um conectivo.
    # Cobre 'E.O.', 'W. V. D.', 'A. H. DOS S.' e o nome de UMA letra ('I').
    tokens = [t for t in _NAO_ALFANUM.sub(' ', limpo).upper().split() if t]
    if not tokens:
        return True
    reais = [t for t in tokens if len(t) > 1 and t not in _CONECTIVOS]
    return not reais


# --------------------------------------------------------------------------
# OAB
# --------------------------------------------------------------------------
#: Mesma forma que `enrichers.parsers.parse_oab` extrai do texto: dígitos (com
#: pontos de milhar) e, opcionalmente, UMA letra de sufixo. `parse_oab` remove
#: `.` e `-` e concatena `UF + número`, e é ESSE formato que está gravado nas
#: 17,7 M linhas de `tribunals_parte` — conferido em produção: 'GO26464',
#: 'BA5249', 'RJ209212A', 'CE14458S', 'PR61230A' (195.651 de 200.000 em
#: `^[A-Z]{2}[0-9]+$`, o resto com sufixo de uma letra). `Parte.oab` tem
#: unique constraint parcial: formatar diferente = Parte duplicada.
_NUMERO_OAB_RE = re.compile(r'[\d.]+(?:-?[A-Za-z])?')


def formatar_oab(numero_oab: str | None, uf_oab: str | None) -> str:
    """`('6094', 'AP')` → `'AP6094'`. Devolve `''` quando não dá pra provar.

    Formatos medidos em 26.569 advogados de produção (semente 20260825):

        só dígitos ........ 23.832 (89,7%)   '6094'
        dígitos + letra ...  2.469 ( 9,3%)   '46601A'
        UF + dígitos ......    103 ( 0,4%)   'MT4960'   ← concatenar duplicaria a UF
        lixo ..............     ~65          'R', 'D', 'A1608', '27467/O'

    A UF repetida (prefixo OU sufixo) é removida antes de reconcatenar: sem
    isso, `uf='MT'` + `numero='MT4960'` viraria `'MTMT4960'`, uma `Parte` nova
    para um advogado que já existe. Número que não COMEÇA com dígito ('R',
    'A1608') faz a função abster — o advogado ainda entra, pelo caminho
    `sem_id` (nome), só não ganha OAB.
    """
    uf = re.sub(r'[^A-Z]', '', (uf_oab or '').upper())
    if len(uf) != 2:
        return ''
    num = re.sub(r'\s+', '', (numero_oab or '').upper())
    if len(num) > 2 and num.startswith(uf):
        num = num[2:]
    if len(num) > 2 and num.endswith(uf):
        num = num[:-2]
    m = _NUMERO_OAB_RE.match(num)
    if not m:
        return ''
    corpo = m.group(0).replace('.', '').replace('-', '')
    # Zero à esquerda FORA. Sem isto a MESMA pessoa vira duas entidades: o
    # piloto do TJRS (faixa `Process.id ∈ [57.000.000, 57.020.000)`) mediu
    # **24.164 `Parte` com OAB criadas, 18.968 depois de normalizar o zero —
    # 5.196 duplicatas, 21,5%**, e 7.290 pares de `ProcessoParte` do mesmo nome
    # em 4.622 processos (23,1% da faixa).
    #
    # A causa é a fonte, não nós: o DJEN publica a MESMA inscrição nos dois
    # formatos, às vezes no mesmo processo. `Process.id=57000005`, três
    # movimentações da advogada CLAUDIA BRESSLER:
    #
    #     mov mais recente   "numero_oab": "39599"
    #     mov anterior       "numero_oab": "RS039599"
    #     mov anterior       "numero_oab": "RS039599"
    #
    # Sem a normalização isso produzia `RS39599` **e** `RS039599`. E o
    # `RS39599` é a linha CERTA: já existia em `tribunals_parte` (id
    # 469842024) com o CPF `761.955.030-53`, criada pelo enricher — ou seja,
    # o formato sem zero é o que casa com o corpus.
    #
    # Na faixa do piloto, **5.494 de 6.398** `numero_oab` vêm com a UF na
    # frente (85,9%) e **nenhum** começa com `0` puro: o zero só aparece
    # DEPOIS da UF (`RS039599`), que é justamente o que a remoção do prefixo
    # expõe. Na amostra nacional a UF prefixada era 0,4% — o formato varia por
    # tribunal, então normalizar não é otimização, é correção.
    #
    # Divergência contra `parse_oab` ENCERRADA em 31/08/2026 (#96): ele lê de
    # TEXTO ("OAB PA 015237") e preservava o zero, e era essa diferença entre as
    # duas portas de escrita que produzia as duplicatas. As duas agora chamam
    # `canonizar_oab`.
    #
    # A remoção mora em `tribunals.services.oab.canonizar_oab` — a MESMA que
    # `enrichers.parsers.parse_oab` passou a usar em 31/08/2026 (#96). Enquanto
    # eram duas implementações, a divergência entre as portas de escrita
    # produzia 19.494 linhas duplicadas em `tribunals_parte`.
    bruto = f'{uf}{corpo}'
    return canonizar_oab(bruto) or bruto


def polo_de(valor) -> str:
    """`'A'`→ativo, `'P'`→passivo, qualquer outra coisa→outros.

    Os coletores de `diarios/` gravam nas MESMAS colunas com polo `''` e às
    vezes um `papel` — por isso o default é `outros` e não uma exceção.
    """
    return POLO_DJEN.get(str(valor or '').strip().upper(), ProcessoParte.POLO_OUTROS)



# --------------------------------------------------------------------------
# Papel processual a partir do TEXTO da publicação
# --------------------------------------------------------------------------
#: Rótulos que o cabeçalho da publicação usa. Medido em 720 publicações eproc
#: amostradas por 60 âncoras (27/08/2026): **79,2% trazem a tabela**, e estes
#: são os rótulos que apareceram, do mais comum ao menos:
#:   ADVOGADO(A) 1002 · AUTOR 302 · EXEQUENTE 119 · RELATOR 75 · EXECUTADO 60
#:   REQUERENTE 50 · AGRAVANTE 45 · RÉU 30 · AGRAVADO 28 · APELANTE 18
#:   EMBARGANTE 15 · APELADO 14 · IMPETRANTE 7 · RECORRENTE 6 · RECORRIDO 6
#:   INTERESSADO 6 · RELATORA 6 · REQUERIDO 4
#:
#: O JSONB do DJEN só diz `polo: "A"/"P"` — POSIÇÃO. O texto diz a FUNÇÃO, e é
#: a função que separa "ainda discute" de "já executa": EXEQUENTE num processo
#: de polo A não é a mesma coisa que AUTOR.
PAPEIS_CONHECIDOS = {
    'AUTOR', 'AUTORA', 'RÉU', 'RE', 'REU', 'RÉ', 'EXEQUENTE', 'EXECUTADO',
    'REQUERENTE', 'REQUERIDO', 'REQUERIDA', 'AGRAVANTE', 'AGRAVADO',
    'APELANTE', 'APELADO', 'EMBARGANTE', 'EMBARGADO', 'IMPETRANTE',
    'IMPETRADO', 'RECORRENTE', 'RECORRIDO', 'INTERESSADO', 'LITISCONSORTE',
    'TERCEIRO', 'ASSISTENTE', 'SUCESSOR', 'HERDEIRO', 'INVENTARIANTE',
    'CREDOR', 'DEVEDOR', 'SUSCITANTE', 'SUSCITADO', 'DENUNCIADO',
}

#: NÃO são parte. `RELATOR` é o desembargador do acórdão; virou 75 ocorrências
#: na amostra e entraria como pessoa do processo se a gente não recusasse.
#: `ADVOGADO(A)` é parte, mas o papel dele já é o próprio rótulo.
NAO_SAO_PARTE = {'RELATOR', 'RELATORA', 'JUIZ', 'JUÍZA', 'JUIZA',
                 'PROCURADOR', 'PROCURADORA', 'DEFENSOR', 'DEFENSORA',
                 'PERITO', 'PERITA', 'MINISTRO', 'MINISTRA'}

_RE_LINHA_PAPEL = re.compile(
    r'<td>\s*([A-ZÀ-Ú()\s/\.\-º]{3,40}?)\s*</td>\s*<td>\s*:\s*([^<]{2,180})</td>',
    re.IGNORECASE)


def _so_papel(rotulo: str) -> str:
    """`ADVOGADO(A)` → `ADVOGADO`; `AUTOR ` → `AUTOR`; tira sufixo e espaço."""
    r = re.sub(r'\(.*?\)', '', rotulo or '').strip().upper()
    return re.sub(r'\s+', ' ', r)


def papeis_do_texto(cabecalho: str | None) -> dict:
    """`{nome_normalizado: PAPEL}` lido do cabeçalho da publicação.

    Ele NÃO cria parte — só rotula quem o JSONB já trouxe. É de propósito: o
    JSONB é a fonte de QUEM (e passou pelo filtro de segredo); o texto é a
    fonte de QUAL FUNÇÃO. Assim um cabeçalho estranho nunca inventa gente, no
    máximo deixa de rotular — que é a abstenção da regra nº 6.

    Devolve `{}` quando não há tabela, sem levantar.
    """
    if not cabecalho:
        return {}
    fora = {}
    for rotulo, valor in _RE_LINHA_PAPEL.findall(cabecalho):
        papel = _so_papel(rotulo)
        if not papel or papel in NAO_SAO_PARTE:
            continue
        if papel not in PAPEIS_CONHECIDOS and papel != 'ADVOGADO':
            continue                      # rótulo que não conhecemos: abstém
        nome = re.sub(r'\s+', ' ', (valor or '')).strip()
        # `FULANO (OAB RJ250427)` → o nome é só o que vem antes do parêntese
        nome = re.sub(r'\s*\(OAB[^)]*\)\s*$', '', nome, flags=re.IGNORECASE).strip()
        if not nome:
            continue
        chave = _chave_nome(nome)
        # primeira ocorrência vence; se o mesmo nome aparece com DOIS papéis
        # diferentes, a fonte se contradiz e a gente abstém (apaga o rótulo).
        if chave in fora and fora[chave] != papel:
            fora[chave] = ''
        else:
            fora.setdefault(chave, papel)
    return {k: v for k, v in fora.items() if v}


#: Teto do `ProcessoParte.papel` (varchar 120). Cortar aqui e não no banco
#: porque um papel truncado no meio ainda é um papel legível; um `DataError` no
#: meio de um lote de 500 derruba a coleta inteira.
_MAX_PAPEL = 120


def papel_da_fonte(destinatario: dict) -> str:
    """O papel que a PRÓPRIA fonte imprimiu no registro, quando ela imprime.

    O DJEN não imprime (medido: 10 em 23.771 destinatários = 0,04%) e por isso
    este módulo nasceu lendo o papel só do TEXTO. Mas os coletores de
    `diarios/` gravam o rótulo verbatim no mesmo JSONB — e no caso que motivou
    esta função ele é o dado, não um enfeite: a relação da DEPRE imprime
    `Entidade devedora:` e `Reqte:` em campos rotulados, um por linha.

    Medido em 02/09/2026: os 2.568 registros da relação de 10/03/2025 entraram
    em `Movimentacao.destinatarios` com `papel='ENTIDADE DEVEDORA'` e
    `polo='P'`, e `specs_do_processo` devolvia `papel=''` para os três — o polo
    sobrevivia, o papel morria aqui. Para a tela "Quem deve" isso é quase o
    mesmo que não ter extraído.

    A precedência é: **fonte antes de texto**. Quem rotulou o campo foi o
    diário; o `papeis_do_texto` é inferência sobre uma tabela HTML e só entra
    quando a fonte não disse nada.
    """
    valor = destinatario.get('papel') if isinstance(destinatario, dict) else None
    return re.sub(r'\s+', ' ', str(valor or '')).strip()[:_MAX_PAPEL]


def _chave_nome(nome: str) -> str:
    """Nome comparável entre JSONB e texto: sem acento, sem pontuação, upper."""
    import unicodedata
    n = unicodedata.normalize('NFKD', nome or '')
    n = ''.join(ch for ch in n if not unicodedata.combining(ch))
    return re.sub(r'[^A-Z0-9 ]', '', n.upper()).strip()

# --------------------------------------------------------------------------
# Extração das specs de Parte a partir do JSONB
# --------------------------------------------------------------------------
@dataclass
class SpecsProcesso:
    """Specs de `Parte` de um processo, no formato que `_route_parte` espera."""

    por_polo: dict = field(default_factory=dict)
    descartados_segredo: int = 0
    #: A fonte AFIRMOU segredo (marcador textual), não só mascarou o nome.
    marcador_segredo: bool = False
    vistos: set = field(default_factory=set)
    #: Nomes cujo polo a própria janela contradiz — foram para `outros`.
    polo_abstido: int = 0

    def __bool__(self) -> bool:
        return any(self.por_polo.values())


def _polos_contraditorios(movimentacoes) -> set:
    """Nomes que aparecem com MAIS DE UM polo dentro da mesma janela.

    O JSONB do DJEN diz o polo da COMUNICAÇÃO, não o da ação. Quando o processo
    tem recurso, as duas coisas divergem — e divergem legitimamente. Medido em
    02/09/2026 (semente 20260902, 24 âncoras, 900 processos que JÁ TÊM parte de
    enricher, `Process.id=2740674`):

        mov0  RECURSO INOMINADO · RECORRENTE: INSS       → AGNALDO ISMAEL polo P
        mov1  JUIZADO ESPECIAL  · AUTOR: AGNALDO ISMAEL  → AGNALDO ISMAEL polo A

    As duas linhas estão CERTAS, cada uma sobre a sua fase. O que não pode
    existir é o que o código fazia antes: como o dedupe é por `(nome, polo)`,
    ele gravava as DUAS `ProcessoParte` — a mesma pessoa no polo ativo E no
    passivo do mesmo processo, um fato impossível, e uma delas necessariamente
    errada. Escolher uma seria chute; a regra nº 6 manda abster.

    Então a pessoa entra em `outros` — que é o slot de abstenção que o modelo
    já tem (é onde os advogados moram, pelo mesmo motivo: não dá pra provar o
    lado). Perde-se o polo, não a parte.

    Tamanho medido: **8 de 688 nomes (1,16%)** na amostra, e esses 8 explicavam
    8 das 22 divergências de polo contra o enricher. As outras 14 (2,1% dos 654
    que batem por nome) são janelas SÓ com publicação de recurso, onde o DJEN é
    coerente consigo mesmo e não há contradição a detectar — ver `.ia/
    ENRICHMENT.md`.
    """
    polos: dict = {}
    for mov in movimentacoes:
        for d in _como_lista(mov[0]):
            if not isinstance(d, dict):
                continue
            nome = (d.get('nome') or '').strip()[:255]
            if not nome or nome_de_segredo(nome):
                continue
            polos.setdefault(_chave_nome(nome), set()).add(polo_de(d.get('polo')))
    return {k for k, v in polos.items() if len(v) > 1}


def specs_do_processo(movimentacoes) -> SpecsProcesso:
    """`[(destinatarios, destinatario_advogados), …]` → `SpecsProcesso`.

    `movimentacoes` são as N mais recentes do processo (ver `JANELA_MOVS`), e
    NÃO uma só: medido em produção, das 4.640 movimentações lidas nas 6 âncoras
    de custo, 698 tinham `destinatarios = []`. Numa âncora (24581073) 491 de
    672 estavam vazias — ler só a última perderia o processo inteiro.

    Cada lista pode vir como `list` (JSONField do ORM) ou `str` (driver cru:
    psycopg devolve `jsonb` como texto, e `bool('[]')` é `True` — foi assim que
    a primeira medição desta missão reportou 100% de cobertura onde havia 94%).
    Por isso o parse é explícito, nunca `if valor:`.
    """
    out = SpecsProcesso()
    papeis: dict = {}
    for mov in movimentacoes:
        # tupla de 3 desde 27/08/2026 (o cabeçalho entrou); aceita a de 2 para
        # não quebrar chamador antigo — abstenção, não exceção.
        cabecalho = mov[2] if len(mov) > 2 else None
        for k, v in papeis_do_texto(cabecalho).items():
            # o mais RECENTE vence: a primeira movimentação da lista é a mais
            # nova (ORDER BY data_disponibilizacao DESC), então só preenche
            # quem ainda não tem rótulo.
            papeis.setdefault(k, v)

    contraditorios = _polos_contraditorios(movimentacoes)
    out.polo_abstido = len(contraditorios)

    for mov in movimentacoes:
        destinatarios, advogados = mov[0], mov[1]
        for d in _como_lista(destinatarios):
            if not isinstance(d, dict):
                continue
            nome = (d.get('nome') or '').strip()[:255]
            polo = polo_de(d.get('polo'))
            if nome and _tem_marcador_de_segredo(nome):
                out.marcador_segredo = True
            if nome_de_segredo(nome):
                out.descartados_segredo += 1
                continue
            # A janela se contradiz sobre o lado desta pessoa: abstém do polo,
            # mantém a pessoa. Ver `_polos_contraditorios`.
            if _chave_nome(nome) in contraditorios:
                polo = ProcessoParte.POLO_OUTROS
            chave = ('n', nome, polo)
            if chave in out.vistos:
                continue
            out.vistos.add(chave)
            out.por_polo.setdefault(polo, []).append({
                'nome': nome, 'documento': '', 'tipo_documento': '',
                'oab': '', 'tipo': 'desconhecido',
                # Papel: o que a FONTE rotulou vence o que o TEXTO sugere. O
                # DJEN não rotula (0,04%) e cai no `papeis_do_texto`; os
                # coletores de `diarios/` rotulam, e ali o rótulo É o dado —
                # `ENTIDADE DEVEDORA` no polo passivo é o "quem deve".
                # Vazio nos dois = não achamos, e vazio é o que a coluna já
                # guardava. Nunca chutamos um papel por posição.
                'papel': papel_da_fonte(d) or papeis.get(_chave_nome(nome), ''),
            })

        for a in _como_lista(advogados):
            if not isinstance(a, dict):
                continue
            adv = a.get('advogado')
            if not isinstance(adv, dict):
                continue
            nome = (adv.get('nome') or '').strip()[:255]
            if nome_de_segredo(nome):
                out.descartados_segredo += 1
                continue
            oab = formatar_oab(adv.get('numero_oab'), adv.get('uf_oab'))
            # Advogado sempre em `outros`: o DJEN não diz de que lado ele está,
            # e o `polo` que aparece no dict do STF é do polo REPRESENTADO, não
            # do advogado.
            chave = ('a', oab or nome)
            if chave in out.vistos:
                continue
            out.vistos.add(chave)
            out.por_polo.setdefault(ProcessoParte.POLO_OUTROS, []).append({
                'nome': nome, 'documento': '', 'tipo_documento': '',
                'oab': oab, 'tipo': 'advogado',
                'papel': papeis.get(_chave_nome(nome), ''),
            })
    return out


def _tem_marcador_de_segredo(nome: str) -> bool:
    """Só o marcador TEXTUAL (a fonte dizendo "segredo de justiça"), não a
    máscara por iniciais. Máscara por iniciais também é usada em processo de
    família sem segredo decretado — não dá pra provar, então não afirmamos."""
    up = ' '.join(str(nome).split()).upper()
    if any(m in up for m in _MARCADORES_SEGREDO):
        return True
    return _SO_DIGITOS_FINAL.sub('', up).strip() in _NOMES_SEGREDO_EXATOS


def _como_lista(valor) -> list:
    if isinstance(valor, list):
        return valor
    if isinstance(valor, (str, bytes)):
        import json
        try:
            parsed = json.loads(valor)
        except (ValueError, TypeError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


# --------------------------------------------------------------------------
# Leitura: as N movimentações mais recentes de cada processo
# --------------------------------------------------------------------------
#: A auditoria usou 3 (`LATERAL … ORDER BY data_disponibilizacao DESC LIMIT 3`)
#: e é o que medimos: 1,26 s por 1.000 processos, incluindo a janela de pk e o
#: NOT EXISTS. `tribunals_movimentacao` tem 1,52 BILHÃO de linhas — a entrada é
#: SEMPRE pelo índice `mov_processo_data_disp_idx (processo_id, -data_disp)`,
#: nunca por varredura.
JANELA_MOVS = 3

#: `left(texto, 4000)` e não `texto`: a tabela de partes vem SEMPRE no topo da
#: publicação (é o cabeçalho, antes do "SENTENÇA"/"DESPACHO"). Trazer o texto
#: inteiro multiplicaria a leitura por 10 sem acrescentar um papel sequer —
#: e a regra nº 1 aqui é iterar, não acumular.
_CABECALHO_BYTES = 4000

_SQL_MOVS = """
SELECT p.id, m.destinatarios, m.destinatario_advogados, m.cabecalho
FROM unnest(%s::bigint[]) AS p(id)
JOIN LATERAL (
    SELECT destinatarios, destinatario_advogados, left(texto, %s) AS cabecalho
    FROM tribunals_movimentacao
    WHERE processo_id = p.id
    ORDER BY data_disponibilizacao DESC
    LIMIT %s
) m ON TRUE
"""

#: `SET LOCAL`, nunca `SET`. O pgbouncer roda em `pool_mode=transaction`: um
#: `SET` de sessão vai para uma conexão de servidor que é devolvida ao pool
#: antes da consulta seguinte, então o teto NÃO acompanha a query — medido em
#: 25/08/2026, um `SET statement_timeout='20s'` deixou passar uma consulta de
#: 405 s. Pior: o `SET` fica PINADO naquela conexão e a próxima aplicação que a
#: receber herda o teto (censo: 63 de 250 conexões do pool carregavam
#: `statement_timeout` de sessão alheio). Mesmo motivo em `djen/jobs.py:336`.
TIMEOUT_LEITURA_S = 120
TIMEOUT_ESCRITA_S = 60


def _cursor_com_teto(cur, segundos: int) -> None:
    cur.execute('SET LOCAL statement_timeout = %s', [int(segundos * 1000)])


def ler_movimentacoes(process_ids: list[int], janela: int = JANELA_MOVS) -> dict:
    """`{process_id: [(destinatarios, advogados), …]}` — as `janela` mais
    recentes de cada processo. Transação curta, com teto."""
    if not process_ids:
        return {}
    por_processo: dict = {}
    with transaction.atomic():
        with connection.cursor() as cur:
            _cursor_com_teto(cur, TIMEOUT_LEITURA_S)
            cur.execute(_SQL_MOVS, [list(process_ids), _CABECALHO_BYTES, janela])
            for pid, dest, advs, cabecalho in cur.fetchall():
                por_processo.setdefault(pid, []).append((dest, advs, cabecalho))
    return por_processo


def sem_processoparte(process_ids: list[int]) -> list[int]:
    """Filtra para os que têm ZERO `ProcessoParte` (D2).

    Processo que já tem parte é PULADO, não complementado: a linha existente
    veio do enricher (com CPF/CNPJ e papel) ou do Datajud, e é melhor que a
    nossa. Complementar criaria duplicata semântica — o enricher grava
    `papel='AUTOR'` e nós gravaríamos `papel=''` para a mesma pessoa.
    """
    if not process_ids:
        return []
    with transaction.atomic():
        with connection.cursor() as cur:
            _cursor_com_teto(cur, TIMEOUT_LEITURA_S)
            cur.execute(
                'SELECT DISTINCT processo_id FROM tribunals_processoparte '
                'WHERE processo_id = ANY(%s)',
                [list(process_ids)],
            )
            ja_tem = {r[0] for r in cur.fetchall()}
    return [pid for pid in process_ids if pid not in ja_tem]


# --------------------------------------------------------------------------
# Escrita
# --------------------------------------------------------------------------
@dataclass
class ResultadoLote:
    janela: int = 0                 # processos lidos da faixa de pk
    alvo: int = 0                   # … destes, sem nenhuma ProcessoParte
    pulados_com_parte: int = 0
    sem_movimentacao: int = 0
    sem_destinatario: int = 0       # tem movimentação, mas os JSONB estão vazios
    so_segredo: int = 0             # tinha destinatário, e TODOS eram de segredo
    descartados_segredo: int = 0    # destinatários individuais descartados
    com_marcador_segredo: int = 0   # processos onde a fonte AFIRMOU segredo
    polo_abstido: int = 0           # nomes cujo polo a janela contradiz → `outros`
    partes_upsert: int = 0          # entidades Parte roteadas
    linhas_tentadas: int = 0        # ProcessoParte oferecidas ao bulk_create
    linhas_confirmadas: int = 0     # contagem INDEPENDENTE no banco, pós-insert
    processos_tocados: list = field(default_factory=list)
    segundos_leitura: float = 0.0
    segundos_escrita: float = 0.0


def sem_parte_de_terceiro(process_ids: list[int]) -> list[int]:
    """Filtra para os que não têm parte de OUTRA procedência (`fonte IS NULL`).

    Variante deliberadamente mais permissiva que `sem_processoparte`, e o
    motivo é medido. `sem_processoparte` pula todo processo que já tenha
    QUALQUER `ProcessoParte`, porque "a linha existente veio do enricher (com
    CPF/CNPJ e papel) e é melhor que a nossa". Isso continua verdade para o
    enricher — mas deixou de ser verdade quando o processo já tem linha da
    NOSSA PRÓPRIA promoção: em 02/09/2026, os 823 processos da relação da DEPRE
    com mais de 3 movimentações receberam linhas com `papel=''` (vindas de
    outras publicações do mesmo processo) e, por já "terem parte", nunca mais
    seriam alcançados pelo registro que traz o ENTE DEVEDOR rotulado.

    Pular por causa de uma linha que nós mesmos escrevemos, e pior, para não
    escrever a linha melhor, é o oposto do que a regra queria proteger.

    `fonte IS NULL` = enricher/legado/Datajud: esses continuam intocados.
    """
    if not process_ids:
        return []
    with transaction.atomic():
        with connection.cursor() as cur:
            _cursor_com_teto(cur, TIMEOUT_LEITURA_S)
            cur.execute(
                'SELECT DISTINCT processo_id FROM tribunals_processoparte '
                'WHERE processo_id = ANY(%s) AND fonte IS NULL',
                [list(process_ids)],
            )
            de_terceiro = {r[0] for r in cur.fetchall()}
    return [pid for pid in process_ids if pid not in de_terceiro]


def ler_movimentacoes_por_pk(mov_ids: list[int]) -> dict:
    """`{process_id: [(destinatarios, advogados, cabecalho), …]}` para
    movimentações NOMEADAS, sem janela.

    Existe porque a janela de `JANELA_MOVS=3` mais recentes é um teto invisível
    para quem SABE quais linhas acabou de gravar. Medido em 02/09/2026, na
    relação da DEPRE de 10/03/2025: dos 2.568 processos, **823 (32%) têm mais
    de 3 movimentações**, e nenhum deles ganhou o ente devedor — a linha da
    DEPRE não estava entre as 3 mais recentes. Os 1.445 que ganharam estão
    TODOS na faixa de até 3. O coletor conhece os pks do lote; usá-los é trocar
    uma heurística por um fato.
    """
    if not mov_ids:
        return {}
    por_processo: dict = {}
    with transaction.atomic():
        with connection.cursor() as cur:
            _cursor_com_teto(cur, TIMEOUT_LEITURA_S)
            cur.execute(
                'SELECT processo_id, destinatarios, destinatario_advogados, '
                'left(texto, %s) FROM tribunals_movimentacao WHERE id = ANY(%s)',
                [_CABECALHO_BYTES, list(mov_ids)],
            )
            for pid, dest, advs, cabecalho in cur.fetchall():
                por_processo.setdefault(pid, []).append((dest, advs, cabecalho))
    return por_processo


def promover_lote(process_ids: list[int], *, janela: int = JANELA_MOVS,
                  dry_run: bool = False, fonte: str = FONTE,
                  movs_por_processo: dict | None = None) -> ResultadoLote:
    """Promove um lote JÁ FILTRADO (só processos sem `ProcessoParte`).

    Idempotência pela CONSTRAINT, não por delete: tudo aqui sai com
    `representa=NULL`, então `uniq_processo_parte_polo_papel_principal`
    (`processo, parte, polo, papel` WHERE `representa IS NULL`) cobre 100% das
    linhas e `bulk_create(ignore_conflicts=True)` é idempotente e race-safe.

    É PROIBIDO usar o caminho do `apply_event`/`apply_batch`
    (`DELETE FROM tribunals_processoparte WHERE processo_id = ANY(...)`): ele
    apagaria dado do enricher, que é melhor que o nosso.
    """
    from enrichers.drainer import _bulk_upsert_partes, _route_parte
    from enrichers.stream import STATUS_OK

    res = ResultadoLote(alvo=len(process_ids))
    if not process_ids:
        return res

    t0 = time.time()
    # `movs_por_processo` injetado = o chamador SABE quais linhas quer promover
    # (ver `ler_movimentacoes_por_pk`). Sem ele, a heurística das `janela` mais
    # recentes, que é o que o backfill por faixa de pk tem à disposição.
    movs = (movs_por_processo if movs_por_processo is not None
            else ler_movimentacoes(process_ids, janela))
    res.segundos_leitura = time.time() - t0

    eventos: dict = {}
    specs_por_pid: dict = {}
    for pid in process_ids:
        lista = movs.get(pid)
        if not lista:
            res.sem_movimentacao += 1
            continue
        specs = specs_do_processo(lista)
        res.descartados_segredo += specs.descartados_segredo
        res.polo_abstido += specs.polo_abstido
        if specs.marcador_segredo:
            res.com_marcador_segredo += 1
        if not specs:
            # Distingue "os JSONB estavam vazios" de "tinha gente, e era toda
            # de segredo" — a segunda é um processo que NÃO ganha parte e
            # precisa aparecer no relatório em vez de sumir no resto.
            if specs.descartados_segredo:
                res.so_segredo += 1
            else:
                res.sem_destinatario += 1
            continue
        specs_por_pid[pid] = specs
        eventos[pid] = {'process_id': pid, 'status': STATUS_OK,
                        'partes': specs.por_polo}

    if not eventos:
        return res

    if dry_run:
        # `_bulk_upsert_partes` ESCREVE `Parte`. Chamá-lo aqui foi um defeito
        # real desta missão: o primeiro `--dry-run` do piloto criou **39.303
        # linhas de `Parte` órfãs** em produção (20.609 `desconhecido` +
        # 18.694 `advogado`, nenhuma com `ProcessoParte`). O controle que
        # provou a autoria: na hora ANTERIOR, a mesma consulta devolveu **0**
        # órfã de qualquer tipo — o drainer cria `Parte` e `ProcessoParte` na
        # mesma transação, então órfã recente não é dele.
        #
        # Um `--dry-run` que escreve é pior que não ter `--dry-run`: ele é
        # usado justamente por quem ainda não decidiu rodar.
        res.linhas_tentadas = sum(
            len({(_route_parte(spec), polo)
                 for polo, lista in specs.por_polo.items() for spec in lista})
            for specs in specs_por_pid.values()
        )
        return res

    t0 = time.time()
    # Reusa o upsert de `Parte` do drainer (4 caminhos de constraint), inclusive
    # a armadilha do caminho `sem_id`: ele casa o nome com uma `Parte` existente
    # que tenha CNPJ completo antes de criar — é o que evita duplicar
    # "ESTADO DO AMAPÁ" 40 mil vezes. Do DJEN só nascem 2 caminhos: `oab`
    # (advogados) e `sem_id` (destinatários, `tipo='desconhecido'` — medido:
    # 6.398 de 6.398 `Parte` sem doc e sem OAB estão em 'desconhecido', então
    # a chave `(nome, tipo)` casa com o que já existe).
    spec_to_id = _bulk_upsert_partes(eventos)
    res.partes_upsert = len(spec_to_id)

    linhas = []
    vistos: set = set()
    for pid, specs in specs_por_pid.items():
        for polo, lista in specs.por_polo.items():
            for spec in lista:
                parte_id = spec_to_id.get(_route_parte(spec))
                if not parte_id:
                    continue
                # `papel` do TEXTO quando o cabeçalho confirmou; senão o
                # default histórico (vazio). Ele entra na chave porque a
                # constraint é `(processo, parte, polo, papel)`.
                papel = (spec.get('papel') or PAPEL)[:40]
                chave = (pid, parte_id, polo, papel)
                if chave in vistos:
                    continue
                vistos.add(chave)
                linhas.append(ProcessoParte(
                    processo_id=pid, parte_id=parte_id, polo=polo,
                    papel=papel, representa_id=None, fonte=fonte,
                ))
    res.linhas_tentadas = len(linhas)

    if linhas and not dry_run:
        # Ordem total por (processo, parte, polo) — regra da casa: todo
        # bulk_create de tabela escrita por mais de um worker sai em ordem
        # comum, senão dois lados formam ciclo de espera e o Postgres mata um.
        linhas.sort(key=lambda pp: (pp.processo_id, pp.parte_id, pp.polo))
        with transaction.atomic():
            with connection.cursor() as cur:
                _cursor_com_teto(cur, TIMEOUT_ESCRITA_S)
            ProcessoParte.objects.bulk_create(
                linhas, ignore_conflicts=True, batch_size=500,
            )
            # TOCA A CAMPAINHA. `ProcessoParte` é OUTRA TABELA: escrever nela não
            # mexe em `Process.atualizado_em`, e `sync_processos_atualizados` é
            # keyset POR `atualizado_em`. Sem este UPDATE o dado fica perfeito no
            # Postgres e invisível na tela — medido em 27/08/2026: 100 partes no
            # banco e 0 no índice, em 5 de 5 processos conferidos.
            # `atualizado_em` é `auto_now`, e `auto_now` só roda em `Model.save()`
            # — nem `.update()` nem `bulk_create` o disparam. Mesma lição que
            # `datajud/ingestion.py` já tinha aprendido e registrado.
            # Dentro da MESMA transação: ou entram as partes e a campainha, ou
            # nenhum dos dois. Campainha sem dado é reindex à toa; dado sem
            # campainha é o buraco que estamos fechando.
            pids = sorted(specs_por_pid)
            with connection.cursor() as cur:
                _cursor_com_teto(cur, TIMEOUT_ESCRITA_S)
                cur.execute('UPDATE tribunals_process SET atualizado_em = now() '
                            'WHERE id = ANY(%s)', [pids])
        res.processos_tocados = sorted(specs_por_pid)
        # Contagem INDEPENDENTE: `bulk_create(ignore_conflicts=True)` não
        # devolve pk confiável, e o log do próprio job não é prova (regra nº 5).
        res.linhas_confirmadas = _contar_linhas_djen(res.processos_tocados, fonte)
    res.segundos_escrita = time.time() - t0
    return res


def _contar_linhas_djen(process_ids: list[int], fonte: str = FONTE) -> int:
    """Contagem INDEPENDENTE, no banco, das linhas que ESTE lote criou.

    O `fonte` acompanha o da escrita de propósito: contar por `'djen'` uma
    passada que carimbou `'diario'` devolveria 0 e o job reportaria "não
    gravei" tendo gravado — número redondo, do jeito errado.
    """
    if not process_ids:
        return 0
    with transaction.atomic():
        with connection.cursor() as cur:
            _cursor_com_teto(cur, TIMEOUT_LEITURA_S)
            cur.execute(
                'SELECT count(*) FROM tribunals_processoparte '
                'WHERE processo_id = ANY(%s) AND fonte = %s',
                [list(process_ids), fonte],
            )
            return cur.fetchone()[0]
