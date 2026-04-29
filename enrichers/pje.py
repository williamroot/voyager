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

from djen.proxies import ProxyScrapePool, cortex_proxy_url
from tribunals.models import Process

from . import stream
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


class BasePjeEnricher:
    """Subclasse define BASE_URL, LIST_URL, DETALHE_PATH e TRIBUNAL_SIGLA."""

    BASE_URL: str = ''
    LIST_URL: str = ''
    DETALHE_PATH: str = ''           # ex.: '/consultapublica/ConsultaPublica/DetalheProcessoConsultaPublica'
    TRIBUNAL_SIGLA: str = ''
    LOG_NAME: str = 'voyager.enrichers.pje'

    def __init__(self, pool: Optional[ProxyScrapePool] = None):
        if not (self.BASE_URL and self.LIST_URL and self.DETALHE_PATH and self.TRIBUNAL_SIGLA):
            raise NotImplementedError('Subclasse deve definir BASE_URL/LIST_URL/DETALHE_PATH/TRIBUNAL_SIGLA')
        self.pool = pool or ProxyScrapePool.singleton()
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.timeout = (10, 60)
        self.logger = logging.getLogger(self.LOG_NAME)

    def enriquecer(self, processo: Process) -> dict:
        """Faz scraping no PJe e publica o resultado no stream.

        Não escreve no DB — o drainer aplica os campos e partes em bulk.
        Retorna apenas um sumário pro RQ guardar como resultado do job.
        """
        if processo.tribunal_id != self.TRIBUNAL_SIGLA:
            raise PjeEnricherError(
                f'Tribunal {processo.tribunal_id} não suportado por {self.__class__.__name__}.'
            )

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
        except Exception as exc:
            stream.publish(stream.build_erro_payload(**base, erro=f'busca: {exc}'))
            return {'cnj': processo.numero_cnj, 'status': 'erro', 'erro': str(exc)[:200]}

        if not link_detalhe:
            stream.publish(stream.build_nao_encontrado_payload(**base))
            return {'cnj': processo.numero_cnj, 'status': 'nao_encontrado'}

        try:
            soup = self._fetch_detalhe(link_detalhe)
            dados = self._extrair_dados(soup)
            partes = self._extrair_partes(soup)
        except Exception as exc:
            self.logger.exception('falha ao parsear detalhe', extra={'cnj': processo.numero_cnj})
            stream.publish(stream.build_erro_payload(**base, erro=f'parse: {exc}'))
            return {'cnj': processo.numero_cnj, 'status': 'erro', 'erro': str(exc)[:200]}

        stream.publish(stream.build_ok_payload(**base, dados=dados, partes=partes))
        return {
            'cnj': processo.numero_cnj,
            'status': 'ok',
            'classe_raw': dados.get('classe'),
            'partes_total': sum(len(v) for v in partes.values()),
        }

    # ---------- HTTP ----------

    MAX_PROXY_ROTATIONS = 10

    def _next_proxy(self, exclude: set) -> Optional[str]:
        """Próximo proxy do pool ProxyScrape, evitando os já tentados nessa
        request. Foco no pool (1500 IPs rotativos); Cortex só como fallback
        quando o pool está realmente exausto."""
        for _ in range(40):
            url = self.pool.get()
            if url and url not in exclude:
                return url
        cortex = cortex_proxy_url()
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
        for tentativa in range(1, self.MAX_PROXY_ROTATIONS + 1):
            proxy_url = self._next_proxy(tentados)
            if not proxy_url:
                self.logger.warning('pool exausto sem proxy disponível', extra={
                    'tentativa': tentativa, 'url': url,
                })
                break
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
            if resp.status_code in (403, 429):
                self.logger.warning('proxy bloqueado pelo PJe, rotacionando', extra={
                    'proxy': proxy_url, 'status': resp.status_code,
                    'tentativa': tentativa,
                })
                if proxy_url != cortex_proxy_url():
                    self.pool.mark_bad(proxy_url)
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

    def _buscar_processo(self, numero_cnj: str) -> Optional[str]:
        resp = self._get(self.LIST_URL)
        soup = BeautifulSoup(resp.text, 'html.parser')
        vs = soup.find('input', {'name': 'javax.faces.ViewState'})
        if not vs or not vs.get('value'):
            raise PjeEnricherError('javax.faces.ViewState não encontrado.')

        fields = self._extract_form_fields(soup)
        search_id = self._find_search_script_id(soup) or 'fPP:j_id268'
        self.logger.info('search button id', extra={'id': search_id})

        payload = dict(fields)
        payload[CAMPO_NUM] = numero_cnj
        payload['fPP'] = 'fPP'
        payload['AJAXREQUEST'] = '_viewRoot'
        payload['javax.faces.ViewState'] = vs['value']
        payload[search_id] = search_id
        payload['AJAX:EVENTS_COUNT'] = '1'

        resp = self._post(self.LIST_URL, payload)
        # Match do link de detalhe — DETALHE_PATH varia por tribunal (TRF1 usa
        # /consultapublica/..., TRF3 usa /pje/...).
        path_re = re.escape(self.DETALHE_PATH) + r"/[^\"'<>\s]+"
        m = re.search(f"({path_re})", resp.text)
        if m:
            return self.BASE_URL + m.group(1).replace('&amp;', '&')
        m_id = re.search(r"idProcessoTrf['\"]?\s*[:=]\s*['\"]?(\d+)", resp.text)
        if m_id:
            return f'{self.BASE_URL}{self.DETALHE_PATH}/listView.seam?ca={m_id.group(1)}'
        # Não logamos `resp.text` porque a página de resposta do PJe pode
        # conter PII (nome de outras partes, advogados) — só o cnj e tamanho
        # bastam pra triagem operacional.
        self.logger.warning('detalhe não encontrado', extra={
            'cnj': numero_cnj, 'resp_len': len(resp.text),
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
