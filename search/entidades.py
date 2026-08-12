"""Índice canônico de ENTIDADES — a base do autocomplete que substitui a busca
por texto livre no mapa comercial e na listagem.

PROBLEMA QUE ESTE MÓDULO RESOLVE
================================
Hoje "quem deve" é digitado à mão. Digitar "INSS" não acha
"Instituto Nacional do Seguro Social"; digitar o nome completo não acha as
grafias que o tribunal inventou. Medido no dado real de prod (12/08/2026):

    tribunals_parte ........... 16.684.051 linhas (81,6% com documento)
    tribunals_processoparte ... 78.866.432 linhas
    raiz de CNPJ 29.979.036 (INSS) ... 610 linhas de `Parte`,
                                       610 CNPJs distintos,
                                       11 grafias distintas de nome

Ou seja: o INSS está fatiado em 610 "partes" no Postgres. Nenhuma delas é o
INSS — todas são. O índice `voyager-entidades` reconstrói a entidade única e
guarda TODAS as grafias, pra que o autocomplete devolva **uma linha** ("INSS")
e a busca resultante cubra as 11 grafias.

DECISÕES (o porquê de cada uma)
===============================

1. CHAVE CANÔNICA = RAIZ DO CNPJ (8 primeiros dígitos)
   -------------------------------------------------
   A raiz identifica a PESSOA JURÍDICA; os 4 dígitos seguintes são a ORDEM da
   filial e os 2 finais são DV. Agrupar pelo CNPJ completo trataria cada
   gerência executiva do INSS como um devedor diferente:

       29.979.036/0001-40 → 23.230 processos     29.979.036/0012-01 → 2.067
       29.979.036/0002-21 →    925               29.979.036/0988-76 →   158

   Agrupar por NOME também não serve: 11 grafias no mesmo órgão (e "CEAB-DJ
   INSS" não parece com "Instituto Nacional do Seguro Social"). A raiz une os
   dois: matriz e filiais viram UMA entidade, e o nome vira `variantes[]`.

2. CNPJ MASCARADO (`29.9**.***/****-**`) → **NÃO FUNDE**
   ----------------------------------------------------
   O tribunal mascara documento por LGPD. A máscara deixa só um PREFIXO
   parcial da raiz (2-3 dígitos). Fundir por prefixo parcial juntaria entidades
   diferentes — `29.9**` casa com 29.900.000 CNPJs possíveis; seria um erro
   silencioso e irreversível no índice que alimenta "quem deve".
   Critério explícito: **documento mascarado é tratado como AUSENTE** — a linha
   cai no agrupamento por nome normalizado (caminho 3) e o documento mascarado
   NÃO entra em `documentos[]`. Quantos foram descartados assim vai em
   `Agregador.stats['documentos_mascarados']` e no campo por-entidade
   `documentos_mascarados` (auditoria).
   (Regra da casa: precisão > velocidade — abster > chutar.)

3. SEM DOCUMENTO (18,4% das linhas) → CHAVE POR NOME NORMALIZADO
   -------------------------------------------------------------
   Medido: ente público quase nunca tem CNPJ no dado do tribunal — numa janela
   de 200k ids, dos nomes que casam a regex de ente público, 704 eram
   `tipo=desconhecido` **sem documento** contra 270 `pj` com documento. Jogar
   fora quem não tem CNPJ seria jogar fora justamente o município/estado/
   fazenda — o universo de "quem deve".
   Por isso todo doc carrega `chave: 'cnpj' | 'nome'`: quem consome precisa
   saber que confiança tem. `chave='cnpj'` é identidade PROVADA por documento;
   `chave='nome'` é heurística de grafia (dois municípios homônimos em UFs
   diferentes colapsam — limite conhecido, registrado em SEARCH_SCHEMA.md).

4. ESCOPO = PJ + ENTES PÚBLICOS. ADVOGADO FORA. PF FORA (MVP).
   -----------------------------------------------------------
   O universo do produto é "quem deve". Advogado REPRESENTA o devedor, não é o
   devedor — sem esse corte, em SP uma advogada apareceu no top-10 de devedores.
   Corte por `tipo='advogado'` OU `oab` preenchida (os dois, porque nenhum dos
   dois sozinho cobre 100%). Pessoa física fica fora do MVP (volume enorme,
   valor comercial baixo, e risco de LGPD num autocomplete).

5. NOME CANÔNICO = A GRAFIA MAIS FREQUENTE
   ----------------------------------------
   Não a mais longa (seria "AUTORIDADE COATORA EM MANDADO DE SEGURANÇA -
   GERENTE EXECUTIVO(A) DO INSS EM CARUARU/PE", que aparece 1× ), nem a
   primeira encontrada (depende da ordem de leitura). Frequência = como o dado
   real chama a entidade. Desempate determinístico: caixa mista > CAIXA ALTA,
   depois mais longa, depois alfabética.

6. `n_processos` VEM DO ES, NUNCA DO POSTGRES
   -------------------------------------------
   `Parte.total_processos` está preenchido em só 39,3% das linhas — precomputar
   a partir dele mostraria "0 processos" em 6 de cada 10 entidades. Por isso o
   BUILD (`grupo_to_doc`) não escreve `n_processos`: a contagem é uma segunda
   passada **ES→ES** (`contar_processos_entidades`), que roda o OR de
   `query_contagem` contra `voyager-processos` e grava o resultado por `update`
   parcial. Zero Postgres.

   Por que a segunda passada existe: `n_partes` é proxy FRACO de prevalência.
   Medido no índice real (12/08), buscar "inss" devolvia

       1º  Gerente Executivo do INSS de São Paulo/Centro    109 partes
       2º  INSTITUTO NACIONAL DO SEGURO SOCIAL              764 partes

   — e o 2º é quem aparece em **4.402.239** processos contra 6.665 do 1º.
   `n_partes` conta linhas de CADASTRO, não litígio: uma gerência que o
   tribunal redigitou 109 vezes empata com uma autarquia federal.

   ESCOPO PARCIAL, POR DESENHO. Contar as 1,14M entidades seria varrer um índice
   de 77M docs 1,14M vezes pra decidir a ordem de uma lista de 10 linhas. Só
   entra quem DISPUTA o autocomplete (`escopo_contagem`, default
   `n_partes >= 2` OU ente público = 182.196 entidades, 16% do índice). Medido
   em 55 termos de busca reais: esse corte cobre 94,5% das entidades que
   aparecem no top-10 e 98,0% das do top-3.

   AUSENTE ≠ ZERO. Quem ficou fora do escopo **não tem o campo** — nunca 0.
   "Não contamos" e "contamos e deu zero" são fatos diferentes, e o ranking
   trata os dois de forma diferente (ver `query_autocomplete`): ausente cai no
   `n_partes`; zero é medição e ranqueia abaixo de desconhecido.

   O NÚMERO ENVELHECE: `voyager-processos` cresce todo dia, então vai junto
   `n_processos_em`. E um rebuild do índice de entidades APAGA a contagem (o
   `_bulk` do build usa `index`, que substitui o doc inteiro) — depois de
   reconstruir, rode `contar_processos_entidades` de novo.

7. FALSO-MERGE É PIOR QUE FALSO-SPLIT
   -----------------------------------
   Num cadastro de devedores, juntar duas entidades erradas é irreversível pra
   quem consome (a busca devolve processos de outra empresa); deixar duas
   entradas da mesma entidade é só uma linha a mais no autocomplete. O primeiro
   build completo (12/08) provou isso com números: a normalização herdada
   colapsava **144 empresas** distintas na chave "INDUSTRIA COMERCIO", 88 em
   "EMPREENDIMENTOS IMOBILIARIOS" e 54 em "TRANSPORTES", porque (a) descartava
   o PRIMEIRO segmento curto como sigla — mas ali mora a MARCA ("HENRIMAR -
   INDUSTRIA E COMERCIO LTDA") — e (b) tratava inicial de uma letra como
   conectivo ("A&E TRANSPORTES" e "A. O. TRANSPORTES" viravam "TRANSPORTES").
   Corrigidos os dois em `normalizar_nome`. Sigla só é descartada DEPOIS do
   primeiro segmento; token de 1 letra nunca é stopword.

8. PLACEHOLDER NÃO É ENTIDADE
   ---------------------------
   "INFORMAÇÃO PROTEGIDA" (segredo de justiça) somava **4.212 linhas** e era a
   MAIOR "entidade" do índice — apareceria no topo do autocomplete de quem
   deve. É NULL disfarçado de string. `NOMES_PLACEHOLDER` corta esses marcadores
   (não é lista de nomes de entidade — é lista de nulos).

9. CONSOLIDAÇÃO NOME → CNPJ, COM DUPLA PROVA
   ------------------------------------------
   A mesma entidade não pode sair 2× na lista só porque metade das linhas do
   tribunal veio sem documento (a Defensoria Pública da União saía). Funde-se um
   grupo-por-nome num grupo-por-CNPJ quando o nome bate com o nome CANÔNICO do
   grupo-por-CNPJ **e** aponta pra um CNPJ inequívoco (único, ou dominante por
   `DOMINANCIA_MIN`/`DOMINANCIA_FATOR`). Homônimo de verdade fica separado.

O QUE É O PRODUTO
=================
`variantes[]` (ordenado por frequência desc). É ele que vira a query: um OR de
`match_phrase` contra o campo TEXTO `partes` do `voyager-processos`, que existe
em 100% dos 71M docs — enquanto o reindex do nested `participacoes` (1,9% em
12/08) não termina. Ver `query_variantes()`. Medido: o OR das 104 grafias do
INSS devolve 4.418.229 processos contra 4.402.239 da grafia principal sozinha —
**+15.990 processos que a busca por texto livre perdia**.

RECALL ≠ PRECISÃO — dois campos
-------------------------------
`variantes` guarda TUDO (é o OR, quer recall). `variantes_busca` guarda só as
grafias com peso e é o que o autocomplete procura (quer precisão). Sem essa
separação o INSS — que tem UMA linha grafada "Instituto Nacional do Seguro
Social (UNIÃO)" entre 764 — vinha em 1º na busca por "uniao".

Relação com `search/agg_estado.py`: aquele módulo funde grafias DENTRO de uma
agregação do ES (efêmero, top-N). Este constrói o cadastro persistente. A ideia
de normalização é a mesma; a implementação aqui é mais estrita (tira sufixo de
papel do PJe e forma societária), então as chaves NÃO são intercambiáveis.
O gate de ente público é COMPARTILHADO (`agg_estado.eh_ente_publico`) de
propósito — uma fonte só pra "isso é ente público?".
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone

# fonte ÚNICA do gate de ente público: RE_ENTE_PUBLICO (tribunals/estagio.py) +
# o complemento que corrige o falso-negativo institucional (FUNASA/IBAMA/
# "ESTADO DE MINAS GERAIS"...). Importado, não copiado.
from search.agg_estado import eh_ente_publico

#: sufixo do índice (o nome completo sai de `search.client.index_name`)
INDICE = 'entidades'

# --------------------------------------------------------------------------- #
# Documento (CPF/CNPJ)
# --------------------------------------------------------------------------- #
_RE_NAO_DIGITO = re.compile(r'\D+')
#: o tribunal mascara com X/x/* (ex.: '639.XXX.XXX-XX', '29.9**.***/****-**')
_RE_MASCARA = re.compile(r'[Xx*]')

TAM_CNPJ = 14
TAM_CPF = 11
TAM_RAIZ = 8


def so_digitos(valor: str | None) -> str:
    return _RE_NAO_DIGITO.sub('', valor or '')


def eh_mascarado(documento: str | None) -> bool:
    """`29.9**.***/****-**` / `639.XXX.XXX-XX` — LGPD do tribunal."""
    return bool(_RE_MASCARA.search(documento or ''))


def raiz_cnpj(documento: str | None) -> str | None:
    """8 primeiros dígitos do CNPJ — a identidade da PJ (matriz ∪ filiais).

    `None` quando não há CNPJ utilizável: vazio, mascarado (ver decisão 2), ou
    com quantidade de dígitos que não é de CNPJ (CPF, lixo).
    """
    doc = documento or ''
    if not doc or eh_mascarado(doc):
        return None
    digitos = so_digitos(doc)
    if len(digitos) != TAM_CNPJ:
        return None
    if digitos == '0' * TAM_CNPJ:          # CNPJ zerado = ausência disfarçada
        return None
    return digitos[:TAM_RAIZ]


def eh_cpf(documento: str | None, tipo_documento: str | None = '') -> bool:
    """CPF (real ou declarado) — prova de pessoa física, mesmo se `tipo` mentir."""
    if (tipo_documento or '').strip().upper() == 'CPF':
        return True
    doc = documento or ''
    if not doc or eh_mascarado(doc):
        return False
    return len(so_digitos(doc)) == TAM_CPF


# --------------------------------------------------------------------------- #
# Normalização de nome (chave de fusão quando não há CNPJ)
# --------------------------------------------------------------------------- #
#: parênteses SEM aninhamento, aplicado em loop — o PJe produz "(REQUERIDO(A))"
_RE_PARENTESES = re.compile(r'\([^()]*\)')
_RE_NAO_ALNUM = re.compile(r'[^A-Z0-9]+')
_RE_SEPARADOR = re.compile(r'[-–—/|:;,]')

#: conectivos que não distinguem entidade ("Estado DE São Paulo" == "Estado São
#: Paulo"). Token de 1 letra NÃO entra aqui de propósito: em nome de empresa ele
#: é INICIAL, não conectivo — com 'A'/'O'/'E' na lista, "A&E TRANSPORTES LTDA"
#: e "A. O. TRANSPORTES LTDA" viravam os dois a chave "TRANSPORTES" (medido no
#: build completo de 12/08: 54 empresas distintas fundidas nessa chave).
_STOPWORDS = frozenset({'DA', 'DE', 'DO', 'DAS', 'DOS', 'EM',
                        'NO', 'NA', 'NOS', 'NAS', 'AS', 'OS'})

#: token de até N chars sozinho num segmento é SIGLA e some quando sobra corpo
#: ("Instituto Nacional do Seguro Social - INSS" ≡ "... Seguro Social")
_SIGLA_MAX_CHARS = 8

#: nomes que NÃO são entidade: são marcador de ausência de dado. Medido no build
#: completo (12/08): "INFORMAÇÃO PROTEGIDA" (segredo de justiça) somava 4.212
#: linhas de `Parte` e virava a MAIOR "entidade" do índice — apareceria no topo
#: do autocomplete de "quem deve". Não é lista de nomes de entidade (isso o
#: módulo não tem): é lista de NULOS disfarçados de string.
NOMES_PLACEHOLDER = frozenset({
    'INFORMACAO PROTEGIDA', 'INFORMACAO SIGILOSA', 'SEGREDO JUSTICA',
    'NOME PROTEGIDO', 'PARTE SIGILOSA', 'SIGILOSO', 'SIGILOSA',
    'NAO INFORMADO', 'NAO INFORMADA', 'NAO IDENTIFICADO', 'NAO IDENTIFICADA',
    'DESCONHECIDO', 'DESCONHECIDA', 'SEM NOME', 'A APURAR', 'NOME', 'NULL',
})

#: papel processual que o PJe cola no NOME da parte. Fora dos parênteses ele
#: viraria parte da chave e quebraria a fusão ("MUNICIPIO DE X - EXECUTADO"
#: ≠ "MUNICIPIO DE X"). Só removido quando é o segmento/token FINAL.
_PAPEIS_PJE = frozenset({
    'REQUERIDO', 'REQUERIDA', 'REQUERENTE', 'IMPETRADO', 'IMPETRADA',
    'IMPETRANTE', 'EXECUTADO', 'EXECUTADA', 'EXEQUENTE', 'AUTOR', 'AUTORA',
    'REU', 'RE', 'RECORRENTE', 'RECORRIDO', 'RECORRIDA', 'APELANTE',
    'APELADO', 'APELADA', 'AGRAVANTE', 'AGRAVADO', 'AGRAVADA',
    'INTERESSADO', 'INTERESSADA', 'ASSISTENTE', 'LITISCONSORTE',
    'TERCEIRO', 'EMBARGANTE', 'EMBARGADO', 'EMBARGADA', 'PERITO',
    'REPRESENTANTE', 'SUCESSOR', 'HERDEIRO', 'INVENTARIANTE',
})

#: forma societária — ruído puro pra identidade. O que importa é não quebrar a
#: chave por causa de "S.A." vs "S/A" vs "SA" (que viram, respectivamente, os
#: tokens finais `S A`, `S A` e `SA`). Removido só no FIM, nunca esvaziando.
_FORMAS_SOCIETARIAS = frozenset({'SA', 'S', 'A', 'LTDA', 'ME', 'EPP',
                                 'EIRELI', 'MEI', 'CIA'})


def _sem_acento(texto: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFKD', texto)
                   if not unicodedata.combining(c))


def _sem_parenteses(texto: str) -> str:
    """Remove trechos entre parênteses, inclusive aninhados ('(REQUERIDO(A))')."""
    for _ in range(4):
        novo = _RE_PARENTESES.sub(' ', texto)
        if novo == texto:
            break
        texto = novo
    return texto


def _eh_sigla(segmento: str) -> bool:
    toks = _RE_NAO_ALNUM.sub(' ', segmento).split()
    return len(toks) == 1 and 2 <= len(toks[0]) <= _SIGLA_MAX_CHARS


def _tira_papel_final(tokens: list[str]) -> list[str]:
    """Descarta papel processual grudado no fim ('… EXECUTADO'), se sobrar nome."""
    while len(tokens) > 1 and tokens[-1] in _PAPEIS_PJE:
        tokens = tokens[:-1]
    return tokens


def _tira_forma_societaria(tokens: list[str]) -> list[str]:
    while len(tokens) > 1 and tokens[-1] in _FORMAS_SOCIETARIAS:
        tokens = tokens[:-1]
    return tokens


def normalizar_nome(nome: str | None) -> str:
    """Chave de fusão por NOME. Algorítmica — nenhum mapa manual de nomes.

    Passos:
      1. maiúscula sem acento (o dado vem em 3 caixas diferentes);
      2. remove conteúdo entre parênteses (o PJe cola "(REQUERIDO(A))");
      3. dos segmentos separados por -/|:;, descarta os que são SIGLA — **menos
         o PRIMEIRO**: sigla vem DEPOIS do nome ("… - INSS", "/DF"), enquanto o
         primeiro segmento curto é a MARCA ("HENRIMAR - INDUSTRIA E COMERCIO
         LTDA"). Medido no build completo de 12/08: sem essa exceção, 144
         empresas distintas colapsavam na chave "INDUSTRIA COMERCIO" e 88 em
         "EMPREENDIMENTOS IMOBILIARIOS" — falso-merge, o pior erro possível
         num cadastro de devedores;
      4. não-alfanumérico vira espaço; remove conectivos (DE/DO/DA/…);
      5. remove papel processual e forma societária no FIM.

    As 4 grafias abaixo colapsam na mesma chave `INSTITUTO NACIONAL SEGURO SOCIAL`:
        'Instituto Nacional do Seguro Social - INSS'
        'INSTITUTO NACIONAL DO SEGURO SOCIAL'
        'INSTITUTO NACIONAL DO SEGURO SOCIAL (REQUERIDO(A))'
        'Instituto Nacional do Seguro Social — REQUERIDO'
    """
    texto = _sem_parenteses(_sem_acento(nome or '').upper())

    segmentos = [s for s in _RE_SEPARADOR.split(texto) if s.strip()]
    if len(segmentos) > 1:
        corpo = [s for i, s in enumerate(segmentos) if i == 0 or not _eh_sigla(s)]
        texto = ' '.join(corpo or segmentos)

    tokens = _RE_NAO_ALNUM.sub(' ', texto).split()
    uteis = [t for t in tokens if t not in _STOPWORDS] or tokens
    uteis = _tira_forma_societaria(_tira_papel_final(uteis))
    return ' '.join(uteis)


def limpar_rotulo(nome: str | None) -> str:
    """Tira do rótulo EXIBIDO o papel processual que o PJe colou no nome.

    `'INSTITUTO … (REQUERIDO(A))'` → `'INSTITUTO …'` (parênteses) e
    `'Município de São Paulo - EXECUTADO'` → `'Município de São Paulo'`
    (sufixo solto). Preserva caixa e acento do original — é rótulo de UI, não
    chave. Uma sigla legítima no fim (`'… - INSS'`) NÃO é papel e fica.
    """
    texto = re.sub(r'\s{2,}', ' ', _sem_parenteses(nome or '')).strip()
    tokens = texto.split()
    while tokens:
        ultimo = _RE_NAO_ALNUM.sub('', _sem_acento(tokens[-1]).upper())
        if not ultimo:                       # separador solto ('-', '/')
            tokens.pop()
            continue
        if len(tokens) > 1 and ultimo in _PAPEIS_PJE:
            tokens.pop()
            continue
        break
    return ' '.join(tokens).strip(' -–—/|:;,')


def nome_canonico(variantes: dict[str, int]) -> str:
    """Rótulo humano da entidade: a grafia MAIS FREQUENTE (decisão 5).

    `variantes`: {grafia: nº de linhas de Parte}. Desempate determinístico —
    caixa mista antes de CAIXA ALTA (mais legível), depois a mais longa
    (costuma ser a completa), depois alfabética.
    """
    if not variantes:
        return ''
    escolhida = sorted(
        variantes.items(),
        key=lambda kv: (-kv[1], kv[0].isupper(), -len(kv[0]), kv[0]),
    )[0][0]
    return limpar_rotulo(escolhida) or escolhida


# --------------------------------------------------------------------------- #
# Escopo — quem entra no cadastro de "quem deve"
# --------------------------------------------------------------------------- #
FORA_ADVOGADO = 'advogado'
FORA_PESSOA_FISICA = 'pessoa_fisica'
FORA_SEM_NOME = 'sem_nome'
FORA_PLACEHOLDER = 'placeholder'
FORA_NAO_PJ_NEM_ENTE = 'nao_pj_nem_ente'

DENTRO_CNPJ = 'cnpj'
DENTRO_TIPO_PJ = 'tipo_pj'
DENTRO_ENTE_PUBLICO = 'ente_publico'

#: motivos de entrada (ordem de precedência = ordem de avaliação em `classificar`)
MOTIVOS_DENTRO = (DENTRO_CNPJ, DENTRO_TIPO_PJ, DENTRO_ENTE_PUBLICO)
MOTIVOS_FORA = (FORA_ADVOGADO, FORA_PESSOA_FISICA, FORA_SEM_NOME,
                FORA_PLACEHOLDER, FORA_NAO_PJ_NEM_ENTE)


def classificar(nome: str | None, documento: str | None, tipo_documento: str | None,
                tipo: str | None, oab: str | None) -> tuple[bool, str]:
    """(entra no índice?, motivo). Motivo é auditável — vai pro relatório.

    Precedência (decisão 4):
      nome placeholder  → FORA ("INFORMAÇÃO PROTEGIDA" é NULL, não entidade)
      advogado          → FORA (representa o devedor, não é o devedor)
      CPF               → FORA (documento PROVA pessoa física, mesmo se `tipo` mentir)
      CNPJ (14 dígitos) → DENTRO
      tipo='pj'         → DENTRO (PJ sem documento — 18,4% da base não tem doc)
      ente público      → DENTRO (regex; pega Município/Estado/Fazenda `desconhecido`)
      resto             → FORA
    """
    if not (nome or '').strip():
        return False, FORA_SEM_NOME
    if (tipo or '') == 'advogado' or (oab or '').strip():
        return False, FORA_ADVOGADO
    if eh_cpf(documento, tipo_documento):
        return False, FORA_PESSOA_FISICA
    # depois dos dois cortes baratos de propósito: eles já eliminam ~78% das
    # linhas e `normalizar_nome` é a parte cara do laço (16,9M chamadas viram 3,7M)
    if normalizar_nome(nome) in NOMES_PLACEHOLDER:
        return False, FORA_PLACEHOLDER
    if raiz_cnpj(documento):
        return True, DENTRO_CNPJ
    if (tipo or '') == 'pj':
        return True, DENTRO_TIPO_PJ
    if eh_ente_publico(nome)[0]:
        return True, DENTRO_ENTE_PUBLICO
    return False, FORA_NAO_PJ_NEM_ENTE


# --------------------------------------------------------------------------- #
# Agregação
# --------------------------------------------------------------------------- #
#: teto de grafias guardadas por entidade — proteção de memória no build de
#: 16,7M linhas. Estourou: `variantes_truncadas=True` no doc (honestidade).
MAX_VARIANTES = 300
#: teto de CNPJs listados por entidade (o INSS tem 610 filiais)
MAX_DOCUMENTOS = 1000

#: `variantes_busca` = as grafias que o AUTOCOMPLETE enxerga. Uma grafia entra
#: se aparece em ≥2 linhas E em ≥1% das linhas da entidade — os 3 primeiros
#: colocados entram sempre (senão entidade de 1 linha ficaria invisível).
#: Medido 12/08: o INSS tem a grafia "Instituto Nacional do Seguro Social
#: (UNIÃO)" em UMA das suas 764 linhas (o tribunal digitou as duas partes no
#: mesmo campo). Com ela indexada, buscar "uniao" devolvia o INSS em 1º lugar —
#: a entidade grande sequestra a busca por causa de um typo. `variantes`
#: continua com TUDO (o OR de recall precisa); só a busca fica limpa.
VARIANTE_BUSCA_MIN_LINHAS = 2
VARIANTE_BUSCA_MIN_SHARE = 0.01
VARIANTE_BUSCA_TOP_SEMPRE = 3

#: consolidação nome→cnpj entre homônimos: só funde se um CNPJ concentrar esta
#: fração das linhas E for este múltiplo do segundo colocado. Calibrado no dado
#: real: INSS = 610 linhas contra 5 CNPJs errados de 1 linha (0,993 e 610×) —
#: passa; 3 "AUTO POSTO SÃO JOSÉ LTDA" com volume parecido — não passa.
DOMINANCIA_MIN = 0.9
DOMINANCIA_FATOR = 10


class Grupo:
    """Uma entidade em construção. `__slots__` porque são ~1M destes em memória."""

    __slots__ = ('chave', 'valor', 'variantes', 'documentos', 'tipos',
                 'n_partes', 'mascarados', 'variantes_descartadas',
                 'documentos_descartados', 'menor_parte_id', 'absorvidos')

    def __init__(self, chave: str, valor: str):
        self.chave = chave                 # 'cnpj' | 'nome'
        self.valor = valor                 # raiz do CNPJ | nome normalizado
        self.variantes: dict[str, int] = {}
        self.documentos: dict[str, int] = {}
        self.tipos: dict[str, int] = {}
        self.n_partes = 0
        self.mascarados = 0
        self.variantes_descartadas = 0
        self.documentos_descartados = 0
        self.menor_parte_id = None
        #: grupos-por-nome absorvidos na consolidação (auditoria)
        self.absorvidos = 0

    def somar(self, parte_id, nome, documento, tipo):
        self.n_partes += 1
        if self.menor_parte_id is None or (parte_id or 0) < self.menor_parte_id:
            self.menor_parte_id = parte_id
        if nome in self.variantes:
            self.variantes[nome] += 1
        elif len(self.variantes) < MAX_VARIANTES:
            self.variantes[nome] = 1
        else:
            self.variantes_descartadas += 1
        self.tipos[tipo] = self.tipos.get(tipo, 0) + 1
        if not documento:
            return
        if eh_mascarado(documento):
            self.mascarados += 1          # NÃO entra em documentos[] (decisão 2)
        elif documento in self.documentos:
            self.documentos[documento] += 1
        elif len(self.documentos) < MAX_DOCUMENTOS:
            self.documentos[documento] = 1
        else:
            self.documentos_descartados += 1

    def absorver(self, outro: 'Grupo'):
        """Engole outro grupo (consolidação nome→cnpj). Respeita os tetos."""
        self.n_partes += outro.n_partes
        self.mascarados += outro.mascarados
        self.variantes_descartadas += outro.variantes_descartadas
        self.documentos_descartados += outro.documentos_descartados
        self.absorvidos += 1 + outro.absorvidos
        if outro.menor_parte_id is not None:
            self.menor_parte_id = min(self.menor_parte_id or outro.menor_parte_id,
                                      outro.menor_parte_id)
        for nome, n in outro.variantes.items():
            if nome in self.variantes:
                self.variantes[nome] += n
            elif len(self.variantes) < MAX_VARIANTES:
                self.variantes[nome] = n
            else:
                self.variantes_descartadas += n
        for doc, n in outro.documentos.items():
            if doc in self.documentos:
                self.documentos[doc] += n
            elif len(self.documentos) < MAX_DOCUMENTOS:
                self.documentos[doc] = n
            else:
                self.documentos_descartados += n
        for tipo, n in outro.tipos.items():
            self.tipos[tipo] = self.tipos.get(tipo, 0) + n


def entidade_id(chave: str, valor: str) -> str:
    """`_id` do ES — determinístico, pra reindex ser idempotente.

    `cnpj:29979036` é legível e auditável a olho. Pro caminho por nome o valor
    pode ter 200 chars e caracteres que não cabem num `_id` — vira hash curto,
    mas o `nome_normalizado` continua no `_source` pra depurar.
    """
    if chave == 'cnpj':
        return f'cnpj:{valor}'
    return 'nome:' + hashlib.sha1(valor.encode('utf-8')).hexdigest()[:20]


class Agregador:
    """Acumula linhas de `Parte` → entidades. Global em memória, por design.

    Não dá pra fechar um grupo antes do fim da leitura: a última linha lida
    pode ser mais uma grafia do INSS. Por isso o build é UMA passada e a
    escrita no ES acontece no fim. Estruturas enxutas (`__slots__`, dicts de
    contagem) e tetos (`MAX_VARIANTES`/`MAX_DOCUMENTOS`) mantêm isso viável.
    """

    def __init__(self):
        self.grupos: dict[tuple[str, str], Grupo] = {}
        self.stats = {
            'lidas': 0, 'dentro': 0, 'fora': 0,
            'documentos_mascarados': 0,   # linhas cujo doc mascarado foi ignorado
            **{f'fora_{m}': 0 for m in MOTIVOS_FORA},
            **{f'dentro_{m}': 0 for m in MOTIVOS_DENTRO},
        }

    def add(self, parte_id, nome, documento, tipo_documento, tipo, oab) -> bool:
        self.stats['lidas'] += 1
        dentro, motivo = classificar(nome, documento, tipo_documento, tipo, oab)
        if not dentro:
            self.stats['fora'] += 1
            self.stats[f'fora_{motivo}'] += 1
            return False
        self.stats['dentro'] += 1
        self.stats[f'dentro_{motivo}'] += 1

        documento = (documento or '').strip()
        if documento and eh_mascarado(documento):
            self.stats['documentos_mascarados'] += 1

        raiz = raiz_cnpj(documento)
        if raiz:
            chave, valor = 'cnpj', raiz
        else:
            valor = normalizar_nome(nome)
            if not valor:
                self.stats['fora'] += 1
                self.stats['dentro'] -= 1
                self.stats[f'dentro_{motivo}'] -= 1
                self.stats[f'fora_{FORA_SEM_NOME}'] += 1
                return False
            chave = 'nome'

        g = self.grupos.get((chave, valor))
        if g is None:
            g = Grupo(chave, valor)
            self.grupos[(chave, valor)] = g
        g.somar(parte_id, (nome or '').strip(), documento, tipo or '')
        return True

    # -- consolidação nome → cnpj -------------------------------------------- #
    def consolidar(self) -> dict:
        """Absorve grupos-por-NOME em grupos-por-CNPJ quando o nome é o MESMO.

        Sem isso a "Defensoria Pública da União" aparece DUAS vezes no
        autocomplete: uma pelas linhas que trouxeram CNPJ e outra pelas que não
        trouxeram (medido na fatia de 200k). É a mesma entidade — só que metade
        das linhas do tribunal veio sem documento.

        A ligação exige duas provas, senão abstém (regra da casa: abster >
        chutar):
          1. o nome normalizado do grupo-por-nome tem de bater com o nome
             normalizado CANÔNICO (a grafia mais frequente) do grupo-por-CNPJ —
             não com uma variante qualquer, senão a grafia genérica "UNIÃO
             FEDERAL" pendurada no CNPJ da AGU engoliria o grupo "União
             Federal" inteiro;
          2. o nome tem de apontar para um CNPJ **inequívoco**: ou é o único,
             ou um DOMINA (≥ `DOMINANCIA_MIN` das linhas e ≥ `DOMINANCIA_FATOR`×
             o segundo colocado). Sem a regra de dominância o INSS não
             consolidava: além da raiz 29979036 (610 linhas), o dado real tem 5
             CNPJs errados digitados por tribunal com o MESMO nome (1 linha cada
             — medido 12/08), e o empate técnico falso fazia abster. Homônimo de
             verdade ("AUTO POSTO SÃO JOSÉ LTDA" em 3 cidades, 3 CNPJs com
             volume parecido) continua separado — não dá pra saber a qual
             pertence.

        Retorna a estatística da passada (vai pro relatório).
        """
        por_nome_canonico: dict[str, list] = {}
        for chave, grupo in self.grupos.items():
            if chave[0] != 'cnpj':
                continue
            norm = normalizar_nome(nome_canonico(grupo.variantes))
            if norm:
                por_nome_canonico.setdefault(norm, []).append(chave)

        fundidos = ambiguos = linhas_fundidas = 0
        for chave in [c for c in self.grupos if c[0] == 'nome']:
            alvos = por_nome_canonico.get(chave[1])
            if not alvos:
                continue
            alvo = self._alvo_dominante(alvos)
            if alvo is None:
                ambiguos += 1                    # homônimo de verdade: abstém
                continue
            grupo = self.grupos.pop(chave)
            self.grupos[alvo].absorver(grupo)
            fundidos += 1
            linhas_fundidas += grupo.n_partes

        self.stats['consolidados_nome_em_cnpj'] = fundidos
        self.stats['consolidacao_linhas'] = linhas_fundidas
        self.stats['consolidacao_ambiguos'] = ambiguos
        return {'fundidos': fundidos, 'linhas': linhas_fundidas,
                'ambiguos': ambiguos}

    def _alvo_dominante(self, alvos: list):
        """O CNPJ inequívoco entre candidatos homônimos — ou `None` (abstém)."""
        if len(alvos) == 1:
            return alvos[0]
        ranking = sorted(alvos, key=lambda c: -self.grupos[c].n_partes)
        lider = self.grupos[ranking[0]].n_partes
        vice = self.grupos[ranking[1]].n_partes
        total = sum(self.grupos[c].n_partes for c in alvos)
        if total and lider / total >= DOMINANCIA_MIN and lider >= DOMINANCIA_FATOR * vice:
            return ranking[0]
        return None

    # -- saída --------------------------------------------------------------- #
    def docs(self, agora: str | None = None):
        """Gera (`_id`, doc) por entidade. Iterador — não materializa a lista."""
        agora = agora or datetime.now(timezone.utc).isoformat()
        for g in self.grupos.values():
            yield entidade_id(g.chave, g.valor), grupo_to_doc(g, agora)

    def resumo(self) -> dict:
        """Números do build (o que vai pro relatório)."""
        por_chave = {'cnpj': 0, 'nome': 0}
        for (chave, _), _g in self.grupos.items():
            por_chave[chave] += 1
        entidades = len(self.grupos)
        dentro = self.stats['dentro']
        return {
            **self.stats,
            'entidades': entidades,
            'entidades_por_chave': por_chave,
            # taxa de fusão = quanto do fatiamento do Postgres foi desfeito
            'taxa_fusao_pct': round(100.0 * (1 - entidades / dentro), 2) if dentro else 0.0,
            'linhas_por_entidade': round(dentro / entidades, 2) if entidades else 0.0,
        }


def variantes_de_busca(variantes: list, n_partes: int) -> list:
    """Subconjunto de `variantes` que vai pro autocomplete (ver constantes).

    `variantes`: lista de (grafia, ocorrências) JÁ ordenada por frequência desc.
    Recall (o OR contra `partes`) usa a lista inteira; precisão da BUSCA usa
    esta — grafia de 1 linha é typo de cartório com a mesma frequência que é
    grafia legítima, e num índice de 1,1M entidades o typo de quem é grande
    ganha de quem é o alvo.
    """
    if not variantes:
        return []
    piso = max(VARIANTE_BUSCA_MIN_LINHAS,
               int(VARIANTE_BUSCA_MIN_SHARE * n_partes) + 1)
    escolhidas = [nome for nome, n in variantes if n >= piso]
    if len(escolhidas) < VARIANTE_BUSCA_TOP_SEMPRE:
        escolhidas = [nome for nome, _ in variantes[:VARIANTE_BUSCA_TOP_SEMPRE]]
    return escolhidas


def grupo_to_doc(g: Grupo, agora: str) -> dict:
    """Grupo → documento do índice `voyager-entidades` (ver ENTIDADE_MAPPING).

    `variantes` sai ORDENADO POR FREQUÊNCIA desc: quem consome pode pegar os
    top-K com a garantia de estar pegando as grafias que mais aparecem
    (`query_variantes` depende disso).
    """
    variantes = sorted(g.variantes.items(), key=lambda kv: (-kv[1], kv[0]))
    documentos = sorted(g.documentos)
    ente, por_complemento = False, True
    for nome, _n in variantes:
        ok, comp = eh_ente_publico(nome)
        if ok:
            ente = True
            por_complemento = por_complemento and comp
    tipo = sorted(g.tipos.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if g.tipos else ''
    return {
        'entidade_id': entidade_id(g.chave, g.valor),
        'chave': g.chave,                       # procedência: 'cnpj' (provada) | 'nome' (heurística)
        'raiz_cnpj': g.valor if g.chave == 'cnpj' else None,
        'nome_canonico': nome_canonico(g.variantes),
        'nome_normalizado': g.valor if g.chave == 'nome' else normalizar_nome(
            nome_canonico(g.variantes)),
        'variantes': [nome for nome, _ in variantes],
        # frequência de cada grafia, MESMA ORDEM de `variantes`. Serve pro
        # consumidor cortar cauda ("só as grafias com ≥ N linhas") antes de
        # montar o OR — grafia de 1 linha pendurada num CNPJ grande é o vetor
        # de over-match conhecido (ver SEARCH_SCHEMA.md).
        'variantes_n': [n for _, n in variantes],
        'n_variantes': len(variantes),
        # o que o AUTOCOMPLETE enxerga (sem as grafias de 1 linha, que são typo
        # de cartório e deixam a entidade grande sequestrar buscas alheias)
        'variantes_busca': variantes_de_busca(variantes, g.n_partes),
        'variantes_truncadas': bool(g.variantes_descartadas),
        'documentos': documentos,
        'n_documentos': len(documentos),
        # linhas cujo CNPJ veio MASCARADO e por isso não fundiu por raiz
        'documentos_mascarados': g.mascarados,
        'tipo': tipo,
        # grupos-por-nome absorvidos na consolidação (0 = nunca foi consolidado)
        'grupos_absorvidos': g.absorvidos,
        'eh_ente_publico': ente,
        'ente_publico_por_complemento': bool(ente and por_complemento),
        # proxy de prevalência — NÃO é contagem de processos (decisão 6)
        'n_partes': g.n_partes,
        'parte_id_min': g.menor_parte_id,
        'atualizado_em': agora,
    }


# --------------------------------------------------------------------------- #
# Queries — o que o consumidor faz com o índice
# --------------------------------------------------------------------------- #
#: subcampos do `search_as_you_type` (o ES cria _2gram/_3gram/_index_prefix)
def _campos_autocomplete(campo: str) -> list[str]:
    return [campo, f'{campo}._2gram', f'{campo}._3gram']


#: peso da PREVALÊNCIA no ranking. Medido: com `bool_prefix` de 1 termo o score
#: textual satura (todos os 338 casamentos de "inss" pontuaram 2,0), então quem
#: decide a ordem é este fator — e ele PRECISA ser forte, senão "Gerente
#: Executivo do INSS em Manaus" (7 linhas) fica na frente do INSS (610).
FATOR_PREVALENCIA = 4

#: campo de prevalência MEDIDO (contagem real em `voyager-processos`) e o
#: proxy que sobrou pra quem ficou fora do escopo da contagem.
CAMPO_PREVALENCIA = 'n_processos'
CAMPO_PREVALENCIA_FALLBACK = 'n_partes'

#: peso da ATESTAÇÃO — ver `funcoes_prevalencia`. Fator ALTO comprime mais
#: (`log10(2 + fator·n)`), então este número controla o quanto o cadastro pode
#: desempatar contagens parecidas; calibrado em 4 (mesmo `FATOR_PREVALENCIA`)
#: porque a folga medida no caso "inss" é 1,66 contra os 1,33 que o score
#: textual dá de vantagem pro nome curto.
FATOR_ATESTACAO = 4


def _fator(campo: str) -> dict:
    return {'field_value_factor': {'field': campo, 'modifier': 'log2p',
                                   'factor': FATOR_PREVALENCIA, 'missing': 0}}


def funcoes_prevalencia() -> list:
    """As funções de prevalência do `function_score`.

    Duas delas são mutuamente exclusivas (a prevalência propriamente dita):
    contadas usam `n_processos`; NÃO-contadas (campo ausente) caem no `n_partes`,
    que é o comportamento anterior. A terceira é a ATESTAÇÃO, e só existe pra
    quem foi contado (ver mais abaixo).

    Os filtros são `exists`/`must_not exists`, e NÃO `score_mode: max` entre os
    dois campos, de propósito:

      * `max` faria `n_partes` virar PISO de todo mundo, e aí a entidade que a
        contagem provou pequena ("Gerente Executivo do INSS de São Paulo/Centro":
        109 linhas de cadastro, 6.665 processos) manteria o score inflado que a
        gente está justamente tentando corrigir. Quem foi medido é ranqueado
        pela MEDIDA, não pelo proxy;
      * e `max` colapsaria ausente com zero — os dois cairiam no `n_partes`.
        AUSENTE é "não contamos" (fora do escopo, ver `escopo_contagem`); ZERO é
        "contamos e o OR não achou processo nenhum". O primeiro merece o
        benefício da dúvida do proxy; o segundo é fato medido e fica embaixo:
        `log2p(0) = log10(2) ≈ 0,30` contra `log2p(n_partes)` ≥ 0,78.

    Escala: `log2p` = `log10(2 + fator·valor)`. O INSS (4.402.239) pontua 7,25 e
    uma entidade de 3 processos pontua 1,15 — spread de ~6× num score textual
    que varia entre ~2 e ~5, ou seja, a prevalência decide a ordem (é o que se
    quer) mas o `operator: and` continua sendo quem decide QUEM entra na lista.

    ATESTAÇÃO (a 3ª função, só pra quem foi contado)
    ------------------------------------------------
    `n_processos` é propriedade da FRASE, não da entidade: ele mede "em quantos
    processos aparece um nome que contém estas palavras". Numa entidade grande
    isso cria um cardume de FACETAS — pedaços do mesmo órgão, cada um em seu
    documento, todos casando quase os mesmos processos. Medido, buscando "inss"
    logo depois da 1ª contagem:

         1º  INSS                          53 linhas · 4.255.175
         2º  INSTITUTO NACIONAL … SOCIAL INSS  22 linhas · 4.174.336
         3º  CEAB - INSS                    4 linhas · 2.087.983
        11º  INSTITUTO NACIONAL DO SEGURO SOCIAL  764 linhas · 4.402.239  ← o INSS

    O log achata 4,40 MI e 4,25 MI para 3,5% de diferença, e aí quem decide volta
    a ser o texto — que prefere o nome CURTO ("INSS" casa "inss" como nome
    canônico; a autarquia casa só por variante). Nenhuma transformação monótona
    da contagem conserta isso: 3,5% não vence 33%.

    O que separa a autarquia das suas facetas não é a contagem — é o CADASTRO.
    764 linhas de `Parte` contra 53, 22, 4, 1. `n_partes` responde uma pergunta
    que `n_processos` não responde: *quantas vezes, independentemente, o tribunal
    registrou esta entidade com este nome?* Uma faceta de 1 linha que casa um
    milhão de processos está medindo a promiscuidade da frase, não o tamanho da
    entidade.

    Por isso o ranking de quem foi contado é **prevalência × atestação**:
    `log2p(4·n_processos) × log2p(4·n_partes)`. Medido no caso "inss", a
    atestação abre 1,66× entre a autarquia (764) e a maior faceta (53) — folga
    sobre os 1,33× que o texto dá pro nome curto — e devolve o INSS ao 1º lugar
    sem ressuscitar a gerência de 109 linhas/21 processos (que continua em 17,7
    contra 54,3 da autarquia).

    A atestação NÃO se aplica a quem não foi contado: ali `n_partes` já é a única
    evidência e contá-lo duas vezes mudaria o fallback, que tem que ser
    exatamente o comportamento anterior.
    """
    return [
        {'filter': {'exists': {'field': CAMPO_PREVALENCIA}},
         **_fator(CAMPO_PREVALENCIA)},
        {'filter': {'bool': {'must_not': [{'exists': {'field': CAMPO_PREVALENCIA}}]}},
         **_fator(CAMPO_PREVALENCIA_FALLBACK)},
        {'filter': {'exists': {'field': CAMPO_PREVALENCIA}},
         'field_value_factor': {'field': CAMPO_PREVALENCIA_FALLBACK,
                                'modifier': 'log2p', 'factor': FATOR_ATESTACAO,
                                'missing': 0}},
    ]


def query_autocomplete(termo: str, tamanho: int = 10,
                       somente_ente_publico: bool = False) -> dict:
    """Body do autocomplete: prefixo em nome canônico OU em qualquer variante.

    `bool_prefix` = todos os termos casam como termo inteiro, menos o ÚLTIMO,
    que casa como prefixo — é o comportamento de "digitando". O analyzer do
    campo tem asciifolding, então `uniao` acha `UNIÃO`.

    `dis_max` (melhor campo) e NÃO soma de `should`: somando, quem casa nos
    dois campos ganha o dobro, e aí uma entidade minúscula cujo nome canônico é
    "…INSS Manaus" passa na frente do INSS, que casa "inss" só pela variante
    "INSTITUTO NACIONAL DO SEGURO SOCIAL - INSS". Variante É nome — não vale
    menos. Fica só um empurrãozinho pro nome canônico.

    **`operator: and` primeiro, `or` como rede.** `bool_prefix` casa por OR por
    padrão: medido 12/08, "fazenda sao paulo" devolvia "FAZENDA SÃO MARCELO
    LTDA" no topo (casava 2 de 3 termos num campo curto) e a "FAZENDA DO ESTADO
    DE SÃO PAULO" ficava fora do top-5. Digitar MAIS palavras tem que
    ESTREITAR. A variante OR entra com boost baixo só pra busca com typo/palavra
    a mais não voltar vazia.

    A busca corre em `variantes_busca` (grafias com peso), não em `variantes`
    (tudo) — ver `variantes_de_busca`.

    Ranking = relevância textual × prevalência × atestação, tudo em log pra que
    o INSS não esmague a busca textual (quem digita o nome inteiro de um
    município pequeno tem que achar o município pequeno). Prevalência é
    `n_processos` — a contagem REAL em `voyager-processos` — quando ela existe, e
    `n_partes` quando não existe. Ver `funcoes_prevalencia`: é ali que mora a
    diferença entre "não contamos" e "contamos e deu zero", e o porquê da
    atestação.

    Medido no índice real (1º colocado antes → depois de contar):
        "inss" .................. Gerente Executivo do INSS SP/Centro (21
                                  processos) → INSTITUTO NACIONAL DO SEGURO
                                  SOCIAL (4.402.239)
        "fazenda sao paulo" ..... Fazenda São Paulo Agropecuária Ltda (11) →
                                  Fazenda Pública do Estado de SP (77.053)
        "caixa" ................. o doc de 1 linha da CAIXA → o de 655 linhas
    """
    def _mm(campo, operador, boost):
        return {'multi_match': {'query': termo, 'type': 'bool_prefix',
                                'fields': _campos_autocomplete(campo),
                                'operator': operador, 'boost': boost}}

    campos = [
        _mm('nome_canonico.autocomplete', 'and', 3.0),
        _mm('variantes_busca.autocomplete', 'and', 2.5),
        _mm('nome_canonico.autocomplete', 'or', 1.2),
        _mm('variantes_busca.autocomplete', 'or', 1.0),
    ]
    query = {'dis_max': {'queries': campos, 'tie_breaker': 0.1}}
    if somente_ente_publico:
        query = {'bool': {'must': [query],
                          'filter': [{'term': {'eh_ente_publico': True}}]}}
    return {
        'size': tamanho,
        'query': {
            'function_score': {
                'query': query,
                'functions': funcoes_prevalencia(),
                # `multiply`: o ES já ignora função cujo filtro não casa, então
                # quem foi contado multiplica prevalência × atestação e quem não
                # foi fica só com o fallback `n_partes` (= comportamento anterior)
                'score_mode': 'multiply',
                'boost_mode': 'multiply',
            },
        },
        '_source': ['entidade_id', 'nome_canonico', 'variantes', 'variantes_n',
                    'variantes_busca', 'chave', 'raiz_cnpj', 'documentos',
                    'eh_ente_publico', 'n_partes', 'n_variantes',
                    'n_processos', 'n_processos_em'],
    }


#: teto de cláusulas do OR. O ES tem `indices.query.bool.max_clause_count`
#: (1024 por padrão em 8.x) — e as grafias de cauda longa ("GERENTE EXECUTIVO
#: DO INSS EM MOSSORÓ", 1 linha) somam ~nada de recall e custam latência.
MAX_CLAUSULAS_VARIANTES = 50


def query_variantes(variantes, campo: str = 'partes',
                    max_clausulas: int = MAX_CLAUSULAS_VARIANTES,
                    ocorrencias=None, min_ocorrencias: int = 0) -> dict:
    """OR de `match_phrase` das grafias contra o campo TEXTO `partes`.

    É a query que o autocomplete dispara HOJE: `partes` existe em 100% dos
    71,1M docs de `voyager-processos`, enquanto o nested `participacoes`
    (a forma estruturalmente correta) está em 1,9% — reindex em curso.
    Quando o reindex fechar, esta função vira o fallback e a busca passa a
    filtrar `participacoes.parte_id`/`documento`.

    `match_phrase` (não `match`) porque "INSTITUTO NACIONAL DO SEGURO SOCIAL"
    em `match` traria qualquer "instituto" e qualquer "social".
    `variantes` precisa vir ordenado por frequência (é como `grupo_to_doc`
    escreve) — o corte em `max_clausulas` mantém as grafias que importam.

    `ocorrencias` (o campo `variantes_n`, mesma ordem) + `min_ocorrencias`
    permitem cortar grafia de cauda ANTES do OR. É a defesa contra o
    over-match conhecido: uma grafia genérica ("UNIÃO FEDERAL", 1 linha)
    pendurada no CNPJ de um órgão específico faria o OR varrer meio índice.
    """
    grafias = [v for v in (variantes or []) if (v or '').strip()]
    if ocorrencias and min_ocorrencias > 1:
        pares = list(zip(grafias, list(ocorrencias) + [0] * len(grafias)))
        filtradas = [g for g, n in pares if n >= min_ocorrencias]
        grafias = filtradas or grafias[:1]     # nunca devolve OR vazio
    grafias = grafias[:max_clausulas]
    return {
        'query': {'bool': {
            'should': [{'match_phrase': {campo: g}} for g in grafias],
            'minimum_should_match': 1,
        }},
    }


# --------------------------------------------------------------------------- #
# Contagem REAL de processos (`n_processos`) — segunda passada, ES→ES
# --------------------------------------------------------------------------- #
#: corte default do escopo da contagem. Medido em 55 termos de busca reais
#: (490 entidades distintas no top-10, 147 no top-3) contra o índice de 1,14M:
#:
#:     corte                  entidades   cobre top-10   cobre top-3
#:     n_partes>=2 OU ente     182.196       94,5%          98,0%   ← default
#:     n_partes>=3 OU ente     102.372       89,6%          96,6%
#:     n_partes>=5 OU ente      74.259       81,0%          92,5%
#:     só ente público          68.783       57,8%          58,5%
#:
#: O degrau de 2→3 custa 4,9pp do top-10 e economiza 80k contagens — que, medidas,
#: são ~2min de `_msearch`. Barato demais pra abrir mão da cobertura, ainda mais
#: porque é exatamente na faixa `n_partes=2` que mora o bug relatado ("Fazenda São
#: Paulo Agropecuária Ltda", 2 linhas de cadastro, ganhando da Fazenda Pública do
#: Estado de SP): entidade que disputa o topo tem que ser MEDIDA, não presumida.
#: Ente público entra sempre, com qualquer `n_partes` — é o universo do produto
#: ("quem deve") e ele quase nunca tem CNPJ no dado do tribunal (decisão 3).
CONTAGEM_MIN_PARTES = 2


def escopo_contagem(min_partes: int = CONTAGEM_MIN_PARTES,
                    incluir_ente_publico: bool = True,
                    somente_faltantes: bool = False) -> dict:
    """Query que seleciona QUEM vai ser contado (ver `CONTAGEM_MIN_PARTES`).

    `somente_faltantes` pula quem já tem `n_processos` — é a retomada barata de
    uma passada interrompida, e o que torna re-rodar o comando quase de graça.
    """
    deve: list = [{'range': {'n_partes': {'gte': min_partes}}}]
    if incluir_ente_publico:
        deve.append({'term': {'eh_ente_publico': True}})
    bool_query: dict = {'should': deve, 'minimum_should_match': 1}
    if somente_faltantes:
        bool_query['must_not'] = [{'exists': {'field': 'n_processos'}}]
    return {'bool': bool_query}


def _tokens_grafia(grafia: str) -> tuple:
    """Tokens de IDENTIDADE de uma grafia, pra comparar uma com a outra.

    Segue o `portuguese_asciifolding` (standard + asciifolding, SEM stopword —
    "DO" é token pro ES), mas passa antes pelo `limpar_rotulo`, que descasca o
    papel processual que o PJe cola no nome. Sem isso "FULANO SA - FALIDA" seria
    sub-frase de "FULANO SA - FALIDA (EXECUTADO(A))" e a poda comeria o nome
    LIMPO em favor da variante decorada — um subregistro sistemático em toda
    entidade que o cartório carimbou com o papel.
    """
    return tuple(_RE_NAO_ALNUM.sub(
        ' ', _sem_acento(limpar_rotulo(grafia)).upper()).split())


def _e_sub_frase(menor: tuple, maior: tuple) -> bool:
    """`menor` é uma sequência CONTÍGUA e estritamente menor dentro de `maior`.

    É a relação que o `match_phrase` enxerga: quem procura a frase curta pega
    todo documento que contém a longa.
    """
    if not menor or len(menor) >= len(maior):
        return False
    return any(maior[i:i + len(menor)] == menor
               for i in range(len(maior) - len(menor) + 1))


def grafias_para_contagem(grafias, ocorrencias=None) -> list:
    """Poda de OVER-MATCH: tira do OR a grafia truncada que não é o nome real.

    O problema, medido na 1ª contagem completa (12/08, 182.196 entidades): o
    top-10 de "quem mais litiga no Brasil" saiu assim —

        7.222.852  Procuradoria - Allianz        (4 linhas)  grafias: "PROCURADORIA" | "Procuradoria - Allianz"
        4.469.999  INSTITUTO NACIONAL LTDA       (2 linhas)  grafias: "INSTITUTO NACIONAL" | "INSTITUTO NACIONAL LTDA"
        1.538.141  S.A. (VIAÇÃO AÉREA…) - FALIDA (4 linhas)  grafias: "S." | "S.A. (VIAÇÃO AÉREA RIO-GRANDENSE) - FALIDA"

    `match_phrase` de uma frase CURTA casa todo processo que contém a frase
    LONGA: procurar "PROCURADORIA" traz as 7,2 milhões de procuradorias do país.
    Uma linha truncada do cartório ("PROCURADORIA" digitado sozinho) vira, no
    índice, uma entidade que aparenta litigar mais que o INSS.

    REGRA: entre duas grafias da MESMA entidade em que uma é sub-frase da outra,
    a mais CURTA só sobrevive se for MAIS FREQUENTE que a mais longa. É a
    diferença entre um nome e um pedaço de nome:

        INSS  "INSTITUTO NACIONAL DO SEGURO SOCIAL" (610×) ⊂ "… - INSS" (17×)
              → 610 > 17: a curta é o NOME. Fica. (contagem segue 4.402.239)
        VARIG "S." (1×) ⊂ "S.A. (VIAÇÃO AÉREA RIO-GRANDENSE) - FALIDA" (1×)
              → empate: a curta é um TRUNCAMENTO. Sai.

    Empate cai pra fora de propósito — sem evidência de que a forma curta é o
    nome, a longa é a aposta segura (abster > chutar). Nunca devolve lista vazia:
    a grafia mais longa não é sub-frase de ninguém, então nunca é podada.

    Isto NÃO mexe em `query_variantes`/`variantes_busca` (recall da busca): é uma
    regra de MEDIÇÃO. Na busca, uma grafia a mais custa um resultado a mais; na
    contagem, custa 7 milhões de processos atribuídos a quem não é dono deles.
    """
    limpas = [g for g in (grafias or []) if (g or '').strip()]
    if len(limpas) < 2:
        return limpas
    ocorrencias = ocorrencias or {}
    toks = {g: _tokens_grafia(g) for g in limpas}
    mantidas = []
    for g in limpas:
        engolida = any(
            _e_sub_frase(toks[g], toks[h])
            and int(ocorrencias.get(g, 0)) <= int(ocorrencias.get(h, 0))
            for h in limpas if h != g)
        if not engolida:
            mantidas.append(g)
    return mantidas or limpas[:1]


def query_contagem(variantes, campo: str = 'partes',
                   max_clausulas: int = MAX_CLAUSULAS_VARIANTES,
                   ocorrencias=None) -> dict:
    """Sub-busca do `_msearch` que devolve SÓ o total de processos da entidade.

    `ocorrencias` = `{grafia: nº de linhas de Parte}` (de `variantes`/
    `variantes_n`), usado por `grafias_para_contagem` pra podar a grafia
    truncada. Sem ele a poda não roda — e o topo do índice vira ruído.

    É `query_variantes` + duas coisas que não podem faltar:
      * `size: 0` — não queremos nenhum documento, só o número. Sem isso o ES
        monta e serializa 10 hits × 182k entidades à toa;
      * `track_total_hits: true` — o default do ES 8 é **10.000**, e sem isso o
        INSS voltaria `{"value": 10000, "relation": "gte"}`. Um teto silencioso
        gravado como se fosse contagem seria pior que não contar: empataria as
        50 maiores entidades do país no mesmo número redondo.
    """
    podadas = grafias_para_contagem(variantes, ocorrencias)
    corpo = query_variantes(podadas, campo=campo, max_clausulas=max_clausulas)
    corpo['size'] = 0
    corpo['track_total_hits'] = True
    return corpo


def total_exato(resposta: dict):
    """Total de uma resposta do ES — `None` se não for confiável.

    Devolve `None` (e não 0) quando a sub-busca falhou ou quando o total veio
    truncado (`relation='gte'`, ou seja, `track_total_hits` não pegou): gravar 0
    ali seria inventar "essa entidade não tem processo". Ausência é a resposta
    honesta — o ranking sabe cair no `n_partes`.
    """
    if not isinstance(resposta, dict) or 'error' in resposta:
        return None
    total = (resposta.get('hits') or {}).get('total')
    if isinstance(total, dict):
        if total.get('relation') != 'eq':
            return None
        return int(total.get('value', 0))
    if isinstance(total, int):          # ES antigo devolvia int puro
        return int(total)
    return None


def doc_contagem(n_processos: int, agora: str | None = None) -> dict:
    """O `doc` do `_bulk`/`update` parcial — só os 2 campos da contagem.

    Parcial de propósito: reindexar a entidade inteira aqui apagaria o trabalho
    do build (variantes, documentos, consolidação) por causa de um número.
    """
    return {'n_processos': int(n_processos),
            'n_processos_em': agora or datetime.now(timezone.utc).isoformat()}
