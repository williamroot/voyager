"""Quem ASSINOU o ato — extração determinística do nome do magistrado.

POR QUE ISSO EXISTE
-------------------
Um cliente pediu as tendências de decisão de uma juíza criminal de 1º grau do
TJSP. O dado existe — está escrito no corpo das publicações que já ingerimos —
e nunca foi lido por nada nesta casa. Medido em 03/09/2026 no índice de
publicações:

    5.631.275 publicações do TJSP carregam o marcador `Magistrado(a)`
    8.472.693 no país inteiro, espalhadas por 59 tribunais

PRINCÍPIO: MARCADOR, NUNCA MODELO
---------------------------------
Não há LLM aqui, e não é economia: é que o nome do magistrado é **impresso pela
fonte num lugar fixo**, do mesmo jeito que o CNJ é impresso na relação da DEPRE.
Onde a fonte imprime, a leitura é determinística e conferível; onde ela não
imprime, **abstém** (regra nº 6 do CLAUDE.md). Cada formato conhecido é uma
entrada declarada em `MARCADORES`, com nome próprio, e tudo que não casa nenhum
deles é contado — não é `pass` silencioso.

AS TRÊS ARMADILHAS QUE ESTA MEDIÇÃO JÁ PAGOU (03/09/2026)
---------------------------------------------------------
1. **Nome NÃO identifica magistrado.** `match_phrase` por
   "Rafaela Caldeira Gonçalves" devolve 195 publicações; **56 são de TJCE,
   TJRO, TJPE, TJPI e TJMA** — outras pessoas com o mesmo nome. A identidade é
   **(tribunal, órgão, nome normalizado)**, nunca o nome sozinho.
2. **Citar não é assinar.** O corpo da publicação cita precedentes de OUTROS
   tribunais com o nome do relator de lá:

       (STJ - AgRg no AREsp: 1683006 SC, Relator: Ministro NEFI CORDEIRO, …)
       (Acórdão 1792182, 0735554-80.2023.8.07.0000, Relator(a): DIAULAS …)

   Extrair isso atribuiria um ministro do STJ a uma vara do TJCE. Toda
   ocorrência **dentro de parênteses** é recusada (`citacao`), porque é ali que
   a citação mora — e o teste de controle prova que a assinatura de verdade
   está sempre FORA.
3. **A menção não é a assinatura.** Na mesma juíza do caso concreto, uma das
   publicações diz *"…reconhece-se a prevenção daquela Magistrada…"* citando o
   nome dela sem que ela tenha decidido nada ali. Por isso a ficha conta
   **atribuições por marcador**, não ocorrências do nome.

O GABARITO MECÂNICO
-------------------
Toda atribuição carrega o intervalo `[inicio, fim)` de onde saiu, e
`Atribuicao.verbatim_ok(texto_limpo)` confere que `texto_limpo[inicio:fim]` é
**exatamente** o nome devolvido. Não é decoração: é o mesmo papel do quádruplo
da DEPRE (`.ia/DIARIOS.md` §15.1). Nome que não passa é ERRO com número
(`erros['verbatim']`), nunca descarte mudo. A conferência é um `slice` — caminho
de código independente da regex que produziu o nome, pela mesma razão que
`Inventario.ver_bloco` não recebe texto (`.ia/DIARIOS.md` §18.3).

O QUE ESTE MÓDULO **NÃO** FAZ
-----------------------------
Não mede mérito. A intimação diz QUEM assinou, EM QUE órgão e SOBRE que classe;
não traz o inteiro teor, então não dá para medir taxa de deferimento. Prometer
isso a partir de intimação seria dado pela metade — que aqui vale menos que
zero.
"""
from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field

#: Carimbo de procedência, mesmo idioma de `ProcessoParte.fonte` (`'djen'`,
#: `'esaj_incidente'`) e de `Incidente.fonte` (`'esaj'`): quem escreveu a linha.
#: Aqui a procedência é o TEXTO da publicação, não uma consulta ao tribunal.
FONTE_TEXTO_PUBLICACAO = 'texto_publicacao'

# --------------------------------------------------------------------------- #
# Limpeza — o texto que a regex lê
# --------------------------------------------------------------------------- #
_RE_TAG = re.compile(r'<[^>]{1,120}>')
#: cada caractere de espaço vira UM espaço — o RUN é preservado de propósito.
#: A fonte usa espaço duplo (e `<br><br>`, e `&nbsp;&nbsp;`) como separador de
#: CAMPO: `'Relatora: MARIA ROSELI MENDES ALENCAR  ROT 0000387-…'`. Colapsar
#: runs apagaria esse sinal e o extrator engoliria `ROT` como sobrenome —
#: aconteceu, e está no teste.
_RE_ESPACO = re.compile(r'[\s   ]')


def limpar(texto: str | None) -> str:
    """Texto pronto pra regex: sem tag HTML, sem entidade, espaço colapsado.

    A ORDEM importa e foi medida: **tag primeiro, entidade depois**. Ao
    contrário, um `&lt;` viraria `<` e fabricaria tag que a fonte não escreveu.

    Sem `html.unescape` o TJGO é ilegível — ele publica o corpo inteiro
    escapado (`RENATO C&Eacute;SAR DORTA PINHEIRO Juiz de Direito`), e o nome
    sairia truncado em "RENATO C". Sem colapsar `&nbsp;` (U+00A0) o `split()`
    do caminhamento não separa token nenhum.

    ⚠️ Consequência declarada: depois disto o nome extraído **não é**
    necessariamente substring do `body` cru — ele é substring do texto LIMPO,
    e é contra ele que o gabarito verbatim roda. A diferença é medida e
    publicada (`verbatim_no_cru`), nunca escondida.
    """
    if not texto:
        return ''
    return _RE_ESPACO.sub(' ', html.unescape(_RE_TAG.sub(' ', texto)))


def _sem_acento(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFKD', s)
                   if not unicodedata.combining(c))


# --------------------------------------------------------------------------- #
# Vocabulário — o que NÃO é nome
# --------------------------------------------------------------------------- #
#: Tratamento que a fonte cola no nome. Sai do nome exibido E da chave; entra
#: em `Atribuicao.tratamento` para não sumir sem registro.
_TRATAMENTOS = frozenset({
    'DR', 'DRA', 'DES', 'DESA', 'DESEMBARGADOR', 'DESEMBARGADORA',
    'MINISTRO', 'MINISTRA', 'MIN', 'JUIZ', 'JUIZA', 'JUÍZA', 'MM', 'MMA',
    'EXMO', 'EXMA', 'DOUTOR', 'DOUTORA', 'SR', 'SRA', 'PROF',
})

#: Palavras em CAIXA ALTA que aparecem coladas ao cargo e **não** são nome.
#: Fechada de propósito e curta: a defesa principal não é esta lista, é a regra
#: estrutural de token (só letra, sem parêntese, sem dígito). Ela existe para o
#: resíduo que passa nessa regra — medido no TJDFT, cuja assinatura de sistema é
#: `DOCUMENTO DATADO E ASSINADO ELETRONICAMENTE PELO(A) MAGISTRADO(A)`.
_RUIDO = frozenset({
    'DOCUMENTO', 'DATADO', 'ASSINADO', 'ASSINATURA', 'ELETRONICAMENTE',
    'DIGITALMENTE', 'DIGITAL', 'SISTEMA', 'REGISTRADA', 'REGISTRADO',
    'ORDEM', 'PELO', 'PELA', 'POR', 'ANTE', 'PERANTE', 'SENHOR', 'SENHORA',
    'SENHORES', 'SENHORAS', 'EXCELENTISSIMO', 'EXCELENTISSIMA',
    'VARA', 'FORO', 'COMARCA', 'JUIZO', 'GABINETE', 'TRIBUNAL', 'CAMARA',
    'TURMA', 'SECAO', 'COLEGIO', 'CORTE', 'RESPECTIVO', 'PRESENTE',
    'MERITISSIMO', 'MERITISSIMA', 'CONCLUSOS', 'CUMPRA', 'INTIME', 'PUBLIQUE',
    'ESTE', 'ESTA', 'AQUELE', 'AQUELA', 'NOSSO', 'NOSSA', 'VOTO', 'ACORDAO',
    'RELATOR', 'RELATORA', 'MAGISTRADO', 'MAGISTRADA', 'FEDERAL', 'TRABALHO',
    # plurais: o acórdão lista o colegiado antes de nomear o relator
    # ('… os Senhores Desembargadores Jorge Rachid Mubárack Maluf, Relator, …')
    'DESEMBARGADORES', 'DESEMBARGADORAS', 'JUIZES', 'MAGISTRADOS',
    'MAGISTRADAS', 'MINISTROS', 'MINISTRAS', 'RELATORES', 'DOUTORES',
    'DIREITO', 'SUBSTITUTO', 'SUBSTITUTA', 'TITULAR', 'RESPONDENDO',
    'RESPONDENTE', 'AUXILIAR', 'COOPERADOR', 'COOPERADORA',
    # endereço do rodapé, que encosta na assinatura em vários tribunais
    # (medido no TJCE: `'… Fortaleza Rua … Juiz de Direito'` virava nome)
    'RUA', 'AVENIDA', 'AV', 'PRACA', 'ALAMEDA', 'RODOVIA', 'ENDERECO',
    'CEP', 'TELEFONE', 'FONE', 'EMAIL', 'SALA', 'ANDAR', 'BAIRRO', 'CENTRO',
    # RÓTULO DE CAMPO do cabeçalho. Lista fechada, e o mesmo idioma do
    # `_CAMPOS_NAO_PARTE` do segmentador do DJE. Ela existe por uma medição:
    # o cabeçalho de 2º grau do TJSP escreve
    #     'Relator(a): PEDRO PAULO MAILLET PREUSS Órgão Julgador: 24ª Câmara'
    # com UM espaço entre o nome e o rótulo seguinte, então o separador de
    # campo não denuncia nada e `Órgão` — que começa em maiúscula — entrava
    # como sobrenome. Medido na amostra de 600 publicações do TJSP: **82 de
    # 542 atribuições (15,1%)** têm um rótulo de campo colado logo depois do
    # nome, e sem esta lista ele entrava. O dano não é cosmético — `… PREUSS
    # Órgão` e `… PREUSS` viram DUAS identidades na unique, e a contagem de
    # nomes distintos caiu de 230 para 210 quando a lista entrou.
    'ORGAO', 'JULGADOR', 'JULGADORA', 'PROCESSO', 'CLASSE', 'ASSUNTO',
    'SESSAO', 'JULGAMENTO', 'TEXTO', 'DATA', 'LOCAL', 'EMENTA', 'DECISAO',
    'DESPACHO', 'SENTENCA', 'RELATORIO', 'DISPOSITIVO', 'REVISOR',
    'APELANTE', 'APELADO', 'APELADA', 'AGRAVANTE', 'AGRAVADO', 'AGRAVADA',
    'EMBARGANTE', 'EMBARGADO', 'EMBARGADA', 'RECORRENTE', 'RECORRIDO',
    'RECORRIDA', 'REQUERENTE', 'REQUERIDO', 'REQUERIDA', 'EXEQUENTE',
    'EXECUTADO', 'EXECUTADA', 'AUTOR', 'AUTORA', 'REU', 'RE', 'INTIMADO',
    'INTIMADA', 'ADVOGADO', 'ADVOGADA', 'ADVS', 'ADV', 'IMPETRANTE',
    'IMPETRADO', 'PACIENTE', 'DENUNCIADO', 'VITIMA', 'PROCURADOR',
    'PROCURADORA', 'DEFENSOR', 'DEFENSORA', 'PROMOTOR', 'PROMOTORA',
})

#: Ligam sobrenomes ("Souza DE Oliveira"). Sozinhos não iniciam nem terminam
#: nome, e ficam FORA da chave de identidade.
_CONECTIVOS = frozenset({'DE', 'DA', 'DO', 'DAS', 'DOS', 'E', 'DEL', 'D'})

#: Peça de nome: **começa em maiúscula** e não tem dígito, parêntese nem
#: pontuação interna. A exigência de maiúscula não é estilo — sem ela o
#: caminhamento para trás atravessa a frase e devolve prosa: medido em
#: 03/09/2026 no TJSP, `'sob a presidência desta'` saiu como nome de
#: magistrado. Nome de magistrado publicado nunca vem em caixa baixa; o que
#: vem em caixa baixa é texto corrido, e texto corrido tem que PARAR a leitura.
_RE_TOKEN_NOME = re.compile(r"^[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’-]*$")

#: Nome brasileiro publicado não passa disso. Teto é alerta, não corte mudo:
#: bater nele conta em `erros['teto_tokens']`.
MAX_TOKENS_NOME = 8
MIN_TOKENS_NOME = 2


# --------------------------------------------------------------------------- #
# Resultado
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Atribuicao:
    """Uma atribuição de autoria lida do texto — com a prova de onde saiu."""

    nome: str
    #: Nome do FORMATO que a produziu (chave de `MARCADORES`).
    formato: str
    #: Tratamento que a fonte imprimiu ('Des.', 'Dr.', 'Ministro') — fora do
    #: nome, nunca descartado.
    tratamento: str = ''
    #: Cargo/função que a fonte imprimiu ao lado ('Juiz de Direito',
    #: 'Relator', 'Pres. Seção de D. Privado'). Vazio = a fonte não publicou.
    cargo: str = ''
    #: Intervalo no TEXTO LIMPO de onde o nome saiu — o gabarito mecânico.
    inicio: int = -1
    fim: int = -1

    def verbatim_ok(self, texto_limpo: str) -> bool:
        """O nome é EXATAMENTE a fatia de onde disse ter vindo?

        Caminho independente da regex: fatia e compara. Se alguém um dia
        normalizar caixa, acento ou espaço dentro do extrator, isto reprova.
        """
        if self.inicio < 0 or self.fim > len(texto_limpo):
            return False
        return texto_limpo[self.inicio:self.fim] == self.nome

    @property
    def chave_nome(self) -> str:
        return normalizar_nome_magistrado(self.nome)


@dataclass
class Leitura:
    """O que a leitura de UM texto produziu — e tudo que ela recusou."""

    atribuicoes: list[Atribuicao] = field(default_factory=list)
    #: motivo → quantas vezes. Abstenção é DADO, não silêncio.
    erros: dict[str, int] = field(default_factory=dict)
    #: quantas vezes cada marcador declarado apareceu no texto (perna A do
    #: inventário: o que a fonte IMPRIMIU, contado fora do extrator).
    marcadores_vistos: dict[str, int] = field(default_factory=dict)

    def _conta(self, motivo: str) -> None:
        self.erros[motivo] = self.erros.get(motivo, 0) + 1

    @property
    def abstencoes(self) -> int:
        return sum(self.erros.values())


def normalizar_nome_magistrado(nome: str | None) -> str:
    """Chave de identidade do nome. Algorítmica, sem mapa manual.

    Maiúscula sem acento, tratamento fora, conectivo fora, espaço colapsado.
    `'Des. José da Silva Neto'`, `'JOSE DA SILVA NETO'` e `'José Da Silva
    Neto'` colapsam em `JOSE SILVA NETO`.

    ⚠️ Não use isto como identidade sozinha — ela é o TERCEIRO componente da
    chave `(tribunal, órgão, nome)`. Só o nome funde quatro pessoas: medido, 56
    de 195 publicações com "Rafaela Caldeira Gonçalves" são de outros estados.
    """
    bruto = _sem_acento(nome or '').upper()
    tokens = [t for t in re.split(r'[^A-Z0-9]+', bruto) if t]
    uteis = [t for t in tokens if t not in _CONECTIVOS and t not in _TRATAMENTOS]
    return ' '.join(uteis or tokens)


def normalizar_orgao(orgao: str | None) -> str:
    """Chave do órgão. Maiúscula sem acento, pontuação vira espaço.

    Não tenta unificar grafias entre tribunais (`SEARCH_SCHEMA.md` mediu 29.488
    grafias distintas em `orgao_julgador`, incompatíveis entre si). Unificar
    seria chutar; aqui a chave é a grafia que a fonte publicou, normalizada só
    no que é seguramente ruído de digitação.
    """
    bruto = _sem_acento(orgao or '').upper()
    return ' '.join(t for t in re.split(r'[^A-Z0-9]+', bruto) if t)


# --------------------------------------------------------------------------- #
# Guardas estruturais
# --------------------------------------------------------------------------- #
def _profundidade_parenteses(texto: str) -> list[int]:
    """Profundidade de parênteses em CADA posição do texto.

    É o que separa a assinatura da CITAÇÃO. Medido: as citações de precedente
    vêm sempre entre parênteses (`(STJ - AgRg …, Relator: Ministro NEFI
    CORDEIRO, …)`), e a assinatura do ato, nunca. `Relator(a):` — que tem
    parêntese no próprio rótulo — continua em profundidade 0 porque o par
    fecha antes do nome.
    """
    profundidade, atual = [], 0
    for c in texto:
        if c == '(':
            atual += 1
            profundidade.append(atual)
        elif c == ')':
            profundidade.append(atual)
            atual = max(0, atual - 1)
        else:
            profundidade.append(atual)
    return profundidade


#: Janela do caminhamento. Nome publicado não tem 300 caracteres; ler o texto
#: inteiro para trás a cada marcador seria O(n·m) num corpo de 56 KB.
JANELA_NOME = 300

#: Espaço duplo (ou mais) é separador de CAMPO na fonte, não de palavra dentro
#: do nome. Depois do primeiro token do nome, um run desses ENCERRA a leitura.
GAP_SEPARADOR = 2

_RE_TOKEN = re.compile(r'\S+')


def _tokens(trecho: str) -> list[tuple[str, int, int]]:
    return [(m.group(), m.start(), m.end()) for m in _RE_TOKEN.finditer(trecho)]


def _aceita(tok: str) -> tuple[str, str]:
    """`(token_limpo, motivo_de_parar)`. Motivo vazio = o token é peça de nome."""
    limpo = tok.strip(',;')
    if not limpo:
        return '', 'vazio'
    chave = _sem_acento(limpo).upper()
    # conectivo é a ÚNICA peça de nome que a fonte publica em caixa baixa
    # ('Airton Vargas da Silva'). Sem esta exceção a exigência de maiúscula
    # parte o nome no `da` e devolve 'Silva' sozinho — que reprova em
    # `nome_curto` e vira abstenção falsa.
    if chave in _CONECTIVOS:
        return limpo, ''
    if not _RE_TOKEN_NOME.match(limpo):
        return '', 'forma'          # minúscula, dígito, parêntese, ponto, barra
    if chave in _TRATAMENTOS or chave in _RUIDO:
        return '', 'rotulo'
    return limpo, ''


def _nome_para_tras(texto: str, fim: int) -> tuple[str, int, int, str]:
    """Caminha do cargo para TRÁS juntando peça de nome.

    Devolve `(nome, inicio, fim, motivo_da_recusa)`. Anda de trás pra frente
    porque é assim que a assinatura é impressa — o cargo é a âncora e o nome
    vem antes. Mesmo espírito de `search/entidades_texto.advogados`, que usa a
    OAB como âncora pelo mesmo motivo: sem âncora, qualquer nome em caixa alta
    da publicação viraria magistrado.

    O nome devolvido é a FATIA entre o primeiro e o último token aceitos —
    nunca `texto[i:]`, senão a vírgula de `'… Maluf, Relator'` entra no nome e
    o gabarito verbatim aprova um nome com pontuação colada.
    """
    base = max(0, fim - JANELA_NOME)
    toks = _tokens(texto[base:fim])
    pecas: list[tuple[int, int]] = []
    anterior_ini = None
    for tok, ini_t, fim_t in reversed(toks):
        if pecas is not None and anterior_ini is not None \
                and anterior_ini - fim_t >= GAP_SEPARADOR and pecas:
            break                     # separador de campo: o nome acabou aqui
        limpo, motivo = _aceita(tok)
        if motivo:
            break
        desloc = tok.find(limpo)
        pecas.append((ini_t + desloc, ini_t + desloc + len(limpo)))
        anterior_ini = ini_t
        if len(pecas) > MAX_TOKENS_NOME:
            return '', -1, -1, 'teto_tokens'
    while pecas and _sem_acento(texto[base + pecas[0][0]:base + pecas[0][1]]).upper() in _CONECTIVOS:
        pecas.pop(0)
    while pecas and _sem_acento(texto[base + pecas[-1][0]:base + pecas[-1][1]]).upper() in _CONECTIVOS:
        pecas.pop()
    if not pecas:
        return '', -1, -1, 'sem_nome'
    if len(pecas) < MIN_TOKENS_NOME:
        return '', -1, -1, 'nome_curto'
    ini = base + pecas[-1][0]
    fim_nome = base + pecas[0][1]
    return texto[ini:fim_nome], ini, fim_nome, ''


def _nome_para_frente(texto: str, inicio: int) -> tuple[str, int, int, str, str]:
    """Lê o nome DEPOIS do rótulo. Devolve `(nome, ini, fim, tratamento, erro)`.

    O tratamento impresso pela fonte (`'Relator: DES. FRANCISCO …'`) sai do
    nome e vai para o campo próprio — some do nome exibido, nunca do registro.
    """
    trecho = texto[inicio:inicio + JANELA_NOME]
    toks = _tokens(trecho)
    tratamentos: list[str] = []
    pecas: list[tuple[int, int]] = []
    anterior_fim = None
    for tok, ini_t, fim_t in toks:
        if pecas and anterior_fim is not None and ini_t - anterior_fim >= GAP_SEPARADOR:
            break                     # separador de campo ('… ALENCAR  ROT 000…')
        seco = _sem_acento(tok.rstrip('.')).upper()
        if not pecas and seco in _TRATAMENTOS:
            tratamentos.append(tok)
            anterior_fim = fim_t
            continue
        limpo, motivo = _aceita(tok)
        if motivo:
            break
        desloc = tok.find(limpo)
        pecas.append((ini_t + desloc, ini_t + desloc + len(limpo)))
        anterior_fim = fim_t
        if len(pecas) > MAX_TOKENS_NOME:
            return '', -1, -1, ' '.join(tratamentos), 'teto_tokens'
        if tok.endswith((',', ';', '.')):
            break                     # a fonte fechou o campo
    while pecas and _sem_acento(trecho[pecas[-1][0]:pecas[-1][1]]).upper() in _CONECTIVOS:
        pecas.pop()
    if not pecas:
        return '', -1, -1, ' '.join(tratamentos), 'sem_nome'
    if len(pecas) < MIN_TOKENS_NOME:
        return '', -1, -1, ' '.join(tratamentos), 'nome_curto'
    ini = inicio + pecas[0][0]
    fim = inicio + pecas[-1][1]
    return texto[ini:fim], ini, fim, ' '.join(tratamentos), ''


# --------------------------------------------------------------------------- #
# MARCADORES — o inventário do que a fonte IMPRIME
# --------------------------------------------------------------------------- #
#: 1) e-SAJ de 2º grau (TJSP). `"… - Magistrado(a) <Nome> - Advs: …"`. É a
#:    jazida medida: 5.631.275 publicações do TJSP em 03/09/2026.
#:    O grupo `nome` é lazy e não atravessa hífen — o hífen é o separador de
#:    campo do cabeçalho do e-SAJ.
FORMATO_ESAJ = 'esaj_magistrado'
_RE_ESAJ = re.compile(
    r'-\s*Magistrado\(a\)\s*'
    r'(?P<nome>[A-ZÀ-ÖØ-Þ][^-\n(]{2,90}?)'
    r'\s*(?:\((?P<cargo>[^)\n]{1,60})\))?\s*-')

#: 2) Assinatura de ato com cargo colado ao nome. O formato de 1º grau — o que
#:    o caso concreto (juíza de violência doméstica do TJSP) usa:
#:    `"RAFAELA CALDEIRA GONÇALVES Juíza de Direito. - ADV: …"`.
#:    O nome vem ANTES; a âncora é o cargo.
FORMATO_ASSINATURA = 'assinatura_cargo'
_RE_CARGO = re.compile(
    r'\b(?P<cargo>'
    r'Ju[ií]z(?:a|\(a\)|\(A\))?\s+(?:de\s+Direito|Federal|do\s+Trabalho|'
    r'Substitut[oa]|de\s+Direito\s+Substitut[oa])'
    r'|Magistrad[oa](?:\(a\))?'
    r'|Desembargador(?:a)?(?:\s+Relator(?:a)?)?'
    r'|Relator(?:a)?'
    r')\b', re.IGNORECASE)

#: 3) Rótulo explícito do cabeçalho: `"Relator: FULANO"`, `"RELATOR(A): X"`,
#:    `"Relatora: MARIA ROSELI MENDES ALENCAR"`. O nome vem DEPOIS.
FORMATO_ROTULO = 'rotulo_relator'
_RE_ROTULO = re.compile(
    r'\b(?P<rotulo>Relator(?:a|\(a\))?|RELATOR(?:A|\(A\))?)\s*\.?\s*:\s*')

#: Inventário declarado — mesmo papel de `MARCADORES_DE_REGISTRO` nos diários:
#: a fonte declara o que imprime, e o que não está declarado **abstém em vez de
#: parecer ausente**. Cada entrada é (formato, regex do MARCADOR literal), e o
#: contador de ocorrências roda FORA do extrator, sobre o texto — é a perna A.
MARCADORES = {
    FORMATO_ESAJ: re.compile(r'-\s*Magistrado\(a\)'),
    FORMATO_ASSINATURA: _RE_CARGO,
    FORMATO_ROTULO: _RE_ROTULO,
}


def _dedup(atribs: list[Atribuicao]) -> list[Atribuicao]:
    """Uma pessoa por publicação, mesmo aparecendo em dois formatos.

    O cabeçalho do e-SAJ de 2º grau imprime o MESMO nome duas vezes —
    `'… EURÍPEDES FAIM Relator - Magistrado(a) Eurípedes Faim - Advs: …'`.
    Deduplicar por `(chave, formato)` contaria dois; a unidade é a PESSOA.
    """
    vistos, saida = set(), []
    for a in atribs:
        if a.chave_nome and a.chave_nome not in vistos:
            vistos.add(a.chave_nome)
            saida.append(a)
    return saida


def _ler_esaj(limpo: str, leitura: Leitura,
              consumidos: list[tuple[int, int]]) -> list[Atribuicao]:
    """Formato 1 — cabeçalho do e-SAJ de 2º grau: `- Magistrado(a) <Nome> -`."""
    achados = []
    for m in _RE_ESAJ.finditer(limpo):
        consumidos.append((m.start(), m.end()))
        bruto = m.group('nome')
        nome = bruto.strip()
        if not nome:
            leitura._conta('esaj_sem_nome')
            continue
        toks = nome.split()
        if len(toks) < MIN_TOKENS_NOME:
            leitura._conta('esaj_nome_curto')
            continue
        if len(toks) > MAX_TOKENS_NOME:
            leitura._conta('esaj_teto_tokens')
            continue
        if any(_aceita(t)[1] for t in toks):
            leitura._conta('esaj_token_invalido')
            continue
        ini = m.start('nome') + bruto.find(nome)
        achados.append(Atribuicao(
            nome=nome, formato=FORMATO_ESAJ, cargo=(m.group('cargo') or '').strip(),
            inicio=ini, fim=ini + len(nome)))
    return achados


def _ler_assinatura(limpo: str, prof: list[int], leitura: Leitura,
                    dentro) -> list[Atribuicao]:
    """Formato 2 — assinatura do ato: `<NOME> Juiz(a) de Direito`."""
    achados = []
    for m in _RE_CARGO.finditer(limpo):
        if dentro(m.start()):
            continue                  # já lido pelo formato do e-SAJ
        if prof[m.start()] > 0:
            leitura._conta('citacao')
            continue
        nome, ini, fim, erro = _nome_para_tras(limpo, m.start())
        if erro:
            leitura._conta(f'assinatura_{erro}')
            continue
        achados.append(Atribuicao(nome=nome, formato=FORMATO_ASSINATURA,
                                  cargo=m.group('cargo'), inicio=ini, fim=fim))
    return achados


def _ler_rotulo(limpo: str, prof: list[int], leitura: Leitura,
                dentro) -> list[Atribuicao]:
    """Formato 3 — rótulo do cabeçalho: `Relator: <NOME>`."""
    achados = []
    for m in _RE_ROTULO.finditer(limpo):
        if dentro(m.start()):
            continue
        if prof[m.start()] > 0:
            leitura._conta('citacao')
            continue
        nome, ini, fim, tratamento, erro = _nome_para_frente(limpo, m.end())
        if erro:
            leitura._conta(f'rotulo_{erro}')
            continue
        achados.append(Atribuicao(nome=nome, formato=FORMATO_ROTULO,
                                  tratamento=tratamento, cargo=m.group('rotulo'),
                                  inicio=ini, fim=fim))
    return achados


def ler(texto: str | None) -> Leitura:
    """Lê UM texto de publicação e devolve as atribuições + o que recusou.

    Nunca levanta: texto ruim vira `Leitura` vazia com o motivo contado.
    """
    leitura = Leitura()
    limpo = limpar(texto)
    if not limpo.strip():
        return leitura

    # perna A do inventário: conta o marcador IMPRESSO, sobre o texto, antes
    # e independentemente de qualquer tentativa de extrair nome.
    for formato, regex in MARCADORES.items():
        n = sum(1 for _ in regex.finditer(limpo))
        if n:
            leitura.marcadores_vistos[formato] = n

    prof = _profundidade_parenteses(limpo)
    #: trechos já lidos pelo formato do e-SAJ. Sem isto, o `Magistrado(a)` do
    #: cabeçalho casa TAMBÉM como cargo da assinatura, o caminhamento para trás
    #: bate no hífen e cada publicação do TJSP fabricava uma abstenção falsa —
    #: inflando justamente o número que diz se o extrator presta.
    consumidos: list[tuple[int, int]] = []

    def dentro(pos: int) -> bool:
        return any(a <= pos < b for a, b in consumidos)

    achados = _ler_esaj(limpo, leitura, consumidos)
    achados += _ler_assinatura(limpo, prof, leitura, dentro)
    achados += _ler_rotulo(limpo, prof, leitura, dentro)

    # gabarito mecânico: cada nome tem de ser a fatia de onde disse ter vindo
    aprovadas = []
    for a in achados:
        if a.verbatim_ok(limpo):
            aprovadas.append(a)
        else:
            leitura._conta('verbatim')
    leitura.atribuicoes = _dedup(aprovadas)
    return leitura


def extrair(texto: str | None) -> list[str]:
    """Só os nomes — atalho para quem não precisa da contabilidade."""
    return [a.nome for a in ler(texto).atribuicoes]
