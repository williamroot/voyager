"""Enricher genérico via PJe consulta pública (sem login).

PJe é o sistema padrão CNJ usado em vários TRFs/TJs. A consulta pública
expõe um form JSF (`fPP`) que aceita o número CNJ e retorna um link pra
página de detalhe com metadados + polos.

Subclasses precisam apenas configurar `BASE_URL`, `LIST_URL` e
`DETALHE_PATH`. Toda a lógica de form/parsing/dedupe de partes é
compartilhada.

Workers só publicam o resultado bruto no stream — o drainer (consumer
único) faz a normalização e o write em bulk no Postgres.
"""
import datetime as _dt
import logging
import re
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup
from django.utils import timezone

from djen.proxies import ProxyScrapePool, cortex_proxy_url, sessao_rotativa
from tribunals.models import Process

from . import stream
from .faixas import faixa_fora_da_fonte
from .parsers import (
    classificar_tipo_parte,
    limpar_nome,
    parse_documento,
    parse_oab,
    parse_role,
)

CAMPO_NUM = 'fPP:numProcesso-inputNumeroProcessoDecoration:numProcesso-inputNumeroProcesso'

DEFAULT_HEADERS = {
    'User-Agent': 'voyager-ops/0.1 (+pje-consulta-publica)',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.8',
}


class PjeEnricherError(Exception):
    pass


class PjeWafChallenge(PjeEnricherError):
    """O AWS WAF do tribunal devolveu um DESAFIO (`x-amzn-waf-action: challenge`,
    HTTP 202 + `awsWafCookie`) em vez do conteúdo.

    Por que é classe separada e, principalmente, por que NÃO é tratada como 403
    de proxy: **o desafio não é do IP, é do site**. Medido no TJPE em
    29/08/2026, mesmo path `/1g/ConsultaPublica/listView.seam`, mesmo
    User-Agent:

    | caminho                                     | desafiadas |
    |---------------------------------------------|-----------|
    | Cortex residencial (IP novo a cada request) | 29 de 30  |
    | IP do próprio host de workers (sem proxy)   | 16 de 20  |

    Trocar de IP não sai do desafio — 30 IPs residenciais distintos levaram 29
    desafios. O código antigo tratava isso como bloqueio POR PROXY: rotacionava
    `MAX_PROXY_ROTATIONS` vezes e chamava `pool.mark_bad()` em cada uma, ou seja
    **queimava até 10 IPs do pool COMPARTILHADO por job** (que serve também a
    ingestão DJEN e os outros enrichers) para no fim gravar `erro` do mesmo
    jeito. Censo de 10 min na `.102` (29/08/2026): TJPE fez 1.379 `mark_bad`,
    o maior de todos os tribunais, para 7 `ok` e 129 `erro`.

    Resolver o desafio (rodar o `challenge.js`, cookie do WAF) seria evasão
    anti-bot — NÃO autorizada (decisão de produto, 25/08/2026). Então o
    comportamento correto é: tentar um punhado de vezes (uma fatia passa),
    **sem marcar IP como ruim**, e desistir com ERRO que diz o número real.
    """
    pass


class PjeServerError(PjeEnricherError):
    """PJe retornou HTTP 200 mas com página de erro JBoss/Hibernate
    (banco do tribunal indisponível, transaction abortada, etc.).
    Não é 403 (proxy block) nem 404 (não encontrado) — é o servidor do
    tribunal com problema interno. Diferenciamos pra:
    1) Não retentar com proxy diferente (não vai resolver)
    2) Marcar Process com status='erro' + mensagem clara `tribunal_indisponivel`
    3) Operacionalmente saber que pausa não é nosso problema, é do TRF
    """
    pass


# Padrões que identificam página de erro do JBoss/Seam do PJe.
# Quando algum aparece em resposta 200, sabemos que o tribunal está com
# problema interno (ex: pool de conexão DB esgotado, transaction abortada).
_PJE_ERROR_MARKERS = (
    'errorUnexpected.seam',
    'IJ000459',  # Transaction is not active
    'Could not open connection',
    'Transaction is not active',
    'GenericJDBCException',
    'Erro inesperado, por favor tente novamente',
    # Página oficial de manutenção/indisponibilidade do TRF1 (publicada
    # quando o PJe sai do ar planejado ou cai). Header "Secretaria de
    # Tecnologia da Informação" + "indisponível no momento".
    'sistema Processo Judicial Eletr',  # "Eletrônico (PJe) está indisponível"
    'indispon&iacute;vel no momento',
    'indisponível no momento',
    'RelatorioIndisponibilidade',  # link no rodapé da página de indisponibilidade
)


def _detect_pje_server_error(text: str) -> str | None:
    """Retorna o marker encontrado se a resposta indica erro JBoss; None caso contrário."""
    if not text:
        return None
    sample = text[:4096]  # checa só os primeiros 4KB — markers ficam no topo
    for m in _PJE_ERROR_MARKERS:
        if m in sample:
            return m
    return None


#: Ações do AWS WAF que significam "não vou te servir agora". `challenge` é o
#: JS challenge (HTTP 202 + `awsWafCookie`); `captcha` é o CAPTCHA. Nenhuma das
#: duas se resolve trocando de IP, e resolvê-las não está autorizado.
_WAF_ACTIONS = frozenset({'challenge', 'captcha'})


def _detectar_desafio_waf(resp: requests.Response) -> bool:
    """`True` se a resposta é um desafio do AWS WAF em vez do conteúdo.

    Duas provas independentes, porque nem todo edge devolve as duas:
    1. o header `x-amzn-waf-action` (o que o ALB carimba — prova direta);
    2. o corpo com o script `awsWafCookie`/`challenge.js` num status que o PJe
       nunca usaria para conteúdo (202/405/403/429).

    Não basta procurar `awswaf` no corpo de QUALQUER 200: a página real do PJe
    pode carregar um script do WAF sem que a requisição tenha sido barrada.
    """
    acao = (resp.headers.get('x-amzn-waf-action') or '').strip().lower()
    if acao in _WAF_ACTIONS:
        return True
    if resp.status_code not in (202, 405, 403, 429):
        return False
    corpo = (resp.text or '')[:4096].lower()
    return 'awswaf' in corpo or 'challenge.js' in corpo


class BasePjeEnricher:
    """Subclasse define BASE_URL, LIST_URL, DETALHE_PATH e TRIBUNAL_SIGLA."""

    BASE_URL: str = ''
    LIST_URL: str = ''
    DETALHE_PATH: str = ''           # ex.: '/consultapublica/ConsultaPublica/DetalheProcessoConsultaPublica'
    # 2º grau (opt-in). Tribunais cujo PJe expõe a consulta pública de 2º grau
    # num host/path próprio (ex.: TJMA → pje2.tjma.jus.br/pje2g) definem estes.
    # Vazios ⇒ tribunal só tem 1º grau público; processos de 2g caem nas URLs
    # de 1g (comportamento legado, inalterado). O HTML do PJe consulta pública
    # é idêntico nos dois grais — só as URLs mudam, então o parsing de
    # detalhe/partes é reaproveitado integralmente. Mesmo padrão do
    # `BaseEsajEnricher` (cpopg/cposg), aqui roteado por host/path.
    BASE_URL_2G: str = ''
    LIST_URL_2G: str = ''
    DETALHE_PATH_2G: str = ''
    TRIBUNAL_SIGLA: str = ''
    LOG_NAME: str = 'voyager.enrichers.pje'
    USER_AGENT: Optional[str] = None  # Subclasse pode sobrescrever (ex: tribunais atrás de WAF que rejeita UA identificador)

    #: Faixas de CNJ que ESTE PJe comprovadamente não tem — o tribunal roda um
    #: SEGUNDO sistema (eproc) e a fatia dele não está na consulta pública do
    #: PJe. Formato `(prefixo, ano_mínimo, motivo)`; vazio = tribunal medido e
    #: sem segunda fonte, ou não medido (abster > chutar). Ver `faixas.py` para
    #: o método e a evidência exigida antes de acrescentar uma linha.
    FORA_DA_FONTE_FAIXAS: tuple = ()

    def __init__(self, pool: Optional[ProxyScrapePool] = None, prefer_cortex: bool = False):
        if not (self.BASE_URL and self.LIST_URL and self.DETALHE_PATH and self.TRIBUNAL_SIGLA):
            raise NotImplementedError('Subclasse deve definir BASE_URL/LIST_URL/DETALHE_PATH/TRIBUNAL_SIGLA')
        self.pool = pool or ProxyScrapePool.singleton()
        self.session = sessao_rotativa()   # cache de proxies limitado — ver AdaptadorProxyLimitado
        self.session.headers.update(DEFAULT_HEADERS)
        if self.USER_AGENT:
            self.session.headers['User-Agent'] = self.USER_AGENT
        self.timeout = (10, 60)
        self.logger = logging.getLogger(self.LOG_NAME)
        # Quando True (cliques manuais via fila `manual`), tenta Cortex
        # primeiro — proxy residencial premium, taxa de sucesso muito
        # maior que pool ProxyScrape rotativo. Click do user vira ~1-3s
        # em vez de 30s+ rotacionando proxies queimados.
        self.prefer_cortex = prefer_cortex
        # cortex_only (granular por tribunal, cache Redis): tribunais que
        # bloqueiam datacenter por completo (TJRO/TJAP) usam SÓ residencial.
        try:
            from .jobs import is_cortex_only
            self.cortex_only = is_cortex_only(self.TRIBUNAL_SIGLA)
        except Exception:  # noqa: BLE001 — cache indisponível: comportamento normal
            self.cortex_only = False

    @classmethod
    def fora_da_fonte(cls, numero_cnj: str) -> Optional[str]:
        """Motivo pelo qual este CNJ NÃO está nesta consulta pública — ou None.

        Barato e sem rede: é só a forma do CNJ. Serve para não gastar
        requisição (nem IP do pool COMPARTILHADO) perguntando ao sistema errado.
        """
        return faixa_fora_da_fonte(numero_cnj, cls.FORA_DA_FONTE_FAIXAS)

    def _recusar_fora_da_fonte(self, processo: Process, motivo: str,
                               direct_apply: bool) -> dict:
        """Recusa CONTADA, nunca corte mudo (regra nº 2 do CLAUDE.md).

        Gêmeo de `BaseEsajEnricher._recusar_fora_do_esaj`: cada recusa entra no
        contador por tribunal que sai em ERROR no refill e em
        `manage.py enrich_fora_do_esaj`.
        """
        from .jobs import registrar_fora_do_esaj
        registrar_fora_do_esaj(self.TRIBUNAL_SIGLA, motivo)
        self._emit(stream.build_nao_encontrado_payload(
            process_id=processo.pk, tribunal=processo.tribunal_id,
            numero_cnj=processo.numero_cnj,
            scraped_at=timezone.now().astimezone(_dt.timezone.utc).isoformat(),
        ), direct_apply)
        return {'cnj': processo.numero_cnj, 'status': 'nao_encontrado',
                'fora_do_esaj': motivo, 'requisicoes': 0}

    def enriquecer(self, processo: Process, direct_apply: bool = False) -> dict:
        """Faz scraping no PJe e publica o resultado no stream.

        Por default, publica no stream — drainer aplica em bulk com baixa
        concorrência. Com `direct_apply=True` (cliques manuais), aplica
        direto no DB no próprio worker — usuário vê dados imediatamente
        em vez de esperar drainer drenar o backlog (~10min em pico).
        """
        if processo.tribunal_id != self.TRIBUNAL_SIGLA:
            raise PjeEnricherError(
                f'Tribunal {processo.tribunal_id} não suportado por {self.__class__.__name__}.'
            )

        # Faixa que a fonte já provou não ter (o tribunal roda um SEGUNDO
        # sistema): não gastamos requisição nem IP do pool COMPARTILHADO pra
        # ouvir "não existe" garantido. Gêmeo do guard do e-SAJ.
        motivo = self.fora_da_fonte(processo.numero_cnj)
        if motivo:
            return self._recusar_fora_da_fonte(processo, motivo, direct_apply)

        # scraped_at sempre em UTC ISO8601 — drainer faz dedup por
        # comparação lexicográfica entre strings, e workers em TZs diferentes
        # quebram a ordem se cada um publicar com seu offset local.
        base = {
            'process_id': processo.pk,
            'tribunal': processo.tribunal_id,
            'numero_cnj': processo.numero_cnj,
            'scraped_at': timezone.now().astimezone(_dt.timezone.utc).isoformat(),
        }

        try:
            link_detalhe = self._buscar_processo(processo.numero_cnj)
        except PjeWafChallenge as exc:
            self._emit(stream.build_erro_payload(**base, erro=f'busca: {exc}'), direct_apply)
            self._sleep_after_waf_challenge()
            return {'cnj': processo.numero_cnj, 'status': 'erro', 'erro': str(exc)[:200]}
        except PjeServerError as exc:
            self._emit(stream.build_erro_payload(**base, erro=f'busca: {exc}'), direct_apply)
            self._sleep_after_server_error()
            return {'cnj': processo.numero_cnj, 'status': 'erro', 'erro': str(exc)[:200]}
        except Exception as exc:
            self._emit(stream.build_erro_payload(**base, erro=f'busca: {exc}'), direct_apply)
            return {'cnj': processo.numero_cnj, 'status': 'erro', 'erro': str(exc)[:200]}

        if not link_detalhe:
            self._emit(stream.build_nao_encontrado_payload(**base), direct_apply)
            return {'cnj': processo.numero_cnj, 'status': 'nao_encontrado'}

        try:
            soup = self._fetch_detalhe(link_detalhe)
            dados = self._extrair_dados(soup)
            partes = self._extrair_partes(soup)
        except PjeWafChallenge as exc:
            self._emit(stream.build_erro_payload(**base, erro=f'detalhe: {exc}'), direct_apply)
            self._sleep_after_waf_challenge()
            return {'cnj': processo.numero_cnj, 'status': 'erro', 'erro': str(exc)[:200]}
        except PjeServerError as exc:
            self._emit(stream.build_erro_payload(**base, erro=f'detalhe: {exc}'), direct_apply)
            self._sleep_after_server_error()
            return {'cnj': processo.numero_cnj, 'status': 'erro', 'erro': str(exc)[:200]}
        except Exception as exc:
            self.logger.exception('falha ao parsear detalhe', extra={'cnj': processo.numero_cnj})
            self._emit(stream.build_erro_payload(**base, erro=f'parse: {exc}'), direct_apply)
            return {'cnj': processo.numero_cnj, 'status': 'erro', 'erro': str(exc)[:200]}

        self._emit(stream.build_ok_payload(**base, dados=dados, partes=partes), direct_apply)
        return {
            'cnj': processo.numero_cnj,
            'status': 'ok',
            'classe_raw': dados.get('classe'),
            'partes_total': sum(len(v) for v in partes.values()),
        }

    def _emit(self, payload: dict, direct_apply: bool) -> None:
        """Publica no stream OU aplica direto no DB.

        `direct_apply=True`: usado pra cliques manuais — chama o
        drainer.apply_event no próprio worker, evitando o lag de drenagem
        (~10min em pico). User vê dados imediatos no DB após scrape OK.
        """
        if direct_apply:
            from .drainer import apply_event
            from django.db import transaction
            try:
                with transaction.atomic():
                    apply_event(payload)
            except Exception:
                self.logger.exception('apply_event direto falhou — fallback pro stream',
                                      extra={'process_id': payload.get('process_id')})
                stream.publish(payload)
        else:
            stream.publish(payload)

    SERVER_ERROR_SLEEP_SECONDS = 30
    #: Espera depois de bater o teto de desafios do WAF. Existe porque sem ela o
    #: job falha em ~0,6 s e o worker pega o próximo na hora: 14 réplicas do TJPE
    #: faziam 2.791 requisições em 10 min para 7 `ok`. Não é castigo, é o único
    #: freio — o desafio não passa por insistência.
    WAF_CHALLENGE_SLEEP_SECONDS = 30

    def _sleep_after_server_error(self) -> None:
        """Da uma pausa pro tribunal recuperar antes do worker pegar
        proximo job. Usado quando o PJe retorna pagina de erro JBoss
        (banco do tribunal travado) — bombardear com mais requests so
        atrapalha. Sleep eh INTRA-job (RQ vai contar isso no timeout
        da call atual antes de ir pro proximo)."""
        self.logger.warning('pje server error — sleep %ds antes de prox job',
                            self.SERVER_ERROR_SLEEP_SECONDS,
                            extra={'tribunal': self.TRIBUNAL_SIGLA})
        time.sleep(self.SERVER_ERROR_SLEEP_SECONDS)

    def _sleep_after_waf_challenge(self) -> None:
        """Freia o worker depois de o WAF ter desafiado até o teto."""
        self.logger.warning('aws waf challenge — sleep %ds antes do prox job',
                            self.WAF_CHALLENGE_SLEEP_SECONDS,
                            extra={'tribunal': self.TRIBUNAL_SIGLA})
        time.sleep(self.WAF_CHALLENGE_SLEEP_SECONDS)

    # ---------- HTTP ----------

    MAX_PROXY_ROTATIONS = 10
    #: Quantas respostas de DESAFIO do AWS WAF aceitamos antes de desistir do
    #: job. É baixo de propósito: o desafio não é por IP (ver `PjeWafChallenge`),
    #: então insistir só gasta requisição. Uma fatia passa — 1 em 15 pelo Cortex,
    #: 4 em 20 pelo IP direto — e por isso o teto não é 1.
    WAF_MAX_TENTATIVAS = 3

    def _next_proxy(self, exclude: set, force_cortex: bool = False) -> Optional[str]:
        """Próximo proxy. Por default: pool ProxyScrape (datacenter) primeiro,
        Cortex (residencial) como fallback. Com prefer_cortex=True (ou
        force_cortex — datacenter bloqueado por WAF): Cortex primeiro.

        force_cortex é o escalonamento: com um pool grande (centenas de IPs),
        as MAX_PROXY_ROTATIONS nunca esgotam o datacenter, então o Cortex
        (fallback natural) jamais era alcançado quando o tribunal bloqueia
        datacenter (403). Ao acumular 403/WAF, o rotation liga force_cortex e
        aí o residencial — que fura o WAF — entra."""
        if getattr(self, 'cortex_only', False):
            # SÓ residencial (datacenter 100% bloqueado neste tribunal).
            cortex = cortex_proxy_url(self.pool)
            return cortex if (cortex and cortex not in exclude) else None
        if self.prefer_cortex or force_cortex:
            cortex = cortex_proxy_url(self.pool)
            if cortex and cortex not in exclude:
                return cortex
        for _ in range(40):
            url = self.pool.get()
            if url and url not in exclude:
                return url
        if not self.prefer_cortex:
            cortex = cortex_proxy_url(self.pool)
            if cortex and cortex not in exclude:
                return cortex
        return None

    def _request_with_rotation(self, method: str, url: str, **kwargs) -> requests.Response:
        """Request com rotação automática em 403/429. Loga cada IP usado e
        cada rotação. Marca proxy do pool como bad em falha (acelera saída
        do pool). Sem sleep entre tentativas — a rotação dá um IP novo.
        Limite: MAX_PROXY_ROTATIONS.
        """
        tentados: set = set()
        last_status = None
        bloqueios_dc = 0  # 403 vindos do datacenter → após N, escala pro Cortex
        desafios_waf = 0  # desafios do AWS WAF: teto próprio, não queima IP
        for tentativa in range(1, self.MAX_PROXY_ROTATIONS + 1):
            # Datacenter bloqueado por WAF (TJRO/TJAP dão 403 em TODO IP do pool):
            # gastar as 10 rotações no datacenter nunca alcança o Cortex. Após 3
            # bloqueios, força o residencial — que fura o WAF.
            proxy_url = self._next_proxy(tentados, force_cortex=bloqueios_dc >= 3)
            if not proxy_url:
                self.logger.warning('pool exausto sem proxy disponível', extra={
                    'tentativa': tentativa, 'url': url,
                })
                break
            # O Cortex é um GATEWAY que troca de IP residencial a cada
            # request — reusá-lo é sempre um IP novo, então ele não entra em
            # `tentados`. Sem esta ressalva, um tribunal `cortex_only` tinha UMA
            # tentativa e só: na 2ª, `_next_proxy` via o Cortex no exclude e
            # devolvia None, o laço quebrava e o job virava `erro` sem retry.
            # (O `BaseEsajEnricher` já fazia certo; aqui faltava.)
            if proxy_url != cortex_proxy_url(self.pool):
                tentados.add(proxy_url)
            proxies = {'http': proxy_url, 'https': proxy_url}
            self.logger.info('pje request', extra={
                'method': method, 'url': url[:120], 'proxy': proxy_url,
                'tentativa': tentativa,
            })
            try:
                resp = self.session.request(
                    method, url, proxies=proxies, timeout=self.timeout, **kwargs,
                )
            except (requests.ConnectionError, requests.Timeout,
                    requests.exceptions.ChunkedEncodingError) as exc:
                self.logger.warning('proxy falhou (transport), rotacionando', extra={
                    'proxy': proxy_url, 'tentativa': tentativa, 'erro': str(exc)[:120],
                })
                if proxy_url != cortex_proxy_url():
                    self.pool.mark_bad(proxy_url)
                continue
            # AWS WAF challenge: HTTP 202/405 + `x-amzn-waf-action: challenge` e/ou
            # página `awsWafCookie` no lugar do conteúdo. NÃO é bloqueio por proxy
            # — trocar de IP não sai do desafio (ver `PjeWafChallenge`, com a
            # medição). Tem teto PRÓPRIO, não gasta as rotações do pool e, acima
            # de tudo, NÃO marca o IP como ruim: o pool é COMPARTILHADO com a
            # ingestão DJEN e com os outros enrichers.
            if _detectar_desafio_waf(resp):
                desafios_waf += 1
                self.logger.warning('desafio do AWS WAF (não é o IP — não queima proxy)', extra={
                    'tribunal': self.TRIBUNAL_SIGLA, 'status': resp.status_code,
                    'desafios': desafios_waf, 'teto': self.WAF_MAX_TENTATIVAS,
                    'url': url[:120],
                })
                if desafios_waf >= self.WAF_MAX_TENTATIVAS:
                    # Teto é ALERTA com o número real, nunca `return` discreto.
                    self.logger.error(
                        'AWS WAF: %d de %d respostas foram desafio em %s — desistindo do job '
                        '(resolver o desafio seria evasão anti-bot, não autorizada)',
                        desafios_waf, tentativa, self.TRIBUNAL_SIGLA,
                        extra={'tribunal': self.TRIBUNAL_SIGLA, 'url': url[:120]})
                    raise PjeWafChallenge(
                        f'aws_waf_challenge: {desafios_waf} desafios em {tentativa} '
                        f'tentativas (HTTP {resp.status_code})')
                continue
            if resp.status_code in (403, 429):
                self.logger.warning('proxy bloqueado pelo PJe (403/429), rotacionando', extra={
                    'proxy': proxy_url, 'status': resp.status_code,
                    'tentativa': tentativa,
                })
                if proxy_url != cortex_proxy_url():
                    self.pool.mark_bad(proxy_url)
                    # O contador do escalonamento. Ficou DECLARADO E NUNCA
                    # INCREMENTADO desde que nasceu: `force_cortex` era
                    # `0 >= 3` em toda tentativa, então o residencial jamais
                    # entrava. Medido em 25/08/2026 no TJRO — 5 de 5 jobs
                    # gastaram as 10 rotações em IP de datacenter (403 em
                    # todos), 13,91 s de mediana, desfecho `erro` em 100%.
                    bloqueios_dc += 1
                last_status = resp.status_code
                continue
            resp.raise_for_status()
            # Pré-200 OK do proxy não significa que o PJe entregou conteúdo
            # útil — TRF1 às vezes retorna 200 com página de erro JBoss
            # (banco do tribunal indisponível). Detecta e levanta exceção
            # específica pra não consumir rotações de proxy à toa.
            err_marker = _detect_pje_server_error(resp.text)
            if err_marker:
                self.logger.warning('pje retornou erro do servidor (não-recuperável via proxy)', extra={
                    'tribunal': self.TRIBUNAL_SIGLA, 'marker': err_marker,
                    'url': url[:120], 'tentativa': tentativa,
                })
                raise PjeServerError(f'tribunal_indisponivel: {err_marker}')
            return resp
        msg = f'{self.MAX_PROXY_ROTATIONS} proxies tentados sem sucesso'
        if last_status:
            msg += f' (último status {last_status})'
        raise requests.HTTPError(msg)

    def _get(self, url: str) -> requests.Response:
        return self._request_with_rotation('GET', url, allow_redirects=True)

    def _post(self, url: str, data: dict) -> requests.Response:
        return self._request_with_rotation('POST', url, data=data)

    # ---------- Etapas ----------

    def _extract_form_fields(self, soup: BeautifulSoup) -> dict:
        form = soup.find('form', {'id': 'fPP'})
        fields: dict = {}
        if not form:
            return fields
        for inp in form.find_all('input'):
            name = inp.get('name')
            if not name:
                continue
            tipo = (inp.get('type') or 'text').lower()
            if tipo in ('checkbox', 'radio') and not inp.get('checked'):
                continue
            fields[name] = inp.get('value', '')
        for sel in form.find_all('select'):
            name = sel.get('name')
            if not name:
                continue
            chosen = sel.find('option', selected=True) or sel.find('option')
            fields[name] = chosen.get('value', '') if chosen else ''
        return fields

    def _find_search_script_id(self, soup: BeautifulSoup) -> Optional[str]:
        form = soup.find('form', {'id': 'fPP'})
        if not form:
            return None
        for script in form.find_all('script'):
            sid = script.get('id', '')
            content = script.string or ''
            if sid.startswith('fPP:j_id') and 'executarPesquisaReCaptcha' in content:
                return sid
        for script in form.find_all('script'):
            sid = script.get('id', '')
            content = script.string or ''
            if (sid.startswith('fPP:j_id')
                and 'A4J.AJAX.Submit' in content
                and 'processosTable' not in sid
                and 'scTabela' not in content):
                return sid
        return None

    @staticmethod
    def _grau(cnj: str) -> str:
        """'2g' se o processo é de 2º grau (foro de origem OOOO == '0000',
        i.e. originário do tribunal), senão '1g'. Independente de tribunal —
        mesma regra do `BaseEsajEnricher._grau`."""
        return '2g' if re.sub(r'\D', '', cnj or '')[-4:] == '0000' else '1g'

    def _urls_for_grau(self, grau: str) -> tuple[str, str, str]:
        """`(BASE_URL, LIST_URL, DETALHE_PATH)` do grau pedido. Cai pro 1º grau
        quando o tribunal não configurou 2º grau (`LIST_URL_2G` vazio) — então
        um CNJ de 2g num tribunal só-1g segue o caminho legado."""
        if grau == '2g' and self.LIST_URL_2G:
            return self.BASE_URL_2G, self.LIST_URL_2G, self.DETALHE_PATH_2G
        return self.BASE_URL, self.LIST_URL, self.DETALHE_PATH

    def _buscar_processo(self, numero_cnj: str) -> Optional[str]:
        """Acha o link de detalhe, roteando por grau com fallback.

        O grau não vem nos metadados do Voyager (Process não tem campo `grau`)
        e NÃO dá pra inferir do CNJ com segurança: só os processos de
        competência originária do tribunal trazem o código do próprio tribunal
        no segmento OOOO; uma Apelação mantém o OOOO da comarca de origem
        (no TJMA, p.ex., G2 termina em `0001`, não `0000`). Por isso o `_grau`
        é só um palpite barato pra escolher por qual instância começar — se ela
        não acha o processo e o tribunal tem 2º grau configurado, tenta a outra
        instância antes de desistir. 1g e 2g são instâncias PJe separadas
        (hosts distintos), então um número só existe em uma delas: o fallback
        é determinístico, não ambíguo. Tribunais só-1g (sem `LIST_URL_2G`)
        fazem uma única busca — comportamento legado inalterado.
        """
        palpite = self._grau(numero_cnj)
        link = self._buscar_em_grau(numero_cnj, palpite)
        if link is None and self.LIST_URL_2G:
            outro = '1g' if palpite == '2g' else '2g'
            self.logger.info('grau fallback', extra={
                'cnj': numero_cnj, 'de': palpite, 'para': outro,
            })
            link = self._buscar_em_grau(numero_cnj, outro)
        return link

    def _buscar_em_grau(self, numero_cnj: str, grau: str) -> Optional[str]:
        base_url, list_url, detalhe_path = self._urls_for_grau(grau)
        resp = self._get(list_url)
        # Rede de segurança: um desafio do WAF que passou pelo `_request_with_
        # rotation` (ex.: veio com HTTP 200) não pode virar "ViewState não
        # encontrado" — esse erro é reservado para MUDANÇA DE LAYOUT, e
        # confundir os dois já mandou gente caçar parser quando o problema era
        # muro. Mesma exceção do teto, para o desfecho ser um só.
        low = (resp.text or '')[:4096].lower()
        if _detectar_desafio_waf(resp) or 'token.awswaf.com' in low:
            raise PjeWafChallenge(f'aws_waf_challenge (HTTP {resp.status_code})')
        soup = BeautifulSoup(resp.text, 'html.parser')
        vs = soup.find('input', {'name': 'javax.faces.ViewState'})
        if not vs or not vs.get('value'):
            amostra = ' '.join((resp.text or '')[:200].split())
            raise PjeEnricherError(f'javax.faces.ViewState não encontrado (HTTP '
                                   f'{resp.status_code}): {amostra}')

        fields = self._extract_form_fields(soup)
        search_id = self._find_search_script_id(soup) or 'fPP:j_id268'
        self.logger.info('search button id', extra={'id': search_id, 'grau': grau})

        payload = dict(fields)
        payload[CAMPO_NUM] = numero_cnj
        payload['fPP'] = 'fPP'
        payload['AJAXREQUEST'] = '_viewRoot'
        payload['javax.faces.ViewState'] = vs['value']
        payload[search_id] = search_id
        payload['AJAX:EVENTS_COUNT'] = '1'

        resp = self._post(list_url, payload)
        # Match do link de detalhe — DETALHE_PATH varia por tribunal (TRF1 usa
        # /consultapublica/..., TRF3/TJMA usam /pje/...; TJMA 2g usa /pje2g/).
        path_re = re.escape(detalhe_path) + r"/[^\"'<>\s]+"
        m = re.search(f"({path_re})", resp.text)
        if m:
            return base_url + m.group(1).replace('&amp;', '&')
        m_id = re.search(r"idProcessoTrf['\"]?\s*[:=]\s*['\"]?(\d+)", resp.text)
        if m_id:
            return f'{base_url}{detalhe_path}/listView.seam?ca={m_id.group(1)}'
        # Não logamos `resp.text` porque a página de resposta do PJe pode
        # conter PII (nome de outras partes, advogados) — só o cnj e tamanho
        # bastam pra triagem operacional.
        self.logger.warning('detalhe não encontrado', extra={
            'cnj': numero_cnj, 'resp_len': len(resp.text), 'grau': grau,
        })
        return None

    def _fetch_detalhe(self, link_detalhe: str) -> BeautifulSoup:
        time.sleep(0.4)
        resp = self._get(link_detalhe)
        return BeautifulSoup(resp.text, 'html.parser')

    # ---------- Parsing do detalhe ----------

    def _extrair_dados(self, soup: BeautifulSoup) -> dict:
        dados: dict = {}
        for prop in soup.select('div.propertyView'):
            label_el = prop.select_one('div.name label, div.name')
            value_el = prop.select_one('div.value')
            if not label_el or not value_el:
                continue
            chave = label_el.get_text(' ', strip=True).rstrip(':').lower()
            valor = value_el.get_text(' ', strip=True)
            if not valor:
                continue
            if 'classe' in chave and 'judicial' in chave:
                dados['classe'] = valor
            elif chave == 'assunto':
                dados['assunto'] = valor
            elif 'autua' in chave or 'distribu' in chave or 'ajuiza' in chave:
                dados['data_autuacao'] = valor
            elif 'valor' in chave and 'causa' in chave:
                dados['valor_causa'] = valor
            elif 'segredo' in chave or 'sigilo' in chave:
                dados['segredo_justica'] = 'sim' in valor.lower()

        for b in soup.find_all('b'):
            label = b.get_text(strip=True).lower()
            if 'rg' in label and 'julgador' in label:
                node = b.next_sibling
                while node is not None:
                    if isinstance(node, str):
                        txt = node.strip()
                        if txt:
                            dados['orgao_julgador'] = txt[:255]
                            break
                    elif getattr(node, 'name', None) == 'br':
                        pass
                    elif getattr(node, 'name', None) in ('div', 'b'):
                        break
                    node = node.next_sibling
                break

        return dados

    # ---------- Polos / Partes ----------

    _IGNORE_TEXTOS = frozenset({'participante', 'situação', 'situacao', 'ativo', 'inativo', ''})

    def _extrair_partes(self, soup: BeautifulSoup) -> dict[str, list[dict]]:
        polos = {'ativo': [], 'passivo': [], 'outros': []}
        for polo, div_id in (('ativo', 'poloAtivo'), ('passivo', 'poloPassivo'), ('outros', 'outrosInteressados')):
            block = soup.find('div', id=re.compile(div_id, re.IGNORECASE))
            if not block:
                continue
            polos[polo] = self._parse_polo(block)
        return polos

    def _parse_polo(self, block) -> list[dict]:
        partes: list[dict] = []
        rows = block.select('tbody tr') or block.select('li')
        for row in rows:
            spans = row.select('td > span span') or row.select('td span') or row.select('span')
            textos = []
            for sp in spans:
                t = sp.get_text(' ', strip=True)
                if not t or t.lower() in self._IGNORE_TEXTOS:
                    continue
                if t in textos:
                    continue
                textos.append(t)
            if not textos:
                continue
            if len(textos) >= 2 and textos[0].count(' - ') >= 2 and textos[1] in textos[0]:
                textos = textos[1:]

            principal = self._parse_pessoa(textos[0])
            principal['representantes'] = []
            for t in textos[1:]:
                rep = self._parse_pessoa(t)
                if rep.get('nome'):
                    principal['representantes'].append(rep)
            if principal.get('nome'):
                partes.append(principal)
        return partes

    def _parse_pessoa(self, text: str) -> dict:
        documento, tipo_doc = parse_documento(text)
        oab = parse_oab(text)
        papel = parse_role(text)
        nome = limpar_nome(text)
        tipo = classificar_tipo_parte(documento, tipo_doc, oab, papel)
        return {
            'nome': nome[:255],
            'documento': documento[:20],
            'tipo_documento': tipo_doc,
            'oab': oab[:20],
            'papel': papel[:120],
            'tipo': tipo,
        }
