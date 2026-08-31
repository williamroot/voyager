"""Agregações das telas de ENTIDADES ("quem deve") — 100% Elasticsearch.

Duas telas, dois serviços:

    ranking_entidades()  →  "quem mais litiga no Brasil" (lista ordenável do
                            índice canônico `voyager-entidades*`)
    ficha_entidade()     →  a página de UMA entidade (os processos dela em
                            `voyager-processos`, explodidos por UF/tribunal/
                            ano/classificação)

Nada aqui toca o Postgres. O ranking lê só o índice de entidades (1,14M docs
pequenos); a ficha lê o índice de processos (77,8M docs) com o MESMO OR de
`match_phrase` que produziu o `n_processos` gravado no cadastro — é o que faz o
número da lista bater com o número da ficha.

Reusa de `search.entidades` (o dono do cadastro): `grafias_para_contagem`
(poda de over-match), `query_variantes` (o OR), `funcoes_prevalencia` (o ranking
calibrado do autocomplete). Reusa de `search.agg_overview`: `parse_filtros`,
`build_filter_clauses`, `_metric_subaggs`. Reusa de `search.agg_estado`:
`_cobertura_amostra`, `_bloco_terms`, `_bloco_ano`, `_total_hits`, `_dc`. Mesma
semântica das outras telas do módulo comercial, zero divergência de número.

================================================================================
CONTRATO JSON — 1. GET /dashboard/api/entidades/     (RANKING)
================================================================================
Login obrigatório (`@login_required`), `@require_GET`, `application/json`.
Cursor inválido ⇒ **400** `{"erro": ...}`. ES fora ⇒ **503**. Nunca 500 cru.

QUERYSTRING
-----------
    q            texto     busca por nome/grafia (mín. 3 chars; abaixo disso é
                           IGNORADA e a lista volta global, com o motivo no
                           bloco `busca` — a tela não fica muda)
    ente         0|1       só ente público (`eh_ente_publico=true`)
    min_partes   int       atestação mínima: `n_partes >= N` (0..1000, default 0)
    contadas     0|1       só entidades que já foram CONTADAS (`n_processos`
                           existe). Ver "AUSENTE ≠ ZERO" abaixo
    ordenar      enum      n_processos (default) | n_partes | n_variantes |
                           nome | relevancia  (`relevancia` sem `q` cai no
                           default — ordenar por relevância de nada é nada)
    n | tamanho  int        itens por página (1..100, default 20; os dois nomes
                           valem, `n` ganha se vierem juntos)
    cursor       opaco     `proximo_cursor` da página anterior (search_after)
    offset       int       alternativa ao cursor pra pular páginas (0..10.000)

    ⚠️ NÃO EXISTE FILTRO POR UF. Não é esquecimento — ver a seção
       "POR QUE O RANKING NÃO FILTRA POR UF" no fim deste docstring.

RESPOSTA
--------
{
  "ordenar": "n_processos",
  "tamanho": 20,
  "total": 1140972,           // int — entidades que passam nos filtros (exato)
  "indice": "voyager-entidades-teste",
  "gerado_em": "2026-08-13T00:00:00+00:00",

  "busca": {                  // o que fizemos com o `q`
    "termo": "inss",
    "aplicada": true,
    "motivo": null,           // "sem_termo" | "termo_curto" quando aplicada=false
    "min_caracteres": 3
  },
  "filtros": {"ente": false, "min_partes": 0, "somente_contadas": false},

  "cobertura_contagem": {     // <COBERTURA_AMOSTRA> — quantas entidades DESTA
                              // lista têm contagem de processos medida
    "campo": "n_processos", "docs_com_o_campo": 182026,
    "total_do_escopo": 1140972,   // entidades que passam nos filtros
    "total_do_universo": 1141610, // o índice inteiro (agg `global`)
    "total_do_indice": 1141610,   // idem, com nome explícito
    "pct": 16.0                   // 100 × docs_com_o_campo / total_do_escopo
  },

  "itens": [{
    "entidade_id": "cnpj:29979036",
    "nome_canonico": "INSTITUTO NACIONAL DO SEGURO SOCIAL",
    "chave": "cnpj",          // 'cnpj' = identidade PROVADA | 'nome' = heurística
    "confianca": "forte",     // "forte" (chave=cnpj) | "fraca" (chave=nome)
    "raiz_cnpj": "29979036",
    "eh_ente_publico": true,
    "n_partes": 768,          // linhas de cadastro fundidas = ATESTAÇÃO
    "n_variantes": 104,
    "grafias_exemplo": ["INSTITUTO NACIONAL DO SEGURO SOCIAL", ...],  // até 3
    "documentos": ["..."],    // até 3 (o INSS tem 650)
    "n_documentos": 650,
    "n_documentos_secundarios": 4,   // CNPJs que o tribunal digitou ERRADO
    "n_processos": 4402239,   // int|null — null = NÃO CONTAMOS (nunca 0)
    "n_processos_em": "2026-08-12T23:26:57+00:00",   // idade do número
    "contagem": "medida",     // "medida" | "nao_contada"
    "nome_suspeito": false
  }],

  "cursor": null,             // eco do cursor recebido
  "proximo_cursor": "eyJvIjp...",  // null quando acabou
  "cursor_proximo": "eyJvIjp...",  // ALIAS de compatibilidade (mesmo valor) —
                                   // a 1ª versão do front leu este nome; use
                                   // `proximo_cursor`, este some depois
  "tem_mais": true,
  "offset": 0,
  "offset_maximo": 10000,     // acima disso, só por cursor (deep paging custa)
  "exclusoes": {...},         // o que NÃO está na lista, e por quê
  "nota": "..."               // caveat pronto pra exibir
}

================================================================================
CONTRATO JSON — 2. GET /dashboard/api/entidades/<entidade_id>/     (FICHA)
================================================================================
`<entidade_id>` é o `_id` do índice: `cnpj:29979036` ou `nome:<sha1[:20]>`.
Entidade inexistente ⇒ **404** `{"erro": ...}`. ES fora ⇒ **503**. Nunca 500 cru.

QUERYSTRING — os MESMOS filtros do mapa (`agg_overview.parse_filtros`):
`uf, tribunal, classificacao, tipo(potencial|confirmado), tem_sinal, ano_min,
ano_max, valor_min, valor_max, codigo_classe, natureza`.
`parte` e `entidade_id` da querystring são IGNORADOS — quem manda é a rota.
(Aqui o filtro por UF EXISTE e é barato: é 1 cláusula na MESMA query. O que não
existe é filtrar o RANKING por UF — de novo, ver a seção do fim.)

RESPOSTA
--------
{
  "entidade": {               // o cadastro, como está no índice canônico
    "entidade_id", "nome_canonico", "chave", "confianca", "raiz_cnpj",
    "eh_ente_publico", "ente_publico_por_complemento", "tipo",
    "n_partes", "n_variantes", "grafias": [...até 8...],
    "documentos": [...até 8...], "n_documentos",
    "documentos_secundarios": [...],      // AUDITORIA: CNPJs errados fundidos
    "n_documentos_secundarios", "entidades_absorvidas": [...],
    "grupos_absorvidos", "nome_suspeito", "nome_suspeito_motivo",
    "n_processos", "n_processos_em", "atualizado_em", "parte_id_min"
  },
  "filtros": {...eco dos filtros saneados...},
  "gerado_em": "...",

  "resumo": {
    "processos": 4402239,          // int — processos DENTRO dos filtros (agora)
    "processos_entidade": 4402239, // int — a entidade INTEIRA (sem filtros)
    "possiveis": 165790,           // int|null — tem_sinal_precatorio=true;
                                   //   null = o sinal nunca foi computado aqui.
                                   //   MESMO nome do mapa/página de estado
    "sinal_processado": true,
    "tribunais": 39,               // int — tribunais distintos no escopo
    "tribunais_siglas": ["TRF3", ...],
    "cobertura_pct": 12.3,         // float|null — % validado, ponderado pelos
                                   //   processos da entidade em cada tribunal
                                   //   (cache do warm; null = sem dado)
    "confirmados": 18765,          // classificacao=PRECATORIO
    "todos": 184037,               // UNIÃO possíveis ∪ confirmados (NUNCA soma)
    "valor": 6092037156.0,         // float — sum(valor_causa) no escopo
    "cobertura_valor": <COBERTURA_AMOSTRA>,   // ~1,5% no INSS: é AMOSTRA
    "cobertura_sinal": <COBERTURA_AMOSTRA>,   // ~3,8% no INSS: é PISO, não share
    "n_processos_indice": 4402239, // o número GRAVADO no cadastro (envelhece)
    "n_processos_em": "...",
    "divergencia_contagem": 0      // int|null — agora − gravado (crescimento)
  },

  "por_uf":            {"itens": [{"uf": "FED", "volume": 4212666}, ...], ...},
  "por_tribunal":      {"itens": [{"tribunal": "TRF3", "volume": 1842369}, ...]},
  "por_ano":           {"itens": [{"ano": 2026, "volume": 815483}, ...],
                        "fora_da_faixa": 1745},
  "por_classificacao": {"itens": [{"classificacao": "NAO_LEAD", ...}], ...},
      // todos no formato de bloco do agg_estado:
      // {itens, outros, sem_dado, cobertura_amostra}

  "consulta": {               // COMO estes números foram obtidos (auditoria)
    "campo": "partes", "metodo": "match_phrase_or",
    "grafias": [...],         // o OR exato que rodou
    "n_grafias": 3,
    "grafias_descartadas": 0, // podadas por over-match (grafias_para_contagem)
    "truncado": false         // bateu o teto de cláusulas do OR
  },
  "nota": "..."
}

<COBERTURA_AMOSTRA> — honestidade obrigatória. Mesma CONTA do `agg_estado`
(uma fonte só), com o terceiro número renomeado porque aqui ele não é o estado:
    {
      "campo": "valor_causa",
      "docs_com_o_campo": 64527,   // docs do ESCOPO que alimentam o bloco
      "total_do_escopo": 4402239,  // denominador do bloco (com os filtros)
      "total_do_universo": 4402239,// contexto: a ENTIDADE inteira, sem filtros
      "pct": 1.5                   // 100 × docs_com_o_campo / total_do_escopo
    }
O front DEVE escrever "amostra de X% dos processos" quando `pct < 100`.

--------------------------------------------------------------------------------
LIMITAÇÕES MEDIDAS (13/08/2026) — o front PRECISA comunicar
--------------------------------------------------------------------------------
1. AUSENTE ≠ ZERO. Só **182.026 de 1.141.610** entidades (16,0%) têm
   `n_processos`; as outras 959 mil NUNCA foram contadas (o escopo da contagem é
   deliberadamente parcial — ver `entidades.escopo_contagem`). Na lista isso vem
   como `n_processos: null` + `contagem: "nao_contada"`, e a UI tem que escrever
   **"não contamos ainda"**. Escrever 0 seria afirmar que a entidade não tem
   processo — uma afirmação que não medimos.

2. A CONTAGEM É POR FRASE DO NOME, NÃO POR VÍNCULO ESTRUTURADO. O número sai de
   um OR de `match_phrase` das grafias contra o campo TEXTO `partes` (presente em
   100% dos 77,8M docs), porque o nested `participacoes` — a forma estruturalmente
   correta — está indexado em ~2% da base. Consequência: uma entidade de nome
   genérico reivindica processos que podem não ser dela. Por isso vão no payload,
   sempre, os dois sinais de confiança:
     * `chave`/`confianca` — `cnpj` é identidade PROVADA por documento; `nome` é
       heurística de grafia (dois municípios homônimos colapsam);
     * `n_partes` — quantas vezes, independentemente, o tribunal cadastrou essa
       entidade. É a ATESTAÇÃO. `n_partes=1` com 1,2 milhão de processos é um
       nome promíscuo, não um litigante gigante.

3. O TOPO DA LISTA É UM CARDUME DE FACETAS DA MESMA ENTIDADE. Medido no top-20
   real por `n_processos` (13/08): o INSS aparece **8 vezes** (a autarquia com 768
   linhas de cadastro, "INSS" com 53, "CEAB - INSS" com 4, "Procuradoria da
   CEAB-DJ INSS" com 1...) e a União 3 vezes — incluindo dois docs `cnpj:` com
   `n_partes=1` e exatamente o mesmo `n_processos` (1.232.679), que são o mesmo
   devedor pendurado em CNPJs diferentes. Isto NÃO é bug deste serviço: é o
   cadastro. A ferramenta honesta que damos ao front é `min_partes` — medido,
   `min_partes=10` derruba a lista de 1,14M pra 1.588 entidades e limpa metade das
   facetas (mas não todas: "INSS" 53 e "INSTITUTO NACIONAL...INSS" 22 sobrevivem).
   A UI deve dizer que a lista ranqueia NOMES, não pessoas jurídicas consolidadas.

4. `valor_causa` é ESPARSO (2,8% da base global; 1,5% dentro do INSS) e é o valor
   DA CAUSA, não o valor do precatório (esse vive no Falcon). Nunca apresentar o
   somatório como "o quanto a entidade deve". Ver `cobertura_valor`.

5. `tem_sinal_precatorio` NÃO cobre a base inteira, e `com_sinal` é um **piso**,
   nunca uma proporção: "165.790 de 4.402.239 (3,8%)" é uma leitura ERRADA do
   número. Quem diz o tamanho do piso é `cobertura_sinal`, que é computado a cada
   resposta — **não repita um número fixo aqui**, ele envelhece. Medido em
   31/08/2026: 79,2% da base tem o sinal computado (82.365.167 de 104.003.151;
   3,6% TRUE), contra os 3,4% de quando este texto foi escrito. Restam 21,6 M
   NULL, dos quais o TJSP era 1,5 M — ver `.ia/ACERVO_CNJ.md`.
   Ver também `sinal_processado`.

6. `documentos_secundarios` são CNPJs que o tribunal digitou ERRADO e que nós
   fundimos nesta entidade (decisão 12 de `search/entidades.py`). Vão no payload
   inteiros, de propósito: é auditoria — alguém vai perguntar por que a busca por
   aquele CNPJ cai aqui. Idem `entidades_absorvidas` (o id de cada entidade
   engolida; a fusão é determinística e reversível).

7. `nome_suspeito=true` (635 entidades) NÃO entra na lista: "JOSÉ" (2 linhas de
   cadastro) casava 1.796.174 processos e "MUNICIPIO DE" 467.493 — cadastro
   truncado com cara de devedor gigante. Fora também as 3 entidades cujo nome é
   só um marcador de papel ("(REQUERIDO(A))"), que têm `nome_normalizado` vazio.

8. O ranking é `search_after` (cursor). `offset` existe pra saltar página, mas
   com teto de 10.000: medido, `from=9980` custa 431ms contra ~80ms constantes do
   cursor. Acima do teto, só cursor.

--------------------------------------------------------------------------------
POR QUE O RANKING NÃO FILTRA POR UF (decisão medida, não preguiça)
--------------------------------------------------------------------------------
O índice de entidades NÃO tem UF, e não tem por um motivo estrutural: entidade
não mora num estado — os PROCESSOS dela é que estão espalhados (o INSS: FED
4.212.666 · MG 77.668 · SP 32.513 · ...). Filtrar/ordenar entidade por UF exige
CONTAR, por UF, o OR de cada entidade contra `voyager-processos`.

O custo, medido em 13/08/2026 contra o ES de prod:
  * uma página de 20 entidades com contagem por UF = **231ms** (msearch de 20
    sub-buscas, 17ms repetida). Barato — mas ERRADO: reordenar as 20 primeiras
    de um ranking GLOBAL não é o ranking de SP. Quem é #1 em RR pode estar em
    #40.000 na lista global e nunca entrar na página;
  * o ranking CORRETO por UF exigiria contar as 182.026 entidades já contadas
    (não as 1,14M) uma vez por UF: 182.026/20 × 231ms ≈ **35 min por UF**, ×28
    UFs ≈ **16 horas** de carga no ES que serve a dashboard de prod — e o
    resultado teria de virar campo persistido (`n_processos_uf`), com um novo
    ciclo de recontagem, não parâmetro de request.

Então NÃO oferecemos `uf` no ranking. Quem quer "quem deve neste estado" já tem
a resposta pronta e barata na página de estado:
`GET /dashboard/api/overview/estado/<uf>/` → bloco `por_entidade_devedora`
(agregação nested no polo passivo). Ela tem outro limite, declarado lá: o nested
`participacoes` é parcial. São dois recortes honestos e diferentes — o front deve
mandar o usuário pra lá em vez de fingir um filtro que a gente não mediu.

A FICHA, essa sim, aceita `uf` (e todos os filtros do mapa): ali é 1 cláusula na
mesma query, custo ~zero.

--------------------------------------------------------------------------------
CACHE (chaves exatas — `delete_pattern` NÃO existe neste Redis; ver .ia/OPS.md)
--------------------------------------------------------------------------------
    comercial:agg:entidades:rank:v1:<md5(json das opções)>     TTL 300s (120s
                                                               com q/filtro)
    comercial:agg:entidades:ficha:<entidade_id>:v1             TTL 300s
    comercial:agg:entidades:ficha:<entidade_id>:f:<md5>        TTL 120s (filtrado)
    comercial:agg:entidades:doc:<md5(entidade_id)>             TTL 3600s (cadastro)
Pra invalidar, monte a chave na mão e use `cache.delete(...)`. NUNCA
`cache.clear()` em prod.
"""
import base64
import binascii
import hashlib
import json
import logging

from django.core.cache import cache

from search import entidades as ent
from search.geo import fonte_publica_valor
from search.agg_overview import (
    _agora_iso,
    _metric_subaggs,
    build_filter_clauses,
    parse_filtros,  # noqa: F401 — reexport: a ficha usa o MESMO parser do mapa
)
from search.agg_estado import (
    _bloco_ano,
    _bloco_terms,
    _cobertura_amostra,
    _dc,
    _total_hits,
)

logger = logging.getLogger('voyager.comercial.agg_entidade')

# --------------------------------------------------------------------------- #
# Constantes
# --------------------------------------------------------------------------- #
#: ordenações aceitas pelo ranking. `relevancia` só faz sentido com `q` — sem
#: termo ela cai no default (ordenar por relevância "de nada" é ordenar por nada).
ORDENACOES = ('n_processos', 'n_partes', 'n_variantes', 'nome', 'relevancia')
ORDENAR_DEFAULT = 'n_processos'

#: campo do índice por ordenação. `nome` ordena por `nome_normalizado` (keyword
#: MAIÚSCULA sem acento) e não por `nome_canonico.raw`: o `.raw` é
#: case/acento-sensível e faria "Álvaro" cair depois de "Zebra".
_CAMPO_ORDENACAO = {
    'n_processos': 'n_processos',      # prevalência MEDIDA
    'n_partes': 'n_partes',            # atestação (linhas de cadastro fundidas)
    'n_variantes': 'n_variantes',      # bagunça de grafia do cartório
    'nome': 'nome_normalizado',
}
#: `nome` é a única ordenação crescente (A→Z); prevalência e atestação são desc.
_ORDEM = {'nome': 'asc'}

RANKING_TAMANHO_DEFAULT = 20
RANKING_TAMANHO_MAX = 100
#: teto do `offset`. Medido 13/08: `from=9980` custa 431ms contra ~80ms
#: constantes do `search_after`. Acima disso a paginação é por cursor.
OFFSET_MAX = 10000
#: mínimo de caracteres pra `q` tocar o ES (mesmo critério do autocomplete):
#: 1-2 letras casam centenas de milhares das 1,14M entidades.
BUSCA_MIN_CARACTERES = 3
#: teto de `min_partes` — acima disso a lista fica vazia e não há caso de uso
MIN_PARTES_MAX = 1000

#: quantos itens de lista cada bloco do ranking mostra por entidade
_MAX_GRAFIAS_ITEM = 3
_MAX_DOCUMENTOS_ITEM = 3
#: na FICHA a amostra é maior (a tela tem espaço e o usuário está auditando)
_MAX_GRAFIAS_FICHA = 8
_MAX_DOCUMENTOS_FICHA = 8

#: tetos de terms na ficha. UF (28) e tribunal (~40) são de cardinalidade baixa:
#: pedimos TODOS os buckets, não um top-N — o front desenha a distribuição
#: inteira e `outros` fica 0.
_UF_TERMS_SIZE = 40
_TRIBUNAIS_TERMS_SIZE = 60
_ANOS_TERMS_SIZE = 100
_CLASSIF_TERMS_SIZE = 20

#: TTLs no padrão do agg_overview/agg_estado
CACHE_TTL = 300
CACHE_TTL_FILTRADO = 120
#: o cadastro da entidade só muda no rebuild do índice (raro) — 1h é folgado
TTL_ENTIDADE = 3600
_CACHE_PREFIX = 'comercial:agg:entidades'


class EntidadeNaoEncontrada(LookupError):
    """`entidade_id` que não existe no índice (a view traduz em 404)."""


class CursorInvalido(ValueError):
    """Cursor corrompido/forjado (a view traduz em 400)."""


# --------------------------------------------------------------------------- #
# Cliente ES (wrappers lazy/mockáveis — mesmo padrão do agg_overview)
# --------------------------------------------------------------------------- #
def get_es():
    from search.client import get_es as _get_es
    return _get_es()


def index_name(suffix: str) -> str:
    from django.conf import settings
    return f'{settings.ELASTICSEARCH_INDEX_PREFIX}-{suffix}'


def indice_entidades() -> str:
    """Índice de entidades ATIVO (hoje ainda o de teste, via setting).

    Mesma fonte que `agg_overview._indice_entidades` e o autocomplete usam:
    promover o índice é trocar `ENTIDADES_INDICE_SUFIXO` no ambiente, sem deploy.
    """
    from django.conf import settings
    sufixo = getattr(settings, 'ENTIDADES_INDICE_SUFIXO', ent.INDICE)
    return index_name(sufixo)


def _search(indice: str, body: dict) -> dict:
    return get_es().search(index=indice, body=body)


# --------------------------------------------------------------------------- #
# Saneamento de entrada (nunca levanta, exceto cursor forjado)
# --------------------------------------------------------------------------- #
def _to_int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _flag(v) -> bool:
    return str(v if v is not None else '').strip().lower() in (
        '1', 'true', 'sim', 'yes', 'on')


def normalizar_ordenacao(valor, tem_busca: bool = True) -> str:
    """Enum desconhecido cai no default — nunca levanta.

    `relevancia` sem termo de busca também cai no default: ordenar 1,14M
    entidades por relevância de query vazia é ordenar por nada.
    """
    s = (valor or '').strip().lower()
    if s not in ORDENACOES:
        return ORDENAR_DEFAULT
    if s == 'relevancia' and not tem_busca:
        return ORDENAR_DEFAULT
    return s


def parse_ranking(qd) -> dict:
    """Querystring (request.GET ou dict) → opções saneadas do ranking.

    Nunca levanta: valor inválido é clampado ou descartado. O `cursor` sai daqui
    como string crua; quem valida é `decodificar_cursor` (que pode levantar
    `CursorInvalido` → 400 na view).
    """
    g = qd.get
    termo = ' '.join((g('q') or '').split())[:120]
    ordenar = normalizar_ordenacao(g('ordenar'),
                                   tem_busca=len(termo) >= BUSCA_MIN_CARACTERES)
    # `n` e `tamanho` são o MESMO parâmetro (o autocomplete do mapa já usa `n`;
    # a tela de lista chama de `tamanho`). Aceitar os dois custa uma linha e
    # evita uma página de 20 itens quando o front pediu 30.
    tamanho = _to_int(g('n'), None)
    if tamanho is None:
        tamanho = _to_int(g('tamanho'), RANKING_TAMANHO_DEFAULT)
    if tamanho is None:
        tamanho = RANKING_TAMANHO_DEFAULT
    tamanho = max(1, min(tamanho, RANKING_TAMANHO_MAX))
    min_partes = max(0, min(_to_int(g('min_partes'), 0) or 0, MIN_PARTES_MAX))
    offset = max(0, min(_to_int(g('offset'), 0) or 0, OFFSET_MAX))
    return {
        'q': termo,
        'ente': _flag(g('ente')),
        'min_partes': min_partes,
        'somente_contadas': _flag(g('contadas')),
        'ordenar': ordenar,
        'tamanho': tamanho,
        'cursor': (g('cursor') or '').strip()[:512] or None,
        'offset': offset,
    }


# --------------------------------------------------------------------------- #
# Cursor (search_after) — opaco pra fora, auditável pra dentro
# --------------------------------------------------------------------------- #
def codificar_cursor(ordenar: str, valores: list) -> str:
    """Valores de `sort` do último hit → cursor opaco (base64 de JSON).

    Opaco de propósito: o front não deve montar cursor à mão (o formato depende
    da ordenação escolhida), e um cursor de outra ordenação daria uma página
    silenciosamente errada. O `ordenar` vai DENTRO do cursor pra isso ser
    detectável.
    """
    bruto = json.dumps({'o': ordenar, 'v': list(valores)},
                       separators=(',', ':'), default=str)
    return base64.urlsafe_b64encode(bruto.encode('utf-8')).decode('ascii')


def decodificar_cursor(cursor: str, ordenar: str) -> list:
    """Cursor → valores de `search_after`. Levanta `CursorInvalido` (→ 400).

    Falhar alto aqui é de propósito: cursor corrompido devolveria uma página
    aleatória, e "a lista pulou 300 entidades" é um bug que ninguém consegue
    reproduzir. Melhor 400 explícito.
    """
    try:
        dados = json.loads(base64.urlsafe_b64decode(cursor.encode('ascii')))
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise CursorInvalido('cursor inválido') from exc
    if not isinstance(dados, dict):
        raise CursorInvalido('cursor inválido')
    if dados.get('o') != ordenar:
        raise CursorInvalido('cursor de outra ordenação')
    valores = dados.get('v')
    if not isinstance(valores, list) or not valores:
        raise CursorInvalido('cursor inválido')
    return valores


def _cursor_de(hit: dict, ordenar: str) -> str:
    return codificar_cursor(ordenar, hit.get('sort') or [])


# --------------------------------------------------------------------------- #
# Honestidade — `cobertura_amostra` com o vocabulário DESTA tela
# --------------------------------------------------------------------------- #
def _cobertura(campo: str, com_campo: int, escopo: int, universo: int) -> dict:
    """`agg_estado._cobertura_amostra` com o denominador renomeado.

    A conta é a mesma (uma fonte só, sem divergência de número entre as telas);
    só o RÓTULO do terceiro número muda: lá ele é "o estado inteiro", aqui é "a
    entidade inteira" — chamar isso de `total_do_estado` numa ficha de entidade
    seria pedir pro front escrever besteira.
    """
    bloco = _cobertura_amostra(campo, com_campo, escopo, universo)
    bloco['total_do_universo'] = bloco.pop('total_do_estado')
    return bloco


def _cobertura_ponderada(buckets_tribunal: list):
    """% enriquecido dos processos DESTA entidade, ponderado por tribunal.

    Reusa o mesmo cache de `dashboard.queries.cobertura_enriquecimento_data`
    que o mapa e a página de estado usam (via `agg_overview`). É leitura de
    cache — **nunca** recomputa e nunca toca o Postgres; cache frio ⇒ `None`
    ("não sabemos ainda"), que é o que a UI escreve.

    Ponderado e não média simples: 95,7% dos processos do INSS estão em 3 TRFs;
    uma média simples deixaria um tribunal com 12 processos pesar igual.
    """
    from search.agg_overview import _cobertura_por_tribunal
    try:
        mapa = _cobertura_por_tribunal()
    except Exception:                       # cache/Redis fora não derruba a ficha
        return None
    peso = coberto = 0.0
    for b in buckets_tribunal or []:
        pct = mapa.get(b.get('key'))
        if pct is None:
            continue
        volume = b.get('doc_count') or 0
        peso += volume
        coberto += volume * pct
    return round(coberto / peso, 1) if peso else None


def _renomear_cobertura(bloco: dict) -> dict:
    """Mesma renomeação, dentro dos blocos montados pelo `agg_estado`."""
    cob = bloco.get('cobertura_amostra') or {}
    if 'total_do_estado' in cob:
        cob['total_do_universo'] = cob.pop('total_do_estado')
    return bloco


# --------------------------------------------------------------------------- #
# Query do ranking
# --------------------------------------------------------------------------- #
#: campos do `_source` que a lista precisa (não trazemos `variantes` inteiro: o
#: INSS tem 104 grafias e 650 CNPJs — payload que ninguém lê × 20 itens)
FONTE_RANKING = [
    'entidade_id', 'nome_canonico', 'chave', 'raiz_cnpj', 'eh_ente_publico',
    'n_partes', 'n_variantes', 'variantes_busca', 'documentos', 'n_documentos',
    'n_documentos_secundarios', 'n_processos', 'n_processos_em', 'nome_suspeito',
]

#: campos do `_source` da FICHA (aqui sim vem a auditoria inteira)
FONTE_FICHA = [*FONTE_RANKING,
    'variantes', 'variantes_n', 'nome_normalizado', 'nome_suspeito_motivo',
    'documentos_secundarios', 'entidades_absorvidas', 'grupos_absorvidos',
    'tipo', 'ente_publico_por_complemento', 'atualizado_em', 'parte_id_min',
    'variantes_truncadas',
]


def query_texto(termo: str) -> dict:
    """Busca textual do RANKING — `operator: and`, sem rede de OR.

    Difere do `entidades.query_autocomplete` de propósito. Lá, digitar palavra a
    mais não pode zerar o dropdown, então existe uma variante `or` com boost
    baixo. Aqui a lista é ORDENADA POR CONTAGEM, e a rede de OR destrói o
    resultado: medido 13/08, `q="municipio de sao paulo"` ordenado por
    `n_processos` com a rede devolvia **INSTITUTO NACIONAL DO SEGURO SOCIAL INSS**
    em 1º (casava "de"/"sao" e ganhava no volume) sobre 368.905 "resultados". Com
    `and` estrito são 237 resultados e o 1º é MUNICIPIO DE SÃO PAULO.

    Casa em `nome_canonico` OU em `variantes_busca` (as grafias com peso) —
    quem digita "inss" tem que achar a entidade cujo nome canônico é "INSTITUTO
    NACIONAL DO SEGURO SOCIAL".
    """
    def _mm(campo):
        return {'multi_match': {
            'query': termo, 'type': 'bool_prefix',
            # reusa o helper do dono do índice (subcampos do search_as_you_type)
            'fields': ent._campos_autocomplete(campo),
            'operator': 'and',
        }}

    return {'bool': {
        'should': [_mm('nome_canonico.autocomplete'),
                   _mm('variantes_busca.autocomplete')],
        'minimum_should_match': 1,
    }}


def _exclusoes() -> list:
    """O que NUNCA entra na lista de "quem deve" (ver limitação 7).

    `must_not term` (e não `must_not exists`): documento SEM o campo — índice
    construído antes da decisão de `nome_suspeito` — CONTINUA na lista. A
    migração é aditiva, ninguém some por falta de campo.
    """
    return [
        {'term': {'nome_suspeito': True}},
        # nome que é só marcador de papel do PJe ("(REQUERIDO(A))"): sobra
        # string vazia na normalização. 3 no índice inteiro, todas com 1 linha
        # de cadastro — e todas apareceriam no TOPO da ordenação alfabética.
        {'term': {'nome_normalizado': ''}},
    ]


def build_body_ranking(opcoes: dict) -> dict:
    """Corpo ES do ranking. 1 request, `_source` enxuto, sort + search_after."""
    ordenar = opcoes['ordenar']
    filtros: list = []
    if opcoes.get('ente'):
        filtros.append({'term': {'eh_ente_publico': True}})
    if opcoes.get('min_partes'):
        filtros.append({'range': {'n_partes': {'gte': opcoes['min_partes']}}})
    if opcoes.get('somente_contadas'):
        filtros.append({'exists': {'field': 'n_processos'}})

    termo = opcoes.get('q') or ''
    busca_aplicada = len(termo) >= BUSCA_MIN_CARACTERES

    bool_query: dict = {'must_not': _exclusoes()}
    if filtros:
        bool_query['filter'] = filtros

    if ordenar == 'relevancia':
        # o texto vai em `must` (PONTUA); em `filter` o _score sairia 0 e o
        # function_score multiplicaria zero — medido, a lista voltava aleatória
        bool_query['must'] = [query_texto(termo)]
        query = {'function_score': {
            'query': {'bool': bool_query},
            # ranking calibrado do autocomplete: texto × prevalência × atestação
            'functions': ent.funcoes_prevalencia(),
            'score_mode': 'multiply', 'boost_mode': 'multiply',
        }}
        sort = ['_score', {'entidade_id': 'asc'}]
    else:
        if busca_aplicada:
            bool_query.setdefault('filter', []).append(query_texto(termo))
        query = {'bool': bool_query}
        campo = _CAMPO_ORDENACAO[ordenar]
        sort = [
            # AUSENTE vai pro FIM: "não contamos" não pode disputar o topo de
            # "quem mais litiga" — e também não pode virar 0 (ver limitação 1)
            {campo: {'order': _ORDEM.get(ordenar, 'desc'), 'missing': '_last'}},
            # desempate estável: sem ele, `search_after` pula/repete entidades
            # empatadas (e empate em `n_processos` é comum entre facetas)
            {'entidade_id': 'asc'},
        ]

    body = {
        'size': opcoes['tamanho'],
        # total EXATO: a tela mostra "N entidades" e o número é o produto aqui
        # (custo medido: 78-95ms sem filtro, 14-30ms com filtro)
        'track_total_hits': True,
        'query': query,
        'sort': sort,
        '_source': FONTE_RANKING,
        'aggs': {
            # quantas entidades DESTE recorte já foram contadas — é o
            # denominador honesto do "não contamos ainda" (1 filter agg)
            'contadas': {'filter': {'exists': {'field': 'n_processos'}}},
            # o índice inteiro, ignorando filtros (o outro denominador)
            'indice': {'global': {}},
        },
    }
    if opcoes.get('cursor'):
        body['search_after'] = decodificar_cursor(opcoes['cursor'], ordenar)
    elif opcoes.get('offset'):
        body['from'] = opcoes['offset']
    return body


# --------------------------------------------------------------------------- #
# Itens
# --------------------------------------------------------------------------- #
def _n_processos(fonte: dict):
    """`n_processos` do doc → int ou None. Ausente/não-numérico = DESCONHECIDO.

    Nunca 0 de enchimento: zero é uma afirmação ("o OR não achou processo") e
    ausência não é.
    """
    bruto = fonte.get('n_processos')
    if isinstance(bruto, bool) or not isinstance(bruto, int | float):
        return None
    return int(bruto)


def _confianca(chave: str) -> str:
    """`cnpj` = identidade PROVADA por documento; `nome` = heurística de grafia."""
    return 'forte' if chave == 'cnpj' else 'fraca'


def item_ranking(fonte: dict) -> dict:
    """Documento do índice → item da lista (o que a tela mostra/usa)."""
    documentos = [d for d in (fonte.get('documentos') or []) if d]
    grafias = fonte.get('variantes_busca') or []
    n = _n_processos(fonte)
    chave = fonte.get('chave') or ''
    return {
        'entidade_id': fonte.get('entidade_id'),
        'nome_canonico': fonte.get('nome_canonico') or '',
        'chave': chave,
        'confianca': _confianca(chave),
        'raiz_cnpj': fonte.get('raiz_cnpj'),
        'eh_ente_publico': bool(fonte.get('eh_ente_publico')),
        # ATESTAÇÃO: quantas linhas de cadastro o tribunal produziu. É o que
        # separa a autarquia (768) da faceta com o mesmo nome (1)
        'n_partes': fonte.get('n_partes'),
        'n_variantes': fonte.get('n_variantes'),
        'grafias_exemplo': grafias[:_MAX_GRAFIAS_ITEM],
        'documentos': documentos[:_MAX_DOCUMENTOS_ITEM],
        'n_documentos': fonte.get('n_documentos')
        if fonte.get('n_documentos') is not None else len(documentos),
        # CNPJs que o tribunal digitou errado e nós fundimos — a UI pode marcar
        'n_documentos_secundarios': fonte.get('n_documentos_secundarios') or 0,
        'n_processos': n,
        'n_processos_em': fonte.get('n_processos_em'),
        'contagem': 'medida' if n is not None else 'nao_contada',
        'nome_suspeito': bool(fonte.get('nome_suspeito')),
    }


def entidade_publica(fonte: dict) -> dict:
    """Documento do índice → bloco `entidade` da ficha (cadastro + auditoria)."""
    documentos = [d for d in (fonte.get('documentos') or []) if d]
    chave = fonte.get('chave') or ''
    return {
        'entidade_id': fonte.get('entidade_id'),
        'nome_canonico': fonte.get('nome_canonico') or '',
        'chave': chave,
        'confianca': _confianca(chave),
        'raiz_cnpj': fonte.get('raiz_cnpj'),
        'eh_ente_publico': bool(fonte.get('eh_ente_publico')),
        'ente_publico_por_complemento': bool(
            fonte.get('ente_publico_por_complemento')),
        'tipo': fonte.get('tipo') or '',
        'n_partes': fonte.get('n_partes'),
        'n_variantes': fonte.get('n_variantes'),
        'grafias': (fonte.get('variantes') or [])[:_MAX_GRAFIAS_FICHA],
        'documentos': documentos[:_MAX_DOCUMENTOS_FICHA],
        'n_documentos': fonte.get('n_documentos')
        if fonte.get('n_documentos') is not None else len(documentos),
        # AUDITORIA (vai inteiro): CNPJs que o tribunal digitou errado pra esta
        # entidade e que fundimos aqui, + os ids das entidades engolidas
        'documentos_secundarios': list(fonte.get('documentos_secundarios') or []),
        'n_documentos_secundarios': fonte.get('n_documentos_secundarios') or 0,
        'entidades_absorvidas': list(fonte.get('entidades_absorvidas') or []),
        'grupos_absorvidos': fonte.get('grupos_absorvidos') or 0,
        'nome_suspeito': bool(fonte.get('nome_suspeito')),
        'nome_suspeito_motivo': fonte.get('nome_suspeito_motivo') or '',
        'n_processos': _n_processos(fonte),
        'n_processos_em': fonte.get('n_processos_em'),
        'atualizado_em': fonte.get('atualizado_em'),
        'parte_id_min': fonte.get('parte_id_min'),
    }


# --------------------------------------------------------------------------- #
# Notas (caveat pronto pra exibir)
# --------------------------------------------------------------------------- #
def _mil(n: int) -> str:
    return f'{int(n):,}'.replace(',', '.')


def nota_ranking(contadas: int, total: int) -> str:
    faltam = max(0, total - contadas)
    base = ('A lista ranqueia NOMES de devedor, não pessoas jurídicas '
            'consolidadas: a mesma entidade aparece mais de uma vez quando o '
            'tribunal a cadastrou com CNPJs ou grafias diferentes (o INSS '
            'ocupa 8 das 20 primeiras posições). Use "atestação mínima" '
            '(min_partes) pra derrubar as facetas de 1 linha de cadastro.')
    if faltam:
        base += (f' {_mil(faltam)} entidades deste recorte ainda NÃO foram '
                 'contadas — aparecem no fim, com "não contamos ainda" '
                 '(nunca zero).')
    return base


def nota_ficha(entidade: dict, n_grafias: int) -> str:
    nota = (f'Contagem por FRASE do nome: união de {n_grafias} '
            f'grafia{"s" if n_grafias != 1 else ""} em `match_phrase` contra o '
            'campo de partes do processo — não é vínculo estruturado, então '
            'nome genérico pode reivindicar processo alheio.')
    if entidade.get('chave') != 'cnpj':
        nota += (' Esta entidade foi agrupada por NOME (sem CNPJ no dado do '
                 'tribunal): homônimos em cidades diferentes colapsam.')
    if (entidade.get('n_partes') or 0) <= 1:
        nota += (' Atestação baixa: o tribunal cadastrou este nome UMA vez — '
                 'trate o número como indício, não como medida.')
    if entidade.get('n_documentos_secundarios'):
        nota += (' Inclui CNPJs que o tribunal digitou errado e que fundimos '
                 'nesta entidade (ver documentos_secundarios).')
    return nota


# --------------------------------------------------------------------------- #
# 1. RANKING
# --------------------------------------------------------------------------- #
def _chave_cache(prefixo: str, dados: dict) -> str:
    bruto = json.dumps(dados, sort_keys=True, default=str)
    return f'{prefixo}:{hashlib.md5(bruto.encode()).hexdigest()}'


def ranking_entidades(filtros: dict | None = None,
                      ordenar: str | None = None,
                      tamanho: int | None = None,
                      cursor: str | None = None) -> dict:
    """Lista ordenável do cadastro de "quem deve". 1 request ES.

    `filtros` é o dict de `parse_ranking` (aceita também os argumentos soltos
    `ordenar`/`tamanho`/`cursor`, que têm precedência — é o que a view usa).
    Levanta `CursorInvalido` (→ 400); falha de ES sobe como exceção (→ 503).

    Contrato completo no topo do módulo.
    """
    opcoes = dict(filtros or {})
    termo = opcoes.get('q') or ''
    if ordenar is not None:
        opcoes['ordenar'] = normalizar_ordenacao(
            ordenar, tem_busca=len(termo) >= BUSCA_MIN_CARACTERES)
    if tamanho is not None:
        opcoes['tamanho'] = max(1, min(_to_int(tamanho, RANKING_TAMANHO_DEFAULT)
                                       or RANKING_TAMANHO_DEFAULT,
                                       RANKING_TAMANHO_MAX))
    if cursor is not None:
        opcoes['cursor'] = cursor or None
    opcoes.setdefault('ordenar', ORDENAR_DEFAULT)
    opcoes.setdefault('tamanho', RANKING_TAMANHO_DEFAULT)
    opcoes.setdefault('cursor', None)
    opcoes.setdefault('offset', 0)

    busca_aplicada = len(termo) >= BUSCA_MIN_CARACTERES
    motivo = None if busca_aplicada else ('sem_termo' if not termo else 'termo_curto')

    filtrado = bool(busca_aplicada or opcoes.get('ente')
                    or opcoes.get('min_partes') or opcoes.get('somente_contadas')
                    or opcoes.get('cursor') or opcoes.get('offset'))
    cache_key = _chave_cache(f'{_CACHE_PREFIX}:rank:v1', {
        **{k: opcoes.get(k) for k in
           ('q', 'ente', 'min_partes', 'somente_contadas', 'ordenar', 'tamanho',
            'cursor', 'offset')},
        # o índice entra na chave: promover `entidades-teste` → `entidades` não
        # pode servir 5 minutos de números do índice antigo
        'indice': indice_entidades(),
    })
    cacheado = cache.get(cache_key)
    if cacheado is not None:
        return cacheado

    body = build_body_ranking(opcoes)
    resp = _search(indice_entidades(), body)

    hits = (resp.get('hits') or {}).get('hits') or []
    total = _total_hits(resp)
    aggs = resp.get('aggregations') or {}
    contadas = _dc(aggs, 'contadas')
    # `global` agg: o tamanho do índice INTEIRO, ignorando os filtros — é o
    # denominador do "só 16% do cadastro foi contado" (custo desprezível: é o
    # doc_count do shard, não uma varredura)
    total_indice = _dc(aggs, 'indice') or total
    itens = [item_ranking(h.get('_source') or {}) for h in hits]

    tem_mais = len(hits) == opcoes['tamanho']
    proximo = (_cursor_de(hits[-1], opcoes['ordenar'])
               if hits and tem_mais else None)

    payload = {
        'ordenar': opcoes['ordenar'],
        'tamanho': opcoes['tamanho'],
        'total': total,
        'indice': indice_entidades(),
        'gerado_em': _agora_iso(),
        'busca': {
            'termo': termo,
            'aplicada': busca_aplicada,
            'motivo': motivo,
            'min_caracteres': BUSCA_MIN_CARACTERES,
        },
        'filtros': {
            'ente': bool(opcoes.get('ente')),
            'min_partes': opcoes.get('min_partes') or 0,
            'somente_contadas': bool(opcoes.get('somente_contadas')),
        },
        # honestidade: quantas entidades DESTE recorte têm contagem medida
        'cobertura_contagem': {
            **_cobertura('n_processos', contadas, total, total_indice),
            'total_do_indice': total_indice,
        },
        'itens': itens,
        'cursor': opcoes.get('cursor'),
        'proximo_cursor': proximo,
        # alias de compatibilidade: a 1ª versão do front leu `cursor_proximo`.
        # Mesmo valor, mesma coisa — some quando o front migrar pro nome acima.
        'cursor_proximo': proximo,
        'tem_mais': bool(proximo),
        'offset': opcoes.get('offset') or 0,
        'offset_maximo': OFFSET_MAX,
        'exclusoes': {
            'nome_suspeito': ('cadastro truncado que casa o país inteiro '
                              '("JOSÉ", "MUNICIPIO DE") — fora da lista'),
            'nome_vazio': ('nome que é só marcador de papel do PJe '
                           '("(REQUERIDO(A))") — fora da lista'),
            'uf': ('o ranking NÃO filtra por UF (entidade não tem UF; '
                   'o custo medido é ~16h de ES). Para "quem deve neste '
                   'estado", use /dashboard/api/overview/estado/<uf>/'),
        },
        'nota': nota_ranking(contadas, total),
    }
    cache.set(cache_key, payload,
              CACHE_TTL_FILTRADO if filtrado else CACHE_TTL)
    return payload


# --------------------------------------------------------------------------- #
# 2. FICHA
# --------------------------------------------------------------------------- #
def carregar_entidade(entidade_id: str) -> dict:
    """Cadastro da entidade (`_source` do índice). Cache 1h.

    `term` no campo `entidade_id` em vez de `GET /_doc/<id>` de propósito: o
    campo é idêntico ao `_id` (ver `entidades.grupo_to_doc`), a busca custa o
    mesmo, e assim "não achou" é uma lista vazia — não uma exceção do driver que
    obrigaria este módulo a acoplar no tipo `NotFoundError` do pacote
    `elasticsearch` (que os testes não instalam).
    """
    eid = (entidade_id or '').strip()[:200]
    if not eid:
        raise EntidadeNaoEncontrada('entidade_id vazio')
    chave = _chave_cache(f'{_CACHE_PREFIX}:doc',
                         {'id': eid, 'indice': indice_entidades()})
    fonte = cache.get(chave)
    if fonte is None:
        resp = _search(indice_entidades(), {
            'size': 1,
            'track_total_hits': False,
            'query': {'bool': {'filter': [{'term': {'entidade_id': eid}}]}},
            '_source': FONTE_FICHA,
        })
        hits = (resp.get('hits') or {}).get('hits') or []
        if not hits:
            raise EntidadeNaoEncontrada(eid)
        fonte = hits[0].get('_source') or {}
        cache.set(chave, fonte, TTL_ENTIDADE)
    return fonte


def grafias_da_consulta(fonte: dict) -> list:
    """As grafias que VÃO pro OR — exatamente as da contagem gravada.

    É `variantes_busca` (grafias com peso) passado por `grafias_para_contagem`
    (poda de over-match) e cortado no teto de cláusulas — a MESMA composição de
    `manage.py contar_processos_entidades`. Sem isso o total da ficha divergiria
    do `n_processos` que a lista mostra, e o usuário veria dois números
    diferentes pra mesma pergunta na mesma sessão.

    Fallback: entidade sem `variantes_busca` (índice antigo) usa o nome canônico.
    """
    grafias = [g for g in (fonte.get('variantes_busca') or []) if (g or '').strip()]
    if not grafias:
        nome = (fonte.get('nome_canonico') or '').strip()
        grafias = [nome] if nome else []
    ocorrencias = dict(zip(fonte.get('variantes') or [],
                           fonte.get('variantes_n') or [], strict=False))
    podadas = ent.grafias_para_contagem(grafias, ocorrencias)
    return podadas[:ent.MAX_CLAUSULAS_VARIANTES]


def build_body_ficha(fonte: dict, filtros: dict, com_aggs: bool = True) -> dict:
    """Corpo ES da ficha: OR das grafias + filtros do mapa + as agregações."""
    grafias = grafias_da_consulta(fonte)
    clausula_entidade = ent.query_variantes(grafias)['query']
    clauses = [clausula_entidade, *build_filter_clauses(filtros)]
    body = {
        'size': 0,
        # exato: é O número da tela. Sem isto o ES 8 pararia em 10.000 e o INSS
        # apareceria com "10.000+" — teto silencioso gravado como se fosse fato
        'track_total_hits': True,
        'query': {'bool': {'filter': clauses}},
    }
    if not com_aggs:
        return body

    aggs = dict(_metric_subaggs())          # valor/potencial/confirmado/todos/sinal
    aggs['valor_conhecido'] = {'filter': {'exists': {'field': 'valor_causa'}}}
    aggs['por_uf'] = {'terms': {'field': 'uf', 'size': _UF_TERMS_SIZE}}
    aggs['uf_conhecida'] = {'filter': {'exists': {'field': 'uf'}}}
    aggs['por_tribunal'] = {'terms': {'field': 'tribunal',
                                      'size': _TRIBUNAIS_TERMS_SIZE}}
    aggs['tribunal_conhecido'] = {'filter': {'exists': {'field': 'tribunal'}}}
    # série temporal: pede por CONTAGEM e ordena por ano no Python (com
    # `order:_key` o ES escolheria os N anos MENORES e cortaria os recentes)
    aggs['por_ano'] = {'terms': {'field': 'ano_cnj', 'size': _ANOS_TERMS_SIZE}}
    aggs['ano_preenchido'] = {'filter': {'exists': {'field': 'ano_cnj'}}}
    aggs['por_classificacao'] = {'terms': {'field': 'classificacao',
                                           'size': _CLASSIF_TERMS_SIZE,
                                           'exclude': ['']}}
    aggs['classificacao_preenchida'] = {'filter': {'bool': {
        'filter': [{'exists': {'field': 'classificacao'}}],
        'must_not': [{'term': {'classificacao': ''}}],
    }}}
    body['aggs'] = aggs
    return body


def _msearch_processos(bodies: list) -> list:
    """1 round-trip HTTP, N sub-buscas em paralelo no ES (padrão agg_estado)."""
    payload: list = []
    for b in bodies:
        payload.append({})
        payload.append(b)
    resp = get_es().msearch(index=index_name('processos'), body=payload)
    respostas = resp.get('responses') or []
    for r in respostas:
        if r.get('error'):
            raise RuntimeError(f'ES msearch: {r["error"]}')
    if len(respostas) != len(bodies):
        raise RuntimeError('ES msearch: resposta incompleta')
    return respostas


def ficha_entidade(entidade_id: str, filtros: dict | None = None) -> dict:
    """Payload completo da ficha. 1 request de agregação (2 sub-buscas se filtrado).

    O cadastro da entidade sai de 1 `term` query no índice de entidades, cacheada
    1h (~10ms) — o custo real é a agregação sobre `voyager-processos`.
    Medido 13/08 contra o ES de prod, sem cache: INSS 2,4s · Caixa 0,8s · União
    0,6s · Prefeitura de SP 0,1s. Com o cache do próprio ES: 10-40ms. Por isso o
    payload é cacheado (`CACHE_TTL`): 2,4s é muito pra uma tela.

    Levanta `EntidadeNaoEncontrada` (→ 404); falha de ES sobe (→ 503).
    """
    eid = (entidade_id or '').strip()[:200]
    filtros = {k: v for k, v in (filtros or {}).items()
               if k not in ('entidade_id', 'parte') and not k.startswith('_')}

    if filtros:
        cache_key = _chave_cache(f'{_CACHE_PREFIX}:ficha:{eid}:f',
                                 {**filtros, 'indice': indice_entidades()})
        ttl = CACHE_TTL_FILTRADO
    else:
        cache_key = f'{_CACHE_PREFIX}:ficha:{eid}:v1'
        ttl = CACHE_TTL
    cacheado = cache.get(cache_key)
    if cacheado is not None:
        return cacheado

    fonte = carregar_entidade(eid)
    grafias = grafias_da_consulta(fonte)

    body_escopo = build_body_ficha(fonte, filtros)
    if filtros:
        # com filtro precisamos do denominador da entidade INTEIRA (o front
        # escreve "X de Y processos desta entidade") — 2ª sub-busca sem aggs
        resp_escopo, resp_total = _msearch_processos(
            [body_escopo, build_body_ficha(fonte, {}, com_aggs=False)])
        total_entidade = _total_hits(resp_total)
    else:
        resp_escopo = _msearch_processos([body_escopo])[0]
        total_entidade = _total_hits(resp_escopo)

    aggs = resp_escopo.get('aggregations') or {}
    total_escopo = _total_hits(resp_escopo)

    sinal_conhecido = _dc(aggs, 'sinal_conhecido')
    n_indice = _n_processos(fonte)
    buckets_trib = (aggs.get('por_tribunal') or {}).get('buckets', [])
    resumo = {
        'processos': total_escopo,
        'processos_entidade': total_entidade,
        # `possiveis` e não `com_sinal`: é o MESMO número, com o MESMO nome que
        # o mapa e a página de estado usam. Vocabulário divergente entre telas
        # irmãs é como nasce "por que o INSS tem dois números diferentes?"
        # sinal nunca computado ⇒ DESCONHECIDO (null), não zero
        'possiveis': _dc(aggs, 'potencial') if sinal_conhecido else None,
        'sinal_processado': bool(sinal_conhecido),
        'tribunais': len(buckets_trib),
        'tribunais_siglas': [b['key'] for b in buckets_trib],
        # Quais dos tribunais desta entidade PUBLICAM valor da causa. O resto
        # fica fora da soma para sempre — não por fila nossa: o PJe consulta
        # pública não expõe o campo (medido em 27 processos reais). Sem isto a
        # ficha diz "valor não informado" e sugere lacuna que não existe.
        'tribunais_que_publicam': sorted(
            b['key'] for b in buckets_trib if fonte_publica_valor(b['key'])),
        'fonte_publica_valor': any(
            fonte_publica_valor(b['key']) for b in buckets_trib),
        # % já validado, PONDERADO pelos processos da entidade em cada tribunal
        # (não é média simples: 95% dos processos do INSS estão em 3 TRFs).
        # Vem do cache do warm — nunca recomputa, nunca toca o Postgres.
        'cobertura_pct': _cobertura_ponderada(buckets_trib),
        'confirmados': _dc(aggs, 'confirmado'),
        'todos': _dc(aggs, 'todos'),
        'valor': round((aggs.get('valor') or {}).get('value') or 0.0, 2),
        # `valor_causa` é esparso (1,5% dentro do INSS) e é o valor DA CAUSA
        'cobertura_valor': _cobertura(
            'valor_causa', _dc(aggs, 'valor_conhecido'),
            total_escopo, total_entidade),
        # `com_sinal` é PISO, não proporção — a cobertura sai daqui, medida a
        # cada resposta (31/08/2026: 79,2% da base, 21,6 M ainda NULL)
        'cobertura_sinal': _cobertura(
            'tem_sinal_precatorio', sinal_conhecido, total_escopo, total_entidade),
        'n_processos_indice': n_indice,
        'n_processos_em': fonte.get('n_processos_em'),
        # o número gravado envelhece (a base cresce todo dia): a diferença é
        # informação, não erro — o front pode mostrar "+N desde a contagem"
        'divergencia_contagem': (total_entidade - n_indice
                                 if n_indice is not None and not filtros
                                 else None),
    }

    uf_conhecida = _dc(aggs, 'uf_conhecida')
    por_uf = _renomear_cobertura(_bloco_terms(
        (aggs.get('por_uf') or {}).get('buckets', []), 'uf',
        uf_conhecida, max(0, total_escopo - uf_conhecida),
        'uf', total_escopo, total_entidade, top_n=_UF_TERMS_SIZE))
    trib_conhecido = _dc(aggs, 'tribunal_conhecido')
    por_tribunal = _renomear_cobertura(_bloco_terms(
        (aggs.get('por_tribunal') or {}).get('buckets', []), 'tribunal',
        trib_conhecido, max(0, total_escopo - trib_conhecido),
        'tribunal', total_escopo, total_entidade, top_n=_TRIBUNAIS_TERMS_SIZE))
    por_ano = _renomear_cobertura(
        _bloco_ano((aggs.get('por_ano') or {}).get('buckets', []),
                   _dc(aggs, 'ano_preenchido'), total_escopo, total_entidade))
    classif_preenchida = _dc(aggs, 'classificacao_preenchida')
    por_classificacao = _renomear_cobertura(_bloco_terms(
        (aggs.get('por_classificacao') or {}).get('buckets', []),
        'classificacao', classif_preenchida,
        max(0, total_escopo - classif_preenchida),
        'classificacao', total_escopo, total_entidade,
        top_n=_CLASSIF_TERMS_SIZE))

    entidade = entidade_publica(fonte)
    payload = {
        'entidade': entidade,
        'filtros': filtros,
        'gerado_em': _agora_iso(),
        'resumo': resumo,
        'por_uf': por_uf,
        'por_tribunal': por_tribunal,
        'por_ano': por_ano,
        'por_classificacao': por_classificacao,
        # COMO o número foi obtido — auditoria da própria medição
        'consulta': {
            'campo': 'partes',
            'metodo': 'match_phrase_or',
            'grafias': grafias,
            'n_grafias': len(grafias),
            'grafias_descartadas': max(
                0, len(fonte.get('variantes_busca') or []) - len(grafias)),
            'truncado': len(grafias) >= ent.MAX_CLAUSULAS_VARIANTES,
        },
        'nota': nota_ficha(entidade, len(grafias)),
    }
    cache.set(cache_key, payload, ttl)
    return payload
