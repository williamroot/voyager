"""Enricher do TRF1 via consulta pública do PJe (sem login).

Endpoint: https://pje1g-consultapublica.trf1.jus.br/consultapublica/ConsultaPublica/listView.seam

Fluxo: 3 requests por consulta —
  1. GET listView.seam → extrai jsessionid + javax.faces.ViewState do form fPP
  2. POST fPP com numeroProcesso → resposta com link de detalhe (linkAdv)
  3. GET detalhe → BeautifulSoup parseia metadata + polos ativo/passivo + advogados
"""
import logging
import re
import time
from datetime import datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from djen.proxies import ProxyScrapePool, cortex_proxy_url
from tribunals.models import Parte, Process, ProcessoParte

from .parsers import (
    classificar_tipo_parte,
    limpar_nome,
    parse_data_br,
    parse_documento,
    parse_oab,
    parse_role,
    parse_valor_brl,
)

logger = logging.getLogger('voyager.enrichers.trf1')

BASE_URL = 'https://pje1g-consultapublica.trf1.jus.br'
LIST_URL = f'{BASE_URL}/consultapublica/ConsultaPublica/listView.seam'

CAMPO_NUM = 'fPP:numProcesso-inputNumeroProcessoDecoration:numProcesso-inputNumeroProcesso'

DEFAULT_HEADERS = {
    'User-Agent': 'voyager-ops/0.1 (+pje-consulta-publica)',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.8',
}


class Trf1EnricherError(Exception):
    pass


class Trf1Enricher:
    def __init__(self, pool: Optional[ProxyScrapePool] = None):
        self.pool = pool or ProxyScrapePool.singleton()
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.timeout = (10, 60)

    def enriquecer(self, processo: Process) -> dict:
        if processo.tribunal_id != 'TRF1':
            raise Trf1EnricherError(f'Tribunal {processo.tribunal_id} não suportado por este enricher.')

        try:
            link_detalhe = self._buscar_processo(processo.numero_cnj)
        except Exception as exc:
            self._marcar_erro(processo, f'busca: {exc}')
            return {'cnj': processo.numero_cnj, 'status': 'erro', 'erro': str(exc)[:200]}

        if not link_detalhe:
            # PJe não tem registro — comum em processos pré-PJe (pré-2014 no TRF1)
            # ou que tramitam em sistemas legados (físico, eproc antigo).
            self._marcar_nao_encontrado(processo)
            return {'cnj': processo.numero_cnj, 'status': 'nao_encontrado'}

        try:
            soup = self._fetch_detalhe(link_detalhe)
            dados = self._extrair_dados(soup)
            partes = self._extrair_partes(soup)
            with transaction.atomic():
                self._aplicar_dados(processo, dados)
                self._aplicar_partes(processo, partes)
                processo.enriquecido_em = timezone.now()
                processo.enriquecimento_status = Process.ENRIQ_OK
                processo.enriquecimento_erro = ''
                processo.save(update_fields=[
                    'classe_codigo', 'classe_nome', 'classe',
                    'assunto_codigo', 'assunto_nome', 'assunto',
                    'data_autuacao', 'valor_causa', 'orgao_julgador_codigo',
                    'orgao_julgador_nome', 'juizo', 'segredo_justica',
                    'enriquecido_em', 'enriquecimento_status', 'enriquecimento_erro',
                ])
        except Exception as exc:
            logger.exception('falha ao parsear detalhe', extra={'cnj': processo.numero_cnj})
            self._marcar_erro(processo, f'parse: {exc}')
            return {'cnj': processo.numero_cnj, 'status': 'erro', 'erro': str(exc)[:200]}

        return {
            'cnj': processo.numero_cnj,
            'status': 'ok',
            'classe': processo.classe_nome,
            'assunto': processo.assunto_nome,
            'orgao_julgador': processo.orgao_julgador_nome,
            'partes_total': sum(len(v) for v in partes.values()),
        }

    def _marcar_nao_encontrado(self, processo: Process) -> None:
        processo.enriquecido_em = timezone.now()
        processo.enriquecimento_status = Process.ENRIQ_NAO_ENCONTRADO
        processo.enriquecimento_erro = ''
        processo.save(update_fields=['enriquecido_em', 'enriquecimento_status', 'enriquecimento_erro'])

    def _marcar_erro(self, processo: Process, msg: str) -> None:
        processo.enriquecido_em = timezone.now()
        processo.enriquecimento_status = Process.ENRIQ_ERRO
        processo.enriquecimento_erro = msg[:1000]
        processo.save(update_fields=['enriquecido_em', 'enriquecimento_status', 'enriquecimento_erro'])

    # ---------- HTTP ----------

    def _proxy(self) -> Optional[dict]:
        url = cortex_proxy_url() or self.pool.get()
        return {'http': url, 'https': url} if url else None

    def _get(self, url: str) -> requests.Response:
        resp = self.session.get(url, proxies=self._proxy(), timeout=self.timeout, allow_redirects=True)
        resp.raise_for_status()
        return resp

    def _post(self, url: str, data: dict) -> requests.Response:
        resp = self.session.post(url, data=data, proxies=self._proxy(), timeout=self.timeout)
        resp.raise_for_status()
        return resp

    # ---------- Etapas ----------

    def _extract_form_fields(self, soup: BeautifulSoup) -> dict:
        """Extrai inputs hidden + valores default do form fPP."""
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
        """O botão fPP:searchProcessos é só visual; chama executarReCaptcha → executarPesquisaReCaptcha
        que está num <script id="fPP:j_idXXX">. É esse ID que deve ir no payload."""
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
        resp = self._get(LIST_URL)
        soup = BeautifulSoup(resp.text, 'html.parser')
        vs = soup.find('input', {'name': 'javax.faces.ViewState'})
        if not vs or not vs.get('value'):
            raise Trf1EnricherError('javax.faces.ViewState não encontrado.')

        fields = self._extract_form_fields(soup)
        search_id = self._find_search_script_id(soup) or 'fPP:j_id268'
        logger.info('search button id', extra={'id': search_id})

        payload = dict(fields)
        payload[CAMPO_NUM] = numero_cnj
        payload['fPP'] = 'fPP'
        payload['AJAXREQUEST'] = '_viewRoot'
        payload['javax.faces.ViewState'] = vs['value']
        payload[search_id] = search_id
        payload['AJAX:EVENTS_COUNT'] = '1'

        resp = self._post(LIST_URL, payload)
        # 1) link direto pro detalhe (match único)
        m = re.search(r"(/consultapublica/ConsultaPublica/DetalheProcessoConsultaPublica/[^\"'<>\s]+)", resp.text)
        if m:
            return BASE_URL + m.group(1).replace('&amp;', '&')
        # 2) idProcessoTrf no JS — fallback
        m_id = re.search(r"idProcessoTrf['\"]?\s*[:=]\s*['\"]?(\d+)", resp.text)
        if m_id:
            return f'{BASE_URL}/consultapublica/ConsultaPublica/DetalheProcessoConsultaPublica/listView.seam?ca={m_id.group(1)}'
        logger.warning('detalhe não encontrado', extra={'cnj': numero_cnj, 'sample': resp.text[:500]})
        return None

    def _fetch_detalhe(self, link_detalhe: str) -> BeautifulSoup:
        time.sleep(0.4)  # gentileza com o servidor
        resp = self._get(link_detalhe)
        return BeautifulSoup(resp.text, 'html.parser')

    # ---------- Parsing do detalhe ----------

    def _extrair_dados(self, soup: BeautifulSoup) -> dict:
        """Extrai metadados do detalhe. Estrutura usada pelo PJe consulta pública:
        <div class="propertyView">
          <div class="name"><label>Classe Judicial</label></div>
          <div class="value">CUMPRIMENTO DE SENTENÇA (156)</div>
        </div>
        """
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

        # Órgão Julgador vem como <b>Órgão Julgador</b><br/>NOME<div...>
        for b in soup.find_all('b'):
            label = b.get_text(strip=True).lower()
            if 'rg' in label and 'julgador' in label:
                # Caminha pelos siblings parando no primeiro NavigableString não-vazio.
                node = b.next_sibling
                while node is not None:
                    if isinstance(node, str):
                        txt = node.strip()
                        if txt:
                            dados['orgao_julgador'] = txt[:255]
                            break
                    elif getattr(node, 'name', None) == 'br':
                        pass  # ignore <br/>
                    elif getattr(node, 'name', None) in ('div', 'b'):
                        break  # próxima seção começou
                    node = node.next_sibling
                break

        return dados

    def _aplicar_dados(self, processo: Process, dados: dict) -> None:
        if 'classe' in dados:
            classe = dados['classe']
            m = re.match(r'(.*?)(?:\s*\(?\s*(\d{2,5})\s*\)?)?\s*$', classe)
            if m:
                processo.classe_nome = (m.group(1) or '').strip()[:255]
                processo.classe_codigo = (m.group(2) or '')[:20]
            else:
                processo.classe_nome = classe[:255]
            if processo.classe_codigo:
                from tribunals.models import ClasseJudicial
                cj, _ = ClasseJudicial.objects.get_or_create(
                    codigo=processo.classe_codigo,
                    defaults={'nome': processo.classe_nome or processo.classe_codigo},
                )
                processo.classe = cj
        if 'assunto' in dados:
            assunto = dados['assunto']
            m = re.match(r'(.*?)(?:\s*\(?\s*(\d{2,5})\s*\)?)?\s*$', assunto)
            processo.assunto_nome = ((m.group(1) if m else assunto) or '').strip()[:255]
            processo.assunto_codigo = ((m.group(2) if m else '') or '')[:20]
            if processo.assunto_codigo:
                from tribunals.models import Assunto
                a, _ = Assunto.objects.get_or_create(
                    codigo=processo.assunto_codigo,
                    defaults={'nome': processo.assunto_nome or processo.assunto_codigo},
                )
                processo.assunto = a
        if 'data_autuacao' in dados:
            dt = parse_data_br(dados['data_autuacao'])
            if dt:
                processo.data_autuacao = dt.date()
        if 'valor_causa' in dados:
            valor = parse_valor_brl(dados['valor_causa'])
            if valor:
                processo.valor_causa = valor
        if 'orgao_julgador' in dados:
            processo.orgao_julgador_nome = dados['orgao_julgador'][:255]
        if 'juizo' in dados:
            processo.juizo = dados['juizo'][:255]
        if 'segredo_justica' in dados:
            processo.segredo_justica = bool(dados['segredo_justica'])

    def _extrair_partes(self, soup: BeautifulSoup) -> dict[str, list[dict]]:
        polos = {'ativo': [], 'passivo': [], 'outros': []}
        for polo, div_id in (('ativo', 'poloAtivo'), ('passivo', 'poloPassivo'), ('outros', 'outrosInteressados')):
            block = soup.find('div', id=re.compile(div_id, re.IGNORECASE))
            if not block:
                continue
            polos[polo] = self._parse_polo(block)
        return polos

    _IGNORE_TEXTOS = frozenset({'participante', 'situação', 'situacao', 'ativo', 'inativo', ''})

    def _parse_polo(self, block) -> list[dict]:
        """Cada <tr> tem múltiplos spans. Estrutura observada no PJe consulta pública:
            tr[0] = cabeçalho ("Participante", "Situação") — pula
            tr[N] = dados:
              spans[0] = concatenado (tudo junto)
              spans[1] = parte principal isolada
              spans[2..-1] = advogados / representantes
              spans[-1] = situação ("Ativo"/"Inativo")
        Nem sempre os spans estão alinhados, mas ignoramos os textos da blacklist
        e pegamos o restante.
        """
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

            # heurística: o primeiro texto pode ser o "concatenado"; se é claramente um
            # super-set de outro span, descarta. O melhor sinal é ter 2+ ocorrências de
            # nomes dentro dele.
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

    def _aplicar_partes(self, processo: Process, polos: dict[str, list[dict]]) -> None:
        # Limpa participações antigas — sempre re-cria. Partes (entidades) NUNCA são deletadas.
        ProcessoParte.objects.filter(processo=processo).delete()
        for polo, partes in polos.items():
            for principal in partes:
                p_principal = self._upsert_parte(principal)
                # get_or_create — evita duplicate quando a mesma parte aparece 2x como principal
                # (ex: HTML lista o mesmo advogado em rows separados sob nodes ambíguos).
                pp_principal, _ = ProcessoParte.objects.get_or_create(
                    processo=processo, parte=p_principal,
                    polo=polo, papel=principal.get('papel', ''),
                    representa=None,
                )
                for rep in principal.get('representantes', []):
                    p_rep = self._upsert_parte(rep)
                    if p_rep.pk == p_principal.pk:
                        continue  # representante == representado → skip
                    ProcessoParte.objects.create(
                        processo=processo, parte=p_rep,
                        polo=polo, papel=rep.get('papel', '') or 'ADVOGADO',
                        representa=pp_principal,
                    )

    def _upsert_parte(self, info: dict) -> Parte:
        documento = info.get('documento') or ''
        oab = info.get('oab') or ''
        defaults = {
            'nome': info['nome'],
            'tipo_documento': info.get('tipo_documento') or '',
            'tipo': info.get('tipo') or 'desconhecido',
        }
        if documento:
            obj, _ = Parte.objects.update_or_create(documento=documento, defaults={**defaults, 'oab': oab})
            return obj
        if oab:
            obj, _ = Parte.objects.update_or_create(oab=oab, defaults={**defaults, 'documento': ''})
            return obj
        return Parte.objects.create(documento='', oab='', **defaults)
