"""Enricher TJDFT via PJe consulta pública (nova SPA Angular + REST API).

Endpoint: https://pje-consultapublica-api.tjdft.jus.br/v1

O TJDFT migrou do PJe clássico (JSF/Seam) pra uma SPA Angular que consome
uma API REST Spring Boot. Por isso NÃO herda BasePjeEnricher — o fluxo é
um pipeline de chamadas JSON, sem `javax.faces.ViewState`, sem `fPP`, sem
HTML parsing.

Fluxo:
  GET /v1/processos?numeroProcesso=<CNJ>          → lista (filtra pelo CNJ)
       result[0].idProcesso                       → token opaco
  GET /v1/processos/{id}/dados                    → classe, assunto, órgão, autuação
  GET /v1/processos/{id}/poloAtivo?page=N         → partes ativas (paginado)
  GET /v1/processos/{id}/poloPassivo?page=N       → partes passivas
  GET /v1/processos/{id}/outrosInteressados?page=N → terceiros (MP, defensoria)

Documentos (CPF/CNPJ) vêm SEM máscara (igual TRF1/TJMG/TJMA, diferente do
TRF3 público). OABs vêm no formato "OAB DF13111-A" ou "OAB DF68420" — os
parsers padrão (parse_oab) já lidam com ambas.

Sem captcha, sem proxy obrigatório (CORS aberto pra origem oficial; testes
diretos do host passam). Caso surja rate limit, plugar o `ProxyScrapePool`
no `_get` é trivial.

Limitação conhecida: a rota `/v1/processos/{id}/dados` não expõe
`valorAcaoEmFloat` — o campo `valor_causa` fica vazio no enriquecimento
TJDFT. Mesma limitação do e-SAJ público.
"""
from __future__ import annotations

import datetime as _dt
import logging
import re
import time
from typing import Iterator, Optional

import requests
from django.utils import timezone

from tribunals.models import Process

from . import stream
from .parsers import (
    classificar_tipo_parte,
    limpar_nome,
    parse_documento,
    parse_oab,
)

DEFAULT_HEADERS = {
    'User-Agent': 'voyager-ops/0.1 (+tjdft-consulta-publica)',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.8',
    # Sem o Referer correto, AWS ALB / Spring rejeita com 403 em alguns
    # endpoints. Replicamos a origem oficial da SPA.
    'Referer': 'https://pje-consultapublica.tjdft.jus.br/',
    'Origin': 'https://pje-consultapublica.tjdft.jus.br',
}

_CNJ_DIGITS_RE = re.compile(r'^\d{20}$')


class TjdftEnricherError(Exception):
    pass


def _format_cnj(raw: str) -> str:
    """20 dígitos → NNNNNNN-DD.AAAA.J.TR.OOOO (formato esperado pela API)."""
    raw = re.sub(r'\D', '', raw or '')
    if not _CNJ_DIGITS_RE.match(raw):
        raise TjdftEnricherError(f'CNJ inválido: {raw!r}')
    return f'{raw[:7]}-{raw[7:9]}.{raw[9:13]}.{raw[13]}.{raw[14:16]}.{raw[16:]}'


# Hierárquico vem como "RAIZ (cod) - FILHO (cod) - ... - FOLHA (codFolha)".
# Para o catálogo (Assunto), só o último nível interessa — o código mais
# específico é o que o classificador usa, e o nome da folha é o que aparece
# em listagens.
def _ultimo_segmento_assunto(texto: str) -> str:
    if not texto:
        return ''
    partes = [p.strip() for p in texto.split(' - ') if p.strip()]
    return partes[-1] if partes else texto.strip()


# ISO `2019-09-30T16:59:28.45` → `30/09/2019` (formato esperado por
# parse_data_br no drainer; manter consistente com PJe/e-SAJ).
def _iso_para_br(iso: str) -> str:
    if not iso:
        return ''
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', iso)
    if not m:
        return ''
    return f'{m.group(3)}/{m.group(2)}/{m.group(1)}'


class TjdftEnricher:
    BASE_URL = 'https://pje-consultapublica-api.tjdft.jus.br'
    TRIBUNAL_SIGLA = 'TJDFT'
    LOG_NAME = 'voyager.enrichers.tjdft'

    # API costuma demorar mais que PJe clássico — sem retry/proxy interno,
    # ser conservador com o timeout total.
    REQUEST_TIMEOUT = (10, 60)
    POLO_PAGE_SIZE = 10  # default da API; iteramos até pageInfo.last
    POLO_MAX_PAGES = 50  # cap defensivo contra loop por bug de paginação

    def __init__(self, prefer_cortex: bool = False):
        # prefer_cortex aceito por compat com a interface do BasePjeEnricher
        # (manual queue passa kwargs); TJDFT não usa pool de proxies hoje.
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.logger = logging.getLogger(self.LOG_NAME)
        self.prefer_cortex = prefer_cortex

    def enriquecer(self, processo: Process, direct_apply: bool = False) -> dict:
        if processo.tribunal_id != self.TRIBUNAL_SIGLA:
            raise TjdftEnricherError(
                f'Tribunal {processo.tribunal_id} não suportado por {self.__class__.__name__}.'
            )

        base = {
            'process_id': processo.pk,
            'tribunal': processo.tribunal_id,
            'numero_cnj': processo.numero_cnj,
            'scraped_at': timezone.now().astimezone(_dt.timezone.utc).isoformat(),
        }

        try:
            id_processo = self._buscar_id_processo(processo.numero_cnj)
        except Exception as exc:
            self._emit(stream.build_erro_payload(**base, erro=f'busca: {exc}'), direct_apply)
            return {'cnj': processo.numero_cnj, 'status': 'erro', 'erro': str(exc)[:200]}

        if not id_processo:
            self._emit(stream.build_nao_encontrado_payload(**base), direct_apply)
            return {'cnj': processo.numero_cnj, 'status': 'nao_encontrado'}

        try:
            dados_raw = self._fetch_dados(id_processo)
            polos_raw = {
                'ativo': self._fetch_polo(id_processo, 'poloAtivo'),
                'passivo': self._fetch_polo(id_processo, 'poloPassivo'),
                'outros': self._fetch_polo(id_processo, 'outrosInteressados'),
            }
        except Exception as exc:
            self.logger.exception('falha ao buscar detalhe', extra={'cnj': processo.numero_cnj})
            self._emit(stream.build_erro_payload(**base, erro=f'detalhe: {exc}'), direct_apply)
            return {'cnj': processo.numero_cnj, 'status': 'erro', 'erro': str(exc)[:200]}

        dados = self._extrair_dados(dados_raw)
        partes = self._extrair_partes(polos_raw)

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

    def _get_json(self, url: str, params: Optional[dict] = None) -> dict:
        resp = self.session.get(url, params=params, timeout=self.REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        # Wrapper padrão: {"status":"ok","code":"200","messages":[...],"result":...}
        if isinstance(data, dict) and data.get('status') and data.get('status') != 'ok':
            raise TjdftEnricherError(f'API erro: status={data.get("status")} code={data.get("code")}')
        return data

    def _buscar_id_processo(self, cnj_raw: str) -> Optional[str]:
        """Lista processos filtrando pelo número e retorna idProcesso do
        primeiro match exato. Retorna None se nada bate."""
        cnj_fmt = _format_cnj(cnj_raw)
        data = self._get_json(
            f'{self.BASE_URL}/v1/processos',
            params={'page': 0, 'numeroProcesso': cnj_fmt},
        )
        for item in data.get('result') or []:
            if item.get('numeroProcesso') == cnj_fmt and item.get('idProcesso'):
                return item['idProcesso']
        return None

    def _fetch_dados(self, id_processo: str) -> dict:
        data = self._get_json(f'{self.BASE_URL}/v1/processos/{id_processo}/dados')
        return data.get('result') or {}

    def _fetch_polo(self, id_processo: str, polo: str) -> list[dict]:
        """Itera páginas até `pageInfo.last`. Lista de participantes."""
        out: list[dict] = []
        for page in range(self.POLO_MAX_PAGES):
            data = self._get_json(
                f'{self.BASE_URL}/v1/processos/{id_processo}/{polo}',
                params={'page': page},
            )
            result = data.get('result') or []
            out.extend(result)
            page_info = data.get('pageInfo') or {}
            current = page_info.get('current') or (page + 1)
            last = page_info.get('last') or current
            if current >= last or not result:
                break
            time.sleep(0.2)  # cortesia / evita rate limit
        return out

    # ---------- Parsing ----------

    def _extrair_dados(self, dados_raw: dict) -> dict:
        """Normaliza pra contrato compartilhado com drainer:normalize_dados."""
        classe = dados_raw.get('classeJudicial') or ''
        assunto = _ultimo_segmento_assunto(dados_raw.get('assunto') or '')
        orgao = dados_raw.get('orgaoJulgador') or dados_raw.get('orgaoJulgadorColegiado') or ''
        autuacao = _iso_para_br(dados_raw.get('dataDistribuicao') or '')

        out: dict = {}
        if classe:
            out['classe'] = classe
        if assunto:
            out['assunto'] = assunto
        if orgao:
            out['orgao_julgador'] = orgao
        if autuacao:
            out['data_autuacao'] = autuacao
        # `valor_causa` não vem na API pública — ficamos sem.
        return out

    # tipo da API TJDFT → polo. Quando vem em `outrosInteressados`, o
    # extrair_partes já força polo='outros' independente do tipo.
    _TIPOS_ATIVO = frozenset({'AUTOR', 'EXEQUENTE', 'REQUERENTE', 'EMBARGANTE',
                               'APELANTE', 'IMPETRANTE', 'AGRAVANTE', 'RECLAMANTE'})
    _TIPOS_PASSIVO = frozenset({'REU', 'RÉU', 'EXECUTADO', 'REQUERIDO', 'EMBARGADO',
                                 'APELADO', 'IMPETRADO', 'AGRAVADO', 'RECLAMADO'})

    def _extrair_partes(self, polos_raw: dict[str, list[dict]]) -> dict[str, list[dict]]:
        """Agrupa cada principal (não-advogado) com seus advogados subsequentes
        como `representantes` — formato esperado pelo drainer (que cria
        ProcessoParte com FK `representa` apontando pro principal).
        """
        polos: dict[str, list[dict]] = {'ativo': [], 'passivo': [], 'outros': []}
        for polo, participantes in polos_raw.items():
            principal_atual: Optional[dict] = None
            for item in participantes:
                pessoa = self._parse_pessoa(item)
                if not pessoa.get('nome'):
                    continue
                if pessoa['tipo'] == 'advogado':
                    if principal_atual is not None:
                        principal_atual['representantes'].append(pessoa)
                    else:
                        # Advogado sem principal anterior — entra como entrada
                        # solta (raro, mas pode acontecer se a API mudar ordem).
                        pessoa['representantes'] = []
                        polos[polo].append(pessoa)
                else:
                    pessoa['representantes'] = []
                    polos[polo].append(pessoa)
                    principal_atual = pessoa
        return polos

    def _parse_pessoa(self, item: dict) -> dict:
        """A API retorna campos estruturados (nome, tipo, procuradoria) +
        um `participante` textual com o doc/OAB embutido. Usamos os parsers
        compartilhados no `participante` pra extrair documento e OAB."""
        participante = item.get('participante') or ''
        nome_estruturado = (item.get('nome') or '').strip()
        tipo_api = (item.get('tipo') or '').strip().upper()

        documento, tipo_doc = parse_documento(participante)
        oab = parse_oab(participante)
        # Nome estruturado é mais confiável que limpar do texto cru —
        # API já entrega sem ruído.
        nome = nome_estruturado or limpar_nome(participante)

        tipo_classificado = classificar_tipo_parte(documento, tipo_doc, oab, tipo_api)
        # Se o `tipo` da API for explicitamente ADVOGADO, forçamos —
        # alguns advogados pessoa-jurídica (raro) cairíam como 'pj' senão.
        if tipo_api == 'ADVOGADO':
            tipo_classificado = 'advogado'

        return {
            'nome': nome[:255],
            'documento': documento[:20],
            'tipo_documento': tipo_doc,
            'oab': oab[:20],
            'papel': tipo_api[:120],
            'tipo': tipo_classificado,
        }


def _polo_para_outros(polo_key: str) -> str:
    """Helper exposto pra eventual reuso (drainer não precisa; mantemos
    privado por convenção)."""
    return 'outros' if polo_key == 'outrosInteressados' else polo_key
