"""Enricher TJMT via PJe consulta pública (nova SPA Angular + REST API).

Endpoint de busca (uma chamada já traz partes + advogados):
  GET https://hellsgate.tjmt.jus.br/consultaprocessual/ProcessosJudiciais/v2
      ?Skip=0&Take=10&numeroUnico=<20 dígitos do CNJ, sem máscara>

O TJMT migrou do PJe clássico (JSF/Seam) pra uma SPA Angular que consome
uma API REST. Por isso NÃO herda BasePjeEnricher — não há HTML form pra
parsear, é um pipeline JSON. Diferente do TJDFT, uma única chamada de busca
já devolve partes e advogados embutidos (não há rota /poloAtivo separada).

### X-Fingerprint (anti-bot)

Toda request precisa do header `X-Fingerprint`, gerado FRESCO por chamada
(o servidor valida uma janela de timestamp). O algoritmo foi extraído do
bundle `main-*.js` da SPA (função `Bd()`):

```js
function Bd(){
  let r=new Date().getTime(),                       // timestamp ms
      t=navigator.userAgent,
      e=`${window.screen.width}x${window.screen.height}`,
      i=navigator.language,
      n=`${t}-${e}-${i}-${r}`,                       // mensagem do HMAC
      o=Wn.HmacSHA256(n, zi.fingerPrint).toString(Wn.enc.Base64);
  return JSON.stringify({signature:o,timestamp:r,userAgent:t,
                         screenResolution:e,language:i});
}
```

Ou seja: `mensagem = "{userAgent}-{WxH}-{language}-{timestamp_ms}"`,
`signature = base64(HMAC_SHA256(mensagem, CHAVE))`, e o header é o JSON
com `{signature, timestamp, userAgent, screenResolution, language}`. A
chave (`environment.fingerPrint`, hardcoded no bundle `chunk-*.js`) é
`A_mesma_mao_que_aplaude_e_a_que_vaia!`. Validado live 2026-06-29 (HTTP
200 via Cortex).

### Headers obrigatórios

`Origin`/`Referer` apontando pra SPA oficial + `X-Fingerprint`. Sem auth,
sem cookie, sem captcha.

### Documentos

CPF/CNPJ vêm SEM máscara, como dígitos crus em
`documentos:[{descricaoTipo:"CPF"/"CNPJ"/"OAB", numero:"..."}]`. Formatamos
pra o padrão canônico (igual TRF1/TJMG/TJDFT) pra casar no dedupe do drainer.

Limitação conhecida: o documento OAB do advogado vem SEM a UF (só o número,
ex.: `"20688"`). Como é um sistema do TJMT, assumimos UF=MT (`OAB_UF`) —
best-effort; advogados de outra seccional ficam com UF possivelmente errada.
A API de busca também não expõe `assunto` (só `classe`).
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import hmac
import json
import logging
import re
from typing import Optional

import requests
from django.utils import timezone

from djen.proxies import ProxyScrapePool, cortex_proxy_url
from tribunals.models import Process

from . import stream
from .parsers import classificar_tipo_parte, limpar_nome

# Chave HMAC do X-Fingerprint (environment.fingerPrint no bundle Angular).
FINGERPRINT_KEY = 'A_mesma_mao_que_aplaude_e_a_que_vaia!'

# Valores estáveis que entram na assinatura. O servidor recomputa o HMAC a
# partir dos campos do JSON, então só precisam ser consistentes entre si.
# Mantemos o `userAgent` do fingerprint == header User-Agent.
_FP_UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
          '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
_FP_SCREEN = '1920x1080'
_FP_LANG = 'pt-BR'

DEFAULT_HEADERS = {
    'User-Agent': _FP_UA,
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.8',
    # Sem Origin/Referer da SPA oficial o gateway rejeita (403).
    'Origin': 'https://consultaprocessual.tjmt.jus.br',
    'Referer': 'https://consultaprocessual.tjmt.jus.br/',
}

_CNJ_DIGITS_RE = re.compile(r'\D')


class TjmtEnricherError(Exception):
    pass


def gerar_fingerprint(ts_ms: Optional[int] = None) -> str:
    """Gera o valor do header `X-Fingerprint` (JSON string).

    Reproduz a função `Bd()` do bundle Angular do TJMT:
      mensagem   = "{userAgent}-{screenResolution}-{language}-{timestamp_ms}"
      signature  = base64(HMAC_SHA256(mensagem, FINGERPRINT_KEY))
      header     = json({signature, timestamp, userAgent, screenResolution, language})

    `ts_ms` é injetável só pra teste; em produção usa o relógio atual (o
    servidor valida uma janela de timestamp, por isso é gerado por request).
    """
    if ts_ms is None:
        ts_ms = int(timezone.now().timestamp() * 1000)
    msg = f'{_FP_UA}-{_FP_SCREEN}-{_FP_LANG}-{ts_ms}'
    signature = base64.b64encode(
        hmac.new(FINGERPRINT_KEY.encode('utf-8'),
                 msg.encode('utf-8'), hashlib.sha256).digest()
    ).decode('ascii')
    # Sem espaços (separators) — espelha JSON.stringify do JS.
    return json.dumps({
        'signature': signature,
        'timestamp': ts_ms,
        'userAgent': _FP_UA,
        'screenResolution': _FP_SCREEN,
        'language': _FP_LANG,
    }, ensure_ascii=False, separators=(',', ':'))


def _so_digitos(raw: str) -> str:
    return _CNJ_DIGITS_RE.sub('', raw or '')


def _formatar_documento(numero: str, tipo: str) -> str:
    """Dígitos crus + tipo → documento canônico (igual aos outros enrichers).

    CPF (11) → 'XXX.XXX.XXX-XX'; CNPJ (14) → 'XX.XXX.XXX/XXXX-XX'. Se o
    tamanho não bate, devolve os dígitos crus (defensivo)."""
    d = _so_digitos(numero)
    if tipo == 'CPF' and len(d) == 11:
        return f'{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}'
    if tipo == 'CNPJ' and len(d) == 14:
        return f'{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}'
    return d


def _iso_para_br(iso: str) -> str:
    """'2026-04-14T10:18:02.841' → '14/04/2026' (formato do parse_data_br)."""
    if not iso:
        return ''
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', iso)
    return f'{m.group(3)}/{m.group(2)}/{m.group(1)}' if m else ''


def _valor_para_br(valor) -> str:
    """354.67 → 'R$ 354,67'; 177781.42 → 'R$ 177.781,42' (parse_valor_brl)."""
    if valor is None:
        return ''
    try:
        s = f'{float(valor):,.2f}'  # '177,781.42'
    except (TypeError, ValueError):
        return ''
    # Troca separadores US → BR.
    return 'R$ ' + s.replace(',', '#').replace('.', ',').replace('#', '.')


class TjmtEnricher:
    BASE_URL = 'https://hellsgate.tjmt.jus.br'
    SEARCH_PATH = '/consultaprocessual/ProcessosJudiciais/v2'
    TRIBUNAL_SIGLA = 'TJMT'
    LOG_NAME = 'voyager.enrichers.tjmt'

    # OAB vem sem UF na API — assumimos a seccional do tribunal (MT).
    OAB_UF = 'MT'

    REQUEST_TIMEOUT = (10, 60)
    MAX_PROXY_ROTATIONS = 8

    # Mapeia tipoParticipacaoProcessual → polo do drainer.
    _POLO_MAP = {'ativo': 'ativo', 'passivo': 'passivo'}

    def __init__(self, pool: Optional[ProxyScrapePool] = None, prefer_cortex: bool = False):
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.logger = logging.getLogger(self.LOG_NAME)
        # Pool ProxyScrape pra paralelizar IPs; Cortex (residencial) como
        # fallback / preferência (no dev o pool não tem chave → prefer_cortex).
        self.pool = pool or ProxyScrapePool.singleton()
        self.prefer_cortex = prefer_cortex

    # ---------- API pública ----------

    def enriquecer(self, processo: Process, direct_apply: bool = False) -> dict:
        if processo.tribunal_id != self.TRIBUNAL_SIGLA:
            raise TjmtEnricherError(
                f'Tribunal {processo.tribunal_id} não suportado por {self.__class__.__name__}.'
            )

        base = {
            'process_id': processo.pk,
            'tribunal': processo.tribunal_id,
            'numero_cnj': processo.numero_cnj,
            'scraped_at': timezone.now().astimezone(_dt.timezone.utc).isoformat(),
        }

        try:
            item = self._buscar(processo.numero_cnj)
        except Exception as exc:
            self.logger.exception('falha na busca', extra={'cnj': processo.numero_cnj})
            self._emit(stream.build_erro_payload(**base, erro=f'busca: {exc}'), direct_apply)
            return {'cnj': processo.numero_cnj, 'status': 'erro', 'erro': str(exc)[:200]}

        if item is None:
            self._emit(stream.build_nao_encontrado_payload(**base), direct_apply)
            return {'cnj': processo.numero_cnj, 'status': 'nao_encontrado'}

        try:
            dados = self._extrair_dados(item)
            partes = self._extrair_partes(item)
        except Exception as exc:
            self.logger.exception('falha ao parsear', extra={'cnj': processo.numero_cnj})
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
        """Próximo IP. Default: pool ProxyScrape primeiro, Cortex como
        fallback. prefer_cortex=True (clique manual / dev) inverte a ordem."""
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

    def _buscar(self, cnj_raw: str) -> Optional[dict]:
        """Chama o endpoint v2 e devolve o `item` cujo numeroUnico bate com o
        CNJ (ou None se não houver resultado). Rotaciona proxy em bloqueio/erro
        de transporte. O X-Fingerprint é gerado FRESCO a cada tentativa (o
        servidor valida janela de timestamp)."""
        numero_unico = _so_digitos(cnj_raw)
        if len(numero_unico) != 20:
            raise TjmtEnricherError(f'CNJ inválido: {cnj_raw!r}')

        url = f'{self.BASE_URL}{self.SEARCH_PATH}'
        params = {'Skip': 0, 'Take': 10, 'numeroUnico': numero_unico}

        tentados: set = set()
        last_erro: Optional[str] = None
        cortex = cortex_proxy_url(self.pool)
        for tentativa in range(1, self.MAX_PROXY_ROTATIONS + 1):
            proxy = self._next_proxy(tentados)
            if not proxy:
                self.logger.warning('pool exausto sem proxy disponível',
                                    extra={'cnj': numero_unico, 'tentativa': tentativa})
                break
            # Cortex rotaciona IP por request — não excluir (reusável). IP do
            # pool é fixo — excluir pra não repetir.
            if proxy != cortex:
                tentados.add(proxy)
            proxies = {'http': proxy, 'https': proxy}
            headers = {'X-Fingerprint': gerar_fingerprint()}
            try:
                resp = self.session.get(url, params=params, headers=headers,
                                        proxies=proxies, timeout=self.REQUEST_TIMEOUT)
            except (requests.ConnectionError, requests.Timeout,
                    requests.exceptions.ChunkedEncodingError) as exc:
                last_erro = f'transporte: {str(exc)[:120]}'
                if proxy != cortex:
                    self.pool.mark_bad(proxy)
                continue

            if resp.status_code in (400, 401, 403, 429):
                # 400/401/403 = X-Fingerprint/Origin rejeitado; 429 = rate
                # limit. Outro IP + fingerprint novo pode passar.
                last_erro = f'bloqueado {resp.status_code}'
                if proxy != cortex:
                    self.pool.mark_bad(proxy)
                continue
            if resp.status_code >= 500:
                last_erro = f'servidor {resp.status_code}'
                continue
            resp.raise_for_status()

            data = resp.json()
            return self._match_item(data, numero_unico)

        raise TjmtEnricherError(
            f'{len(tentados)} proxies tentados sem sucesso'
            + (f' (último: {last_erro})' if last_erro else ''))

    @staticmethod
    def _match_item(data: dict, numero_unico: str) -> Optional[dict]:
        """Acha o item cujo numeroUnico bate (defensivo contra match parcial)."""
        for item in (data.get('itens') or []):
            if _so_digitos(item.get('numeroUnico') or '') == numero_unico:
                return item
        return None

    # ---------- Parsing ----------

    def _extrair_dados(self, item: dict) -> dict:
        out: dict = {}
        classe = item.get('classe') or {}
        nome = (classe.get('nome') or '').strip()
        codigo = classe.get('codigo')
        if nome:
            # "NOME (codigo)" → drainer (_split_nome_codigo) separa nome/código.
            out['classe'] = f'{nome} ({codigo})' if codigo else nome

        orgao = (item.get('orgaoJulgador')
                 or item.get('orgaoJulgadorColegiado') or '').strip()
        if orgao:
            out['orgao_julgador'] = orgao

        autuacao = _iso_para_br(item.get('dataHoraInicio') or '')
        if autuacao:
            out['data_autuacao'] = autuacao

        valor = _valor_para_br(item.get('valorCausa'))
        if valor:
            out['valor_causa'] = valor

        if item.get('segredo'):
            out['segredo_justica'] = True
        # `assunto` não vem na API de busca do TJMT — fica vazio.
        return out

    def _polo_de(self, parte: dict) -> str:
        chave = (parte.get('tipoParticipacaoProcessual') or '').strip().lower()
        return self._POLO_MAP.get(chave, 'outros')

    def _extrair_partes(self, item: dict) -> dict[str, list[dict]]:
        """Cada `parte` é um principal; seus `advogados` viram `representantes`
        (papel ADVOGADO) — formato que o drainer consome (FK `representa`)."""
        polos: dict[str, list[dict]] = {'ativo': [], 'passivo': [], 'outros': []}
        for parte in (item.get('partes') or []):
            principal = self._parse_principal(parte)
            if not principal.get('nome'):
                continue
            principal['representantes'] = [
                self._parse_advogado(adv)
                for adv in (parte.get('advogados') or [])
                if (adv.get('nome') or '').strip()
            ]
            polos[self._polo_de(parte)].append(principal)
        return polos

    def _parse_principal(self, parte: dict) -> dict:
        documento, tipo_doc = self._doc_principal(parte.get('documentos') or [])
        papel = ((parte.get('tipo') or {}).get('descricao') or '').strip().upper()
        nome = (parte.get('nome') or '').strip() or limpar_nome(parte.get('nome') or '')
        return {
            'nome': nome[:255],
            'documento': documento[:20],
            'tipo_documento': tipo_doc,
            'oab': '',
            'papel': papel[:120],
            'tipo': classificar_tipo_parte(documento, tipo_doc, '', papel),
        }

    def _parse_advogado(self, adv: dict) -> dict:
        documentos = adv.get('documentos') or []
        documento, tipo_doc = self._doc_principal(documentos)
        oab = self._oab_de(documentos)
        nome = (adv.get('nome') or '').strip()
        return {
            'nome': nome[:255],
            'documento': documento[:20],
            'tipo_documento': tipo_doc,
            'oab': oab[:20],
            'papel': 'ADVOGADO',
            'tipo': 'advogado',
        }

    @staticmethod
    def _doc_principal(documentos: list[dict]) -> tuple[str, str]:
        """Primeiro CPF/CNPJ da lista → (documento_formatado, tipo)."""
        for doc in documentos:
            tipo = (doc.get('descricaoTipo') or '').strip().upper()
            if tipo in ('CPF', 'CNPJ'):
                return _formatar_documento(doc.get('numero') or '', tipo), tipo
        return '', ''

    def _oab_de(self, documentos: list[dict]) -> str:
        """Documento tipo OAB → 'MT<numero>' (UF assumida; ver docstring do
        módulo). Preserva sufixo alfanumérico (ex.: '30885/O' → 'MT30885O')."""
        for doc in documentos:
            if (doc.get('descricaoTipo') or '').strip().upper() == 'OAB':
                num = re.sub(r'[^0-9A-Za-z]', '', doc.get('numero') or '')
                if num:
                    return f'{self.OAB_UF}{num}'
        return ''
