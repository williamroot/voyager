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

from djen.proxies import ProxyScrapePool, cortex_proxy_url, sessao_rotativa
from tribunals.models import Process

from . import stream
from .faixas import faixa_fora_da_fonte
from .parsers import classificar_tipo_parte, parse_documento, parse_oab

DEFAULT_HEADERS = {
    # e-SAJ rejeita UAs identificadores (ex: 'voyager-ops') com 403. Chrome vanilla passa.
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7',
}

# CNJ vem do DJEN como string de 20 dígitos (sem pontuação).
# Formato unificado: NNNNNNN-DD.AAAA.J.TR.OOOO
_CNJ_RE = re.compile(r'^\d{20}$')


# --- Classificação da resposta do e-SAJ --------------------------------------
#
# Sonda ao vivo de 25/08/2026 (62 requisições a `esaj.tjsp.jus.br`, 3 s entre
# elas, amostra de semente 20260825): o e-SAJ devolve HTTP 200 para CINCO
# páginas estruturalmente diferentes, e o código tratava três delas como se
# fossem a mesma coisa. As fixtures REAIS de cada uma estão em
# `tests/fixtures/tjsp/`.
#
#   detalhe      o cadastro do processo (com ou sem `classeProcesso`)
#   nao_existe   "Não existem informações disponíveis..." (70.439 bytes)
#   segredo      "É necessário informar uma senha... segredo de justiça"
#                (32.963 bytes) — chega por redirect ao `show.do`, tem o
#                container de dados VAZIO e nenhum campo do cadastro
#   lista        vários resultados para o mesmo número unificado
#                (1º grau `#listagemDeProcessos`; 2º grau `#modalIncidentes`
#                "Selecione o processo") — o dado está a UM clique
#   ambiguo      qualquer outra coisa (soft-error/throttle) ⇒ re-tentável
#
# DUAS armadilhas medidas, as duas do mesmo tipo — teste de SUBSTRING numa
# página inteira:
#
# 1. `'classeProcesso' in resp.text` (o teste de "achou" até aqui) casa a
#    CLASSE CSS `<div class="classeProcesso">` da página de LISTA. Resultado:
#    a lista era lida como detalhe, `select_one('#classeProcesso')` não achava
#    nada e o processo virava `ok` com o cadastro inteiro vazio.
# 2. A frase "É necessário informar uma senha para acessar processo em segredo
#    de justiça" está dentro de `<form id="popupSenha" style="display: none;">`
#    em TODA página de detalhe do e-SAJ — 36 das 62 páginas baixadas na sonda a
#    contêm, e 33 dessas 36 são detalhes normais COM partes. Detector por frase
#    marcaria segredo em processo bom, apagando-o do funil.
#
# Por isso tudo aqui é ESTRUTURAL (`id="..."`), nunca frase solta.
DESFECHO_DETALHE = 'detalhe'
DESFECHO_NAO_EXISTE = 'nao_existe'
DESFECHO_SEGREDO = 'segredo'
DESFECHO_LISTA = 'lista'
DESFECHO_AMBIGUO = 'ambiguo'

# `id="..."` que SÓ existem na página de cadastro. A página de lista tem
# `class="classeProcesso"` (classe CSS), nunca `id="classeProcesso"` — é essa
# distinção que separa uma da outra. Inclui os campos do 2º grau
# (`secaoProcesso`/`orgaoJulgadorProcesso`/`relatorProcesso`) e a tabela de
# partes, porque existe variante REAL de detalhe SEM `classeProcesso`
# (fixture `esaj_cpopg_detalhe_sem_classe.html`, uma Carta Precatória Cível
# com partes, assunto, foro e vara, e nenhuma classe).
_IDS_DETALHE = (
    'id="classeProcesso"', 'id="numeroProcesso"', 'id="assuntoProcesso"',
    'id="foroProcesso"', 'id="varaProcesso"', 'id="tablePartesPrincipais"',
    'id="orgaoJulgadorProcesso"', 'id="relatorProcesso"', 'id="secaoProcesso"',
)
# Marcador de "não existe" — o ÚNICO desfecho terminal do e-SAJ (2026-07-06).
_RE_NAO_EXISTE = re.compile(r'[Nn][ãa]o existem informa')
# Lista de resultados: 1º grau e 2º grau usam contêineres diferentes.
_IDS_LISTA = ('id="listagemDeProcessos"', 'id="modalIncidentes"')
# Segredo: o cabeçalho do processo existe (chegamos no `show.do`) e o
# formulário de senha está lá, mas o container de dados veio VAZIO.
_ID_CONTAINER = 'id="containerDadosPrincipaisProcesso"'
_ID_POPUP_SENHA = 'id="popupSenha"'


def classificar_resposta(html: str) -> str:
    """Classifica um HTTP 200 do e-SAJ em um dos cinco desfechos acima.

    Conservador de propósito: `segredo` exige a AUSÊNCIA de todos os campos do
    cadastro, então nenhuma página que traga dado é rotulada segredo. Falso
    positivo aqui apagaria processo bom do funil.
    """
    texto = html or ''
    if _RE_NAO_EXISTE.search(texto):
        return DESFECHO_NAO_EXISTE
    tem_cadastro = any(marca in texto for marca in _IDS_DETALHE)
    if tem_cadastro:
        return DESFECHO_DETALHE
    if any(marca in texto for marca in _IDS_LISTA):
        return DESFECHO_LISTA
    if _ID_CONTAINER in texto and _ID_POPUP_SENHA in texto:
        return DESFECHO_SEGREDO
    return DESFECHO_AMBIGUO


def _so_digitos(valor: str) -> str:
    return re.sub(r'\D', '', valor or '')


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
        self.session = sessao_rotativa()   # cache de proxies limitado — ver AdaptadorProxyLimitado
        self.session.headers.update(DEFAULT_HEADERS)
        self.timeout = (10, 60)
        self.logger = logging.getLogger(self.LOG_NAME)
        # Pool ProxyScrape (2500+ IPs) — sem ele, 60 workers saíam todos do
        # IP do worker e o e-SAJ throttlava (500 / Max retries). Cada processo
        # roda por 1 IP do pool; rotaciona pra outro IP em bloqueio/erro.
        self.pool = pool or ProxyScrapePool.singleton()
        # prefer_cortex: clique manual (rápido) OU host que bloqueia o pool (TJAL).
        self.prefer_cortex = prefer_cortex or self.PREFER_CORTEX

    MAX_INCIDENTES = 12  # teto de incidentes seguidos por processo (custo de proxy)

    #: Faixas de CNJ que ESTE e-SAJ comprovadamente não tem — o tribunal roda um
    #: SEGUNDO sistema (eproc). `(prefixo, ano_mínimo, motivo)`; vazio = sem
    #: segunda fonte medida (abster > chutar). Ver `enrichers/faixas.py`.
    FORA_DA_FONTE_FAIXAS: tuple = ()

    @classmethod
    def fora_da_fonte(cls, numero_cnj: str) -> Optional[str]:
        """Motivo pelo qual este CNJ NÃO está no e-SAJ deste tribunal — ou None."""
        return faixa_fora_da_fonte(numero_cnj, cls.FORA_DA_FONTE_FAIXAS)

    #: Nome histórico do hook, de quando a recusa por faixa só existia no e-SAJ.
    #: Mantido porque `manage.py enrich_fora_do_esaj` e os runbooks o citam.
    fora_do_esaj = fora_da_fonte

    def _recusar_fora_do_esaj(self, processo: Process, motivo: str,
                              direct_apply: bool) -> dict:
        """Recusa CONTADA, nunca corte mudo (regra nº 2 do CLAUDE.md).

        Cada recusa entra num contador por tribunal que sai em ERROR no refill
        e em `manage.py enrich_fora_do_esaj`. Recorte que não se anuncia é o
        `for pagina in range(1, 11)` outra vez.
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

    def enriquecer(self, processo: Process, direct_apply: bool = False,
                   seguir_incidentes: bool = False) -> dict:
        if processo.tribunal_id != self.TRIBUNAL_SIGLA:
            raise EsajEnricherError(
                f'Tribunal {processo.tribunal_id} não suportado por {self.__class__.__name__}.'
            )

        # Faixa que a fonte já provou não ter: não gastamos requisição nem IP
        # do pool COMPARTILHADO pra ouvir "não existe" garantido.
        motivo = self.fora_da_fonte(processo.numero_cnj)
        if motivo:
            return self._recusar_fora_do_esaj(processo, motivo, direct_apply)

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
            desfecho, html = self._fetch_processo(processo.numero_cnj, grau)
        except Exception as exc:
            self._emit(stream.build_erro_payload(**base, erro=f'busca: {exc}'), direct_apply)
            return {'cnj': processo.numero_cnj, 'status': 'erro', 'erro': str(exc)[:200]}

        if desfecho == DESFECHO_NAO_EXISTE:
            self._emit(stream.build_nao_encontrado_payload(**base), direct_apply)
            return {'cnj': processo.numero_cnj, 'status': 'nao_encontrado'}

        if desfecho == DESFECHO_SEGREDO:
            # A fonte RESPONDEU: o processo existe e corre em segredo de
            # justiça. Isso não é `nao_encontrado` (o processo existe) nem um
            # `ok` mudo com o cadastro vazio (era o que acontecia: 17,5% dos
            # `ok` do TJSP não tinham nenhuma parte, ≈ 302 k processos
            # rotulados "enriquecido" sobre uma página de senha).
            # Gravamos o único dado que a fonte deu — `segredo_justica=True` —
            # e NENHUM campo inventado. Ver regra nº 6 do CLAUDE.md.
            self.logger.info('processo em segredo de justiça',
                             extra={'cnj': processo.numero_cnj,
                                    'process_id': processo.pk})
            self._emit(
                stream.build_ok_payload(
                    **base, dados={'segredo_justica': True},
                    partes={'ativo': [], 'passivo': [], 'outros': []}),
                direct_apply)
            return {'cnj': processo.numero_cnj, 'status': 'segredo',
                    'segredo_justica': True, 'partes_total': 0}

        try:
            soup = BeautifulSoup(html, 'html.parser')
            dados = self._extrair_dados(soup, grau)
            # Chegamos no cadastro e ele veio: PERGUNTAMOS e a fonte disse que
            # não corre em segredo. Antes, o `default=False` do BooleanField
            # dizia isso por nós, sem ninguém ter perguntado uma vez —
            # `segredo_justica=true` em 0 de 91.638.494 documentos do índice.
            dados['segredo_justica'] = False
            partes = self._extrair_partes(soup)
        except Exception as exc:
            self.logger.exception('falha ao parsear detalhe', extra={'cnj': processo.numero_cnj})
            self._emit(stream.build_erro_payload(**base, erro=f'parse: {exc}'), direct_apply)
            return {'cnj': processo.numero_cnj, 'status': 'erro', 'erro': str(exc)[:200]}

        n_inc = (self._agregar_incidentes(soup, dados, partes, grau)
                 if seguir_incidentes else 0)

        self._emit(stream.build_ok_payload(**base, dados=dados, partes=partes), direct_apply)
        return {
            'cnj': processo.numero_cnj,
            'status': 'ok',
            'classe_raw': dados.get('classe'),
            'partes_total': sum(len(v) for v in partes.values()),
            'incidentes_seguidos': n_inc,
        }

    def _agregar_incidentes(self, soup, dados: dict, partes: dict, grau: str) -> int:
        """Incidente-following (só no fetch manual/dossiê): no e-SAJ cada parte/
        beneficiário costuma ter um incidente próprio (o precatório/requisição
        dela). A página principal mostra o processo-pai; os dados por parte
        estão nos incidentes. Segue os links, parseia cada um e AGREGA as
        partes (+ o maior valor). Espelha o Juriscope (esajsp.py)."""
        n_inc = 0
        for href in self._extrair_incidentes(soup)[:self.MAX_INCIDENTES]:
            try:
                ihtml = self._fetch_incidente(href)
            except Exception:
                continue
            if not ihtml:
                continue
            try:
                isoup = BeautifulSoup(ihtml, 'html.parser')
                self._merge_partes(partes, self._extrair_partes(isoup))
                idados = self._extrair_dados(isoup, grau)
                if idados.get('valor_causa') and not dados.get('valor_causa'):
                    dados['valor_causa'] = idados['valor_causa']
                if idados.get('classe') and 'precat' in (idados['classe'] or '').lower():
                    dados['classe'] = idados['classe']
                n_inc += 1
            except Exception:
                continue
        return n_inc

    @staticmethod
    def _merge_partes(dest: dict, novo: dict) -> None:
        """Agrega partes de um incidente em dest, dedup por (polo, nome)."""
        vistos = {(polo, p.get('nome', '')) for polo, lst in dest.items() for p in lst}
        for polo, lst in (novo or {}).items():
            dest.setdefault(polo, [])
            for p in lst:
                chave = (polo, p.get('nome', ''))
                if chave not in vistos:
                    vistos.add(chave)
                    dest[polo].append(p)

    def _extrair_incidentes(self, soup) -> list:
        """hrefs dos links de incidente (.incidente / seção incidentesRecursos_).
        Cada parte/beneficiário tem seu incidente (precatório). Usa o href real
        (traz cdLocal+codigo+foro), dedup por processo.codigo."""
        out, seen = [], set()
        anchors = list(soup.select('a.incidente[href], a.linkleituraincidente[href]'))
        for sec in soup.select('[id^="incidentesRecursos_"]'):
            anchors += sec.find_all('a', href=True)
        for a in anchors:
            href = a.get('href', '')
            m = re.search(r'processo\.codigo=([A-Za-z0-9]+)', href)
            if m and m.group(1) not in seen and 'show.do' in href:
                seen.add(m.group(1))
                out.append(href)
        return out

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

    def _next_proxy(self, exclude: set, force_cortex: bool = False) -> Optional[str]:
        """Próximo IP. Default: pool ProxyScrape primeiro, Cortex residencial
        como fallback. prefer_cortex=True (clique manual) inverte a ordem.

        `force_cortex` é o escalonamento por bloqueio, gêmeo do que existe em
        `BasePjeEnricher`: com 2.500 IPs no pool as MAX_PROXY_ROTATIONS nunca o
        esgotam, então o ramo de fallback residencial no fim deste método é
        inalcançável na prática e o job termina em `erro` sem nunca ter tentado
        o Cortex. Medido em 25/08/2026 (A/B, mesmos 5 pids, semente 20260824):
        TJAL pelo pool = 73,1 s no total; pelo Cortex = 17,0 s (4,3 vezes mais
        rápido, mesmos 5 `ok`). No TJAP o pool dava 0 de 5 úteis e o Cortex 5
        de 5, a 0,95 s de mediana contra 7,91 s."""
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

    def _fetch_processo(self, cnj_raw: str, grau: str = '1g') -> tuple[str, Optional[str]]:
        """Retorna `(desfecho, html)` — ver `classificar_resposta`.

        Roteia por grau: 1º grau → `/cpopg/`; 2º grau → `/{CPOSG_PATH}/` (cposg
        no TJSP, cposg5 no TJAL). O detalhe dos dois tem a MESMA estrutura de
        seletores (`_extrair_dados` ramifica por grau).

        Roda por 1 IP do pool ProxyScrape. e-SAJ atrela o JSESSIONID ao IP, então
        open.do + search.do saem pelo MESMO proxy; em bloqueio (403/429), erro de
        transporte ou 5xx (e-SAJ throttlando), rotaciona pra outro IP e refaz a
        sequência inteira (limite MAX_PROXY_ROTATIONS). 403/429/transporte marcam
        o proxy como bad; 5xx é culpa do servidor — rotaciona sem queimar o IP.

        Detecção de "não encontrado": SÓ o marcador explícito "Não existem
        informações" do e-SAJ. Qualquer 200 que não seja detalhe, segredo,
        lista nem not-found é TRANSITÓRIO — rotaciona e, esgotando as rotações,
        vira `erro` (re-tentável), nunca falso-negativo terminal.
        """
        cnj_fmt = _format_cnj(cnj_raw)
        params = self._build_search_params(cnj_fmt, grau)
        path = self.CPOSG_PATH if grau == '2g' else 'cpopg'
        open_url = f'{self.BASE_URL}/{path}/open.do'
        search_url = f'{self.BASE_URL}/{path}/search.do'

        tentados: set = set()
        last_erro: Optional[str] = None
        bloqueios_dc = 0   # 403/429 vindos do datacenter → após 3, força o Cortex
        for tentativa in range(1, self.MAX_PROXY_ROTATIONS + 1):
            proxy = self._next_proxy(tentados, force_cortex=bloqueios_dc >= 3)
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
                    bloqueios_dc += 1
                continue
            if resp.status_code >= 500:
                # e-SAJ sobrecarregado — outro IP pode não estar throttled.
                # Não marca bad: a falha é do servidor, não do proxy.
                last_erro = f'e-SAJ {resp.status_code}'
                continue
            resp.raise_for_status()

            resolvido = self._resolver_200(resp.text, cnj_fmt, path, proxies)
            if resolvido is not None:
                return resolvido
            # 200 AMBÍGUO = provável soft-error/throttle do e-SAJ vindo com 200,
            # ou lista sem o nosso número. TRANSITÓRIO: rotaciona. Se esgotar as
            # rotações vira 'erro' (re-tentável) — nunca falso-negativo terminal
            # (era o bug: 3,25M TJSP presos em nao_encontrado — 2026-07-06).
            last_erro = 'resposta 200 sem cadastro, sem lista útil e sem not-found'
            continue

        raise EsajEnricherError(
            f'{len(tentados)} proxies tentados sem sucesso'
            + (f' (último: {last_erro})' if last_erro else ''))

    def _resolver_200(self, html: str, cnj_fmt: str, path: str,
                      proxies: dict) -> Optional[tuple[str, Optional[str]]]:
        """`(desfecho, html)` quando o 200 é conclusivo; None para rotacionar.

        A página de LISTA é conclusiva num segundo passo: o dado está a UM
        clique. Sem seguir o link, ela caía em "200 ambíguo", queimava as 8
        rotações de IP e terminava em `erro` com o cadastro visível na tela —
        6 das 62 respostas da sonda de 25/08/2026 (4 das 8 do estrato de 2º grau).
        """
        desfecho = classificar_resposta(html)
        if desfecho == DESFECHO_LISTA:
            seguido = self._seguir_lista(html, cnj_fmt, path, proxies)
            if seguido is None:
                return None
            html, desfecho = seguido, classificar_resposta(seguido)
        if desfecho == DESFECHO_NAO_EXISTE:
            # NÃO ENCONTRADO explícito do e-SAJ — só ISSO marca terminal.
            return DESFECHO_NAO_EXISTE, None
        if desfecho in (DESFECHO_DETALHE, DESFECHO_SEGREDO):
            return desfecho, html
        return None

    def _extrair_link_lista(self, html: str, cnj_fmt: str, path: str) -> Optional[str]:
        """URL do processo PEDIDO dentro de uma página de lista, ou None.

        Duas formas reais (fixtures `esaj_cpopg_lista.html` e
        `esaj_cposg_lista.html`):

        - **1º grau** — `#listagemDeProcessos` com `<a class="linkProcesso"
          href="/cpopg/show.do?processo.codigo=...">NNNNNNN-DD.AAAA.J.TR.OOOO</a>`.
        - **2º grau** — `#modalIncidentes` ("Selecione o processo") com
          `<input type="radio" name="processoSelecionado" value="<codigo>">`
          e o número num `<em>` irmão. A URL se monta:
          `/{path}/show.do?processo.codigo=<codigo>&processo.numero=<cnj>`
          (conferido ao vivo em 25/08/2026: 200 com 120.429 bytes, com
          `classeProcesso` e `tablePartesPrincipais`; `processo.foro` é
          dispensável).

        Casa SEMPRE pelo número, nunca pela posição: a lista mistura o
        processo com seus incidentes/recursos, e pegar "o primeiro" traria o
        cadastro de OUTRO processo — inventar dado é pior que não ter.
        """
        alvo = _so_digitos(cnj_fmt)
        soup = BeautifulSoup(html, 'html.parser')

        for a in soup.select('a[href*="show.do"]'):
            if _so_digitos(a.get_text(strip=True)) == alvo:
                href = a.get('href') or ''
                if href:
                    return href if href.startswith('http') else f'{self.BASE_URL}{href}'

        for inp in soup.select('input[name="processoSelecionado"][value]'):
            bloco = inp.find_parent()
            for _ in range(3):
                if bloco is None:
                    break
                if alvo in _so_digitos(bloco.get_text(' ', strip=True)):
                    codigo = inp.get('value') or ''
                    if codigo:
                        return (f'{self.BASE_URL}/{path}/show.do'
                                f'?processo.codigo={codigo}&processo.numero={cnj_fmt}')
                    break
                bloco = bloco.find_parent()
        return None

    def _seguir_lista(self, html: str, cnj_fmt: str, path: str,
                      proxies: dict) -> Optional[str]:
        """Segue o link da lista pelo MESMO IP (o e-SAJ atrela o JSESSIONID ao
        IP). Devolve o HTML da página apontada, ou None se não houver link
        para o nosso número ou se a requisição falhar — o chamador trata como
        ambíguo e rotaciona, jamais como terminal."""
        try:
            url = self._extrair_link_lista(html, cnj_fmt, path)
        except Exception:
            self.logger.exception('falha ao ler a lista de processos',
                                  extra={'cnj': cnj_fmt})
            return None
        if not url:
            return None
        try:
            resp = self.session.get(url, proxies=proxies, timeout=self.timeout,
                                    allow_redirects=True)
        except (requests.ConnectionError, requests.Timeout,
                requests.exceptions.ChunkedEncodingError):
            return None
        if resp.status_code != 200:
            return None
        return resp.text

    def _fetch_incidente(self, href: str) -> Optional[str]:
        """Detalhe de um incidente pelo href real do link `.incidente` (que já
        traz cdLocal+codigo+foro). Rotaciona proxy. None se não obteve detalhe.

        NÃO usa `consultaDeRequisitorios=true`: esse param (fluxo autenticado do
        Juriscope) dispara CAPTCHA na consulta pública. O show.do do incidente
        abre SEM login/captcha (2026-07-06). Checagem tolerante: a página do
        incidente pode não ter `classeProcesso` — aceita `numeroProcesso`/detalhe."""
        open_url = f'{self.BASE_URL}/cpopg/open.do'
        url = href if href.startswith('http') else f'{self.BASE_URL}{href}'
        tentados: set = set()
        bloqueios_dc = 0
        for _ in range(1, self.MAX_PROXY_ROTATIONS + 1):
            proxy = self._next_proxy(tentados, force_cortex=bloqueios_dc >= 3)
            if not proxy:
                break
            if proxy != cortex_proxy_url(self.pool):
                tentados.add(proxy)
            proxies = {'http': proxy, 'https': proxy}
            self.session.cookies.clear()
            try:
                self.session.get(open_url, proxies=proxies, timeout=self.timeout)
                resp = self.session.get(url, proxies=proxies, timeout=self.timeout,
                                        allow_redirects=True)
            except (requests.ConnectionError, requests.Timeout,
                    requests.exceptions.ChunkedEncodingError):
                if proxy != cortex_proxy_url():
                    self.pool.mark_bad(proxy)
                continue
            if resp.status_code in (403, 429):
                if proxy != cortex_proxy_url():
                    self.pool.mark_bad(proxy)
                    bloqueios_dc += 1
                continue
            if resp.status_code >= 500:
                continue
            resp.raise_for_status()
            # NÃO rejeitar por 'captcha' no texto: TODA página e-SAJ tem captcha no
            # JS do header. Confiar nos markers de dado — a página do incidente tem
            # nomeParteEAdvogado mesmo sem classeProcesso; a página-desafio de captcha
            # não tem nenhum desses.
            if any(m in resp.text for m in ('classeProcesso', 'numeroProcesso',
                                            'nomeParteEAdvogado', 'classAttorney')):
                return resp.text
            return None
        return None

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
                doc, doc_tipo = parse_documento(s)
                oab = parse_oab(s) if is_advogado else ''
                # Limpa nome: remove possível doc inline (CPF/CNPJ ou OAB sufixo).
                nome = re.sub(r'\s*(?:CPF|CNPJ|OAB)\s*[:#]?\s*[\dXx*./-]+', '', s).strip()
                if not nome:
                    continue
                # `tipo` da tabela (Exeqte/Reqdo/Agravante/...) é o PAPEL
                # processual → vai pra ProcessoParte.papel. `Parte.tipo` é a
                # categoria canônica (pf/pj/advogado/desconhecido), derivada de
                # doc/oab — NUNCA o papel cru (bug histórico: poluía o donut
                # "Distribuição por tipo" com centenas de papéis).
                papel = ('ADVOGADO' if is_advogado else tipo).upper()
                polos[polo].append({
                    'nome': nome,
                    'documento': doc or '',
                    'tipo_documento': doc_tipo or '',
                    'oab': oab or '',
                    'papel': papel[:120],
                    'tipo': classificar_tipo_parte(doc or '', doc_tipo or '', oab or '', papel),
                })
        return polos

    # Mapeamento de papéis comuns no e-SAJ → polo. Abreviados (1º grau) e por
    # extenso (2º grau usa 'Agravante'/'Agravado', 'Apelante'/'Apelado', etc).
    # Prefixos SEM a vogal final, porque o feminino troca justamente ela:
    # 'requerido' não casa 'REQUERIDA', 'executado' não casa 'EXECUTADA'.
    # Medido no banco em 2026-08-10: 33.338 partes de tribunais e-SAJ (TJAL,
    # TJSP, TJAC) caíram em 'outros' só por causa do gênero — RÉ 19.852,
    # AGRAVADA 4.178, EXECUTADA 2.821, REQUERIDA 2.654, e por aí.
    #
    # O ativo escapou por sorte: 'autor' já cobre 'autora' por prefixo e os
    # demais ('requerente', 'exequente', 'apelante') são neutros. Mesmo assim
    # ficam sem a vogal, para não depender de sorte.
    _PAPEIS_ATIVO = (
        # 'exequent' por extenso FALTAVA (só havia a abreviação 'exeqte'):
        # 'Exequente' caía em 'outros' desde sempre. Achado pelo teste, não
        # pela leitura.
        'exequent', 'exeqte', 'reqte', 'requerent', 'autor', 'apte', 'apelant',
        'embte', 'embargant', 'impte', 'impetrant', 'agvte', 'agravant',
        'rclte', 'reclamant', 'recte', 'recorrent',
    )
    _PAPEIS_PASSIVO = (
        'exectd', 'reqd', 'requerid', 'apd', 'apelad',
        'embd', 'embargad', 'impd', 'impetrad', 'agvd', 'agravad',
        'rcld', 'reclamad', 'recd', 'recorrid', 'executad',
    )
    # Formas CURTAS vão por igualdade, nunca por prefixo: 'ré'/'re' como
    # prefixo engoliria 'requerente', 'reclamante' e 'recorrente' — o polo
    # ativo inteiro viraria passivo.
    _PAPEIS_PASSIVO_EXATOS = frozenset({'réu', 'reu', 'ré', 'réu/ré', 'reu/re'})

    def _polo_para_tipo(self, tipo: str) -> str:
        t = (tipo or '').strip().lower()
        if t in self._PAPEIS_PASSIVO_EXATOS:
            return 'passivo'
        if any(t.startswith(p) for p in self._PAPEIS_ATIVO):
            return 'ativo'
        if any(t.startswith(p) for p in self._PAPEIS_PASSIVO):
            return 'passivo'
        return 'outros'


class TjspEnricher(BaseEsajEnricher):
    BASE_URL = 'https://esaj.tjsp.jus.br'
    TRIBUNAL_SIGLA = 'TJSP'
    LOG_NAME = 'voyager.enrichers.tjsp'

    # --- O TJSP tem um SEGUNDO sistema, e o e-SAJ não sabe dele -------------
    #
    # Achado de 25/08/2026. O TJSP roda **eproc** em paralelo ao e-SAJ, e os
    # processos nascidos nele recebem sequencial de CNJ começando em `4`. O
    # `link` da própria publicação DJEN denuncia o sistema: prefixo 4 aponta
    # `https://eproc1g.tjsp.jus.br/...` / `eproc2g`, enquanto prefixo 0/1
    # aponta `https://www.dje.tjsp.jus.br`. Conferido lendo as movimentações
    # reais desses processos.
    #
    # Prova ao vivo: **16 de 16** CNJ de prefixo 4 dos anos 2025 e 2026
    # devolveram a MESMA página determinística de 70.439 bytes, "Não existem
    # informações disponíveis". Não é intermitência, não é WAF, não é o
    # parser — o e-SAJ simplesmente não tem esses autos.
    #
    # Tamanho (amostra de 15.443 linhas TJSP, semente 20260825, projetada
    # sobre os 16.326.948 processos do tribunal com status):
    #   prefixo 4 = 18,0% do TJSP ≈ **2.940.182 processos**, com
    #   **0,1% de `ok`**, 37,2% de `nao_encontrado` e 62,7% ainda `pendente`.
    #
    # Por que recusar em vez de tentar: cada job desses gasta até
    # MAX_PROXY_ROTATIONS IPs do pool, que é **COMPARTILHADO com todos os
    # tribunais** — a fronteira do refill estava dentro desta faixa e queimava
    # ~10 mil requisições/h para ouvir um "não existe" garantido.
    #
    # ⚠️ O corte é `prefixo 4` **E** `ano >= 2025`, não o prefixo sozinho:
    # medido na mesma janela, CNJ de prefixo 4 e ano 2013 ESTÃO no e-SAJ e
    # devolvem `ok` (33 de 33 numa janela de 45 min). Generalizar o prefixo
    # apagaria processo bom.
    #
    # A porta do eproc existe e é pública — `eproc-consulta.tjsp.jus.br/
    # consulta_1g/`, sem login — mas está atrás de Cloudflare Turnstile com
    # verificação no servidor. Abri-la é decisão de produto, não de parser.
    FORA_DA_FONTE_FAIXAS = (('4', 2025, 'eproc'),)


class TjacEnricher(BaseEsajEnricher):
    BASE_URL = 'https://esaj.tjac.jus.br'
    TRIBUNAL_SIGLA = 'TJAC'
    LOG_NAME = 'voyager.enrichers.tjac'
    CPOSG_PATH = 'cposg5'  # TJAC: 2º grau é /cposg5/

    # --- O TJAC também migrou, e a fatia nova já é MAIORIA do que publica ----
    #
    # Medido em 29/08/2026. Dos `link` de publicação do TJAC que trazem host,
    # **65,7% apontam `eproc1g.tjac.jus.br`** e só 29,8% o `esaj.tjac.jus.br`
    # (a cobertura de `link` no TJAC é baixa — 3,0% —, então este é o retrato
    # do que dá pra ver, não do acervo inteiro).
    #
    # Sonda ao vivo no próprio e-SAJ: **16 de 16** CNJ de prefixo 5 de 2025-2026
    # devolveram "não existe"; o CONTROLE NEGATIVO na mesma janela, prefixo 0
    # dos mesmos anos, devolveu **16 de 16** com cadastro (10 detalhe + 6
    # segredo de justiça). A fonte estava de pé; ela é que não tem a faixa.
    #
    # Tamanho: prefixo 5 + ano >= 2025 é **32,0%** dos processos do TJAC
    # (≈ 49 mil), com **0,43% de `ok`** contra 92,6% do prefixo 0.
    FORA_DA_FONTE_FAIXAS = (('5', 2025, 'eproc'),)


class TjalEnricher(BaseEsajEnricher):
    BASE_URL = 'https://www2.tjal.jus.br'
    TRIBUNAL_SIGLA = 'TJAL'
    LOG_NAME = 'voyager.enrichers.tjal'
    CPOSG_PATH = 'cposg5'  # TJAL: 2º grau é /cposg5/ (TJSP usa /cposg/)
    # 2026-05-30: www2.tjal.jus.br dava ReadTimeout em 100% do pool datacenter,
    # só respondendo via Cortex → PREFER_CORTEX=True.
    # 2026-06-17: reavaliado em prod — o pool ProxyScrape agora responde a ~37%
    # dos IPs (página e-SAJ válida em ~1s; ReadTimeouts são IPs mortos do pool,
    # não bloqueio do TJAL) e o gateway Cortex caiu (ProxyError em 100%). Com
    # MAX_PROXY_ROTATIONS=8, 37%/IP ⇒ ~99,8% de sucesso por processo. Volta pro
    # pool (default) pra paralelizar pelos 2500+ IPs e não depender do Cortex.
    PREFER_CORTEX = False

    # --- TJAL: o eproc ACABOU de começar (2026), e é isso que se quer pegar --
    #
    # A faixa é minúscula hoje — **0,1% do tribunal, ≈ 490 processos** — e está
    # aqui exatamente por isso: é a migração no primeiro ano, antes de virar
    # 13% (TJMG/TJRJ) ou 16% (TJSP). Prefixo 5 NÃO EXISTE no TJAL antes de 2026
    # (amostra de 10,36 M processos: só 0, 8 e 9), então o corte não alcança
    # acervo bom.
    #
    # Sonda ao vivo: **14 de 14** CNJ de prefixo 5 de 2026 deram "não existe" no
    # e-SAJ. `link` de publicação: `eproc1g.tjal.jus.br` já aparece.
    FORA_DA_FONTE_FAIXAS = (('5', 2026, 'eproc'),)
