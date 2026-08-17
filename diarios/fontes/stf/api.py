"""Cliente da API pública de publicações do STF Digital.

A API NÃO é documentada nem versionada — foi descoberta por sniffing do bundle
Angular de `digital.stf.jus.br/publico/publicacoes` (chromium headless + CDP).
Isso define a postura deste módulo: validar tudo o que vem, falhar alto quando o
contrato mudar (`exigir_chaves` + `SchemaDriftAlert` no coletor) e nunca assumir
que "200" significa "dado".

O que foi MEDIDO (16/08/2026) e vira regra aqui
-----------------------------------------------
1. **CSRF de Spring**: POST sem token devolve HTTP 403 com o corpo literal
   `An expected CSRF token cannot be found`. A cura é barata: um GET em
   `/ultimo-dje` planta os cookies `XSRF-TOKEN` + `JSESSIONID` na sessão, e daí
   basta ecoar o valor no header `X-XSRF-TOKEN`.
2. **WAF só na casca**: a rota HTML `/publico/publicacoes` responde 202 com
   `x-amzn-waf-action: challenge`; o caminho `/decisoes-publicacoes/api/public/**`
   NÃO é desafiado. Se um dia for, o coletor morre — por isso o 202/challenge é
   tratado como erro explícito (`DesafioWAF`) em vez de virar JSON inválido.
3. **Cadeia TLS incompleta**: `*.stf.jus.br` é emitido pela GlobalSign GCC R6
   AlphaSSL CA 2025 e o servidor **não manda o intermediário** no handshake
   (`unable to get local issuer certificate`). A casa NÃO desliga verificação
   por isso: `ca_stf.pem` traz o intermediário + a raiz R6, e é ele que vai em
   `verify=`. Ver o comentário em `CA_BUNDLE`.
4. **`quantidade` > 500 → HTTP 422** (`{"userMessage":"deve ser menor ou igual a
   500"}`). 500 é o teto real, não uma escolha nossa.
5. **`total` só vale na PÁGINA 1**: pedindo `pagina=999` de um dia com 742
   publicações a resposta vem com `publicacoes: []` e **`total: 0`** (as
   `agregacoes`, curiosamente, continuam preenchidas). Guardar o `total` da
   primeira página é o que permite fechar a conta no fim da paginação.
6. **As datas são epoch em MILISSEGUNDOS com corte em BRT (UTC-3)**. Errar o
   fuso por 3h vaza publicação do dia vizinho para dentro da janela. Ver
   `janela_epoch_ms`.
"""

import logging
import os
from datetime import date, datetime, timedelta, timezone

from diarios.base import (
    ColetaPausada,
    ColetorError,
    FonteOcupada,
    RespostaInvalida,
    SessaoDiario,
    exigir_chaves,
)

logger = logging.getLogger('voyager.diarios.stf.api')

BASE = 'https://digital.stf.jus.br/decisoes-publicacoes/api/public'
URL_PUBLICACOES = f'{BASE}/publicacoes'
URL_ULTIMO_DJE = f'{BASE}/ultimo-dje'

#: Teto da própria API: `quantidade=1000` devolve 422. Um dia útil (~590
#: publicações) sai em 2 requisições.
QUANTIDADE_MAX = 500

#: Guarda-corpo de paginação. O maior dia medido tem 742 publicações; 40 páginas
#: de 500 são 20 mil. Se estourar isso, alguma coisa está errada (janela mal
#: montada, API repetindo página) e é melhor explodir do que rodar para sempre.
MAX_PAGINAS = 40

#: Corte do dia no fuso de Brasília. O STF divulga o DJe por volta das 20h BRT
#: para publicação no dia útil seguinte, então a fronteira do "dia de
#: divulgação" é a meia-noite BRT = 03:00 UTC.
UTC_MENOS_3 = timezone(timedelta(hours=-3))

#: Chaves de topo da resposta. Sem elas não há dado — e uma API não-contratada
#: pode passar a devolver `{"errors": [...]}` com 200 a qualquer momento.
CHAVES_RESPOSTA = frozenset({'publicacoes', 'total'})

#: Schema de UMA publicação, conferido em 200 itens reais de 13/08/2026. Serve
#: de baseline do `SchemaDriftAlert` — mesma mecânica que o DJEN usa desde o
#: drift de 2026-07-05 (`djen/parser.py::EXPECTED_KEYS`).
CHAVES_PUBLICACAO = frozenset({
    'id', 'processo', 'processoId', 'tipo', 'relator', 'divulgacao', 'publicacao',
    'texto', 'envolvidos', 'codigo', 'observacao', 'responsavel', 'tipoConteudo',
    'confidencialidade', 'colegiado', 'sessao', 'descricao', 'tipoPronunciamentoId',
})

#: Bundle com o intermediário AlphaSSL 2025 + a raiz GlobalSign R6.
#:
#: Por que um bundle próprio em vez de `verify=False`: desligar verificação num
#: coletor que roda sozinho de madrugada é abrir a porta para MITM silencioso
#: numa fonte que é EVIDÊNCIA judicial. Por que não confiar no CA bundle do
#: sistema: o servidor do STF não envia o intermediário, então nem uma máquina
#: com o `ca-certificates` em dia fecha a cadeia.
#:
#: CUSTO DISSO, dito na cara: o intermediário expira em 21/05/2027. Quando o STF
#: trocar o certificado, o coletor falha de forma barulhenta (SSLError) em vez de
#: aceitar qualquer coisa em silêncio — e a cura é rebaixar o novo intermediário
#: (`openssl s_client -connect digital.stf.jus.br:443` → seguir o AIA). Trocável
#: sem deploy por `settings.DIARIOS_STF_CA_BUNDLE`.
CA_BUNDLE = os.path.join(os.path.dirname(__file__), 'ca_stf.pem')

#: O portal legado (`portal.stf.jus.br`, usado só pelo resolvedor de CNJ) recusa
#: UA curto com 403 — `Mozilla/5.0` cru não passa, UA de Chrome completo passa.
#: A API não exige, mas manter o mesmo UA nas duas pontas evita descobrir isso
#: duas vezes.
USER_AGENT_NAVEGADOR = (
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/126.0.0.0 Safari/537.36'
)

#: Contrapeso do UA de navegador: a casa se identifica no `From`, que é o
#: cabeçalho que a RFC 9110 reserva exatamente para "quem é o humano por trás
#: deste agente". Não fingir ser gente é regra de conduta daqui — o UA de Chrome
#: existe porque o IIS do portal recusa o UA honesto, não para nos esconder.
HEADERS_IDENTIFICACAO = {'From': 'voyager-ops (+https://voyager.was.dev.br)'}


class DesafioWAF(RespostaInvalida):
    """O AWS WAF passou a desafiar a rota da API (hoje ele só protege a casca
    HTML). É o cenário que mata este coletor, então tem nome próprio: quem lê o
    log precisa saber que a cura não é retry, é chromium headless — que é lento,
    frágil e caro, e por isso merece uma decisão humana."""


def janela_epoch_ms(dia: date) -> tuple[int, int]:
    """(início, fim) do dia de DIVULGAÇÃO em epoch de milissegundos, corte BRT.

    O fim é o último milissegundo do dia, não a meia-noite seguinte: `dataFim`
    é inclusivo na API, e usar 00:00 do dia seguinte arrastava publicações da
    virada para dentro da janela.
    """
    ini = datetime(dia.year, dia.month, dia.day, tzinfo=UTC_MENOS_3)
    fim = ini + timedelta(days=1)
    return int(ini.timestamp() * 1000), int(fim.timestamp() * 1000) - 1


def corpo_busca(dia: date, pagina: int, quantidade: int = QUANTIDADE_MAX) -> dict:
    """Body do POST de busca.

    `tipoPesquisa` aceita `PUBLICACAO` e `DIVULGACAO` em UNIÃO. Usamos só
    DIVULGACAO de propósito: cada publicação tem exatamente uma data de
    divulgação, então o dia de divulgação PARTICIONA o acervo — coletar dia a
    dia não repete nem perde item. Com a união, o mesmo ato apareceria no dia X
    (divulgação) e no dia X+1 (publicação), dobrando o tráfego para nada.
    Verificado: as 742 publicações de 13/08/2026 vieram todas com
    `divulgacao` em 13/08, sem vazamento de fronteira.

    Os `filtros` vazios são obrigatórios — o backend do Spring exige o objeto,
    e a chave `Sessão` vai acentuada porque é assim que o servidor a espera.
    """
    ini, fim = janela_epoch_ms(dia)
    return {
        'termo': '',
        'pagina': pagina,
        'quantidade': quantidade,
        'data': ini,
        'dataFim': fim,
        'tipoPesquisa': ['DIVULGACAO'],
        'filtros': {'Tipo': [], 'Relator': [], 'Sessão': [], 'Colegiado': []},
        'processo': '',
    }


class SessaoSTF:
    """Sessão HTTP do STF: handshake de CSRF + POST paginado.

    Envolve uma `SessaoDiario` (backoff, circuit-breaker por fonte, kill switch,
    rate limit auto-imposto) em vez de falar com `requests` direto — o cliente
    HTTP da casa já foi pago em incidente e não se reinventa aqui.
    """

    def __init__(self, sessao: SessaoDiario):
        self.sessao = sessao
        self._token: str | None = None
        #: `total` declarado na PÁGINA 1 da última varredura de dia.
        self.ultimo_total: int | None = None

    # -- CSRF -----------------------------------------------------------------
    def _handshake(self) -> str:
        """GET barato que planta `XSRF-TOKEN`/`JSESSIONID` na sessão.

        Vale como health-check de graça: se este GET não devolve uma data, a API
        mudou (ou o WAF chegou) e não adianta tentar a busca.
        """
        resp = self.sessao.get(URL_ULTIMO_DJE, headers=HEADERS_IDENTIFICACAO)
        self._checar_waf(resp)
        token = self.sessao.session.cookies.get('XSRF-TOKEN')
        if not token:
            raise RespostaInvalida(
                'STF: /ultimo-dje não devolveu cookie XSRF-TOKEN — sem ele todo POST volta 403'
            )
        self._token = token
        return token

    def _checar_waf(self, resp) -> None:
        acao = resp.headers.get('x-amzn-waf-action')
        if acao or resp.status_code == 202:
            raise DesafioWAF(
                f'STF: WAF desafiou a API (status={resp.status_code}, x-amzn-waf-action={acao!r}). '
                'Hoje só a rota HTML é protegida; se isto virar rotina, a coleta exige browser.'
            )

    def ultimo_dje(self) -> date:
        """Data do último DJe divulgado, segundo o próprio STF (ex.: 2026-08-14).

        É o teto do catálogo: catalogar dia futuro só criaria unidade que nasce
        vazia e é retentada até o `MAX_TENTATIVAS`.
        """
        resp = self.sessao.get(URL_ULTIMO_DJE, headers=HEADERS_IDENTIFICACAO)
        self._checar_waf(resp)
        self._token = self.sessao.session.cookies.get('XSRF-TOKEN') or self._token
        try:
            bruto = resp.json()
        except ValueError as exc:
            raise RespostaInvalida(f'STF: /ultimo-dje não devolveu JSON ({len(resp.content)} bytes)') from exc
        try:
            return date.fromisoformat(str(bruto).strip())
        except (TypeError, ValueError) as exc:
            raise RespostaInvalida(f'STF: /ultimo-dje devolveu {bruto!r}, esperado YYYY-MM-DD') from exc

    # -- busca ----------------------------------------------------------------
    def buscar(self, dia: date, pagina: int, quantidade: int = QUANTIDADE_MAX) -> dict:
        """Uma página de publicações divulgadas em `dia`. Já validada."""
        if quantidade > QUANTIDADE_MAX:
            # Falha local em vez de gastar um request para colher o 422 da API.
            raise ValueError(f'quantidade={quantidade} > {QUANTIDADE_MAX} (a API devolve HTTP 422)')
        if self._token is None:
            self._handshake()

        corpo = corpo_busca(dia, pagina, quantidade)
        try:
            resp = self._post(corpo)
        except (ColetaPausada, FonteOcupada):
            # Kill switch e circuito aberto NÃO se retenta: adiar é o
            # comportamento correto, e o runner sabe tratar.
            raise
        except ColetorError:
            # O token do Spring expira junto com a sessão do outro lado, e o
            # sintoma é 403 — que a `SessaoDiario` já esgotou em retry antes de
            # chegar aqui. Uma segunda chance com token NOVO é barata e resolve
            # o caso comum (worker longo, sessão reciclada no servidor).
            logger.warning('STF: busca falhou; refazendo handshake de CSRF e tentando de novo')
            self._handshake()
            resp = self._post(corpo)

        self._checar_waf(resp)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise RespostaInvalida(
                f'STF: resposta não-JSON na página {pagina} de {dia} ({len(resp.content)} bytes)'
            ) from exc
        exigir_chaves(payload, CHAVES_RESPOSTA, contexto=f'STF {dia} pág. {pagina}')
        if not isinstance(payload.get('publicacoes'), list):
            raise RespostaInvalida(f'STF {dia} pág. {pagina}: `publicacoes` não é lista')
        return payload

    def _post(self, corpo: dict):
        return self.sessao.post(
            URL_PUBLICACOES, json=corpo,
            headers={'X-XSRF-TOKEN': self._token or '', 'Accept': 'application/json',
                     **HEADERS_IDENTIFICACAO},
        )

    def iter_dia(self, dia: date):
        """Itera TODAS as publicações divulgadas no dia, e devolve o `total`.

        O total fica em `self.ultimo_total`, lido pelo coletor depois do laço.
        Ele vem da PÁGINA 1 porque `total` ZERA nas páginas além do fim
        (medido: `pagina=999` de um dia com 742 itens devolve `total: 0`).
        """
        self.ultimo_total = None
        for pagina in range(1, MAX_PAGINAS + 1):
            payload = self.buscar(dia, pagina)
            itens = payload['publicacoes']
            if pagina == 1:
                self.ultimo_total = int(payload.get('total') or 0)
                logger.info('STF %s: total declarado=%d', dia, self.ultimo_total)
            yield from itens
            if len(itens) < QUANTIDADE_MAX:
                return
        raise RespostaInvalida(
            f'STF {dia}: paginação passou de {MAX_PAGINAS} páginas — janela ou API suspeitas'
        )
