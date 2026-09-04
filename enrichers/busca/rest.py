"""Busca por parte nas APIs REST próprias (TJMT e TJPA) — os clientes HTTP.

São as duas melhores fontes da matriz: total real, paginação de verdade e
nenhum teto observado. Também são as duas em que os nomes de parâmetro/rota não
se adivinham — foram lidos do bundle JavaScript de cada SPA (as tentativas por
palpite deram 405 no TJPA e, pior, 200 com a base inteira no TJMT).

O transporte reusa o enricher de cada tribunal (sessão, pool de proxies,
rotação, `X-Fingerprint` fresco por requisição no TJMT).
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterator

import requests

from djen.proxies import cortex_proxy_url

from .base import (
    ADVOGADO,
    DOCUMENTO,
    NOME,
    OAB,
    BuscaPorParte,
    FonteIndisponivel,
    PaginaResultado,
)
from .rest_parser import (
    parece_base_inteira,
    parse_tjmt,
    parse_tjpa,
    parse_tjpa_nomes,
    rota_tjpa,
)

logger = logging.getLogger('voyager.busca.rest')


def _so_digitos(valor: str) -> str:
    return re.sub(r'\D', '', valor or '')


class _BuscaRest(BuscaPorParte):
    """Chassi comum: rotação de proxy em volta de um GET que devolve JSON."""

    MAX_ROTACOES = 6

    def __init__(self, enricher_cls, prefer_cortex: bool | None = None):
        self.enricher = enricher_cls(prefer_cortex=bool(prefer_cortex))
        self.TRIBUNAL = enricher_cls.TRIBUNAL_SIGLA

    def _get_json(self, url: str, params: dict | None = None,
                  headers: dict | None = None):
        tentados: set = set()
        cortex = cortex_proxy_url(self.enricher.pool)
        ultimo = None
        for _ in range(self.MAX_ROTACOES):
            proxy = self.enricher._next_proxy(tentados)
            if not proxy:
                break
            if proxy != cortex:
                tentados.add(proxy)
            try:
                resp = self.enricher.session.get(
                    url, params=params, headers=headers or {},
                    proxies={'http': proxy, 'https': proxy},
                    timeout=getattr(self.enricher, 'REQUEST_TIMEOUT', (10, 60)))
            except (requests.ConnectionError, requests.Timeout,
                    requests.exceptions.ChunkedEncodingError) as exc:
                ultimo = f'transporte: {str(exc)[:120]}'
                if proxy != cortex:
                    self.enricher.pool.mark_bad(proxy)
                continue
            if resp.status_code in (400, 401, 403, 429):
                ultimo = f'bloqueado {resp.status_code}'
                if proxy != cortex:
                    self.enricher.pool.mark_bad(proxy)
                continue
            if resp.status_code >= 500:
                ultimo = f'servidor {resp.status_code}'
                continue
            if resp.status_code == 204 or not (resp.text or '').strip():
                # 204/corpo vazio é a forma do TJPA dizer "nenhum resultado" —
                # resposta legítima, não falha.
                return None
            try:
                return resp.json()
            except ValueError:
                ultimo = f'corpo não-JSON ({len(resp.text)} bytes)'
                continue
        raise FonteIndisponivel(
            f'{self.TRIBUNAL}: {len(tentados)} proxies sem sucesso'
            + (f' (último: {ultimo})' if ultimo else ''))


class BuscaTjmt(_BuscaRest):
    """`GET hellsgate.tjmt.jus.br/consultaprocessual/ProcessosJudiciais/v2`."""

    CRITERIOS_SUPORTADOS = frozenset({DOCUMENTO, NOME, OAB, ADVOGADO})
    TETO_DA_FONTE = None
    #: `Take` máximo medido: 60 passa, 75 devolve **HTTP 422**. 50 fica com
    #: margem e já corta a conversa em 1/3 — esgotar 112 processos custa 3
    #: requisições e 2 s (medido em 04/09/2026, `advogadoOAB=20688`:
    #: 112 declarados = 112 colhidos = 112 distintos).
    POR_PAGINA = 50

    #: Nomes lidos do bundle da SPA (`chunk-VDMA6QP5.js`,
    #: `getProcessosJudiciais`). Parâmetro fora desta lista é IGNORADO pela API,
    #: que responde 200 com a base inteira — ver `_conferir_sanidade`.
    PARAMETRO = {
        DOCUMENTO: 'parteCpfCnpj',
        NOME: 'parteNome',
        OAB: 'advogadoOAB',
        ADVOGADO: 'NomeOab',
    }

    def __init__(self, enricher_cls, prefer_cortex: bool | None = None):
        super().__init__(enricher_cls, prefer_cortex)
        self.url = f'{enricher_cls.BASE_URL}{enricher_cls.SEARCH_PATH}'
        self._total_sem_filtro: int | None = None

    def _headers(self) -> dict:
        from enrichers.tjmt import gerar_fingerprint
        # Fresco a cada requisição: o servidor valida a janela de timestamp.
        return {'X-Fingerprint': gerar_fingerprint()}

    def _baseline(self) -> int | None:
        """Total da MESMA consulta sem nenhum filtro, para a prova de sanidade.

        Uma requisição por instância, guardada. Custo baixo diante do que ela
        evita: entregar 11,6 milhões de processos aleatórios como se fossem os
        de um CPF.
        """
        if self._total_sem_filtro is None:
            corpo = self._get_json(self.url, {'Skip': 0, 'Take': 1},
                                   self._headers()) or {}
            self._total_sem_filtro = corpo.get('totalRegistros')
        return self._total_sem_filtro

    def paginar(self, criterio: str, valor: str,
                teto_paginas: int = 40) -> Iterator[PaginaResultado]:
        self.exigir_suporte(criterio)
        chave = self.PARAMETRO[criterio]
        termo = _so_digitos(valor) if criterio in (DOCUMENTO, OAB) else valor

        pagina = 1
        while pagina <= teto_paginas:
            corpo = self._get_json(self.url, {
                'Skip': (pagina - 1) * self.POR_PAGINA,
                'Take': self.POR_PAGINA,
                chave: termo,
            }, self._headers())
            if corpo is None:
                return
            resultado = parse_tjmt(corpo, pagina, self.POR_PAGINA)
            if pagina == 1:
                self._conferir_sanidade(resultado, chave)
            yield resultado
            if not resultado.tem_proxima or not resultado.itens:
                return
            pagina += 1

    def _conferir_sanidade(self, resultado: PaginaResultado, chave: str) -> None:
        """Uma busca com filtro não pode devolver o total da busca sem filtro.

        Se devolver, a API não entendeu o parâmetro e está paginando o acervo
        inteiro — 200, JSON válido, dado errado. Falha alto: é indisponibilidade
        da fonte, nunca resultado.
        """
        if parece_base_inteira(resultado.total_declarado, self._baseline()):
            raise FonteIndisponivel(
                f'{self.TRIBUNAL}: o filtro `{chave}` foi ignorado — a resposta '
                f'traz os mesmos {resultado.total_declarado} registros da busca '
                f'sem filtro')


class BuscaTjpa(_BuscaRest):
    """`consilium-rest` — sete rotas, todas lidas do bundle da SPA."""

    CRITERIOS_SUPORTADOS = frozenset({DOCUMENTO, NOME, OAB})
    TETO_DA_FONTE = None
    #: Tamanho pedido na URL. O TJPA não o respeita à risca — pedindo 50, o
    #: `processobycnpj` devolveu 25 e o `processobynomeparteexato`, 56 —, então
    #: ele é só uma dica: quem manda no fim da paginação é "não veio nada novo".
    POR_PAGINA = 50

    def __init__(self, enricher_cls, prefer_cortex: bool | None = None):
        super().__init__(enricher_cls, prefer_cortex)
        self.base = f'{enricher_cls.BASE_URL}/consilium-rest'

    def nomes_parecidos(self, nome: str) -> list[dict]:
        """Desambiguação: as grafias reais que casam com o nome digitado.

        `processobynomeparte` não devolve processo, devolve
        `[{nome, quantidade, sistema}]`. Chamar isto ANTES de buscar é o que
        evita escolher sozinho entre "MARIA JOSE DOS SANTOS" (43 processos) e
        "MARIA JOSE DOS SANTOS SILVA" (12) — grafias diferentes, pessoas
        diferentes.
        """
        corpo = self._get_json(f'{self.base}/processobynomeparte/{nome}')
        return parse_tjpa_nomes(corpo or [])

    def rota(self, criterio: str, valor: str, pagina: int) -> str:
        """Delega para `rest_parser.rota_tjpa` — a regra do índice 1-based mora
        lá, junto do teste que a prova."""
        return rota_tjpa(self.base, criterio, valor, pagina, self.POR_PAGINA)

    def paginar(self, criterio: str, valor: str,
                teto_paginas: int = 40) -> Iterator[PaginaResultado]:
        """Pagina até a fonte parar de trazer processo NOVO.

        O TJPA não sinaliza fim: passada a última página cheia, ele repete uma
        linha indefinidamente (medido: páginas 3 a 6 de uma busca por nome
        devolveram sempre o mesmo processo, com `qtdRegistrosTotal: 1`).
        Confiar no total do envelope também não dá — ele muda de página para
        página nesse endpoint (57, 27, 1). O critério de parada que sobra, e
        que é robusto, é "esta página não trouxe nada que eu já não tivesse".
        """
        self.exigir_suporte(criterio)
        vistos: set[str] = set()
        total_da_primeira = None
        pagina = 1
        while pagina <= teto_paginas:
            corpo = self._get_json(self.rota(criterio, valor, pagina))
            if corpo is None:
                return
            resultado = parse_tjpa(corpo, pagina, self.POR_PAGINA)
            novos = [i for i in resultado.itens if i.numero_cnj not in vistos]
            if not novos:
                return
            vistos.update(i.numero_cnj for i in novos)
            if total_da_primeira is None:
                total_da_primeira = resultado.total_declarado
            # O total que vale é o da PRIMEIRA página: nas seguintes o campo
            # vira contagem local e encolher o total no meio da paginação faria
            # a resposta dizer que achou mais do que a fonte tem.
            resultado.itens = novos
            resultado.total_declarado = total_da_primeira
            resultado.tem_proxima = True
            yield resultado
            pagina += 1
