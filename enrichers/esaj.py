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

    def __init__(self, prefer_cortex: bool = False):
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
        self.prefer_cortex = prefer_cortex
        self._session_inited = False

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

        try:
            html = self._fetch_processo(processo.numero_cnj)
        except Exception as exc:
            self._emit(stream.build_erro_payload(**base, erro=f'busca: {exc}'), direct_apply)
            return {'cnj': processo.numero_cnj, 'status': 'erro', 'erro': str(exc)[:200]}

        if html is None:
            self._emit(stream.build_nao_encontrado_payload(**base), direct_apply)
            return {'cnj': processo.numero_cnj, 'status': 'nao_encontrado'}

        try:
            soup = BeautifulSoup(html, 'html.parser')
            dados = self._extrair_dados(soup)
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

    def _ensure_session(self) -> None:
        """e-SAJ exige JSESSIONID válido antes do search.do; sem isso o
        search retorna a página de busca em vez do redirect pro detalhe."""
        if self._session_inited:
            return
        self.session.get(self.OPEN_URL, timeout=self.timeout)
        self._session_inited = True

    @staticmethod
    def _build_search_params(cnj_fmt: str) -> dict:
        """Monta os params do search.do a partir do CNJ formatado.

        `numeroDigitoAnoUnificado` = NNNNNNN-DD.AAAA e `foroNumeroUnificado` =
        OOOO. Derivado por segmento (split em '.') — independente de tribunal.
        O código antigo cravava `.split('.8.26')` (J.TR do TJSP); a versão por
        segmento dá o mesmo resultado pro TJSP e funciona pra TJAL (.8.02) e
        qualquer outro e-SAJ.
        """
        parts = cnj_fmt.split('.')  # ['NNNNNNN-DD','AAAA','J','TR','OOOO']
        return {
            'conversationId': '',
            'cbPesquisa': 'NUMPROC',
            'dadosConsulta.localPesquisa.cdLocal': '-1',
            'numeroDigitoAnoUnificado': f'{parts[0]}.{parts[1]}',
            'foroNumeroUnificado': parts[4],
            'dadosConsulta.valorConsultaNuUnificado': cnj_fmt,
            'dadosConsulta.tipoNuProcesso': 'UNIFICADO',
        }

    def _fetch_processo(self, cnj_raw: str) -> Optional[str]:
        """Retorna o HTML do detalhe ou None se o processo não foi encontrado.

        Detecção de "não encontrado": busca por NUMPROC com 1 resultado redireciona
        (302) pra show.do. Sem resultado, retorna a própria página de busca (sem
        redirect). Usamos `response.history` pra distinguir.
        """
        cnj_fmt = _format_cnj(cnj_raw)
        params = self._build_search_params(cnj_fmt)

        self._ensure_session()

        resp = self.session.get(self.SEARCH_URL, params=params,
                                timeout=self.timeout, allow_redirects=True)
        resp.raise_for_status()

        # Sem redirect → não encontrou (search.do voltou a própria página de busca).
        # Resposta com `formConsulta` é o form de pesquisa (sem resultado).
        if not resp.history and 'formConsulta' in resp.text:
            return None
        # Página de detalhe tem campos como #numeroProcesso ou #classeProcesso.
        # Se não tem nem o redirect nem campos do detalhe, trata como não encontrado.
        if 'numeroProcesso' not in resp.text and 'classeProcesso' not in resp.text:
            return None
        return resp.text

    # ---------- Parsing ----------

    def _extrair_dados(self, soup: BeautifulSoup) -> dict:
        def t(sel: str) -> str:
            el = soup.select_one(sel)
            return el.get_text(strip=True) if el else ''

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
