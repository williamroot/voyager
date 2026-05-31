"""Enrichers e-SAJ consulta pública (sem login) — TJSP, TJAL, ...

e-SAJ é o sistema da Softplan usado por vários TJs (SP, AL, ...). O fluxo de
consulta pública é idêntico entre eles — só muda o host. `BaseEsajEnricher`
concentra toda a lógica; cada subclasse configura só `BASE_URL`,
`TRIBUNAL_SIGLA` e `LOG_NAME` (mesmo padrão de `BasePjeEnricher`).

Endpoint (ex. TJSP): https://esaj.tjsp.jus.br/cpopg/...
Endpoint (ex. TJAL): https://www2.tjal.jus.br/cpopg/...

Fluxo (HTTP puro, sem Selenium nem captcha):
  GET  /cpopg/open.do                                          → estabelece JSESSIONID
  GET  /cpopg/search.do?cbPesquisa=NUMPROC&...valorConsultaNuUnificado=<CNJ_formatado>
                                                               → 302 → /cpopg/show.do?processo.codigo=...&processo.foro=...
                                                               (segue redirect)
  Parse do HTML detalhe                                        → dados + partes

Estratégia portada do ESAJSPProcessDataProcessor do JURISCOPE
(`falcon/datamodel/processors/esajsp.py`), versão pública sem login.

Não cabe em BasePjeEnricher (form/flow são diferentes do PJe). Mesma interface:
construtor aceita `prefer_cortex`, método `enriquecer(processo, direct_apply)`.
"""
import datetime as _dt
import logging
import re
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup
from django.utils import timezone

from djen.proxies import ProxyScrapePool, cortex_proxy_url
from tribunals.models import Process

from . import stream
from .parsers import parse_documento, parse_oab

DEFAULT_HEADERS = {
    # e-SAJ rejeita UAs identificadores (ex: 'voyager-ops') com 403. Chrome vanilla passa.
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7',
}

# CNJ vem do DJEN como string de 20 dígitos (sem pontuação).
# Formato unificado: NNNNNNN-DD.AAAA.J.TR.OOOO
_CNJ_RE = re.compile(r'^\d{20}$')


class EsajEnricherError(Exception):
    pass


def _format_cnj(raw: str) -> str:
    """20 dígitos → NNNNNNN-DD.AAAA.J.TR.OOOO."""
    raw = re.sub(r'\D', '', raw or '')
    if not _CNJ_RE.match(raw):
        raise EsajEnricherError(f'CNJ inválido: {raw!r}')
    return f'{raw[:7]}-{raw[7:9]}.{raw[9:13]}.{raw[13]}.{raw[14:16]}.{raw[16:]}'


class BaseEsajEnricher:
    # Subclasse OBRIGATÓRIA: host do e-SAJ do tribunal + sigla CNJ.
    BASE_URL: Optional[str] = None
    TRIBUNAL_SIGLA: Optional[str] = None
    LOG_NAME = 'voyager.enrichers.esaj'
    # Path do módulo de 2º grau (foro OOOO == '0000'). 1º grau é sempre 'cpopg';
    # 2º grau varia: TJSP = 'cposg', TJAL = 'cposg5' (override na subclasse).
    CPOSG_PATH = 'cposg'

    # Limite de IPs distintos tentados por processo antes de desistir.
    MAX_PROXY_ROTATIONS = 8

    # Alguns hosts e-SAJ bloqueiam IPs datacenter (o pool ProxyScrape) mas
    # aceitam residencial. Ex.: www2.tjal.jus.br dá ReadTimeout em 100% do pool
    # mas responde via Cortex (residencial). Subclasse seta True pra rotear pelo
    # Cortex em vez do pool. esaj.tjsp.jus.br aceita o pool → fica False.
    PREFER_CORTEX = False

    def __init__(self, pool: Optional[ProxyScrapePool] = None, prefer_cortex: bool = False):
        if not self.BASE_URL or not self.TRIBUNAL_SIGLA:
            raise NotImplementedError(
                f'{self.__class__.__name__} precisa definir BASE_URL e TRIBUNAL_SIGLA.'
            )
        self.OPEN_URL = f'{self.BASE_URL}/cpopg/open.do'
        self.SEARCH_URL = f'{self.BASE_URL}/cpopg/search.do'
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.timeout = (10, 60)
        self.logger = logging.getLogger(self.LOG_NAME)
        # Pool ProxyScrape (2500+ IPs) — sem ele, 60 workers saíam todos do
        # IP do worker e o e-SAJ throttlava (500 / Max retries). Cada processo
        # roda por 1 IP do pool; rotaciona pra outro IP em bloqueio/erro.
        self.pool = pool or ProxyScrapePool.singleton()
        # prefer_cortex: clique manual (rápido) OU host que bloqueia o pool (TJAL).
        self.prefer_cortex = prefer_cortex or self.PREFER_CORTEX

    def enriquecer(self, processo: Process, direct_apply: bool = False) -> dict:
        if processo.tribunal_id != self.TRIBUNAL_SIGLA:
            raise EsajEnricherError(
                f'Tribunal {processo.tribunal_id} não suportado por {self.__class__.__name__}.'
            )

        base = {
            'process_id': processo.pk,
            'tribunal': processo.tribunal_id,
            'numero_cnj': processo.numero_cnj,
            'scraped_at': timezone.now().astimezone(_dt.timezone.utc).isoformat(),
        }

        # foro OOOO == '0000' ⇒ processo de 2º grau (tribunal): consulta o cposg,
        # não o cpopg (1º grau). Senão é falso "não encontrado" — o cpopg só tem 1g.
        grau = self._grau(processo.numero_cnj)

        try:
            html = self._fetch_processo(processo.numero_cnj, grau)
        except Exception as exc:
            self._emit(stream.build_erro_payload(**base, erro=f'busca: {exc}'), direct_apply)
            return {'cnj': processo.numero_cnj, 'status': 'erro', 'erro': str(exc)[:200]}

        if html is None:
            self._emit(stream.build_nao_encontrado_payload(**base), direct_apply)
            return {'cnj': processo.numero_cnj, 'status': 'nao_encontrado'}

        try:
            soup = BeautifulSoup(html, 'html.parser')
            dados = self._extrair_dados(soup, grau)
            partes = self._extrair_partes(soup)
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
        if direct_apply:
            from django.db import transaction
            from .drainer import apply_event
            try:
                with transaction.atomic():
                    apply_event(payload)
            except Exception:
                self.logger.exception('apply_event direto falhou — fallback pro stream',
                                      extra={'process_id': payload.get('process_id')})
                stream.publish(payload)
        else:
            stream.publish(payload)

    # ---------- HTTP ----------

    def _next_proxy(self, exclude: set) -> Optional[str]:
        """Próximo IP. Default: pool ProxyScrape primeiro, Cortex residencial
        como fallback. prefer_cortex=True (clique manual) inverte a ordem."""
        if self.prefer_cortex:
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

    @staticmethod
    def _grau(cnj: str) -> str:
        """'2g' se o processo é de 2º grau (foro de origem OOOO == '0000',
        i.e. originário do tribunal), senão '1g'. Independente de tribunal."""
        return '2g' if re.sub(r'\D', '', cnj or '')[-4:] == '0000' else '1g'

    @staticmethod
    def _build_search_params(cnj_fmt: str, grau: str = '1g') -> dict:
        """Monta os params do search.do a partir do CNJ formatado.

        `numeroDigitoAnoUnificado` = NNNNNNN-DD.AAAA e `foroNumeroUnificado` =
        OOOO. Derivado por segmento (split em '.') — independente de tribunal.
        O código antigo cravava `.split('.8.26')` (J.TR do TJSP); a versão por
        segmento dá o mesmo resultado pro TJSP e funciona pra TJAL (.8.02) e
        qualquer outro e-SAJ.

        1º grau (cpopg) e 2º grau (cposg) usam nomes de campo DIFERENTES pro CNJ:
        cpopg = `dadosConsulta.valorConsultaNuUnificado`; cposg = `dePesquisaNuUnificado`.
        """
        parts = cnj_fmt.split('.')  # ['NNNNNNN-DD','AAAA','J','TR','OOOO']
        params = {
            'conversationId': '',
            'cbPesquisa': 'NUMPROC',
            'numeroDigitoAnoUnificado': f'{parts[0]}.{parts[1]}',
            'foroNumeroUnificado': parts[4],
        }
        if grau == '2g':
            params.update({
                'paginaConsulta': '1',
                'dePesquisaNuUnificado': cnj_fmt,
                'dePesquisa': '',
                'tipoNuProcesso': 'UNIFICADO',
            })
        else:
            params.update({
                'dadosConsulta.localPesquisa.cdLocal': '-1',
                'dadosConsulta.valorConsultaNuUnificado': cnj_fmt,
                'dadosConsulta.tipoNuProcesso': 'UNIFICADO',
            })
        return params

    def _fetch_processo(self, cnj_raw: str, grau: str = '1g') -> Optional[str]:
        """Retorna o HTML do detalhe ou None se o processo não foi encontrado.

        Roteia por grau: 1º grau → `/cpopg/`; 2º grau → `/{CPOSG_PATH}/` (cposg
        no TJSP, cposg5 no TJAL). O detalhe dos dois tem a MESMA estrutura de
        seletores (`_extrair_dados` ramifica por grau).

        Roda por 1 IP do pool ProxyScrape. e-SAJ atrela o JSESSIONID ao IP, então
        open.do + search.do saem pelo MESMO proxy; em bloqueio (403/429), erro de
        transporte ou 5xx (e-SAJ throttlando), rotaciona pra outro IP e refaz a
        sequência inteira (limite MAX_PROXY_ROTATIONS). 403/429/transporte marcam
        o proxy como bad; 5xx é culpa do servidor — rotaciona sem queimar o IP.

        Detecção de "não encontrado": sem resultado, search.do retorna a própria
        página de busca (`formConsulta`) sem os campos do detalhe.
        """
        cnj_fmt = _format_cnj(cnj_raw)
        params = self._build_search_params(cnj_fmt, grau)
        path = self.CPOSG_PATH if grau == '2g' else 'cpopg'
        open_url = f'{self.BASE_URL}/{path}/open.do'
        search_url = f'{self.BASE_URL}/{path}/search.do'

        tentados: set = set()
        last_erro: Optional[str] = None
        for tentativa in range(1, self.MAX_PROXY_ROTATIONS + 1):
            proxy = self._next_proxy(tentados)
            if not proxy:
                self.logger.warning('pool exausto sem proxy disponível',
                                    extra={'cnj': cnj_fmt, 'tentativa': tentativa})
                break
            # Cortex é um gateway que rotaciona IP residencial a cada request —
            # não excluir, pra poder reusar em rotações (vira IP novo toda vez).
            # Proxies do pool são IP fixo: excluir pra não repetir o mesmo.
            if proxy != cortex_proxy_url(self.pool):
                tentados.add(proxy)
            proxies = {'http': proxy, 'https': proxy}
            # Sessão limpa por IP: JSESSIONID novo atado ao proxy desta tentativa.
            self.session.cookies.clear()
            try:
                # open.do estabelece o JSESSIONID; sem ele search.do volta o form.
                self.session.get(open_url, proxies=proxies, timeout=self.timeout)
                resp = self.session.get(search_url, params=params, proxies=proxies,
                                        timeout=self.timeout, allow_redirects=True)
            except (requests.ConnectionError, requests.Timeout,
                    requests.exceptions.ChunkedEncodingError) as exc:
                last_erro = f'transporte: {str(exc)[:120]}'
                if proxy != cortex_proxy_url():
                    self.pool.mark_bad(proxy)
                continue

            if resp.status_code in (403, 429):
                last_erro = f'bloqueado {resp.status_code}'
                if proxy != cortex_proxy_url():
                    self.pool.mark_bad(proxy)
                continue
            if resp.status_code >= 500:
                # e-SAJ sobrecarregado — outro IP pode não estar throttled.
                # Não marca bad: a falha é do servidor, não do proxy.
                last_erro = f'e-SAJ {resp.status_code}'
                continue
            resp.raise_for_status()

            # Sem redirect → não encontrou (search.do voltou a própria página de busca).
            # Resposta com `formConsulta` é o form de pesquisa (sem resultado).
            if not resp.history and 'formConsulta' in resp.text:
                return None
            # Página de detalhe tem #numeroProcesso ou #classeProcesso. Sem
            # redirect nem campos do detalhe → trata como não encontrado.
            if 'numeroProcesso' not in resp.text and 'classeProcesso' not in resp.text:
                return None
            return resp.text

        raise EsajEnricherError(
            f'{len(tentados)} proxies tentados sem sucesso'
            + (f' (último: {last_erro})' if last_erro else ''))

    # ---------- Parsing ----------

    def _extrair_dados(self, soup: BeautifulSoup, grau: str = '1g') -> dict:
        def t(sel: str) -> str:
            el = soup.select_one(sel)
            return el.get_text(strip=True) if el else ''

        if grau == '2g':
            # 2º grau (cposg): seção + órgão julgador (câmara/turma/presidência) e
            # relator no lugar de foro/vara. Sem data de distribuição/valor nos
            # mesmos campos do 1g. Partes usam a MESMA #tablePartesPrincipais.
            secao = t('#secaoProcesso')
            orgao_jul = t('#orgaoJulgadorProcesso')
            orgao = ' — '.join(x for x in (secao, orgao_jul) if x) or None
            return {
                'classe':         t('#classeProcesso') or None,
                'assunto':        t('#assuntoProcesso') or None,
                'orgao_julgador': orgao,
                'juizo':          t('#relatorProcesso') or None,
                'data_autuacao':  None,
                'valor_causa':    None,
            }

        # `varaProcesso` é o juízo específico; `foroProcesso` é a unidade física.
        # Drainer espera `orgao_julgador` como nome único — concatena os dois.
        vara = t('#varaProcesso')
        foro = t('#foroProcesso')
        orgao = ' — '.join(x for x in (foro, vara) if x) or None

        return {
            'classe':         t('#classeProcesso') or None,
            'assunto':        t('#assuntoProcesso') or None,
            'orgao_julgador': orgao,
            'juizo':          vara or None,
            'data_autuacao':  t('#dataHoraDistribuicaoProcesso') or None,
            'valor_causa':    t('#valorAcaoProcesso') or None,
        }

    _IGNORE_TEXTOS = {'advogado:', 'advogada:', 'advogados:', 'advogadas:'}

    def _extrair_partes(self, soup: BeautifulSoup) -> dict[str, list[dict]]:
        """e-SAJ não separa por polo na consulta pública — usa só
        `#tablePartesPrincipais` com tipo (Exeqte/Exectdo/Reqte/Reqdo/Autor/Réu).
        Mapeamos tipo → polo (ativo/passivo/outros) heurístico.
        """
        polos: dict[str, list[dict]] = {'ativo': [], 'passivo': [], 'outros': []}
        table = soup.select_one('#tablePartesPrincipais')
        if not table:
            return polos

        for tr in table.select('tr'):
            tipo_el = tr.select_one('.tipoDeParticipacao')
            nome_box = tr.select_one('.nomeParteEAdvogado')
            if not nome_box:
                continue
            tipo = (tipo_el.get_text(strip=True).rstrip(':') if tipo_el else '').strip()
            polo = self._polo_para_tipo(tipo)

            # Itens dentro do td: o 1º <span> normalmente é a parte; demais são
            # advogados (precedidos por label "Advogado:" / "Advogada:"). e-SAJ
            # frequentemente mistura tudo em texto solto — varremos linha a linha.
            is_advogado = False
            for raw in nome_box.stripped_strings:
                s = raw.strip()
                if not s:
                    continue
                if s.lower() in self._IGNORE_TEXTOS or s.lower().startswith(('advogado', 'advogada')):
                    # Tudo depois desse marker até o próximo nome é advogado.
                    is_advogado = True
                    continue
                doc, _doc_tipo = parse_documento(s)
                oab = parse_oab(s) if is_advogado else ''
                # Limpa nome: remove possível doc inline (CPF/CNPJ ou OAB sufixo).
                nome = re.sub(r'\s*(?:CPF|CNPJ|OAB)\s*[:#]?\s*[\dXx*./-]+', '', s).strip()
                if not nome:
                    continue
                polos[polo].append({
                    'nome': nome,
                    'documento': doc or '',
                    'oab': oab or '',
                    'tipo': 'advogado' if is_advogado else (tipo.lower() or 'desconhecido'),
                })
        return polos

    # Mapeamento de papéis comuns no e-SAJ → polo.
    # Exeqte/Reqte/Autor/Apte/Embte/Impte → ativo
    # Exectdo/Reqdo/Réu/Apdo/Embdo/Impdo  → passivo
    _PAPEIS_ATIVO = ('exeqte', 'reqte', 'autor', 'apte', 'embte', 'impte', 'agvte', 'rclte')
    _PAPEIS_PASSIVO = ('exectdo', 'reqdo', 'réu', 'reu', 'apdo', 'embdo', 'impdo', 'agvdo', 'rcldo')

    def _polo_para_tipo(self, tipo: str) -> str:
        t = (tipo or '').strip().lower()
        if any(t.startswith(p) for p in self._PAPEIS_ATIVO):
            return 'ativo'
        if any(t.startswith(p) for p in self._PAPEIS_PASSIVO):
            return 'passivo'
        return 'outros'


class TjspEnricher(BaseEsajEnricher):
    BASE_URL = 'https://esaj.tjsp.jus.br'
    TRIBUNAL_SIGLA = 'TJSP'
    LOG_NAME = 'voyager.enrichers.tjsp'


class TjalEnricher(BaseEsajEnricher):
    BASE_URL = 'https://www2.tjal.jus.br'
    TRIBUNAL_SIGLA = 'TJAL'
    LOG_NAME = 'voyager.enrichers.tjal'
    CPOSG_PATH = 'cposg5'  # TJAL: 2º grau é /cposg5/ (TJSP usa /cposg/)
    # www2.tjal.jus.br dá ReadTimeout em 100% do pool datacenter — só responde
    # via Cortex residencial (validado 2026-05-30). Roteia por Cortex.
    PREFER_CORTEX = True
