"""Enricher TJPA via portal próprio "Consulta Unificada" (SPA Angular + REST).

Endpoint: https://consulta-processual-unificada-prd.tjpa.jus.br/consilium-rest

O TJPA saiu do PJe clássico (JSF/Seam) e do PJe-REST padrão pra um portal
próprio: SPA Angular que consome uma API REST `consilium-rest`. Não herda
nem `BasePjeEnricher` (JSF) nem o fluxo multi-chamada do TJDFT — aqui UMA
chamada devolve busca + dados + partes:

  GET /consilium-rest/processobycnj/{cnj}   ({cnj} aceita formatado OU dígitos)

Resposta:
  {numero, numeroFormatado, listaProcessos:[{
      numeroFormatado, classe ("39 - Inventário"), assunto ("7676 - ..."),
      comarca, vara, valorCausa, valorCausaFormatado ("R$ 120.000,00"),
      dataDistribuicao (ms epoch), segredoJustica ("Sim"/"Não"),
      partes:[{nome, tipo, polo (A/P/T), cpfcnpj, tppessoa (F/J), flsegredo}]
  }]}

Particularidades:
  - reCAPTCHA existe no frontend mas NÃO é enforced no servidor pra este
    endpoint — devolve dados sem token, sem login.
  - User-Agent precisa ser de browser real; UAs identificadores ("voyager-ops")
    levam 429/block. Referer da origem oficial é obrigatório.
  - Rate-limit: a API devolve HTTP 429 sob rajada → rotaciona proxy (cada IP
    do Cortex/pool é diferente) + espera entre tentativas.
  - `cpfcnpj` vem null na consulta pública (sem doc) → classificamos pf/pj
    via `tppessoa` (F/J); advogado via papel/OAB.
  - `classe`/`assunto` vêm como "CÓDIGO - Nome" (ordem inversa do PJe, que dá
    "Nome (código)") → reordenamos pra "Nome (código)" pro `_split_nome_codigo`
    do drainer extrair o código.

Partes: `polo` A=ativo, P=passivo, T=outros. `tipo` é o papel processual
(AUTOR/REQUERENTE/REU/INVENTARIADO/ADVOGADO/REPRESENTANTE DA PARTE/...).
Advogados e representantes entram como `representantes` do principal anterior
(mesmo contrato do TJDFT/e-SAJ — drainer cria ProcessoParte com FK `representa`).
"""
from __future__ import annotations

import datetime as _dt
import logging
import re
import time
from typing import Optional

import requests
from django.utils import timezone

from djen.proxies import ProxyScrapePool, cortex_proxy_url
from tribunals.models import Process

from . import stream
from .parsers import classificar_tipo_parte, parse_documento, parse_oab

# UA de navegador real — a API do TJPA devolve 429/block pra UAs identificadores.
DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.8',
    'Referer': 'https://consulta-processual-unificada-prd.tjpa.jus.br/',
    'Origin': 'https://consulta-processual-unificada-prd.tjpa.jus.br',
}

_CNJ_DIGITS_RE = re.compile(r'^\d{20}$')

# Papéis que NÃO são parte principal — entram como representantes do principal
# anterior (mesmo polo). Advogado é detectado também por OAB/classificação.
_PAPEIS_REPRESENTANTE = frozenset({
    'ADVOGADO', 'ADVOGADA', 'REPRESENTANTE DA PARTE', 'REPRESENTANTE',
    'REPRESENTANTE LEGAL', 'PROCURADOR', 'PROCURADORA', 'DEFENSOR', 'DEFENSORA',
})

# Pará é UTC-3 o ano todo (sem horário de verão) — converte epoch ms pra data
# local sem depender de tz database.
_TZ_PARA = _dt.timezone(_dt.timedelta(hours=-3))


class TjpaEnricherError(Exception):
    pass


def _cnj_digits(raw: str) -> str:
    """Normaliza pra 20 dígitos (o endpoint aceita formatado OU só dígitos;
    mandamos dígitos por simplicidade)."""
    raw = re.sub(r'\D', '', raw or '')
    if not _CNJ_DIGITS_RE.match(raw):
        raise TjpaEnricherError(f'CNJ inválido: {raw!r}')
    return raw


def _ms_para_br(ms) -> str:
    """Epoch em milissegundos → 'DD/MM/YYYY' (formato esperado pelo
    parse_data_br do drainer). '' se inválido/ausente."""
    if not ms:
        return ''
    try:
        dt = _dt.datetime.fromtimestamp(int(ms) / 1000, tz=_TZ_PARA)
    except (ValueError, OverflowError, OSError, TypeError):
        return ''
    return dt.strftime('%d/%m/%Y')


def _codigo_nome_para_nome_codigo(texto: str) -> str:
    """'39 - Inventário' → 'Inventário (39)'.

    O TJPA entrega classe/assunto como 'CÓDIGO - Nome'; o `_split_nome_codigo`
    do drainer espera 'Nome (CÓDIGO)' pra separar o código. Sem reordenar, o
    código viraria parte do nome e o classificador ficaria sem `classe_codigo`.
    Se não casar o padrão, devolve o texto cru (drainer trata como nome puro).
    """
    if not texto:
        return ''
    m = re.match(r'^\s*(\d{1,6})\s*-\s*(.+?)\s*$', texto)
    if m:
        return f'{m.group(2).strip()} ({m.group(1)})'
    return texto.strip()


class TjpaEnricher:
    BASE_URL = 'https://consulta-processual-unificada-prd.tjpa.jus.br'
    TRIBUNAL_SIGLA = 'TJPA'
    LOG_NAME = 'voyager.enrichers.tjpa'

    REQUEST_TIMEOUT = (10, 60)
    # Limite de IPs distintos tentados por processo antes de desistir.
    MAX_PROXY_ROTATIONS = 8
    # Espera-base entre tentativas após 429 (cresce linearmente por tentativa).
    RATE_LIMIT_BACKOFF = 1.5

    def __init__(self, pool: Optional[ProxyScrapePool] = None, prefer_cortex: bool = False):
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.logger = logging.getLogger(self.LOG_NAME)
        # Pool ProxyScrape p/ paralelizar IPs; Cortex (residencial) como fallback
        # ou preferência (clique manual / dev).
        self.pool = pool or ProxyScrapePool.singleton()
        self.prefer_cortex = prefer_cortex

    def enriquecer(self, processo: Process, direct_apply: bool = False) -> dict:
        if processo.tribunal_id != self.TRIBUNAL_SIGLA:
            raise TjpaEnricherError(
                f'Tribunal {processo.tribunal_id} não suportado por {self.__class__.__name__}.'
            )

        base = {
            'process_id': processo.pk,
            'tribunal': processo.tribunal_id,
            'numero_cnj': processo.numero_cnj,
            'scraped_at': timezone.now().astimezone(_dt.timezone.utc).isoformat(),
        }

        try:
            proc_raw = self._buscar_processo(processo.numero_cnj)
        except Exception as exc:
            self.logger.warning('falha ao buscar processo', extra={
                'cnj': processo.numero_cnj, 'erro': str(exc)[:200]})
            self._emit(stream.build_erro_payload(**base, erro=f'busca: {exc}'), direct_apply)
            return {'cnj': processo.numero_cnj, 'status': 'erro', 'erro': str(exc)[:200]}

        if not proc_raw:
            self._emit(stream.build_nao_encontrado_payload(**base), direct_apply)
            return {'cnj': processo.numero_cnj, 'status': 'nao_encontrado'}

        try:
            dados = self._extrair_dados(proc_raw)
            partes = self._extrair_partes(proc_raw.get('partes') or [])
        except Exception as exc:
            self.logger.exception('falha ao parsear processo', extra={'cnj': processo.numero_cnj})
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
        como fallback. prefer_cortex=True inverte a ordem."""
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

    def _buscar_processo(self, cnj_raw: str) -> Optional[dict]:
        """Faz a chamada `processobycnj/{cnj}` rotacionando IP em 429/erro de
        transporte. Retorna o dict do 1º processo de `listaProcessos`, ou None
        se a API não achar nada (lista vazia).

        Roda por 1 IP do pool; em 429 (rate-limit), erro de transporte ou 5xx,
        rotaciona pra outro IP. Cortex rotaciona IP residencial a cada request
        (não é excluído entre tentativas); proxies do pool são IP fixo (excluídos
        pra não repetir). 429/transporte marcam o proxy do pool como bad; 5xx é
        culpa do servidor → rotaciona sem queimar o IP.
        """
        cnj = _cnj_digits(cnj_raw)
        url = f'{self.BASE_URL}/consilium-rest/processobycnj/{cnj}'

        tentados: set = set()
        last_erro: Optional[str] = None
        for tentativa in range(1, self.MAX_PROXY_ROTATIONS + 1):
            proxy = self._next_proxy(tentados)
            if not proxy:
                self.logger.warning('pool exausto sem proxy disponível',
                                    extra={'cnj': cnj, 'tentativa': tentativa})
                break
            cortex = cortex_proxy_url(self.pool)
            if proxy != cortex:
                tentados.add(proxy)
            proxies = {'http': proxy, 'https': proxy}
            try:
                resp = self.session.get(url, proxies=proxies, timeout=self.REQUEST_TIMEOUT)
            except (requests.ConnectionError, requests.Timeout,
                    requests.exceptions.ChunkedEncodingError) as exc:
                last_erro = f'transporte: {str(exc)[:120]}'
                if proxy != cortex:
                    self.pool.mark_bad(proxy)
                continue

            if resp.status_code == 429:
                # Rate-limit: espera (cresce por tentativa) e rotaciona IP.
                last_erro = 'rate-limited 429'
                if proxy != cortex:
                    self.pool.mark_bad(proxy)
                time.sleep(self.RATE_LIMIT_BACKOFF * tentativa)
                continue
            if resp.status_code in (403, 401):
                last_erro = f'bloqueado {resp.status_code}'
                if proxy != cortex:
                    self.pool.mark_bad(proxy)
                continue
            if resp.status_code == 404:
                # Endpoint responde 404 quando não há processo pra esse CNJ.
                return None
            if resp.status_code >= 500:
                last_erro = f'TJPA {resp.status_code}'
                continue
            resp.raise_for_status()

            try:
                data = resp.json()
            except ValueError as exc:
                last_erro = f'json inválido: {str(exc)[:80]}'
                continue

            lista = (data or {}).get('listaProcessos') or []
            if not lista:
                return None
            return lista[0]

        raise TjpaEnricherError(
            f'{len(tentados)} proxies tentados sem sucesso'
            + (f' (último: {last_erro})' if last_erro else ''))

    # ---------- Parsing ----------

    def _extrair_dados(self, proc: dict) -> dict:
        """Normaliza pro contrato compartilhado com drainer:normalize_dados."""
        out: dict = {}

        classe = _codigo_nome_para_nome_codigo(proc.get('classe') or '')
        if classe:
            out['classe'] = classe

        assunto = _codigo_nome_para_nome_codigo(proc.get('assunto') or '')
        if assunto:
            out['assunto'] = assunto

        # orgao_julgador = comarca + vara (drainer guarda como nome único).
        comarca = (proc.get('comarca') or '').strip()
        vara = (proc.get('vara') or '').strip()
        # 'Não Informado' é sentinela do TJPA — descarta.
        comarca = '' if comarca.lower() == 'não informado' else comarca
        vara = '' if vara.lower() == 'não informado' else vara
        orgao = ' — '.join(x for x in (comarca, vara) if x)
        if orgao:
            out['orgao_julgador'] = orgao
        if vara:
            out['juizo'] = vara

        data_autuacao = _ms_para_br(proc.get('dataDistribuicao')) or \
            (proc.get('dataDistribuicaoFormatada') or '').strip()
        if data_autuacao:
            out['data_autuacao'] = data_autuacao

        # valorCausaFormatado já vem 'R$ X.XXX,XX' (parse_valor_brl espera isso).
        valor = (proc.get('valorCausaFormatado') or '').strip()
        if valor:
            out['valor_causa'] = valor

        segredo = (proc.get('segredoJustica') or '').strip().lower()
        if segredo in ('sim', 'não', 'nao'):
            out['segredo_justica'] = segredo == 'sim'

        return out

    def _extrair_partes(self, partes_raw: list[dict]) -> dict[str, list[dict]]:
        """Agrupa por polo (A/P/T → ativo/passivo/outros) preservando a ordem
        e, DENTRO de cada polo, anexa advogados/representantes ao principal
        anterior como `representantes` — formato esperado pelo drainer (FK
        `representa`). Um representante sem principal anterior no mesmo polo
        (ex.: advogado listado antes da parte) entra como entrada solta.
        """
        polos: dict[str, list[dict]] = {'ativo': [], 'passivo': [], 'outros': []}

        # Bucketiza por polo preservando a ordem original (o agrupamento de
        # representantes é por-polo: a lista flat intercala polos).
        por_polo: dict[str, list[dict]] = {'ativo': [], 'passivo': [], 'outros': []}
        for item in partes_raw:
            polo = self._polo_de(item.get('polo'))
            por_polo[polo].append(item)

        for polo, itens in por_polo.items():
            principal_atual: Optional[dict] = None
            for item in itens:
                pessoa = self._parse_pessoa(item)
                if not pessoa.get('nome'):
                    continue
                if self._eh_representante(pessoa):
                    if principal_atual is not None:
                        principal_atual['representantes'].append(pessoa)
                    else:
                        pessoa['representantes'] = []
                        polos[polo].append(pessoa)
                else:
                    pessoa['representantes'] = []
                    polos[polo].append(pessoa)
                    principal_atual = pessoa
        return polos

    @staticmethod
    def _polo_de(polo: Optional[str]) -> str:
        p = (polo or '').strip().upper()
        if p == 'A':
            return 'ativo'
        if p == 'P':
            return 'passivo'
        return 'outros'

    @staticmethod
    def _eh_representante(pessoa: dict) -> bool:
        if pessoa.get('tipo') == 'advogado':
            return True
        papel = (pessoa.get('papel') or '').strip().upper()
        return papel in _PAPEIS_REPRESENTANTE

    def _parse_pessoa(self, item: dict) -> dict:
        nome = (item.get('nome') or '').strip()
        papel = (item.get('tipo') or '').strip().upper()
        cpfcnpj = item.get('cpfcnpj') or ''
        tppessoa = (item.get('tppessoa') or '').strip().upper()

        documento, tipo_doc = parse_documento(cpfcnpj)
        oab = parse_oab(cpfcnpj)

        tipo = classificar_tipo_parte(documento, tipo_doc, oab, papel)
        # cpfcnpj vem null na consulta pública — classificar cai em 'desconhecido'.
        # `tppessoa` (F/J) preenche pf/pj sem precisar do doc.
        if tipo == 'desconhecido':
            if tppessoa == 'J':
                tipo = 'pj'
            elif tppessoa == 'F':
                tipo = 'pf'

        return {
            'nome': nome[:255],
            'documento': (documento or '')[:20],
            'tipo_documento': tipo_doc or '',
            'oab': (oab or '')[:20],
            'papel': papel[:120],
            'tipo': tipo,
        }
