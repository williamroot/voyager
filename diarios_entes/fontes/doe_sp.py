"""DOE-SP — Diário Oficial do Estado de São Paulo (API de publicação).

A MELHOR ENGENHARIA DAS QUATRO PORTAS SONDADAS
==============================================
JSON puro, sem PDF no caminho crítico, sem WAF, sem login, sem captcha, sem
proxy: 25 requests consecutivos sem sleep deram 25/25 OK em 3,4s (7,3 req/s) de
IP de datacenter. O `content` do detalhe já vem com o HTML integral do ato.

Descoberta por engenharia reversa dos chunks Next.js de doe.sp.gov.br (o
`buildId` do path muda a cada deploy deles) — logo, endpoint NÃO CONTRATADO,
sem documentação nem versionamento prometido. A defesa é falhar alto quando o
payload mudar de forma, e é o que `exigir_chaves`/`_exigir_busca_por_termo`
fazem aqui.

AS DUAS ARMADILHAS QUE ESTE ARQUIVO EXISTE PARA NÃO CAIR
--------------------------------------------------------
1. `SearchTerms` É IGNORADO EM SILÊNCIO. `?SearchTerms=precatório` devolve
   HTTP 200 com `totalItems=487579` — o universo inteiro do ano. O parâmetro
   certo é `Terms`, que devolve 117. Um erro de NOME de parâmetro não dá erro:
   dá um número 4.000x maior. Quem só olhasse a contagem reportaria
   "487 mil publicações sobre precatório".
   O discriminador MECÂNICO (medido nas duas respostas, fixtures no repo):
   com `Terms`, cada item traz `termsFound=[{'term': ..., 'matchesFound': n}]`
   e `totalTermsFound>=1`; com `SearchTerms`, a chave `termsFound` nem existe e
   `totalTermsFound` é 0 em 20/20. Por isso a validação aqui é de CONTEÚDO.

2. `Terms` É SENSÍVEL A ACENTO, e as duas grafias são conjuntos DIFERENTES —
   não é o mesmo resultado com ruído: `precatório` dá 117 (apostilas de ação
   judicial) e `precatorio` dá 10 (despachos da PGE). Buscar só uma perde a
   outra. Daí a união das variantes.

O QUE ESTA FONTE É
------------------
Sinal de DESFECHO, não acervo: de 30 publicações ALEATÓRIAS de 14/08/2026,
**0** tinham CNJ (texto médio de 2.784 chars: contracheque, licitação,
apostila). Só 0,017% do fluxo diário fala de precatório (117 em 228 dias). O
filão é o tipo "Apostila de Ação Judicial" — 30 dos 117 casos de 2026 — em que
o Estado averba que cumpriu decisão judicial, com CNJ + nome + vara.
"""

import logging
from datetime import date, timedelta

from diarios.base import (
    RespostaInvalida,
    UnidadeColeta,
    UnidadeInexistente,
    exigir_chaves,
    external_id_de,
    registrar,
)

from ..coletor import (
    FRASES_PRECATORIO,
    TERMOS_PRECATORIO,
    ColetorEnte,
    ItemEnte,
    dobrar_liso,
    enriquecer_item,
    exigir_json,
    html_para_texto,
)
from ..models import ESFERA_ESTADUAL

logger = logging.getLogger('voyager.diarios_entes.doesp')

API = 'https://do-api-web-search.doe.sp.gov.br'
BUSCA = f'{API}/v2/advanced-search/publications'
DETALHE = f'{API}/v2/publications'
PORTAL = 'https://doe.sp.gov.br'

#: `PageNumber` é 1-BASED. Pedir 0 devolve HTTP 400 "O número da Página de
#: Resultados solicitado é inválido" — erro honesto, mas erro.
PRIMEIRA_PAGINA = 1
#: 1000 é aceito (testado). Nas buscas por termo o resultado é de uma ou duas
#: dezenas por dia, então 100 já sobra e mantém o payload pequeno.
TAMANHO_PAGINA = 100
#: teto de segurança: o dia mais cheio medido tem 3.318 publicações; se uma
#: consulta por termo devolver isso, é sinal de que o filtro foi ignorado.
MAX_ITENS_POR_CONSULTA = 500

CHAVES_BUSCA = {'items', 'totalItems', 'currentPage', 'totalPages'}
CHAVES_DETALHE = {'id', 'date', 'title', 'content', 'slug'}

#: A API só aceita UM termo por request, então cada consulta é um request. As
#: frases entram como `Terms` também (medido: "ofício requisitório" → 22 hits
#: em 2026), e valem como confiança ALTA; os termos soltos são a rede.
CONSULTAS = tuple(FRASES_PRECATORIO) + tuple(TERMOS_PRECATORIO)


@registrar
class DoeSpColetor(ColetorEnte):
    """Publicações do DOE-SP que falam de precatório, dia a dia."""

    slug = 'doe-sp'
    nome = 'Diário Oficial do Estado de São Paulo'
    esfera = ESFERA_ESTADUAL

    #: cobertura medida ao vivo por bissecção (16/08/2026): 2023-07-09 devolve
    #: `totalItems=0` e 2023-07-10 devolve 2.473; todo mês anterior a julho/2023
    #: devolve 0. A API não serve o acervo legado (o campo `isLegacy` existe nos
    #: itens, mas a busca não alcança nada antes dessa data).
    janela_inicio = date(2023, 7, 10)

    #: 2 req/s auto-impostos, contra 7,3 req/s medidos sem um único 403. O teto
    #: é nosso: não há rate limit do outro lado para nos proteger de nós mesmos.
    rps = 2.0

    def __init__(self):
        super().__init__()
        # O DOE-SP não valida Referer de fato, mas mandá-lo é dizer quem somos e
        # de onde viemos — mesma conduta do User-Agent `voyager-ops`.
        self.sessao.session.headers.update({'Referer': f'{PORTAL}/', 'Accept': 'application/json'})

    # ── catálogo ─────────────────────────────────────────────────────────────
    def catalogar(self, data_inicio: date, data_fim: date):
        """Uma unidade por dia PUBLICADO. Custa 1 request por dia.

        O request não é desperdício: ele responde `totalItems` do dia, que
        distingue "não houve diário" (sábado, feriado → 0, e o dia nem vira
        unidade) de "houve diário e nada casou" (vira unidade e fecha VAZIA).
        Sem isso, o backfill retentaria todo fim de semana para sempre.
        """
        d = data_inicio
        while d <= data_fim:
            total = self._total_do_dia(d)
            if total:
                yield UnidadeColeta(
                    chave=d.isoformat(), data=d,
                    rotulo=f'DOE-SP {d:%d/%m/%Y} · {total} publicações',
                    meta={'publicacoes_no_dia': total, 'consultas': list(CONSULTAS)},
                )
            d += timedelta(days=1)

    # ── coleta ───────────────────────────────────────────────────────────────
    def coletar(self, unidade: UnidadeColeta):
        dia = unidade.data
        consultas = unidade.meta.get('consultas') or list(CONSULTAS)

        achados: dict[str, dict] = {}
        for consulta in consultas:
            for pub in self._buscar(consulta, dia):
                ident = pub.get('id')
                if not ident:
                    continue
                alvo = achados.setdefault(ident, {'busca': pub, 'consultas': []})
                alvo['consultas'].append(consulta)

        if not achados:
            if not unidade.meta.get('publicacoes_no_dia') and self._total_do_dia(dia) == 0:
                raise UnidadeInexistente(f'DOE-SP não publicou em {dia}')
            return

        for ident, dados in achados.items():
            item = self._montar(ident, dados['busca'], dados['consultas'])
            if item is not None:
                yield item

    # ── rede ─────────────────────────────────────────────────────────────────
    def _total_do_dia(self, dia: date) -> int:
        resp = self.sessao.get(BUSCA, params={
            'PageNumber': PRIMEIRA_PAGINA, 'PageSize': 1,
            'FromDate': dia.isoformat(), 'ToDate': dia.isoformat(),
        })
        payload = exigir_json(resp, contexto=f'DOE-SP total {dia}')
        exigir_chaves(payload, {'totalItems'}, contexto=f'DOE-SP total {dia}')
        return int(payload.get('totalItems') or 0)

    def _buscar(self, termo: str, dia: date) -> list[dict]:
        itens: list[dict] = []
        pagina = PRIMEIRA_PAGINA
        while True:
            resp = self.sessao.get(BUSCA, params={
                'PageNumber': pagina, 'PageSize': TAMANHO_PAGINA,
                'FromDate': dia.isoformat(), 'ToDate': dia.isoformat(),
                'Terms': termo,
            })
            ctx = f'DOE-SP busca {termo!r} {dia}'
            payload = exigir_json(resp, contexto=ctx)
            exigir_chaves(payload, CHAVES_BUSCA, contexto=ctx)
            pagina_itens = payload.get('items') or []
            if pagina_itens:
                _exigir_busca_por_termo(pagina_itens, termo, contexto=ctx)
            itens.extend(pagina_itens)
            if len(itens) >= MAX_ITENS_POR_CONSULTA:
                logger.error('DOE-SP: %s devolveu %d itens em %s — filtro provavelmente '
                             'ignorado, parando', termo, len(itens), dia)
                break
            if not payload.get('hasNextPage') or not pagina_itens:
                break
            pagina += 1
        return itens

    def _detalhe(self, ident: str) -> dict:
        resp = self.sessao.get(f'{DETALHE}/{ident}')
        ctx = f'DOE-SP detalhe {ident}'
        payload = exigir_json(resp, contexto=ctx)
        return exigir_chaves(payload, CHAVES_DETALHE, contexto=ctx)

    # ── item ─────────────────────────────────────────────────────────────────
    def _montar(self, ident: str, busca: dict, consultas: list[str]) -> ItemEnte | None:
        detalhe = self._detalhe(ident)
        texto = html_para_texto(detalhe.get('content') or '')
        if not texto.strip():
            logger.warning('DOE-SP: publicação sem conteúdo, ignorada', extra={'id': ident})
            return None

        # Confirmação no CORPO, não na contagem: o termo tem que estar no texto
        # do ato. É esta linha que teria pego a armadilha do `SearchTerms`.
        dobrado = dobrar_liso(texto)
        casaram = [c for c in consultas if dobrar_liso(c) in dobrado]
        if not casaram:
            logger.warning('DOE-SP: termo não confirmado no corpo, ignorada', extra={
                'id': ident, 'consultas': consultas, 'chars': len(texto)})
            return None

        orgao = ' · '.join(x for x in (detalhe.get('secondLevelSectionName'),
                                       detalhe.get('section')) if x)
        item = ItemEnte(
            external_id=external_id_de(self.slug, ident),
            esfera=ESFERA_ESTADUAL,
            ente='Estado de São Paulo',
            uf='SP',
            # `territory_id` fica VAZIO de propósito: o IBGE de 7 dígitos é
            # código de MUNICÍPIO. Preencher com o do estado (35) seria inventar
            # compatibilidade com o SICONFI que não existe.
            territory_id='',
            data_publicacao=date.fromisoformat((detalhe.get('date') or '')[:10]),
            titulo=(detalhe.get('title') or '').strip(),
            orgao=orgao,
            tipo_documento=(detalhe.get('publicationType') or '').strip(),
            # O DOE-SP não numera edição por ato; o que ele dá é o `authCode`
            # ('2026.08.13.1.2.34.15.2.16.3.218.2048316'), o código com que o
            # próprio portal autentica a publicação. É a coordenada verificável
            # do ato na fonte, e é isso que guardamos aqui — não um número de
            # edição inventado.
            edicao=str(detalhe.get('authCode') or '')[:40],
            link=f'{PORTAL}/{detalhe.get("slug")}' if detalhe.get('slug') else '',
            link_texto='',
            texto=texto,                 # cabe inteiro: 17.267 chars na amostra
            texto_integral_chars=len(texto),
            texto_completo=True,
            consultas=sorted(set(casaram)),
            confianca=self.confianca_de(casaram),
        )
        return enriquecer_item(item)


def _exigir_busca_por_termo(itens: list[dict], termo: str, contexto: str = '') -> None:
    """Prova que o filtro de termo FOI aplicado — a armadilha do `SearchTerms`.

    Duas evidências independentes, ambas medidas nas fixtures reais:
      · estrutural: com `Terms`, todo item traz `termsFound` e
        `totalTermsFound >= 1`; com o parâmetro errado, `termsFound` some e
        `totalTermsFound` é 0 em 20/20;
      · textual: o termo (sem acento, minúsculo) aparece no `excerpt`/`title` em
        20/20 dos itens filtrados e em 0/20 do universo.
    Exigir as duas evita tanto o param errado quanto uma mudança de contrato em
    que a chave continue existindo e pare de significar algo.
    """
    com_termos = sum(1 for i in itens if int(i.get('totalTermsFound') or 0) >= 1)
    agulha = dobrar_liso(termo)
    com_texto = sum(
        1 for i in itens
        if agulha in dobrar_liso(f'{i.get("excerpt") or ""} {i.get("title") or ""}')
    )
    n = len(itens)
    if com_termos == 0 or com_texto < n * 0.5:
        raise RespostaInvalida(
            f'{contexto}: o filtro de termo NÃO foi aplicado — '
            f'{com_termos}/{n} itens com totalTermsFound>=1, '
            f'{com_texto}/{n} com o termo no excerpt. '
            'É a assinatura do parâmetro ignorado (SearchTerms devolve o universo).'
        )
