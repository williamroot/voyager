"""Enricher genérico via PJe consulta pública (sem login).

PJe é o sistema padrão CNJ usado em vários TRFs/TJs. A consulta pública
expõe um form JSF (`fPP`) que aceita o número CNJ e retorna um link pra
página de detalhe com metadados + polos.

Subclasses precisam apenas configurar `BASE_URL`, `LIST_URL` e
`DETALHE_PATH`. Toda a lógica de form/parsing/dedupe de partes é
compartilhada.
"""
import logging
import re
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup
from django.db import IntegrityError, transaction
from django.utils import timezone

from djen.proxies import ProxyScrapePool, cortex_proxy_url
from tribunals.models import Assunto, ClasseJudicial, Parte, Process, ProcessoParte

from .parsers import (
    classificar_tipo_parte,
    is_documento_mascarado,
    limpar_nome,
    parse_data_br,
    parse_documento,
    parse_oab,
    parse_role,
    parse_valor_brl,
    real_casa_com_mascara,
)

CAMPO_NUM = 'fPP:numProcesso-inputNumeroProcessoDecoration:numProcesso-inputNumeroProcesso'

DEFAULT_HEADERS = {
    'User-Agent': 'voyager-ops/0.1 (+pje-consulta-publica)',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.8',
}


class PjeEnricherError(Exception):
    pass


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
        if processo.tribunal_id != self.TRIBUNAL_SIGLA:
            raise PjeEnricherError(
                f'Tribunal {processo.tribunal_id} não suportado por {self.__class__.__name__}.'
            )

        try:
            link_detalhe = self._buscar_processo(processo.numero_cnj)
        except Exception as exc:
            self._marcar_erro(processo, f'busca: {exc}')
            return {'cnj': processo.numero_cnj, 'status': 'erro', 'erro': str(exc)[:200]}

        if not link_detalhe:
            self._marcar_nao_encontrado(processo)
            return {'cnj': processo.numero_cnj, 'status': 'nao_encontrado'}

        try:
            soup = self._fetch_detalhe(link_detalhe)
            dados = self._extrair_dados(soup)
            partes = self._extrair_partes(soup)
            with transaction.atomic():
                # Lock no Process serializa enriquecimentos concorrentes do
                # mesmo processo: dois jobs duplicados (auto-enqueue + retry,
                # backfill re-running) viram fila em vez de race em
                # _aplicar_partes (DELETE+INSERT poderia perder dados).
                processo = Process.objects.select_for_update().get(pk=processo.pk)
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
            self.logger.exception('falha ao parsear detalhe', extra={'cnj': processo.numero_cnj})
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
        self.logger.warning('detalhe não encontrado', extra={'cnj': numero_cnj, 'sample': resp.text[:500]})
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

    @staticmethod
    def _upsert_catalogo(model, codigo: str, nome: str):
        """Idempotente e race-safe: tenta INSERT ignorando conflito (não
        levanta IntegrityError em corrida) e devolve a row pelo codigo.
        Evita poluir a transação atômica do enriquecimento com falhas de
        chave duplicada quando 2 workers veem um código pela 1ª vez ao
        mesmo tempo."""
        nome_final = (nome or codigo)[:255]
        model.objects.bulk_create(
            [model(codigo=codigo, nome=nome_final)],
            ignore_conflicts=True,
        )
        return model.objects.get(codigo=codigo)

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
                processo.classe = self._upsert_catalogo(
                    ClasseJudicial, processo.classe_codigo, processo.classe_nome,
                )
        if 'assunto' in dados:
            assunto = dados['assunto']
            m = re.match(r'(.*?)(?:\s*\(?\s*(\d{2,5})\s*\)?)?\s*$', assunto)
            processo.assunto_nome = ((m.group(1) if m else assunto) or '').strip()[:255]
            processo.assunto_codigo = ((m.group(2) if m else '') or '')[:20]
            if processo.assunto_codigo:
                processo.assunto = self._upsert_catalogo(
                    Assunto, processo.assunto_codigo, processo.assunto_nome,
                )
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

    def _aplicar_partes(self, processo: Process, polos: dict[str, list[dict]]) -> None:
        ProcessoParte.objects.filter(processo=processo).delete()
        for polo, partes in polos.items():
            for principal in partes:
                p_principal = self._upsert_parte(principal)
                pp_principal, _ = ProcessoParte.objects.get_or_create(
                    processo=processo, parte=p_principal,
                    polo=polo, papel=principal.get('papel', ''),
                    representa=None,
                )
                for rep in principal.get('representantes', []):
                    p_rep = self._upsert_parte(rep)
                    if p_rep.pk == p_principal.pk:
                        continue
                    ProcessoParte.objects.create(
                        processo=processo, parte=p_rep,
                        polo=polo, papel=rep.get('papel', '') or 'ADVOGADO',
                        representa=pp_principal,
                    )

    def _upsert_parte(self, info: dict) -> Parte:
        """Dedupe de Parte em 4 caminhos em ordem de confiança:

        1. OAB — precedência total pra advogados (chave única estável).
        2. Documento REAL — PK natural global (CPF/CNPJ).
        3. Documento MASCARADO + nome — TRF3 esconde dígitos; tenta primeiro
           reusar Parte com doc REAL que case com a máscara (mesma entidade
           vista em TRF1 com CNPJ completo).
        4. Sem doc nem OAB — `(nome, tipo)` único pra órgãos públicos
           (Procuradoria, Defensoria) que aparecem sem CNPJ.

        Todos os caminhos usam `_safe_upsert_parte` que é race-safe via
        bulk_create(ignore_conflicts) + get — não levanta IntegrityError
        nem MultipleObjectsReturned em alta concorrência (4+ workers).
        """
        documento = info.get('documento') or ''
        oab = info.get('oab') or ''
        nome = (info.get('nome') or '')[:255]
        base = {
            'nome': nome,
            'tipo_documento': info.get('tipo_documento') or '',
            'tipo': info.get('tipo') or 'desconhecido',
        }

        if oab:
            return self._safe_upsert_parte(
                lookup={'oab': oab},
                defaults={**base, 'documento': documento},
            )

        if documento:
            if is_documento_mascarado(documento):
                from django.db.models import Q
                candidatos = (
                    Parte.objects
                    .filter(nome=nome).exclude(documento='')
                    .exclude(Q(documento__contains='X') | Q(documento__contains='x') | Q(documento__contains='*'))
                )
                for c in candidatos:
                    if real_casa_com_mascara(c.documento, documento):
                        return c
                return self._safe_upsert_parte(
                    lookup={'nome': nome, 'documento': documento},
                    defaults={**base, 'oab': ''},
                )
            return self._safe_upsert_parte(
                lookup={'documento': documento},
                defaults={**base, 'oab': ''},
            )

        # Prioridade pra completude dos dados: se existe exatamente UMA Parte
        # com mesmo nome e CNPJ REAL preenchido (formato XX.XXX.XXX/XXXX-XX),
        # reusa essa entidade. CPFs/OABs não entram — risco de homônimo é alto
        # e dedupar por OAB já é o caminho 1. CNPJ identifica unicamente uma
        # PJ, então 1 match com mesmo nome → é a mesma entidade.
        candidatos = Parte.objects.filter(nome=nome).extra(
            where=[r"documento ~ '^[0-9]{2}\.[0-9]{3}\.[0-9]{3}/[0-9]{4}-[0-9]{2}$'"],
        )
        if candidatos.count() == 1:
            return candidatos.first()

        return self._safe_upsert_parte(
            lookup={'documento': '', 'oab': '', 'nome': nome, 'tipo': base['tipo']},
            defaults={'tipo_documento': base['tipo_documento']},
        )

    @staticmethod
    def _safe_upsert_parte(*, lookup: dict, defaults: dict) -> Parte:
        """Race-safe upsert via SELECT → bulk_create(ignore_conflicts) → SELECT.

        Funciona porque cada par `lookup` corresponde a uma UniqueConstraint
        partial em Parte (real, mascarado, oab, sem-doc-sem-oab). Em corrida:
          - Worker A faz get(): 0 rows
          - Worker B faz get(): 0 rows
          - A: bulk_create([Parte(...)], ignore_conflicts) — INSERT vence
          - B: bulk_create([Parte(...)], ignore_conflicts) — INSERT vira no-op
          - Ambos fazem get() final e veem o MESMO row.

        bulk_create(ignore_conflicts=True) emite INSERT ... ON CONFLICT DO
        NOTHING — não levanta IntegrityError nem polui a transação atômica
        do enriquecimento.
        """
        existing = Parte.objects.filter(**lookup).first()
        if existing is not None:
            dirty = {k: v for k, v in defaults.items() if getattr(existing, k) != v}
            if dirty:
                for k, v in dirty.items():
                    setattr(existing, k, v)
                try:
                    existing.save(update_fields=list(dirty))
                except IntegrityError:
                    pass  # outro worker já atualizou — eventual consistency
            return existing

        Parte.objects.bulk_create(
            [Parte(**{**lookup, **defaults})],
            ignore_conflicts=True,
        )
        # Constraint partial garante exatamente 1 row pelo lookup.
        return Parte.objects.get(**lookup)
