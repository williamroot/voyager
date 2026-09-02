"""Agregações da PÁGINA DE ESTADO do Mapa Comercial — 100% Elasticsearch.

O usuário clica num estado no mapa (`/dashboard/overview/mapa/`) e abre a página
dedicada daquele estado, que "explode" os dados: tipo de processo, ano, tribunal,
entidade devedora e classificação. Toda leitura é do índice `voyager-processos`
— NUNCA do Postgres (é o gargalo de prod). Única exceção: a **cobertura**
(% validado), que vem do cache já existente (`dashboard.queries.
cobertura_enriquecimento_data`) via `agg_overview._cobertura_por_{uf,tribunal}`.

Reusa de `search.agg_overview`: `parse_filtros` (mesmos filtros do mapa),
`build_filter_clauses`, `_metric_subaggs` (volume/valor/possíveis/confirmados/
união) e as funções de cobertura. Mesma semântica, zero divergência de número.

================================================================================
CONTRATO JSON — GET /dashboard/api/overview/estado/<uf>/
================================================================================
Login obrigatório (`@login_required`), `@require_GET`, `application/json`.

`<uf>` ∈ 27 siglas de UF + `FED` (camada federal: TRF*/TST/STJ/STF/TRT* — processo
federal não pertence a uma UF única). UF inválida ⇒ **400** `{"erro": ...}`.
ES indisponível ⇒ **503** `{"erro": ...}`. Nunca 500 cru.

QUERYSTRING
-----------
Aceita **exatamente os mesmos filtros do mapa** (ver `agg_overview.parse_filtros`):
`tribunal, classificacao, tipo(potencial|confirmado), tem_sinal, ano_min, ano_max,
valor_min, valor_max, codigo_classe, natureza`. O filtro `uf` da querystring é
IGNORADO — quem manda é o `<uf>` da rota.
Mais:
    metrica   possiveis | confirmados | todos      (default: `todos`)

`metrica` é a LENTE dos blocos de detalhe (o "escopo"):
    possiveis   ⇒ tem_sinal_precatorio = true            (sinal DJEN, amplo)
    confirmados ⇒ classificacao = 'PRECATORIO'           (ML, preciso)
    todos       ⇒ UNIÃO dos dois (`bool.should` + minimum_should_match=1) —
                  **NUNCA a soma**: medido 12/08/2026, dos 47.720 confirmados só
                  6.421 têm sinal de texto; somar contaria a interseção 2×, e os
                  41.299 (87%) restantes só aparecem nesta visão.
Valor desconhecido cai no default (`todos`), nunca 500.

O `resumo` NÃO é afetado pela `metrica` (traz os 4 números do estado sempre, pro
front dar contexto); os blocos `por_*` são calculados DENTRO do escopo. O tamanho
do escopo vem em `resumo.escopo_volume`.

RESPOSTA
--------
{
  "uf": "SP",
  "uf_nome": "São Paulo",
  "metrica": "todos",
  "filtros": { ...eco dos filtros saneados (inclui "uf")... },
  "gerado_em": "2026-08-12T12:00:00+00:00",

  "resumo": {
    "volume": 4253470,          // int   — processos do estado (com os filtros)
    "possiveis": 70259,         // int|null — tem_sinal_precatorio=true;
                                //   NULL se o sinal nunca foi computado no
                                //   estado (desconhecido ≠ zero)
    "sinal_processado": true,   // bool  — o backfill do sinal cobriu o estado?
    "confirmados": 4378,        // int   — classificacao=PRECATORIO
    "todos": 71972,            // int   — UNIÃO possíveis ∪ confirmados
    "valor": 1302599875615.84,  // float — sum(valor_causa) do ESTADO
    "valor_escopo": 51230.0,    // float — sum(valor_causa) dentro do escopo
    "cobertura_pct": 12.3,      // float|null — % validado (cache; null = sem dado)
    "tribunais": 1,             // int   — nº de tribunais distintos no estado
    "escopo_volume": 71972,     // int   — base dos blocos por_* (lente `metrica`)
    "cobertura_valor": <COBERTURA_AMOSTRA>   // quão esparso é valor_causa aqui
  },

  "por_tipo_processo": {        // terms classe_nome (o gráfico-rei: campo presente
                                // em 100% dos docs — MAS pode vir VAZIO, ver nota)
    "itens": [ {"tipo": "PRECATÓRIO", "volume": 32182}, ... ],  // top 15, desc
    "outros": 1432,             // int — cauda além do top 15 (com classe preenchida)
    "sem_dado": 2884,           // int — docs no escopo com classe_nome VAZIA ('')
    "cobertura_amostra": <COBERTURA_AMOSTRA>
  },

  "por_ano": {                  // terms ano_cnj, ORDENADO POR ANO asc (série temporal)
    "itens": [ {"ano": 2011, "volume": 136}, ... ],
    "outros": 0,
    "sem_dado": 0,
    "fora_da_faixa": 13,        // int — anos fora de [1990, ano_atual+1]: CNJ
                                //   malformado (SP tem buckets em 9400/9900).
                                //   Ficam FORA da série pra não estourar o eixo X.
    "cobertura_amostra": <COBERTURA_AMOSTRA>
  },

  "por_tribunal": {             // terms tribunal + métricas (drill-down dentro da UF)
    "itens": [ {"tribunal": "TJSP", "volume": 71972, "valor": 0.0,
                "possiveis": 70259, "sinal_processado": true,
                "confirmados": 4378, "todos": 71972,
                "cobertura_pct": 12.3} ],
    "outros": 0,
    "cobertura_amostra": <COBERTURA_AMOSTRA>
  },

  "por_classificacao": {        // terms classificacao (PRECATORIO/PRE_PRECATORIO/…)
    "itens": [ {"classificacao": "NAO_LEAD", "volume": 59629}, ... ],
    "outros": 0,
    "sem_dado": 21,             // classificacao vazia ('') = ainda não classificado
    "cobertura_amostra": <COBERTURA_AMOSTRA>
  },

  "por_entidade_devedora": {    // nested participacoes (polo=passivo) em docs com
                                // tem_ente_publico_passivo=true — QUEM DEVE
    "itens": [ {"entidade": "Instituto Nacional do Seguro Social - INSS",
                "volume": 44363,        // processos (reverse_nested), grafias somadas
                "participacoes": 44363, // participações nested (≈ volume)
                "variantes": 3,         // nº de grafias fundidas
                "grafias": [ {"nome": "...", "volume": 23171}, ... ],  // até 5
                "por_complemento": false  // true = entrou pela regex complementar
               }, ... ],                                              // top 15
    "cobertura_amostra": <COBERTURA_AMOSTRA>,
    "docs_com_ente_publico": 9896,   // docs no escopo com a flag true (base do filtro)
    "flag_processada": true,         // a flag tem_ente_publico_passivo existe no escopo?
    "buckets_brutos": 200,           // buckets que o ES devolveu (antes de tratar)
    "buckets_fundidos": 12,          // buckets absorvidos pela normalização de grafia
    "descartados_nao_ente": 9,       // grupos descartados por NÃO casar RE_ENTE_PUBLICO
    "descartados_nao_ente_volume": 812,
    "aceitos_por_complemento": 5,    // itens que só entraram pela regex
                                     //   complementar (auditoria da heurística)
    "advogados_excluidos": 4210,     // participações do passivo fora por serem
                                     //   advogado/procuradoria (representam o
                                     //   devedor, não são o devedor)
    "truncado": true,                // ES devolveu o teto de buckets ⇒ cauda oculta
    "nota": "..."                    // caveat pronto pra exibir
  }
}

<COBERTURA_AMOSTRA> — **honestidade obrigatória**. Campo parcial NÃO pode ser
apresentado como retrato do estado. Formato:
    {
      "campo": "participacoes",
      "docs_com_o_campo": 20332,   // docs do ESCOPO que realmente alimentam o bloco
      "total_do_escopo": 71972,    // denominador do bloco (lente `metrica`)
      "total_do_estado": 4253470,  // contexto: o estado inteiro
      "pct": 28.3                  // 100 × docs_com_o_campo / total_do_escopo
    }
O front DEVE escrever "amostra de X% dos processos" quando `pct < 100`.

--------------------------------------------------------------------------------
LIMITAÇÕES MEDIDAS (12/08/2026) — o front precisa comunicar
--------------------------------------------------------------------------------
- `classe_nome` está presente em 100% dos docs, mas pode ser **string vazia**:
  RO 69,6% vazio (746.667/1.072.089), MG 0,5%, SP 1,7%. Por isso o bloco tem
  `sem_dado` e o `cobertura_amostra` desconta os vazios — "100% de cobertura"
  seria mentira em RO.
- `participacoes` (nested) está indexado parcialmente (6,59M participações no
  índice): RO tem **zero** participação indexada ⇒ o bloco de entidade devedora
  volta vazio com `pct: 0`. Vazio aqui é "não indexamos ainda", NÃO "não há ente
  público" — o front tem que dizer isso.
- `tem_ente_publico_passivo` também é parcial (6,1% da base). `flag_processada`
  = False ⇒ o filtro de entidade não tem base no estado.
- `valor_causa` é esparso (2,8% da base; federal/DJEN não traz) e é o valor DA
  CAUSA, não o valor do precatório (esse vive no Falcon). Ver `cobertura_valor`.
- Fusão de grafias: `.raw` é case/acento-sensível, então a MESMA entidade vira
  vários buckets (o INSS vira 3). Fundimos por chave normalizada
  (`normalizar_entidade`) somando os counts. Como um processo pode ter 2 grafias
  da mesma entidade, o `volume` fundido pode superestimar em alguns processos
  (limite superior; sem fusão a subestimação seria muito maior).
- O filtro de ente público usa `RE_ENTE_PUBLICO` (fonte única, de
  `tribunals/estagio.py`) — pessoa física do mesmo processo entra na agregação
  nested e é descartada aqui (ex.: "Daniel Gerber", 2.636 processos em 12/08).
  Quem não casa a regex fica FORA: contabilizado em `descartados_nao_ente`
  (em SP, 44 dos 60 primeiros buckets do ES eram não-ente — por isso pedimos
  200 buckets, não 15).
- Advogado do polo passivo é EXCLUÍDO (`eh_advogado != true`): ele representa o
  devedor, não é o devedor (em SP uma advogada aparecia no top-10 com 168; em MG
  a "Procuradoria-Geral do Município X" competia com o próprio "Município X").
  Quanto saiu por isso vem em `advogados_excluidos`.
- O gate de ente público tem um COMPLEMENTO (`RE_ENTE_PUBLICO_COMPLEMENTO`)
  porque a regex canônica do Estágio tem falso-negativo institucional (derrubava
  FUNASA/IBAMA/UnB/DNIT/CEF em FED e "ESTADO DE MINAS GERAIS" em MG). O que
  entrou por ele vem marcado (`por_complemento` no item, `aceitos_por_complemento`
  no bloco). Resíduo conhecido: sobram fora nomes públicos de baixo volume
  (ICMBio, IPEA, IPHAN, IBGE ≤ 8 processos cada) — é heurística, não cadastro.
- `truncado: true` ⇒ o top-15 saiu de um top-200 do ES; entidade de cauda longa
  pode não aparecer. Não é ranking exaustivo do estado.
- `ano_cnj` tem lixo: CNJ malformado gera anos como 9400/9900. Fora de
  [1990, ano_atual+1] vai pra `fora_da_faixa`, não pra série.
--------------------------------------------------------------------------------
"""
import hashlib
import json
import logging
import re
import unicodedata

from django.core.cache import cache

from search.agg_overview import (
    ANO_MIN_PLAUSIVEL,
    CLASSIF_CONFIRMADO,
    _agora_iso,
    _ano_atual,
    _cobertura_por_tribunal,
    _cobertura_por_uf,
    _metric_subaggs,
    build_filter_clauses,
    parse_filtros,  # noqa: F401  — reexport: as views usam o MESMO parser do mapa
)
from search.geo import (UF_DO_TRIBUNAL, UF_FEDERAL, fonte_publica_valor,
                       uf_tem_fonte_de_valor)
from tribunals.estagio import RE_ENTE_PUBLICO

logger = logging.getLogger('voyager.comercial.agg_estado')

#: métricas (lentes) aceitas pela página de estado
METRICAS = ('possiveis', 'confirmados', 'todos')
METRICA_DEFAULT = 'todos'

#: TTLs. Eram 5min/2min "no padrão do agg_overview" — e o padrão não servia
#: aqui, porque esta agregação é MUITO mais cara que a do overview.
#:
#: Medido em produção (02/09/2026), com o `_msearch` do `voyager-processos`:
#:
#:     MA frio 22,16s · PA frio 20,95s · qualquer um quente 0,00s
#:
#: O teto do cliente ES é 30s. Ou seja: passada fria já nasce a dois terços do
#: limite, e QUALQUER carga concorrente a empurra por cima — foi exatamente o
#: que aconteceu neste dia, com o `update_by_query` do `proc_digits` rodando:
#: `ConnectionTimeout` no msearch e **503 na tela do estado**.
#:
#: Não adianta estrangular o vizinho: baixar o backfill de 800 para 200 docs/s
#: mudou 22,16s para 20,95s. Os 20s são o CUSTO da agregação num índice de
#: 1,55 bi num nó só, disk-I/O-bound — não são culpa de quem está do lado.
#:
#: E não existe job de aquecimento para esta tela (há `warm_charts_leves`,
#: `warm_kpis`, `warm_command_center` e outros dez; nenhum para `agg_estado`).
#: Com 300s, o cache expirava e o PRÓXIMO visitante pagava os 20s na cara.
#:
#: 30min é a escolha honesta enquanto o warm não existe: o número é um
#: agregado de estado sobre um acervo que muda devagar, então meia hora de
#: defasagem custa muito menos que um 503. O warm dedicado continua sendo o
#: conserto certo — ver #64, que é o MESMO defeito na tela de leads.
CACHE_TTL = 1800
CACHE_TTL_FILTRADO = 600
_CACHE_PREFIX = 'comercial:agg:estado'

#: TTL do que o WARM escreve. Maior que o intervalo do warm (60 min) para que
#: três passadas possam falhar antes de alguém pagar a agregação fria — e
#: MUITO menor que os 7 dias do `_WARM_TTL` dos charts.
#:
#: A diferença não é gosto. Sete dias fariam a tela continuar publicando um
#: número de uma semana atrás, com cara de novo, depois que o warm morresse —
#: que é exatamente a falha silenciosa que o `vigia_backfills` existe para
#: acabar. Com 4 h, warm morto degrada para o caminho preguiçoso
#: (`CACHE_TTL`), que é lento e correto, em vez de rápido e velho.
WARM_TTL = 4 * 3600


def cache_key_estado(uf: str, metrica: str, filtros: dict | None = None) -> str:
    """A chave de cache da página do estado — UMA função, dois chamadores.

    O `agg_estado()` e o warm (`dashboard.tasks.warm_agg_estado`) TÊM que
    derivar a mesma string, e a única forma de garantir isso é não haver duas
    derivações. Foi assim que o #64 descobriu que o warm da tela de leads
    computava 24 payloads para popular 12 chaves e ainda deixava a view no
    miss: a chave era montada em dois lugares, e os dois discordavam.
    """
    uf = normalizar_uf(uf)
    metrica = normalizar_metrica(metrica)
    filtros = {k: v for k, v in (filtros or {}).items() if k != 'uf'}
    if filtros:
        chave = json.dumps(filtros, sort_keys=True, default=str)
        return (f'{_CACHE_PREFIX}:{uf}:{metrica}:f:'
                f'{hashlib.md5(chave.encode()).hexdigest()}')
    return f'{_CACHE_PREFIX}:{uf}:{metrica}:v1'

#: quantos itens cada bloco devolve (+ "outros" agregando a cauda)
TOP_N = 15
#: buckets pedidos ao ES pra entidade — MUITO folgado de propósito: as grafias
#: duplicadas comem slots (o INSS come 3) e os não-entes também (medido em SP,
#: 12/08: 44 dos 60 primeiros buckets NÃO eram ente público). Custo medido do
#: terms 200 + reverse_nested: ~50-125ms.
ENTIDADES_TERMS_SIZE = 200
#: teto de tribunais/anos/classificações (baixa cardinalidade)
_TRIBUNAIS_TERMS_SIZE = 40
_ANOS_TERMS_SIZE = 80
_CLASSIF_TERMS_SIZE = 20
#: quantas grafias devolvemos por entidade fundida (evidência da fusão na UI)
_MAX_GRAFIAS = 5

UF_NOME = {
    'AC': 'Acre', 'AL': 'Alagoas', 'AP': 'Amapá', 'AM': 'Amazonas',
    'BA': 'Bahia', 'CE': 'Ceará', 'DF': 'Distrito Federal',
    'ES': 'Espírito Santo', 'GO': 'Goiás', 'MA': 'Maranhão',
    'MT': 'Mato Grosso', 'MS': 'Mato Grosso do Sul', 'MG': 'Minas Gerais',
    'PA': 'Pará', 'PB': 'Paraíba', 'PR': 'Paraná', 'PE': 'Pernambuco',
    'PI': 'Piauí', 'RJ': 'Rio de Janeiro', 'RN': 'Rio Grande do Norte',
    'RS': 'Rio Grande do Sul', 'RO': 'Rondônia', 'RR': 'Roraima',
    'SC': 'Santa Catarina', 'SP': 'São Paulo', 'SE': 'Sergipe',
    'TO': 'Tocantins',
    UF_FEDERAL: 'Justiça Federal e Superior',
}

#: UFs válidas = as 27 do mapa tribunal→UF + a camada federal
UFS_VALIDAS = frozenset(UF_DO_TRIBUNAL.values()) | {UF_FEDERAL}


class UfInvalida(ValueError):
    """UF fora do conjunto válido (a view traduz em 400)."""


# --------------------------------------------------------------------------- #
# Saneamento de entrada
# --------------------------------------------------------------------------- #
def normalizar_uf(uf: str) -> str:
    """'sp' → 'SP'. Levanta `UfInvalida` se não for UF conhecida (ou FED)."""
    s = (uf or '').strip().upper()
    if s not in UFS_VALIDAS:
        raise UfInvalida(f'UF inválida: {uf!r}')
    return s


def normalizar_metrica(metrica: str) -> str:
    """Enum desconhecido cai no default (`todos`) — nunca levanta."""
    s = (metrica or '').strip().lower()
    return s if s in METRICAS else METRICA_DEFAULT


# --------------------------------------------------------------------------- #
# Normalização de grafia de entidade (algorítmica — sem mapa manual de nomes)
# --------------------------------------------------------------------------- #
#: parênteses SEM aninhamento — aplicado em loop por `_sem_parenteses` porque o
#: PJe produz "(REQUERIDO(A))" (aninhado): 1 passada deixaria um ')' órfão.
_RE_PARENTESES = re.compile(r'\([^()]*\)')
_RE_NAO_ALNUM = re.compile(r'[^A-Z0-9]+')
_RE_SEPARADOR = re.compile(r'[-–—/|:;,]')
#: conectivos que não distinguem entidade ("Estado DE São Paulo" == "Estado São Paulo")
_STOPWORDS = frozenset({'DA', 'DE', 'DO', 'DAS', 'DOS', 'E', 'EM',
                        'NO', 'NA', 'NOS', 'NAS', 'A', 'O', 'AS', 'OS'})
#: token de até N chars num segmento próprio é tratado como SIGLA e descartado
#: ("Instituto Nacional do Seguro Social - INSS" ≡ "Instituto Nacional do
#: Seguro Social"). É o que funde os 3 buckets do INSS.
_SIGLA_MAX_CHARS = 8


#: COMPLEMENTO do `RE_ENTE_PUBLICO` (que continua sendo o gate primário).
#: Medido no ES em 12/08/2026: a regex canônica do Estágio tem FALSO-NEGATIVO
#: institucional — em FED ela derrubava 153 dos 200 buckets, incluindo FUNASA
#: (314 processos), IBAMA, UnB, FNDE, SUFRAMA, DNIT, UFG, CEF, FUNAI; em MG
#: derrubava "ESTADO DE MINAS GERAIS" (o alternante `estado d` exige fronteira
#: de palavra depois do "d", então "estado DE minas" NÃO casa).
#: São PADRÕES institucionais (não lista de nomes) e deliberadamente
#: conservadores — "BANCO GM S.A" e "EPAVE SERVICOS LTDA" continuam fora.
#: Quantos itens entraram por aqui vai no payload (`aceitos_por_complemento`),
#: pra auditoria. Não mexemos em `tribunals/estagio.py`: a regex de lá é feature
#: de modelo treinado — mudar lá mudaria a predição do Estágio do Crédito.
RE_ENTE_PUBLICO_COMPLEMENTO = re.compile(
    r'\bestados?\s+d[eoa]s?\b'                       # ESTADO DE MINAS GERAIS
    r'|\bfunda[çc][ãa]o\s+(nacional|federal|estadual|municipal|p[úu]blica'
    r'|universidade)\b'                              # FUNDACAO NACIONAL DE SAUDE
    r'|\buniversidade\s+(federal|estadual|d[oa]\s+estado)\b'
    r'|\binstituto\s+(federal|brasileiro|estadual)\b'  # IBAMA
    r'|\bfundo\s+(nacional|estadual|municipal)\b'      # FNDE
    r'|\bsuperintend[êe]ncia\b|\bdepartamento\s+(nacional|estadual|municipal)\b'
    r'|\bag[êe]ncia\s+(nacional|estadual|reguladora)\b'
    r'|\bconselho\s+(federal|regional|nacional)\b'
    r'|\bc[âa]mara\s+municipal\b|\bassembleia\s+legislativa\b'
    r'|\btribunal\s+d[eo]\b|\bminist[ée]rio\s+p[úu]blico\b'
    r'|\bdefensoria\s+p[úu]blica\b|\bprocuradoria\b'
    r'|\bcaixa\s+econ[ôo]mica\s+federal\b'
    r'|\bempresa\s+brasileira\s+de\b'                 # EBSERH/Embrapa & cia
    r'|\bminist[ée]rio\s+d[ao]\b|\breceita\s+federal\b|\bbanco\s+central\b'
    r'|\bpol[íi]cia\s+(federal|militar|civil|rodovi[áa]ria)\b'
    r'|\binstituto\s+de\s+previd[êe]ncia\b'
    r'|\bmunicipal\b',                                # autarquia/serviço municipal
    re.I)


def eh_ente_publico(nome: str) -> tuple:
    """(é ente público?, entrou pelo complemento?) — gate primário = Estágio."""
    n = nome or ''
    if RE_ENTE_PUBLICO.search(n):
        return True, False
    if RE_ENTE_PUBLICO_COMPLEMENTO.search(n):
        return True, True
    return False, False


def _sem_parenteses(s: str) -> str:
    """Remove trechos entre parênteses, inclusive aninhados ('(REQUERIDO(A))')."""
    for _ in range(4):
        novo = _RE_PARENTESES.sub(' ', s)
        if novo == s:
            break
        s = novo
    return s


def _sem_acento(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFKD', s)
                   if not unicodedata.combining(c))


def _eh_sigla(segmento: str) -> bool:
    toks = _RE_NAO_ALNUM.sub(' ', segmento).split()
    return len(toks) == 1 and 2 <= len(toks[0]) <= _SIGLA_MAX_CHARS


def normalizar_entidade(nome: str) -> str:
    """Chave de fusão de grafias: upper + sem acento + sem pontuação/sigla/conectivo.

    ALGORÍTMICO de propósito (nenhum mapa manual de nomes):
      1. maiúscula + remove acento (`.raw` é case/acento-sensível);
      2. remove conteúdo entre parênteses (o PJe cola "(REQUERIDO(A))" no nome);
      3. descarta segmentos que são SIGLA (`- INSS`, `/DF`) quando sobra corpo;
      4. troca não-alfanumérico por espaço e colapsa espaço;
      5. remove conectivos (DE/DO/DA/…).

    Exemplos (medidos no ES, 12/08/2026) — as 3 grafias abaixo colapsam na MESMA
    chave `INSTITUTO NACIONAL SEGURO SOCIAL`:
        'Instituto Nacional do Seguro Social - INSS'
        'INSTITUTO NACIONAL DO SEGURO SOCIAL - INSS'
        'INSTITUTO NACIONAL DO SEGURO SOCIAL'
        'INSTITUTO NACIONAL DO SEGURO SOCIAL (REQUERIDO(A))'
    """
    s = _sem_parenteses(_sem_acento(nome or '').upper())

    segmentos = [p for p in _RE_SEPARADOR.split(s) if p.strip()]
    if len(segmentos) > 1:
        corpo = [p for p in segmentos if not _eh_sigla(p)]
        s = ' '.join(corpo or segmentos)

    s = _RE_NAO_ALNUM.sub(' ', s).strip()
    toks = s.split()
    uteis = [t for t in toks if t not in _STOPWORDS]
    return ' '.join(uteis or toks)


def _rotulo_canonico(grafias: list) -> str:
    """Rótulo legível pra chave fundida: a grafia MAIS FREQUENTE.

    Empate: prefere grafia com caixa mista (mais legível que CAIXA ALTA), depois
    a mais longa (costuma ser a completa), depois alfabética — determinístico.
    Do rótulo escolhido some o marcador de papel que o PJe cola no nome
    ("… (REQUERIDO(A))" — 761 processos no TJMG) quando sobra nome.
    """
    nome = sorted(
        grafias,
        key=lambda g: (-g['volume'], g['nome'].isupper(), -len(g['nome']), g['nome']),
    )[0]['nome']
    limpo = re.sub(r'\s{2,}', ' ', _sem_parenteses(nome)).strip(' -–—/|:;,')
    return limpo or nome


def fundir_entidades(buckets: list, top_n: int = TOP_N) -> dict:
    """Funde grafias, descarta não-entes e devolve o top-N + estatística honesta.

    `buckets`: buckets crus do terms `participacoes.nome.raw` já com a sub-agg
    `processos` (reverse_nested). Cada item: {'key', 'doc_count', 'processos':
    {'doc_count'}}.

    Retorna {'itens', 'buckets_brutos', 'buckets_fundidos', 'descartados_nao_ente',
    'descartados_nao_ente_volume'}.
    """
    grupos: dict = {}
    for b in buckets:
        nome = b.get('key') or ''
        chave = normalizar_entidade(nome)
        if not chave:
            continue
        participacoes = b.get('doc_count', 0)
        # nº de PROCESSOS (parent docs) — reverse_nested; cai no nested count se
        # a sub-agg não vier (ex.: mock antigo)
        processos = (b.get('processos') or {}).get('doc_count', participacoes)
        g = grupos.setdefault(chave, {'volume': 0, 'participacoes': 0, 'grafias': []})
        g['volume'] += processos
        g['participacoes'] += participacoes
        g['grafias'].append({'nome': nome, 'volume': processos})

    itens, descartados, descartado_vol, por_complemento = [], 0, 0, 0
    for g in grupos.values():
        # ente público? basta UMA grafia casar (gate primário = regex do Estágio)
        vereditos = [eh_ente_publico(gr['nome']) for gr in g['grafias']]
        if not any(ok for ok, _ in vereditos):
            descartados += 1
            descartado_vol += g['volume']
            continue
        complemento = not any(ok and not comp for ok, comp in vereditos)
        por_complemento += int(complemento)
        grafias = sorted(g['grafias'], key=lambda x: -x['volume'])
        itens.append({
            'entidade': _rotulo_canonico(grafias),
            'volume': g['volume'],
            'participacoes': g['participacoes'],
            'variantes': len(grafias),
            'grafias': grafias[:_MAX_GRAFIAS],
            # entrou pela regex complementar (auditoria da heurística)
            'por_complemento': complemento,
        })
    itens.sort(key=lambda x: (-x['volume'], x['entidade']))

    return {
        'itens': itens[:top_n],
        'buckets_brutos': len(buckets),
        # quantos buckets do ES foram ABSORVIDOS por outro (grafia duplicada)
        'buckets_fundidos': max(0, len(buckets) - len(grupos)),
        'descartados_nao_ente': descartados,
        'descartados_nao_ente_volume': descartado_vol,
        'aceitos_por_complemento': por_complemento,
    }


# --------------------------------------------------------------------------- #
# Montagem da query ES (2 sub-buscas num único _msearch)
# --------------------------------------------------------------------------- #
def clausula_metrica(metrica: str) -> dict:
    """Lente `metrica` como cláusula ES. `todos` é UNIÃO (should), NUNCA soma."""
    if metrica == 'possiveis':
        return {'term': {'tem_sinal_precatorio': True}}
    if metrica == 'confirmados':
        return {'term': {'classificacao': CLASSIF_CONFIRMADO}}
    return {'bool': {
        'should': [
            {'term': {'tem_sinal_precatorio': True}},
            {'term': {'classificacao': CLASSIF_CONFIRMADO}},
        ],
        'minimum_should_match': 1,
    }}


def _preenchido(campo: str) -> dict:
    """Filter agg "campo preenchido": existe E não é string vazia.

    Necessário porque `classe_nome`/`classificacao` existem em 100% dos docs mas
    vêm VAZIOS em massa (RO: 69,6% da classe é '') — `exists` sozinho mentiria.
    """
    return {'filter': {'bool': {
        'filter': [{'exists': {'field': campo}}],
        'must_not': [{'term': {campo: ''}}],
    }}}


def build_body_estado(uf: str, filtros: dict) -> dict:
    """Sub-busca 1 — números do ESTADO (sem a lente `metrica`): resumo + tribunais."""
    aggs = dict(_metric_subaggs())
    aggs['valor_conhecido'] = {'filter': {'exists': {'field': 'valor_causa'}}}
    # terms (barato, keyword de baixa cardinalidade) em vez de `cardinality`
    # (medido 12/08: 735ms vs 395ms em FED) — e já dá a lista, não só a contagem.
    aggs['tribunais'] = {'terms': {'field': 'tribunal', 'size': _TRIBUNAIS_TERMS_SIZE}}
    return {
        'size': 0,
        'track_total_hits': True,
        'query': {'bool': {'filter': build_filter_clauses({**filtros, 'uf': uf})}},
        'aggs': aggs,
    }


def build_body_escopo(uf: str, filtros: dict, metrica: str) -> dict:
    """Sub-busca 2 — blocos de detalhe DENTRO da lente `metrica`."""
    clauses = build_filter_clauses({**filtros, 'uf': uf}) + [clausula_metrica(metrica)]
    return {
        'size': 0,
        'track_total_hits': True,
        'query': {'bool': {'filter': clauses}},
        'aggs': {
            'valor': {'sum': {'field': 'valor_causa'}},
            'valor_conhecido': {'filter': {'exists': {'field': 'valor_causa'}}},

            'por_classe': {'terms': {'field': 'classe_nome', 'size': TOP_N,
                                     'exclude': ['']}},
            'classe_preenchida': _preenchido('classe_nome'),

            # série temporal. Pede por CONTAGEM (default) e ordena por ano no
            # Python: com `order:_key asc` o ES escolheria os N anos MENORES e
            # poderia cortar justamente os anos recentes (os que importam).
            'por_ano': {'terms': {'field': 'ano_cnj', 'size': _ANOS_TERMS_SIZE}},
            'ano_preenchido': {'filter': {'exists': {'field': 'ano_cnj'}}},

            'por_tribunal': {'terms': {'field': 'tribunal',
                                       'size': _TRIBUNAIS_TERMS_SIZE},
                             'aggs': _metric_subaggs()},

            'por_classificacao': {'terms': {'field': 'classificacao',
                                            'size': _CLASSIF_TERMS_SIZE,
                                            'exclude': ['']}},
            'classificacao_preenchida': _preenchido('classificacao'),

            # base honesta do bloco de entidade: quantos docs do escopo TÊM
            # participação indexada (o nested é parcial) e quantos têm a flag
            'participacoes_presentes': {'filter': {'nested': {
                'path': 'participacoes', 'query': {'match_all': {}}}}},
            'ente_flag_conhecida': {'filter': {
                'exists': {'field': 'tem_ente_publico_passivo'}}},

            'entidades': {
                'filter': {'term': {'tem_ente_publico_passivo': True}},
                'aggs': {'nested': {
                    'nested': {'path': 'participacoes'},
                    'aggs': {
                        # polo passivo E não-advogado: advogado do polo passivo
                        # REPRESENTA o devedor, não é o devedor (medido em SP:
                        # advogada com 168 processos entrava no top-10; em MG a
                        # "Procuradoria-Geral do Município X" competia com o
                        # próprio "Município X")
                        'passivo': {
                            'filter': {'bool': {
                                'filter': [{'term': {'participacoes.polo': 'passivo'}}],
                                'must_not': [
                                    {'term': {'participacoes.eh_advogado': True}}],
                            }},
                            'aggs': {'nomes': {
                                'terms': {'field': 'participacoes.nome.raw',
                                          'size': ENTIDADES_TERMS_SIZE},
                                # doc_count do nested conta PARTICIPAÇÕES; o
                                # reverse_nested devolve o nº de PROCESSOS
                                'aggs': {'processos': {'reverse_nested': {}}},
                            }},
                        },
                        # quantas participações do passivo saíram por serem
                        # advogado — honestidade do recorte acima
                        'advogados': {'filter': {'bool': {'filter': [
                            {'term': {'participacoes.polo': 'passivo'}},
                            {'term': {'participacoes.eh_advogado': True}},
                        ]}}},
                    },
                }},
            },
        },
    }


# --------------------------------------------------------------------------- #
# Execução
# --------------------------------------------------------------------------- #
def get_es():
    """Wrapper lazy/mockável do cliente ES (mesmo padrão do agg_overview)."""
    from search.client import get_es as _get_es
    return _get_es()


def index_name(suffix: str) -> str:
    from django.conf import settings
    return f'{settings.ELASTICSEARCH_INDEX_PREFIX}-{suffix}'


def _run_msearch(bodies: list) -> list:
    """1 round-trip HTTP, N sub-buscas executadas em paralelo pelo ES."""
    es = get_es()
    payload: list = []
    for b in bodies:
        payload.append({})
        payload.append(b)
    resp = es.msearch(index=index_name('processos'), body=payload)
    respostas = resp.get('responses') or []
    for r in respostas:
        if r.get('error'):
            raise RuntimeError(f'ES msearch: {r["error"]}')
    if len(respostas) != len(bodies):
        raise RuntimeError('ES msearch: resposta incompleta')
    return respostas


# --------------------------------------------------------------------------- #
# Parse das respostas
# --------------------------------------------------------------------------- #
def _total_hits(resp: dict) -> int:
    total = resp.get('hits', {}).get('total', {})
    v = total.get('value') if isinstance(total, dict) else total
    return v or 0


def _dc(aggs: dict, chave: str) -> int:
    return (aggs.get(chave) or {}).get('doc_count', 0)


def _cobertura_amostra(campo: str, docs_com_campo: int,
                       total_escopo: int, total_estado: int) -> dict:
    """Bloco de honestidade: sobre QUANTOS docs o gráfico foi realmente desenhado."""
    pct = round(100.0 * docs_com_campo / total_escopo, 1) if total_escopo else 0.0
    return {
        'campo': campo,
        'docs_com_o_campo': docs_com_campo,
        'total_do_escopo': total_escopo,
        'total_do_estado': total_estado,
        'pct': pct,
    }


def _bloco_ano(buckets: list, preenchidos: int, total_escopo: int,
               total_estado: int) -> dict:
    """Série temporal por `ano_cnj`, saneada.

    O ES devolve ano por CONTAGEM; entregamos ordenado por ANO. Anos fora da
    janela plausível [1990, ano_atual+1] são LIXO de CNJ malformado (medido em
    SP, 12/08: buckets em 9400/9500/9700/9900) — não entram na série, viram
    `fora_da_faixa` pro front poder mostrar o resíduo sem distorcer o eixo X.
    """
    ano_teto = _ano_atual() + 1
    itens, fora = [], 0
    for b in buckets:
        ano = b['key']
        if isinstance(ano, float):
            ano = int(ano)
        if ANO_MIN_PLAUSIVEL <= ano <= ano_teto:
            itens.append({'ano': ano, 'volume': b['doc_count']})
        else:
            fora += b['doc_count']
    itens.sort(key=lambda x: x['ano'])
    somados = sum(i['volume'] for i in itens)
    return {
        'itens': itens,
        'outros': max(0, preenchidos - somados - fora),
        'sem_dado': max(0, total_escopo - preenchidos),
        'fora_da_faixa': fora,
        'cobertura_amostra': _cobertura_amostra('ano_cnj', preenchidos,
                                                total_escopo, total_estado),
    }


def _bloco_terms(buckets: list, chave: str, preenchidos: int,
                 sem_dado: int, campo: str, total_escopo: int,
                 total_estado: int, top_n: int = TOP_N) -> dict:
    itens = [{chave: b['key'], 'volume': b['doc_count']} for b in buckets[:top_n]]
    somados = sum(i['volume'] for i in itens)
    return {
        'itens': itens,
        # cauda = preenchidos − top exibido (não usamos sum_other_doc_count: com
        # `exclude` ele não é comparável ao denominador que queremos)
        'outros': max(0, preenchidos - somados),
        'sem_dado': sem_dado,
        'cobertura_amostra': _cobertura_amostra(campo, preenchidos,
                                                total_escopo, total_estado),
    }


def _metricas_do_bucket(b: dict) -> dict:
    """Métricas de um bucket (mesma semântica de `agg_overview._parse_buckets`)."""
    sinal_conhecido = _dc(b, 'sinal_conhecido')
    possiveis = _dc(b, 'potencial')
    return {
        'volume': b.get('doc_count', 0),
        'valor': round((b.get('valor') or {}).get('value') or 0.0, 2),
        # sinal nunca computado ⇒ DESCONHECIDO (null), não zero
        'possiveis': possiveis if sinal_conhecido else None,
        'sinal_processado': bool(sinal_conhecido),
        'confirmados': _dc(b, 'confirmado'),
        'todos': _dc(b, 'todos'),
    }


def _nota_entidade(participacoes_docs: int, flag_processada: bool,
                   truncado: bool) -> str:
    if not flag_processada:
        return ('A flag de ente público no polo passivo ainda não foi computada '
                'neste estado — ausência aqui NÃO significa ausência de devedor '
                'público.')
    if not participacoes_docs:
        return ('Nenhuma participação (parte) indexada neste recorte: o índice de '
                'partes está parcial. Vazio = "ainda não indexamos", não '
                '"não há ente público".')
    base = ('Amostra: só os processos com partes já indexadas. Grafias da mesma '
            'entidade foram fundidas por normalização; advogados/procuradorias '
            'do polo passivo (que representam, não devem) e nomes que não são '
            'ente público foram descartados.')
    if truncado:
        base += (f' Ranking derivado do top-{ENTIDADES_TERMS_SIZE} do índice — '
                 'entidade de cauda longa pode faltar.')
    return base


# --------------------------------------------------------------------------- #
# API pública do serviço
# --------------------------------------------------------------------------- #
def agg_estado(uf: str, filtros: dict | None = None,
               metrica: str = METRICA_DEFAULT) -> dict:
    """Payload completo da página do estado. 1 request ES (msearch de 2 sub-buscas).

    Levanta `UfInvalida` (→ 400 na view) pra UF fora do conjunto válido; qualquer
    falha de ES sobe como exceção (→ 503 na view). Cache curto por
    (uf + metrica + filtros), no padrão do `agg_overview`.
    """
    uf = normalizar_uf(uf)
    metrica = normalizar_metrica(metrica)
    filtros = {k: v for k, v in (filtros or {}).items() if k != 'uf'}

    cache_key = cache_key_estado(uf, metrica, filtros)
    ttl = CACHE_TTL_FILTRADO if filtros else CACHE_TTL
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    resp_estado, resp_escopo = _run_msearch([
        build_body_estado(uf, filtros),
        build_body_escopo(uf, filtros, metrica),
    ])

    a_estado = resp_estado.get('aggregations') or {}
    a_escopo = resp_escopo.get('aggregations') or {}
    total_estado = _total_hits(resp_estado)
    total_escopo = _total_hits(resp_escopo)

    # ---- resumo (números do ESTADO; independem da lente) -------------------
    sinal_conhecido = _dc(a_estado, 'sinal_conhecido')
    tribunais = [b['key'] for b in
                 (a_estado.get('tribunais') or {}).get('buckets', [])]
    resumo = {
        'volume': total_estado,
        'possiveis': _dc(a_estado, 'potencial') if sinal_conhecido else None,
        'sinal_processado': bool(sinal_conhecido),
        'confirmados': _dc(a_estado, 'confirmado'),
        'todos': _dc(a_estado, 'todos'),
        'valor': round((a_estado.get('valor') or {}).get('value') or 0.0, 2),
        'valor_escopo': round((a_escopo.get('valor') or {}).get('value') or 0.0, 2),
        'cobertura_pct': _cobertura_por_uf().get(uf),
        'tribunais': len(tribunais),
        'tribunais_siglas': tribunais,
        # A FONTE deste estado publica valor da causa? Sem isto, a página diz
        # "valor não informado" e o usuário entende "o tribunal não informou" —
        # quando o PJe consulta pública não expõe o campo em tribunal nenhum
        # (medido em 27 processos reais, 13/08/2026). `false` = definitivo, não
        # há o que buscar; `true` e vazio = a lacuna é nossa.
        'fonte_publica_valor': uf_tem_fonte_de_valor(uf),
        'tribunais_que_publicam': sorted(t for t in tribunais
                                         if fonte_publica_valor(t)),
        'escopo_volume': total_escopo,
        'cobertura_valor': _cobertura_amostra(
            'valor_causa', _dc(a_estado, 'valor_conhecido'),
            total_estado, total_estado),
    }

    # ---- blocos (dentro da lente) ------------------------------------------
    classe_preenchida = _dc(a_escopo, 'classe_preenchida')
    por_tipo = _bloco_terms(
        (a_escopo.get('por_classe') or {}).get('buckets', []), 'tipo',
        classe_preenchida, max(0, total_escopo - classe_preenchida),
        'classe_nome', total_escopo, total_estado)

    por_ano = _bloco_ano((a_escopo.get('por_ano') or {}).get('buckets', []),
                         _dc(a_escopo, 'ano_preenchido'), total_escopo, total_estado)

    classif_preenchida = _dc(a_escopo, 'classificacao_preenchida')
    por_classificacao = _bloco_terms(
        (a_escopo.get('por_classificacao') or {}).get('buckets', []),
        'classificacao', classif_preenchida,
        max(0, total_escopo - classif_preenchida),
        'classificacao', total_escopo, total_estado)

    cob_trib = _cobertura_por_tribunal()
    itens_trib = []
    for b in (a_escopo.get('por_tribunal') or {}).get('buckets', []):
        item = {'tribunal': b['key']}
        item.update(_metricas_do_bucket(b))
        item['cobertura_pct'] = cob_trib.get(b['key'])
        itens_trib.append(item)
    por_tribunal = {
        'itens': itens_trib,
        'outros': max(0, total_escopo - sum(i['volume'] for i in itens_trib)),
        'cobertura_amostra': _cobertura_amostra(
            'tribunal', total_escopo, total_escopo, total_estado),
    }

    ent_filter = a_escopo.get('entidades') or {}
    ent_nested = ent_filter.get('nested') or {}
    nomes = ((ent_nested.get('passivo') or {}).get('nomes') or {}).get('buckets', [])
    fund = fundir_entidades(nomes)
    participacoes_docs = _dc(a_escopo, 'participacoes_presentes')
    flag_processada = bool(_dc(a_escopo, 'ente_flag_conhecida'))
    truncado = len(nomes) >= ENTIDADES_TERMS_SIZE
    por_entidade = {
        'itens': fund['itens'],
        'cobertura_amostra': _cobertura_amostra(
            'participacoes', participacoes_docs, total_escopo, total_estado),
        'docs_com_ente_publico': ent_filter.get('doc_count', 0),
        'flag_processada': flag_processada,
        'buckets_brutos': fund['buckets_brutos'],
        'buckets_fundidos': fund['buckets_fundidos'],
        'descartados_nao_ente': fund['descartados_nao_ente'],
        'descartados_nao_ente_volume': fund['descartados_nao_ente_volume'],
        'aceitos_por_complemento': fund['aceitos_por_complemento'],
        # participações do polo passivo que saíram por serem advogado/procuradoria
        'advogados_excluidos': _dc(ent_nested, 'advogados'),
        'truncado': truncado,
        'nota': _nota_entidade(participacoes_docs, flag_processada, truncado),
    }

    payload = {
        'uf': uf,
        'uf_nome': UF_NOME.get(uf, uf),
        'metrica': metrica,
        'filtros': {**filtros, 'uf': uf},
        'gerado_em': _agora_iso(),
        'resumo': resumo,
        'por_tipo_processo': por_tipo,
        'por_ano': por_ano,
        'por_tribunal': por_tribunal,
        'por_classificacao': por_classificacao,
        'por_entidade_devedora': por_entidade,
    }
    cache.set(cache_key, payload, ttl)
    return payload
