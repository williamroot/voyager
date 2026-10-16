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
import time
from collections.abc import Iterator

import requests

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
    parse_tjrs_nomes,
    parse_tjrs_processos,
    rota_tjpa,
)

logger = logging.getLogger('voyager.busca.rest')

#: Cabeçalhos que cada API exige. Ficam AQUI e não no enricher porque são
#: propriedade da fonte, não do transporte: o TJPA devolve 429/bloqueio para
#: User-Agent identificador e exige o `Referer` da origem oficial.
HEADERS = {
    'TJPA': {
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.8',
        'Referer': 'https://consulta-processual-unificada-prd.tjpa.jus.br/',
        'Origin': 'https://consulta-processual-unificada-prd.tjpa.jus.br',
    },
    'TJMT': {
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Origin': 'https://consultaprocessual.tjmt.jus.br',
        'Referer': 'https://consultaprocessual.tjmt.jus.br/',
    },
}


def _so_digitos(valor: str) -> str:
    return re.sub(r'\D', '', valor or '')


class _BuscaRest(BuscaPorParte):
    """Chassi comum: rotação de proxy em volta de um GET que devolve JSON."""

    #: Rotações por requisição. 10, e não 6, pelo mesmo motivo do e-SAJ: numa
    #: malha em que boa parte dos IPs não responde, poucas tentativas viram
    #: "fonte indisponível" com a fonte de pé.
    MAX_ROTACOES = 10

    #: Pausa entre PÁGINAS. As duas APIs punem rajada: o TJPA devolve 429 e o
    #: TJMT bloqueia com 403 — medido na primeira busca real em produção, que
    #: trouxe 300 de 1.158 processos e aí levou 403 em seis IPs seguidos.
    #: Paginar rápido demais não acelera nada: queima o pool e para no meio.
    PAUSA_ENTRE_PAGINAS_S = 1.0

    #: `(conectar, ler)` da busca. As duas APIs REST responderam em 2 a 26 s nas
    #: medições, mas o padrão vale: buscar por parte é varredura do lado deles.
    TIMEOUT_BUSCA = (10, 180)

    def __init__(self, enricher_cls, prefer_cortex: bool | None = None):
        self.enricher = enricher_cls(prefer_cortex=bool(prefer_cortex))
        self.TRIBUNAL = enricher_cls.TRIBUNAL_SIGLA

    def _get_json(self, url: str, params: dict | None = None,
                  headers: dict | None = None):
        tentados: set = set()
        cortex = None
        if self.enricher.pool is not None:
            from djen.proxies import cortex_proxy_url
            cortex = cortex_proxy_url(self.enricher.pool)
        ultimo = None
        for tentativa in range(1, self.MAX_ROTACOES + 1):
            proxy = self.enricher._next_proxy(tentados)
            if not proxy and self.enricher.pool is not None:
                break
            if proxy != cortex:
                tentados.add(proxy)
            try:
                resp = self.enricher.session.get(
                    url, params=params,
                    headers=dict(HEADERS.get(self.TRIBUNAL, {}), **(headers or {})),
                    proxies=({'http': proxy, 'https': proxy} if proxy else None),
                    timeout=self.TIMEOUT_BUSCA)
            except (requests.ConnectionError, requests.Timeout,
                    requests.exceptions.ChunkedEncodingError) as exc:
                ultimo = f'transporte: {str(exc)[:120]}'
                if proxy != cortex:
                    self.enricher.pool.mark_bad(proxy)
                continue
            if resp.status_code in (400, 401, 403, 429):
                # 403/429 aqui é RAJADA, não IP ruim: trocar de endereço e
                # disparar de novo no mesmo instante queima o pool inteiro.
                # Espera crescente entre as tentativas — 1s, 2s, 3s...
                ultimo = f'bloqueado {resp.status_code}'
                if proxy != cortex and self.enricher.pool is not None:
                    self.enricher.pool.mark_bad(proxy)
                time.sleep(min(tentativa, 5))
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
        # Fresco a cada requisição: o servidor valida a janela de timestamp.
        # Vem de `enrichers/fingerprints.py`, que é puro — importar o módulo do
        # enricher aqui puxaria Django e impediria o motor de rodar fora do
        # container (é assim que `scripts/validar_busca_parte.py` funciona).
        from enrichers.fingerprints import tjmt

        return {'X-Fingerprint': tjmt()}

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
            time.sleep(self.PAUSA_ENTRE_PAGINAS_S)

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
    """`consilium-rest` — sete rotas, todas lidas do bundle da SPA.

    `ADVOGADO` não tem rota própria e mesmo assim é suportado: no TJPA o
    advogado é **participante do processo**, então ele responde pela rota do
    nome da parte. Medido em 08/09/2026 com uma advogada real de Belém — o
    desambiguador devolve "BERNARDO ARAUJO DA LUZ (116)" e a rota exata pagina
    os 116. Ausência de ROTA não era ausência de BUSCA, e a versão anterior
    deste catálogo dizia "a fonte não oferece" por ter lido só o bundle.
    """

    CRITERIOS_SUPORTADOS = frozenset({DOCUMENTO, NOME, OAB, ADVOGADO})
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
            time.sleep(self.PAUSA_ENTRE_PAGINAS_S)


class BuscaTjrs(BuscaPorParte):
    """Consulta processual pública do TJRS (eproc) — rotas lidas do bundle da SPA.

    Dois critérios, os dois exercitados ao vivo em 15/10/2026:

    * **nome** — em DOIS passos, como a SPA: `eprocBuscaPorNomesDaParte`
      devolve as PARTES (nome, CPF/CNPJ mascarado, `codParte1g`/`codParte2g`),
      500 por página, ativos e baixados; `eprocBuscaProcessosPorCodParte` lista
      os processos de cada uma. Só as grafias IGUAIS ao nome pedido seguem; as
      parecidas que a pesquisa fonética trouxe e não seguimos vão no aviso.
    * **oab** — `eprocBuscaProcessosPorOAB`. A janela é `registroInicial` …
      `registroInicial + 1999` (2.000 linhas por chamada, qualquer que seja o
      `registroFinal`) e o fim é **404** "Nenhum registro". Medido com uma OAB
      real de Porto Alegre: janelas 1, 1001 e 2001 trouxeram 2.000, 2.000 e
      1.433 linhas; 3001 deu 404; 3.394 processos distintos na união.

    Fora: **documento** (a consulta pública não busca por CPF/CNPJ — só mostra
    a máscara no resultado do nome) e **advogado** por nome (não há rota).

    Paginação honesta (revisão do PR #27): uma página só é emitida com
    processo novo, `tem_proxima` diz a verdade (lookahead de uma página), e
    o fim é a FONTE dizer que acabou (página curta / 404) — página inteira de
    repetidos não encerra. Teto de requisições atingido sai como `aviso_fonte`
    na última página, com `tem_proxima=True`: o job conta como truncado.
    """

    CRITERIOS_SUPORTADOS = frozenset({NOME, OAB})
    TETO_DA_FONTE = None
    POR_PAGINA = 500
    JANELA_OAB = 2000
    SITUACOES = ('A', 'B')
    TIMEOUT_BUSCA = (10, 180)
    PAUSA_ENTRE_PAGINAS_S = 0.5
    #: Requisições por página emitida, no máximo — as que vêm só com repetidos
    #: ou de partes sem processo não viram página, mas também não são de graça.
    REQUISICOES_POR_PAGINA = 4

    def __init__(self, enricher_cls, prefer_cortex: bool | None = None):
        self.enricher = enricher_cls(prefer_cortex=bool(prefer_cortex))
        self.TRIBUNAL = enricher_cls.TRIBUNAL_SIGLA
        self.avisos: list[str] = []

    def _get(self, caminho: str, params: dict):
        from enrichers.tjrs import TjrsEnricherError
        try:
            status, corpo = self.enricher.get_api(caminho, params, timeout=self.TIMEOUT_BUSCA)
        except TjrsEnricherError as exc:
            raise FonteIndisponivel(f'TJRS: {exc}') from exc
        return None if status == 404 else corpo

    def partes_por_nome(self, nome: str, teto_requisicoes: int = 20) -> list[dict]:
        """Todas as partes de grafia igual, paginando a pesquisa nas duas situações."""
        from .rest_parser import _sem_acento
        alvo = _sem_acento(nome)
        partes: dict[str, dict] = {}
        parecidas: set[str] = set()
        feitas = 0
        for situacao in self.SITUACOES:
            pagina = 1
            while True:
                if feitas >= teto_requisicoes:
                    self.avisos.append(
                        f'a lista de partes com nome parecido passou de {feitas} páginas '
                        f'de {self.POR_PAGINA}; o resto não foi lido')
                    return list(partes.values())
                corpo = self._get('/eprocBuscaPorNomesDaParte', {
                    'totalRegistrosPorPagina': str(self.POR_PAGINA), 'numeroPagina': str(pagina),
                    'nomeParte': nome, 'tipoPesquisa': 'F', 'situacao': situacao, 'perfil': '0'})
                feitas += 1
                linhas = (corpo or {}).get('data') or []
                for p in parse_tjrs_nomes(corpo, nome):
                    partes.setdefault(p['codParte1g'], p)
                parecidas.update(str(b.get('nome') or '').strip() for b in linhas
                                 if _sem_acento(str(b.get('nome') or '')) != alvo)
                if len(linhas) < self.POR_PAGINA:
                    break
                pagina += 1
        parecidas.discard('')
        if parecidas:
            self.avisos.append(
                f'{len(parecidas)} grafia(s) parecida(s) não seguida(s) — a busca usa só a '
                f'grafia exata (ex.: {", ".join(sorted(parecidas)[:3])})')
        return list(partes.values())

    def paginar(self, criterio: str, valor: str,
                teto_paginas: int = 40) -> Iterator[PaginaResultado]:
        self.exigir_suporte(criterio)
        self.avisos = []
        teto_req = teto_paginas * self.REQUISICOES_POR_PAGINA
        brutas = (self._lotes_oab(valor, teto_req) if criterio == OAB
                  else self._lotes_nome(valor, teto_req))
        vistos: set[str] = set()
        pendente: PaginaResultado | None = None
        emitidas = 0
        for itens in brutas:
            novos = [i for i in itens if i.numero_cnj not in vistos]
            if not novos:
                continue
            vistos.update(i.numero_cnj for i in novos)
            if pendente is not None:
                pendente.tem_proxima = True
                yield pendente
                emitidas += 1
                if emitidas >= teto_paginas:
                    return              # o job vê teto + tem_proxima → truncado
            pendente = PaginaResultado(itens=novos, pagina=emitidas + 1, tem_proxima=False)
        if pendente is None and self.avisos:
            pendente = PaginaResultado(itens=[], pagina=1)
        if pendente is not None:
            if self.avisos:
                pendente.aviso_fonte = '; '.join(self.avisos)
                if any('não foi lido' in a or 'teto' in a for a in self.avisos):
                    pendente.tem_proxima = True
            yield pendente

    def _lotes_nome(self, nome: str, teto_req: int):
        feitas = 0
        for parte in self.partes_por_nome(nome):
            for situacao in self.SITUACOES:
                numero = 1
                while True:
                    if feitas >= teto_req:
                        self.avisos.append(f'teto de {teto_req} requisições atingido')
                        return
                    params = {'totalRegistrosPorPagina': str(self.POR_PAGINA),
                              'numeroPagina': str(numero),
                              'codParte1g': parte['codParte1g'],
                              'tipoPesquisa': 'F', 'situacao': situacao, 'perfil': '0'}
                    if parte['codParte2g']:
                        params['codParte2g'] = parte['codParte2g']
                    corpo = self._get('/eprocBuscaProcessosPorCodParte', params)
                    feitas += 1
                    linhas = len((corpo or {}).get('data') or [])
                    yield parse_tjrs_processos(corpo)
                    if linhas < self.POR_PAGINA:
                        break
                    numero += 1
                    time.sleep(self.PAUSA_ENTRE_PAGINAS_S)

    def _lotes_oab(self, valor: str, teto_req: int):
        uf, numero = oab_tjrs(valor)
        feitas = 0
        for situacao in self.SITUACOES:
            inicio = 1
            while True:
                if feitas >= teto_req:
                    self.avisos.append(f'teto de {teto_req} requisições atingido')
                    return
                corpo = self._get('/eprocBuscaProcessosPorOAB', {
                    'ufOAB': uf, 'numeroOAB': numero, 'situacao': situacao,
                    'registroInicial': str(inicio),
                    'registroFinal': str(inicio + self.JANELA_OAB - 1),
                    'comarca': '', 'ordenarPor': 'M'})
                feitas += 1
                linhas = (corpo or {}).get('data') or []
                if not linhas:
                    break
                yield parse_tjrs_processos(corpo)
                inicio += self.JANELA_OAB
                time.sleep(self.PAUSA_ENTRE_PAGINAS_S)


def oab_tjrs(valor: str) -> tuple[str, str]:
    """`'RS083749'` → `('RS', '83749')`; `'12345A/RS'` → `('RS', '12345A')`.

    UF: as duas letras do começo (ou do fim, depois de `/`); padrão RS. Número:
    sem zero à esquerda (medido: `83749` responde), preservando a letra de
    sufixo da inscrição suplementar (`12345A`) — ela é parte do número.
    """
    bruto = (valor or '').upper().replace(' ', '')
    m = re.match(r'^([A-Z]{2})?[-/]?0*(\d+)([A-Z]?)(?:[-/]([A-Z]{2}))?$', bruto)
    if not m:
        digitos = _so_digitos(bruto)
        return 'RS', str(int(digitos)) if digitos else ''
    uf = m.group(1) or m.group(4) or 'RS'
    return uf, m.group(2) + m.group(3)
