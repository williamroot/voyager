"""Base dos coletores de diário oficial de ente devedor.

O que muda em relação ao contrato de `diarios/base.py` (e por quê)
==================================================================
Nada no contrato — só o DESTINO. Esta família declara
`destino = DESTINO_PROPRIO` e sobrescreve `persistir()`, exatamente o gancho
que o `base.py` já reservou para a fonte sem tribunal. `catalogar()`/`coletar()`
continuam com a mesma assinatura, o runner é o mesmo, o watermark é o
`EdicaoDiario` de sempre. Nenhuma linha de `diarios/base.py` foi tocada.

A única liberdade tomada é o TIPO DO ITEM: `coletar()` devolve `ItemEnte`, não
`ParsedItem`. Motivo objetivo, não estético — `ParsedItem` é a forma exata de
uma `Movimentacao` e por isso exige `cnj` obrigatório e não tem lugar para
`ente`/`uf`/`territory_id`. Aqui o CNJ é a EXCEÇÃO (0 de 30 publicações
aleatórias do DOE-SP têm um) e o ente É a chave. Enfiar isso num ParsedItem
significaria inventar um CNJ vazio e perder a identidade do ente — o oposto do
"abster > chutar". Como `persistir()` é nosso, o runner nunca vê a diferença.

Conduta de rede (medida em 16/08/2026, não é chute)
---------------------------------------------------
Nenhuma das portas tem WAF, rate limit, captcha ou `robots.txt`: 25 requests
consecutivos ao DOE-SP sem sleep deram 25/25 OK em 3,4s (7,3 req/s) de IP de
DATACENTER. Ou seja, o servidor NÃO vai nos defender de nós mesmos — o teto é
auto-imposto (`rps` baixo, `PROXY_DIRETO`, User-Agent que nos identifica). Sem
Cortex de propósito: é um SPOF a menos, e aqui ele não é necessário.
"""

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

import requests
from bs4 import BeautifulSoup
from django.db import transaction

from diarios.base import (
    DESTINO_PROPRIO,
    PROXY_DIRETO,
    ColetorDiario,
    RespostaInvalida,
    UnidadeColeta,
    achar_cnjs,
)
from tribunals.models import Process

from .models import CONFIANCA_ALTA, CONFIANCA_BAIXA, PublicacaoOficial

logger = logging.getLogger('voyager.diarios_entes')

LOTE_DB = 200
#: CPF só é CONTADO (ver comentário do campo no model). A regex fica aqui para
#: não haver duas verdades sobre o formato.
CPF_RE = re.compile(r'\b\d{3}\.\d{3}\.\d{3}-\d{2}\b')


# ─────────────────────────────────────────────────────────────────────────────
# 1. CONSULTAS — por que frase e não termo solto
# ─────────────────────────────────────────────────────────────────────────────
# Medição do recon (16/08/2026), que é o que separa sinal de lixo:
#   · "precatório" solto no Querido Diário casa 282 de 7.819 gazetas de
#     julho/2026 (3,6%) — e a MAIORIA é linha de RREO/RGF orçamentário
#     ("31.5- RECEITA DE PRECATÓRIOS - FUNDEF E FUNDEB 1.296.000,00").
#     Só 2 das 12 gazetas amostradas traziam CNJ.
#   · a FRASE "câmara de conciliação de precatórios" casa 64 gazetas em 2,5
#     anos, e a primeira delas é a convocação de Maceió com a tabela
#     PARTE x PRECATÓRIO Nº x HORÁRIO DA SESSÃO — lead puro.
# Daí a separação: frase = confiança ALTA; termo solto = rede secundária,
# marcada como BAIXA para a triagem consumir por último.
#
# No Querido Diário a aspa É o operador de frase, e isso foi conferido ao vivo:
#   com aspas  → total_gazettes = 64
#   sem aspas  → total_gazettes = 10000 (o cap do OpenSearch: casou "o país")
# ATENÇÃO: isso vale para FRASE. Termo solto entre aspas era perda de 66% do
# recall — ver `querido_diario._buscar`, corrigido em 16/08/2026.
FRASES_PRECATORIO = (
    'câmara de conciliação de precatórios',
    'acordo direto de precatórios',
    'ordem cronológica de precatórios',
    'ofício requisitório',
    'regime especial de precatórios',
)
TERMOS_PRECATORIO = (
    'precatório',
    # SEM acento de propósito, e a medição é do DOE-SP: lá o parâmetro `Terms`
    # é sensível a acento e devolve conjuntos DIFERENTES (precatório=117,
    # precatorio=10 no mesmo período) — não é ruído, é outro conjunto de
    # despachos da PGE, e perder isso é perder documento.
    # No Querido Diário ela só passou a render depois do fim das aspas em termo
    # solto (com aspas era 0 em 12/12 dias medidos; sem aspas, 5-7/dia).
    'precatorio',
)


# ─────────────────────────────────────────────────────────────────────────────
# 2. O ITEM
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class ItemEnte:
    """Uma publicação de diário de ente, pronta para virar `PublicacaoOficial`."""
    external_id: str
    esfera: str
    ente: str
    data_publicacao: date
    texto: str
    uf: str = ''
    territory_id: str = ''
    titulo: str = ''
    orgao: str = ''
    tipo_documento: str = ''
    edicao: str = ''
    link: str = ''
    link_texto: str = ''
    texto_integral_chars: int = 0
    texto_completo: bool = False
    consultas: list = field(default_factory=list)
    confianca: str = CONFIANCA_BAIXA
    cnjs: list = field(default_factory=list)
    cpfs_no_texto: int = 0


# ─────────────────────────────────────────────────────────────────────────────
# 3. VALIDADORES DE "200 QUE NÃO É DADO" — os específicos desta família
# ─────────────────────────────────────────────────────────────────────────────
# `diarios/base.py` já dá `exigir_pdf`/`exigir_ancora`/`exigir_chaves`. Faltava
# o caso desta família, que é o mais traiçoeiro dos quatro: SPA catch-all
# devolvendo HTTP 200 + text/html para QUALQUER path.
#   · https://queridodiario.ok.org.br/api/gazettes?... → 200, 20.943 bytes de
#     Angular. A API de verdade é api.queridodiario.ok.org.br (host diferente!).
#   · https://www.diariooficial.rs.gov.br/doe/materias/feed/rss.xml → 200,
#     2.700 bytes de index.html. E esse path é anunciado pelo <link
#     rel="alternate"> do próprio site.
# Um health-check por status code aprovaria os dois. É o mesmo erro dos
# "180 milhões de PDFs".
def exigir_json(resp: requests.Response, contexto: str = '') -> dict:
    """Exige JSON de verdade: Content-Type + corpo parseável + objeto no topo."""
    ctype = (resp.headers.get('Content-Type') or '').lower()
    if 'json' not in ctype:
        amostra = (resp.text or '')[:120].replace('\n', ' ')
        raise RespostaInvalida(
            f'{contexto}: Content-Type {ctype!r} com {len(resp.content)} bytes '
            f'(provável SPA catch-all): {amostra!r}'
        )
    try:
        payload = resp.json()
    except ValueError as exc:
        raise RespostaInvalida(f'{contexto}: corpo não é JSON ({len(resp.content)} bytes)') from exc
    if not isinstance(payload, dict):
        raise RespostaInvalida(f'{contexto}: JSON no topo é {type(payload).__name__}, esperado objeto')
    return payload


def exigir_texto(corpo: bytes, min_bytes: int = 512, contexto: str = '') -> str:
    """Exige texto de gazeta, não página de erro/HTML.

    O `.txt` do Querido Diário é texto NATIVO extraído do PDF (ruído medido de
    0,04% a 0,59% de chars fora do alfabeto pt-BR em 12 gazetas) — por isso ele,
    e não o PDF: dispensa OCR. Mas o mesmo bucket serve HTML de erro, então
    tamanho mínimo + ausência de `<html` é o piso.
    """
    if not corpo or len(corpo) < min_bytes:
        raise RespostaInvalida(f'{contexto}: {len(corpo or b"")} bytes (< {min_bytes})')
    texto = corpo.decode('utf-8', errors='replace')
    if '<html' in texto[:2000].lower() or '<!doctype html' in texto[:2000].lower():
        raise RespostaInvalida(f'{contexto}: veio HTML onde deveria vir texto de gazeta')
    return texto


# ─────────────────────────────────────────────────────────────────────────────
# 4. TEXTO — dobra sem acento, janela em torno do que importa
# ─────────────────────────────────────────────────────────────────────────────
def dobrar(texto: str) -> str:
    """Minúsculas e sem acento, PRESERVANDO O COMPRIMENTO (1 char → 1 char).

    O comprimento importa: os índices desta string são usados para recortar o
    texto ORIGINAL (verbatim). Um `NFD` normal encurtaria/alongaria e o recorte
    sairia deslocado.
    """
    return ''.join(unicodedata.normalize('NFD', c)[0].lower() for c in (texto or ''))


def dobrar_liso(texto: str) -> str:
    """`dobrar()` + espaço em branco colapsado. Para COMPARAR frase, nunca para
    recortar (aqui o comprimento muda de propósito).

    Existe porque as duas fontes quebram frase no meio: o `excerpt` do DOE-SP
    vem com `\\n\\n` do meio do parágrafo e o `.txt` do Querido Diário é o PDF
    linearizado, com quebra a cada linha impressa. Procurar
    'câmara de conciliação de precatórios' literalmente falharia num texto que
    escreve 'CÂMARA DE CONCILIAÇÃO DE\\nPRECATÓRIOS' — e a consequência seria
    silenciosa: o documento certo descartado como "termo não confirmado".
    """
    return re.sub(r'\s+', ' ', dobrar(texto))


def janela_de_texto(texto: str, alvos: list[str], raio: int = 6000) -> tuple[str, bool]:
    """Recorta o texto em torno das ocorrências dos `alvos`. Devolve (trecho, inteiro?).

    POR QUE NÃO GUARDAR A GAZETA INTEIRA: a média das 12 gazetas amostradas em
    jul/2026 é de 772.500 chars (a do Rio de Janeiro de 30/07 sozinha tem
    6.616.216) e 99% disso é licitação, folha e decreto de trânsito. O ato que
    interessa — a tabela PARTE x PRECATÓRIO Nº x HORÁRIO de Maceió — cabe em
    ~4.000 chars. O raio default de 6.000 pega a tabela inteira com folga.

    O trecho é VERBATIM (recorte, não reescrita) e `link_texto` mantém o
    integral sempre recuperável. Se os alvos cobrem o documento todo, devolve o
    documento todo e marca `inteiro=True`.
    """
    if not texto:
        return ('', True)
    dobrado = dobrar(texto)
    intervalos: list[list[int]] = []
    for alvo in alvos:
        agulha = dobrar(alvo)
        if not agulha:
            continue
        pos = dobrado.find(agulha)
        while pos != -1:
            intervalos.append([max(0, pos - raio), min(len(texto), pos + len(agulha) + raio)])
            pos = dobrado.find(agulha, pos + len(agulha))
    if not intervalos:
        # Sem âncora: devolve o começo do documento em vez de mentir com vazio.
        return (texto[:raio * 2], len(texto) <= raio * 2)
    intervalos.sort()
    fundidos = [intervalos[0]]
    for ini, fim in intervalos[1:]:
        if ini <= fundidos[-1][1]:
            fundidos[-1][1] = max(fundidos[-1][1], fim)
        else:
            fundidos.append([ini, fim])
    if len(fundidos) == 1 and fundidos[0] == [0, len(texto)]:
        return (texto, True)
    partes = [texto[i:f] for i, f in fundidos]
    return ('\n[...]\n'.join(partes), False)


def html_para_texto(html: str) -> str:
    """HTML da publicação → texto legível, sem script/style.

    O `content` do DOE-SP é HTML 4.01 completo (o da amostra tem 17.267 chars)
    com a diagramação do jornal dentro. O extrator lê texto, não tag.
    """
    sopa = BeautifulSoup(html or '', 'lxml')
    for tag in sopa(['script', 'style']):
        tag.decompose()
    texto = sopa.get_text('\n')
    texto = re.sub(r'[ \t\xa0]+', ' ', texto)
    return re.sub(r'\n{3,}', '\n\n', texto).strip()


def enriquecer_item(item: ItemEnte) -> ItemEnte:
    """Preenche o que se extrai do TEXTO: CNJs e contagem de CPF.

    `achar_cnjs` é o do `diarios/base.py` — tolerante ao espaço espúrio que o
    extrator de PDF injeta e que come 8,1% dos números no caderno do TJSP.
    Aqui ele também paga: a gazeta de Maceió tem 41 CNJs em tabela justificada.
    """
    item.cnjs = achar_cnjs(item.texto)
    item.cpfs_no_texto = len(CPF_RE.findall(item.texto or ''))
    return item


# ─────────────────────────────────────────────────────────────────────────────
# 5. A BASE
# ─────────────────────────────────────────────────────────────────────────────
class ColetorEnte(ColetorDiario):
    """Coletor de diário de ente devedor: destino próprio, sem tribunal."""

    destino = DESTINO_PROPRIO
    modo_proxy = PROXY_DIRETO   # medido: 200 de IP datacenter, zero 403
    esfera = ''                 # 'municipal' | 'estadual'

    #: fontes de ente NÃO têm janela de exclusividade contra o DJEN — são
    #: universos disjuntos (Executivo x Judiciário). O `janela_inicio` aqui é a
    #: COBERTURA MEDIDA da fonte (a data da publicação mais antiga que ela
    #: serve); pedir antes disso é pedir o que não existe.
    janela_fim = None

    def esperado(self, unidade: UnidadeColeta) -> int | None:
        """Sempre None, DE PROPÓSITO.

        A fonte declara quantas publicações existem no dia (o DOE-SP diz
        `totalItems=3164` para 14/08/2026), mas nós persistimos só as que casam
        consulta — 1 naquele dia. Devolver o total da fonte aqui faria o gate de
        cobertura do runner reprovar 100% das coletas corretas. O gabarito certo
        para esta família é o do TESTE (achar os 3 CNJs da tabela de Maceió),
        não o do runner.
        """
        return None

    # -- persistência ---------------------------------------------------------
    def persistir(self, itens: list[ItemEnte], unidade: UnidadeColeta, run) -> tuple[int, int]:
        """Grava `PublicacaoOficial` (nunca `Movimentacao`) e vincula processos.

        Idempotente por `uniq(fonte, external_id)` + `ignore_conflicts`, igual à
        ingestão do DJEN: re-coletar o mesmo dia é seguro e é o que todo retry
        faz.
        """
        if not itens:
            return (0, 0)
        ext_ids = [i.external_id for i in itens]
        with transaction.atomic():
            ja = set(
                PublicacaoOficial.objects
                .filter(fonte=self.slug, external_id__in=ext_ids)
                .values_list('external_id', flat=True)
            )
            novos = [i for i in itens if i.external_id not in ja]
            PublicacaoOficial.objects.bulk_create(
                [self._para_model(i) for i in novos],
                ignore_conflicts=True, batch_size=LOTE_DB,
            )
            self._vincular_processos(novos)
        return (len(novos), len(ja))

    def _para_model(self, item: ItemEnte) -> PublicacaoOficial:
        return PublicacaoOficial(
            fonte=self.slug,
            external_id=item.external_id[:120],
            esfera=item.esfera or self.esfera,
            ente=item.ente[:160],
            uf=(item.uf or '')[:2],
            territory_id=(item.territory_id or '')[:7],
            data_publicacao=item.data_publicacao,
            titulo=(item.titulo or '')[:300],
            orgao=(item.orgao or '')[:255],
            tipo_documento=(item.tipo_documento or '')[:120],
            edicao=(item.edicao or '')[:40],
            link=(item.link or '')[:500],
            link_texto=(item.link_texto or '')[:500],
            texto=item.texto or '',
            texto_integral_chars=item.texto_integral_chars or len(item.texto or ''),
            texto_completo=item.texto_completo,
            consultas=item.consultas or [],
            confianca=item.confianca,
            cnjs=item.cnjs or [],
            cpfs_no_texto=item.cpfs_no_texto,
        )

    def _vincular_processos(self, itens: list[ItemEnte]) -> int:
        """Liga a publicação aos `Process` que JÁ EXISTEM com aquele CNJ.

        NÃO cria processo. Para criar seria preciso inventar o `tribunal` (FK
        obrigatória) a partir dos dígitos J.TR do número — e um CNJ citado num
        decreto do Executivo não prova sequer que o processo é do acervo. Abster
        > chutar: o vínculo aparece sozinho no dia em que o processo entrar pela
        porta judicial.
        """
        cnjs = sorted({c for i in itens for c in (i.cnjs or [])})
        if not cnjs:
            return 0
        por_cnj: dict[str, list[int]] = {}
        for i in range(0, len(cnjs), LOTE_DB):
            for numero, pk in Process.objects.filter(
                numero_cnj__in=cnjs[i:i + LOTE_DB]
            ).values_list('numero_cnj', 'pk'):
                por_cnj.setdefault(numero, []).append(pk)
        if not por_cnj:
            return 0
        pubs = dict(
            PublicacaoOficial.objects
            .filter(fonte=self.slug, external_id__in=[i.external_id for i in itens])
            .values_list('external_id', 'pk')
        )
        Liga = PublicacaoOficial.processos.through
        ligacoes = [
            Liga(publicacaooficial_id=pubs[i.external_id], process_id=pk)
            for i in itens if i.external_id in pubs
            for c in (i.cnjs or []) for pk in por_cnj.get(c, ())
        ]
        if ligacoes:
            Liga.objects.bulk_create(ligacoes, ignore_conflicts=True, batch_size=LOTE_DB)
        return len(ligacoes)

    # -- ajudante comum às fontes --------------------------------------------
    @staticmethod
    def confianca_de(consultas: list[str]) -> str:
        """ALTA só quando casou FRASE. Termo solto é rede, não sinal."""
        alvo = {dobrar(f) for f in FRASES_PRECATORIO}
        return CONFIANCA_ALTA if any(dobrar(c) in alvo for c in consultas) else CONFIANCA_BAIXA
