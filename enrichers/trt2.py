"""Enricher TRT2 via PJe consulta pública (SPA Angular + REST API + captcha de imagem).

O TRT2 roda uma versão do PJe consulta pública que é SPA Angular (não JSF/Seam),
servida por uma API REST em `pje.trt2.jus.br/pje-consulta-api/api`. Diferente do
PJe clássico (TRF1/TJMG), não há form JSF nem ViewState — é JSON direto. E
diferente do TJMT (também SPA), o detalhe do processo é protegido por **captcha
de imagem-texto** (JPEG 300×90, 6 chars `[a-z0-9]`).

### Fluxo

1. `GET /processos/dadosbasicos/{cnj}` com header `X-Grau-Instancia: 1|2`
   → `[{id, numero, classe, codigoOrgaoJulgador, segredoJustica, ...}]`
   **Sem captcha.** Devolve o `id` numérico interno do processo.
2. `GET /processos/{id}` → se exigir captcha, devolve `{tokenDesafio, imagem}`
   (JPEG base64) em vez do detalhe. Se não exigir, devolve o detalhe direto.
3. `solve_image(imagem)` via CapSolver → string de 6 chars.
4. `GET /processos/{id}?tokenDesafio={t}&resposta={r}` → detalhe real com
   partes, movimentos, assuntos, valor da causa.
5. Resposta errada devolve `{mensagem, tokenDesafio, imagem}` (novo desafio) —
   o loop re-tenta com o novo token até `MAX_TENTATIVAS_CAPTCHA`.

### Captcha: imagem-texto, NÃO hCaptcha

O captcha do TRT2 é OCR trivial (JPEG com 6 letras/números distorcidos). Não é
hCaptcha/reCAPTCHA/Turnstile — não precisa do farm de celulares do Hermes. O
`enrichers/captcha.py::solve_image` (CapSolver `ImageToTextTask`) resolve direto.

O CapSolver às vezes devolve 5 chars (o captcha exige 6) — a confiança baixa
indica falha de OCR. Tentamos até `MAX_TENTATIVAS_CAPTCHA=3` vezes com novos
desafios antes de desistir com `erro` (regra nº 2: teto é alerta, não corte
mudo).

### Grau de instância

O header `X-Grau-Instancia` é **obrigatório** (sem ele: `ARQ-012 Grau da
instância não definido`). O grau não vem nos metadados do Voyager — inferimos
do CNJ: foro de origem `0000` → 2º grau (originária/apelação), senão → 1º grau.
Se o grau inferido não achar, tentamos o outro (mesmo fallback do
`BasePjeEnricher`).

### Documentos

O `login` da parte principal é o CPF/CNPJ **sem máscara** (11 ou 14 dígitos).
Os `representantes` (advogados) têm `documento` já formatado (`325.188.498-07`)
e `tipoDocumento` (`CPF`/`CNPJ`). A API **não publica OAB** — os advogados vêm
com CPF, sem inscrição OAB (mesmo comportamento do TJDFT). O `papel` vem em
`tipo` (`RECLAMANTE`/`RECLAMADO`/`ADVOGADO`/`PERITO`/...).

### Proxy

CloudFront + nginx, sem WAF/AWS challenge. Não bloqueou datacenter nos testes
(HTTP 200 direto). Pool-first com Cortex fallback — se surgir 403 em prod, o
`_next_proxy` escala pro residencial.
"""
from __future__ import annotations

import datetime as _dt
import logging
import re
from typing import Optional

import requests
from django.utils import timezone

from djen.proxies import ProxyScrapePool, cortex_proxy_url, sessao_rotativa
from tribunals.models import Process

from . import stream
from .captcha import CaptchaError, solve_image
from .parsers import classificar_tipo_parte

API_BASE = 'https://pje.trt2.jus.br/pje-consulta-api/api'

DEFAULT_HEADERS = {
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.8',
    'Origin': 'https://pje.trt2.jus.br',
    'Referer': 'https://pje.trt2.jus.br/consultaprocessual/',
    'User-Agent': ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                   'AppleWebKit/537.36 (KHTML, like Gecko) '
                   'Chrome/120.0.0.0 Safari/537.36'),
}

#: Mapeia polo do JSON → polo do drainer. `TERCEIROS` cai em `outros`.
_POLO_MAP = {
    'ATIVO': 'ativo',
    'PASSIVO': 'passivo',
    'TERCEIROS': 'outros',
}

_CNJ_DIGITS_RE = re.compile(r'\D')


class Trt2EnricherError(Exception):
    pass


def _so_digitos(raw: str) -> str:
    return _CNJ_DIGITS_RE.sub('', raw or '')


def _formatar_documento(numero: str, tipo: str | None = None) -> str:
    """Dígitos crus + tipo (opcional) → documento canônico.

    CPF (11) → 'XXX.XXX.XXX-XX'; CNPJ (14) → 'XX.XXX.XXX/XXXX-XX'. Sem tipo,
    infere pelo tamanho. Tamanho inesperado → dígitos crus (defensivo)."""
    d = _so_digitos(numero)
    if len(d) == 11:
        return f'{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}'
    if len(d) == 14:
        return f'{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}'
    return d


def _iso_para_br(iso: str) -> str:
    """'2024-02-29T11:58:33.853' → '29/02/2024' (formato do parse_data_br)."""
    if not iso:
        return ''
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', iso)
    return f'{m.group(3)}/{m.group(2)}/{m.group(1)}' if m else ''


def _valor_para_br(valor) -> str:
    """1267071.0 → 'R$ 1.267.071,00' (formato do parse_valor_brl)."""
    if valor is None:
        return ''
    try:
        s = f'{float(valor):,.2f}'
    except (TypeError, ValueError):
        return ''
    return 'R$ ' + s.replace(',', '#').replace('.', ',').replace('#', '.')


def _tem_captcha(json_resp: dict) -> bool:
    """True se a resposta é um desafio de captcha (tem `tokenDesafio` + `imagem`
    e NÃO tem os campos do detalhe real como `id`/`numero`/`poloAtivo`).

    Resposta errada também tem `mensagem` + novo `tokenDesafio` + `imagem` —
    conta como captcha (rotação)."""
    if not isinstance(json_resp, dict):
        return False
    tem_desafio = bool(json_resp.get('tokenDesafio')) and bool(json_resp.get('imagem'))
    tem_detalhe = 'poloAtivo' in json_resp or 'poloPassivo' in json_resp
    return tem_desafio and not tem_detalhe


class Trt2Enricher:
    BASE_URL = API_BASE
    TRIBUNAL_SIGLA = 'TRT2'
    LOG_NAME = 'voyager.enrichers.trt2'

    REQUEST_TIMEOUT = (10, 60)
    MAX_PROXY_ROTATIONS = 8
    #: Quantos captchas tentamos resolver antes de desistir. Cada tentativa
    #: gasta um solve no CapSolver (~$0,001-0,002) — o teto é baixo de propósito.
    #: O CapSolver às vezes devolve 5 chars (o captcha exige 6); re-tentar com
    #: novo desafio é mais barato que insistir no mesmo.
    MAX_TENTATIVAS_CAPTCHA = 3

    #: Pausa entre o GET do captcha e o POST da resposta. O servidor do TRT2
    #: não exige, mas evita rajada em falha de OCR (cada solve leva 1-5s).
    SLEEP_ENTRE_TENTATIVAS_S = 0.5

    def __init__(self, pool: Optional[ProxyScrapePool] = None, prefer_cortex: bool = False):
        self.session = sessao_rotativa()
        self.session.headers.update(DEFAULT_HEADERS)
        self.logger = logging.getLogger(self.LOG_NAME)
        self.pool = pool or ProxyScrapePool.singleton()
        self.prefer_cortex = prefer_cortex

    # ---------- API pública ----------

    def enriquecer(self, processo: Process, direct_apply: bool = False) -> dict:
        if processo.tribunal_id != self.TRIBUNAL_SIGLA:
            raise Trt2EnricherError(
                f'Tribunal {processo.tribunal_id} não suportado por {self.__class__.__name__}.'
            )

        base = {
            'process_id': processo.pk,
            'tribunal': processo.tribunal_id,
            'numero_cnj': processo.numero_cnj,
            'scraped_at': timezone.now().astimezone(_dt.timezone.utc).isoformat(),
        }

        try:
            detalhe = self._consultar(processo.numero_cnj)
        except Exception as exc:
            self.logger.exception('falha na consulta', extra={'cnj': processo.numero_cnj})
            self._emit(stream.build_erro_payload(**base, erro=f'busca: {exc}'), direct_apply)
            return {'cnj': processo.numero_cnj, 'status': 'erro', 'erro': str(exc)[:200]}

        if detalhe is None:
            self._emit(stream.build_nao_encontrado_payload(**base), direct_apply)
            return {'cnj': processo.numero_cnj, 'status': 'nao_encontrado'}

        try:
            dados = self._extrair_dados(detalhe)
            partes = self._extrair_partes(detalhe)
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

    # ---------- HTTP com rotação de proxy ----------

    def _next_proxy(self, exclude: set) -> Optional[str]:
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

    def _request_with_rotation(self, method: str, url: str, **kwargs) -> requests.Response:
        """Request com rotação em 403/429/erro de transporte. Limite:
        MAX_PROXY_ROTATIONS. Marca proxy bad em falha (acelera saída do pool)."""
        tentados: set = set()
        last_status = None
        cortex = cortex_proxy_url(self.pool)
        for tentativa in range(1, self.MAX_PROXY_ROTATIONS + 1):
            proxy = self._next_proxy(tentados)
            if not proxy:
                self.logger.warning('pool exausto sem proxy disponível',
                                    extra={'tentativa': tentativa, 'url': url[:120]})
                break
            if proxy != cortex:
                tentados.add(proxy)
            proxies = {'http': proxy, 'https': proxy}
            self.logger.info('trt2 request', extra={
                'method': method, 'url': url[:120], 'proxy': proxy,
                'tentativa': tentativa,
            })
            try:
                resp = self.session.request(
                    method, url, proxies=proxies, timeout=self.REQUEST_TIMEOUT, **kwargs,
                )
            except (requests.ConnectionError, requests.Timeout,
                    requests.exceptions.ChunkedEncodingError) as exc:
                self.logger.warning('proxy falhou (transport), rotacionando', extra={
                    'proxy': proxy, 'tentativa': tentativa, 'erro': str(exc)[:120],
                })
                if proxy != cortex:
                    self.pool.mark_bad(proxy)
                continue
            if resp.status_code in (403, 429):
                self.logger.warning('proxy bloqueado (403/429), rotacionando', extra={
                    'proxy': proxy, 'status': resp.status_code, 'tentativa': tentativa,
                })
                if proxy != cortex:
                    self.pool.mark_bad(proxy)
                last_status = resp.status_code
                continue
            resp.raise_for_status()
            return resp
        msg = f'{self.MAX_PROXY_ROTATIONS} proxies tentados sem sucesso'
        if last_status:
            msg += f' (último status {last_status})'
        raise requests.HTTPError(msg)

    def _get(self, url: str, **kwargs) -> requests.Response:
        return self._request_with_rotation('GET', url, **kwargs)

    # ---------- Orquestração do captcha ----------

    @staticmethod
    def _grau(cnj: str) -> str:
        """'2g' se foro de origem == '0000' (originária/apelação), senão '1g'.
        Mesma regra do `BasePjeEnricher._grau`."""
        digitos = _so_digitos(cnj)
        return '2g' if digitos[-4:] == '0000' else '1g'

    @staticmethod
    def _grau_header(grau: str) -> str:
        return '2' if grau == '2g' else '1'

    def _consultar(self, cnj_raw: str) -> Optional[dict]:
        """Fluxo completo: dadosbasicos → detalhe (com captcha se exigido).

        Retorna o JSON do detalhe completo, ou None se o processo não existe
        em nenhum grau. Levanta em erro de rede/captcha não resolvido.
        """
        digitos = _so_digitos(cnj_raw)
        if len(digitos) != 20:
            raise Trt2EnricherError(f'CNJ inválido: {cnj_raw!r}')

        palpite = self._grau(cnj_raw)
        detalhe = self._tentar_grau(digitos, palpite)
        if detalhe is None:
            outro = '1g' if palpite == '2g' else '2g'
            self.logger.info('grau fallback', extra={
                'cnj': cnj_raw, 'de': palpite, 'para': outro,
            })
            detalhe = self._tentar_grau(digitos, outro)
        return detalhe

    def _tentar_grau(self, cnj_digitos: str, grau: str) -> Optional[dict]:
        """Busca o processo num grau específico. None = não existe neste grau."""
        headers = dict(self.session.headers)
        headers['X-Grau-Instancia'] = self._grau_header(grau)

        # 1. dadosbasicos (sem captcha) → id
        resp = self._get(f'{API_BASE}/processos/dadosbasicos/{cnj_digitos}',
                         headers=headers)
        data = resp.json()
        if not data:
            return None
        # A API devolve lista; pegamos o 1º item (match por numero se houver).
        item = data[0]
        pid = item.get('id')
        if not pid:
            return None
        self.logger.info('trt2 dadosbasicos', extra={
            'cnj': cnj_digitos, 'grau': grau, 'id': pid,
            'classe': item.get('classe'),
        })

        # 2. detalhe (pode exigir captcha)
        return self._detalhe_com_captcha(pid, grau, headers)

    def _detalhe_com_captcha(self, pid: int, grau: str, headers: dict) -> dict:
        """GET /processos/{id} — resolve captcha se exigido, com retentativa.

        Resposta errada devolve novo `tokenDesafio` + `imagem` (rotação) —
        o loop pega o novo token e re-tenta. Teto: MAX_TENTATIVAS_CAPTCHA.
        Ao esgotar: ERRO com o número (regra nº 2), nunca `return` discreto.
        """
        import time as _time
        url = f'{API_BASE}/processos/{pid}'
        tentativas = 0

        while tentativas < self.MAX_TENTATIVAS_CAPTCHA:
            tentativas += 1
            resp = self._get(url, headers=headers)
            json_resp = resp.json()

            if not _tem_captcha(json_resp):
                # Sem captcha — detalhe completo (ou resposta de erro não-captcha).
                if json_resp.get('mensagemErro'):
                    raise Trt2EnricherError(
                        f'tribunal erro: {json_resp["mensagemErro"]}')
                return json_resp

            # Captcha pendente: resolver e re-enviar.
            token = json_resp['tokenDesafio']
            imagem_b64 = json_resp['imagem']
            self.logger.info('trt2 captcha pendente', extra={
                'pid': pid, 'tentativa': tentativas,
                'teto': self.MAX_TENTATIVAS_CAPTCHA,
            })
            try:
                resposta = solve_image(imagem_b64, module='common', case_sensitive=False)
            except CaptchaError as exc:
                self.logger.warning('captcha solve falhou', extra={
                    'pid': pid, 'tentativa': tentativas, 'erro': str(exc)[:120],
                })
                if tentativas >= self.MAX_TENTATIVAS_CAPTCHA:
                    raise Trt2EnricherError(
                        f'captcha nao resolvido em {tentativas} tentativas: {exc}')
                _time.sleep(self.SLEEP_ENTRE_TENTATIVAS_S)
                continue

            # Submeter resposta.
            resp2 = self._get(url, headers=headers,
                              params={'tokenDesafio': token, 'resposta': resposta})
            json_resp2 = resp2.json()

            if not _tem_captcha(json_resp2):
                if json_resp2.get('mensagemErro'):
                    raise Trt2EnricherError(
                        f'tribunal erro: {json_resp2["mensagemErro"]}')
                self.logger.info('trt2 captcha resolvido', extra={
                    'pid': pid, 'tentativa': tentativas, 'resposta': resposta,
                })
                return json_resp2

            # Resposta errada — novo desafio disponível em json_resp2.
            self.logger.warning('captcha resposta incorreta — rotacionando', extra={
                'pid': pid, 'tentativa': tentativas,
                'resposta': resposta, 'mensagem': json_resp2.get('mensagem'),
            })
            if tentativas >= self.MAX_TENTATIVAS_CAPTCHA:
                raise Trt2EnricherError(
                    f'captcha: {tentativas} respostas incorretas em '
                    f'{self.MAX_TENTATIVAS_CAPTCHA} tentativas')
            _time.sleep(self.SLEEP_ENTRE_TENTATIVAS_S)

        # Inalcançável: o while só sai por return ou raise acima.
        raise Trt2EnricherError('captcha: loop exaurido sem resposta')

    # ---------- Parsing ----------

    def _extrair_dados(self, d: dict) -> dict:
        out: dict = {}
        classe = (d.get('classe') or '').strip()
        if classe:
            out['classe'] = classe
        orgao = (d.get('orgaoJulgador') or '').strip()
        if orgao:
            out['orgao_julgador'] = orgao
        autuacao = _iso_para_br(d.get('autuadoEm') or d.get('distribuidoEm') or '')
        if autuacao:
            out['data_autuacao'] = autuacao
        valor = _valor_para_br(d.get('valorDaCausa'))
        if valor:
            out['valor_causa'] = valor
        if d.get('segredoJustica'):
            out['segredo_justica'] = True
        # Assuntos: lista de {codigo, descricao, principal}. Pegamos o principal.
        assuntos = d.get('assuntos') or []
        descricoes = [a.get('descricao', '').strip() for a in assuntos
                      if (a.get('descricao') or '').strip()]
        if descricoes:
            out['assunto'] = '; '.join(descricoes)
        return out

    def _extrair_partes(self, d: dict) -> dict[str, list[dict]]:
        polos: dict[str, list[dict]] = {'ativo': [], 'passivo': [], 'outros': []}
        for json_polo_key, drainer_polo in (
            ('poloAtivo', 'ativo'),
            ('poloPassivo', 'passivo'),
            ('poloOutros', 'outros'),
        ):
            for parte in (d.get(json_polo_key) or []):
                principal = self._parse_principal(parte)
                if not principal.get('nome'):
                    continue
                principal['representantes'] = [
                    self._parse_representante(r)
                    for r in (parte.get('representantes') or [])
                    if (r.get('nome') or '').strip()
                ]
                polos[drainer_polo].append(principal)
        return polos

    def _parse_principal(self, parte: dict) -> dict:
        nome = (parte.get('nome') or '').strip()
        # `login` é CPF/CNPJ sem máscara (11 ou 14 dígitos).
        login = parte.get('login') or ''
        documento = ''
        tipo_doc = ''
        if login:
            d = _so_digitos(login)
            if len(d) == 11:
                documento = _formatar_documento(d)
                tipo_doc = 'CPF'
            elif len(d) == 14:
                documento = _formatar_documento(d)
                tipo_doc = 'CNPJ'
        papel = (parte.get('tipo') or '').strip().upper()
        # `polo` no JSON pode ser ATIVO/PASSIVO/TERCEIROS — mas já mapeamos
        # pelo loop externo. Mantemos o `tipo` como papel processual.
        tipo = classificar_tipo_parte(documento, tipo_doc, '', papel)
        return {
            'nome': nome[:255],
            'documento': documento[:20],
            'tipo_documento': tipo_doc,
            'oab': '',
            'papel': papel[:120],
            'tipo': tipo,
        }

    def _parse_representante(self, r: dict) -> dict:
        nome = (r.get('nome') or '').strip()
        doc_raw = r.get('documento') or ''
        tipo_doc = (r.get('tipoDocumento') or '').strip().upper()
        documento = _formatar_documento(doc_raw) if doc_raw else ''
        # Se `documento` veio vazio, tentar `login` (CPF/CNPJ sem máscara).
        if not documento and r.get('login'):
            documento = _formatar_documento(r.get('login'))
        # OAB não vem na API do TRT2 (advogados têm CPF, sem inscrição OAB).
        papel = (r.get('tipo') or 'ADVOGADO').strip().upper()
        # Confirmar pelo campo `papeis` se há algo mais específico.
        papeis = r.get('papeis') or []
        if papeis:
            ident = (papeis[0].get('identificador') or '').lower()
            if ident:
                papel = papeis[0].get('nome', papel).upper()
        tipo = classificar_tipo_parte(documento, tipo_doc, '', papel)
        return {
            'nome': nome[:255],
            'documento': documento[:20],
            'tipo_documento': tipo_doc,
            'oab': '',
            'papel': papel[:120],
            'tipo': tipo,
        }