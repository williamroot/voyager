"""Enricher do TJRS via consulta processual pública (SPA Angular + REST).

Porta: `https://consulta.tjrs.jus.br/consulta-processual/` (a SPA que o portal
do TJRS embute em `www.tjrs.jus.br/busca/?tb=proc`). Ela fala com

    https://consulta-processual-service.tjrs.jus.br/api/consulta-service/v1

Recon de 15/10/2026, dos hosts do Voyager (workers e web) e do Mac:

* A consulta pública do **eproc** (`eproc1g/eproc2g.tjrs.jus.br/eproc/
  externo_controlador.php?acao=processo_consulta_publica`) responde **403
  "Forbidden"** (9 bytes, `text/plain`, `Server: cloudflare`) para qualquer
  IP/UA testado — é regra do Cloudflare para essa `acao` específica; a tela de
  login do mesmo host responde 200. O próprio eproc não linka mais essa
  consulta: aponta para a busca do portal, que é esta SPA. Ver
  `.ia/ENRICHMENT.md` §TJRS.
* Esta API serve eproc 1º grau, eproc 2º grau (`…8.21.7000`) e Turmas
  Recursais (`…8.21.9000`). Processo de prefixo `8` (execução penal, 2024+)
  NÃO está nela — 16 de 16 sondas deram 404 com controle 200 (ver
  `FORA_DA_FONTE_FAIXAS`).

### Credencial (ALTCHA)

Sem login e sem captcha visual: `GET /public/auth/token` dá um desafio de
prova de trabalho, `POST /public/auth/submit` troca a solução por um usuário
anônimo (`ChaAnon_<n>`), e a credencial `Basic` deriva dele
(`fingerprints.credencial_tjrs`). **Cada credencial vale 7 consultas** — a 8ª
devolve 429 "Limite de chamadas atingido". Novas credenciais não tiveram teto
observado (60 em 40 s do mesmo IP) e não são presas ao IP (a mesma credencial
respondeu 200 por 4 proxies do pool e pelo Cortex).

### O que a ficha pública traz (e o que não traz)

`/consultaProcesso` devolve classe (só o NOME, sem código TPU), assunto (a
hierarquia inteira, da folha para a raiz, separada por vírgula), comarca,
órgão julgador, relator no 2º grau, data de distribuição, segredo, partes com
o PAPEL (EXEQUENTE, EXECUTADO, REQUERENTE...), os 5 últimos eventos e os
processos vinculados (o precatório do 2º grau, `…8.21.7000`, aparece aqui).

NÃO traz: valor da causa, documento (CPF/CNPJ) das partes e advogados — no
eproc o campo `advogados` vem `[{nome: null, oab: null}]`. A busca por nome
devolve o CPF/CNPJ MASCARADO (`547.5**.***-**`), mas custa uma consulta por
parte e não entra na ficha. Abster > chutar: esses campos ficam vazios.
"""
from __future__ import annotations

import datetime as _dt
import logging
import re
import time

import requests
from django.utils import timezone

from djen.proxies import ProxyScrapePool, cortex_proxy_url, sessao_rotativa
from tribunals.models import Process
from tribunals.services.oab import canonizar_oab

from . import stream
from .faixas import faixa_fora_da_fonte
from .fingerprints import (
    altcha_formulario,
    comarca_tjrs,
    credencial_tjrs,
    resolver_altcha,
)
from .parsers import classificar_tipo_parte

_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36')

DEFAULT_HEADERS = {
    'User-Agent': _UA,
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'pt-BR,pt;q=0.9',
    'Origin': 'https://consulta.tjrs.jus.br',
    'Referer': 'https://consulta.tjrs.jus.br/',
}


class TjrsEnricherError(Exception):
    pass


class TjrsCredencialError(TjrsEnricherError):
    """A troca ALTCHA → credencial falhou (desafio, submit ou resposta)."""


def _so_digitos(valor: str) -> str:
    return re.sub(r'\D', '', valor or '')


def assunto_folha(assunto_cnj: str) -> str:
    """`"Licença-Prêmio, Licenças / Afastamentos, Servidor Público Civil, DIREITO
    ADMINISTRATIVO…"` → `"Licença-Prêmio"`.

    A API publica a hierarquia da FOLHA para a RAIZ separada por `, `. O drainer
    guarda o nome da folha (`split_assunto_folha`); aqui o separador é outro,
    então a folha é o primeiro segmento. Se o texto não tem vírgula, ele já é a
    folha.
    """
    texto = (assunto_cnj or '').strip()
    if not texto:
        return ''
    return texto.split(', ')[0].strip()


def _data_br(texto: str) -> str:
    """`"12/08/2026 10:37:13"` → `"12/08/2026"` (formato do `parse_data_br`)."""
    m = re.match(r'\s*(\d{2}/\d{2}/\d{4})', texto or '')
    return m.group(1) if m else ''


class TjrsEnricher:
    TRIBUNAL_SIGLA = 'TJRS'
    LOG_NAME = 'voyager.enrichers.tjrs'

    #: Sonda do vigia (`tick_vigia_fontes`): a SPA é a porta pública; responde
    #: 200 com ~88 KB quando está de pé.
    LIST_URL = 'https://consulta.tjrs.jus.br/consulta-processual/'
    BASE_URL = 'https://consulta-processual-service.tjrs.jus.br'
    API_URL = f'{BASE_URL}/api/consulta-service/v1'
    AUTH_URL = f'{BASE_URL}/api/consulta-service/public/auth'

    #: Consultas por credencial, medido (a 8ª dá 429). Renovamos ANTES de
    #: esgotar, como a SPA faz, para não gastar uma ida e volta com o 429.
    CONSULTAS_POR_CREDENCIAL = 7

    REQUEST_TIMEOUT = (10, 60)
    MAX_PROXY_ROTATIONS = 8

    #: Credenciais recusadas (401) seguidas antes de desistir da chamada. Em
    #: 16/10/2026 00:00 UTC a API passou a devolver 401 vazio para TODA
    #: credencial — inclusive para a própria SPA num Chromium (a credencial que
    #: ela calculou é byte a byte a nossa). Sem teto, cada job queimaria 8
    #: provas de trabalho numa fonte fora; com ele, vira `erro` re-tentável.
    MAX_CREDENCIAIS_RECUSADAS = 3

    #: Prefixo `8` de 2024 em diante: 16/16 sondas 404 com controle 200
    #: (15/10/2026). É a execução penal, que não está nesta API.
    FORA_DA_FONTE_FAIXAS = (('8', 2024, 'tjrs_prefixo_8_fora_da_consulta'),)

    _TIPOS_ATIVO = frozenset({
        'AUTOR', 'AUTORA', 'EXEQUENTE', 'REQUERENTE', 'EMBARGANTE', 'APELANTE',
        'IMPETRANTE', 'AGRAVANTE', 'RECORRENTE', 'RECLAMANTE', 'CREDOR',
        'SUSCITANTE', 'DEPRECANTE', 'POLO ATIVO',
    })
    _TIPOS_PASSIVO = frozenset({
        'RÉU', 'REU', 'RÉ', 'EXECUTADO', 'EXECUTADA', 'REQUERIDO', 'REQUERIDA',
        'EMBARGADO', 'APELADO', 'IMPETRADO', 'AGRAVADO', 'RECORRIDO',
        'RECLAMADO', 'DEVEDOR', 'SUSCITADO', 'DEPRECADO', 'POLO PASSIVO',
    })

    def __init__(self, pool: ProxyScrapePool | None = None, prefer_cortex: bool = False):
        self.session = sessao_rotativa()
        self.session.headers.update(DEFAULT_HEADERS)
        self.logger = logging.getLogger(self.LOG_NAME)
        self.pool = pool or ProxyScrapePool.singleton()
        self.prefer_cortex = prefer_cortex
        self._credencial: str | None = None
        self._restantes = 0
        self.requisicoes = 0

    @classmethod
    def fora_da_fonte(cls, numero_cnj: str) -> str | None:
        return faixa_fora_da_fonte(numero_cnj, cls.FORA_DA_FONTE_FAIXAS)

    # ---------- API pública ----------

    def enriquecer(self, processo: Process, direct_apply: bool = False) -> dict:
        if processo.tribunal_id != self.TRIBUNAL_SIGLA:
            raise TjrsEnricherError(
                f'Tribunal {processo.tribunal_id} não suportado por {self.__class__.__name__}.')

        base = {
            'process_id': processo.pk,
            'tribunal': processo.tribunal_id,
            'numero_cnj': processo.numero_cnj,
            'scraped_at': timezone.now().astimezone(_dt.UTC).isoformat(),
        }

        motivo = self.fora_da_fonte(processo.numero_cnj)
        if motivo:
            from .jobs import registrar_fora_do_esaj
            registrar_fora_do_esaj(self.TRIBUNAL_SIGLA, motivo)
            self._emit(stream.build_nao_encontrado_payload(**base), direct_apply)
            return {'cnj': processo.numero_cnj, 'status': 'nao_encontrado',
                    'fora_do_esaj': motivo, 'requisicoes': 0}

        try:
            item = self.buscar_ficha(processo.numero_cnj)
        except Exception as exc:
            self.logger.exception('falha na busca', extra={'cnj': processo.numero_cnj})
            self._emit(stream.build_erro_payload(**base, erro=f'busca: {exc}'), direct_apply)
            return {'cnj': processo.numero_cnj, 'status': 'erro', 'erro': str(exc)[:200]}

        if item is None:
            self._emit(stream.build_nao_encontrado_payload(**base), direct_apply)
            return {'cnj': processo.numero_cnj, 'status': 'nao_encontrado'}

        try:
            dados = self.extrair_dados(item)
            partes = self.extrair_partes(item)
        except Exception as exc:
            self.logger.exception('falha ao parsear', extra={'cnj': processo.numero_cnj})
            self._emit(stream.build_erro_payload(**base, erro=f'parse: {exc}'), direct_apply)
            return {'cnj': processo.numero_cnj, 'status': 'erro', 'erro': str(exc)[:200]}

        try:
            partes = self.somar_partes_de_terceiros(processo.pk, partes)
        except Exception:
            # Ler o banco é melhoria, não pré-requisito: sem ele a ficha ainda
            # sai, só sem os advogados vindos do DJEN.
            self.logger.exception('falha ao ler partes DJEN', extra={'cnj': processo.numero_cnj})

        self._emit(stream.build_ok_payload(**base, dados=dados, partes=partes), direct_apply)
        return {
            'cnj': processo.numero_cnj,
            'status': 'ok',
            'classe_raw': dados.get('classe'),
            'partes_total': sum(len(v) for v in partes.values()),
            'requisicoes': self.requisicoes,
        }

    def buscar_ficha(self, cnj: str) -> dict | None:
        """`/consultaProcesso` → o item do processo, ou `None` se a fonte diz
        que não tem (404 "Nenhum registro encontrado")."""
        digitos = _so_digitos(cnj)
        if len(digitos) != 20:
            raise TjrsEnricherError(f'CNJ inválido: {cnj!r}')
        status, corpo = self.get_api('/consultaProcesso', {
            'numeroProcesso': digitos, 'codComarca': comarca_tjrs(digitos)})
        if status == 404:
            return None
        for item in (corpo or {}).get('data') or []:
            if _so_digitos(item.get('numeroCNJ') or '') == digitos:
                return item
        # 200 sem o processo pedido: não é "não existe", é resposta que não
        # sabemos ler. Erro re-tentável, nunca `nao_encontrado` (regra nº 6).
        raise TjrsEnricherError('200 sem o processo pedido no corpo')

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

    def _next_proxy(self, exclude: set) -> str | None:
        """Mesma política dos demais REST (TJMT): pool primeiro, Cortex de
        fallback; `prefer_cortex` inverte."""
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

    def _nova_credencial(self, proxies: dict | None) -> str:
        """Desafio ALTCHA → solução → usuário anônimo → credencial `Basic`."""
        r = self.session.get(f'{self.AUTH_URL}/token', proxies=proxies,
                             timeout=self.REQUEST_TIMEOUT)
        self.requisicoes += 1
        if r.status_code != 200:
            raise TjrsCredencialError(f'token HTTP {r.status_code}')
        solucao = resolver_altcha(r.json())
        r = self.session.post(f'{self.AUTH_URL}/submit', proxies=proxies,
                              files={'altcha': (None, altcha_formulario(solucao))},
                              timeout=self.REQUEST_TIMEOUT)
        self.requisicoes += 1
        if r.status_code != 200:
            raise TjrsCredencialError(f'submit HTTP {r.status_code}')
        corpo = r.json()
        if not corpo.get('success') or not corpo.get('username'):
            raise TjrsCredencialError(f'submit recusado: {str(corpo)[:120]}')
        self._restantes = int(corpo.get('queries') or self.CONSULTAS_POR_CREDENCIAL)
        return credencial_tjrs(corpo['username'])

    def get_api(self, caminho: str, params: dict,  # noqa: PLR0912 — um ramo por status medido
                timeout: tuple | None = None) -> tuple[int, dict | None]:
        """GET autenticado na API. Devolve `(status, json)`.

        200 → json; 404 → `(404, json)` (a fonte diz "nenhum registro");
        401/429 → credencial nova e tenta de novo; 403/transporte → outro IP;
        5xx → outra tentativa. Esgotadas as rotações, levanta.
        """
        url = f'{self.API_URL}{caminho}'
        tentados: set = set()
        ultimo: str | None = None
        recusadas = 0
        cortex = cortex_proxy_url(self.pool) if self.pool is not None else None
        for tentativa in range(1, self.MAX_PROXY_ROTATIONS + 1):
            proxy = self._next_proxy(tentados) if self.pool is not None else None
            if self.pool is not None and not proxy:
                break
            if proxy and proxy != cortex:
                tentados.add(proxy)
            proxies = {'http': proxy, 'https': proxy} if proxy else None
            try:
                if not self._credencial or self._restantes <= 0:
                    self._credencial = self._nova_credencial(proxies)
                self._restantes -= 1
                resp = self.session.get(
                    url, params=params, proxies=proxies,
                    headers={'Authorization': f'Basic {self._credencial}'},
                    timeout=timeout or self.REQUEST_TIMEOUT)
                self.requisicoes += 1
            except (requests.ConnectionError, requests.Timeout,
                    requests.exceptions.ChunkedEncodingError) as exc:
                ultimo = f'transporte: {str(exc)[:120]}'
                if proxy and proxy != cortex:
                    self.pool.mark_bad(proxy)
                continue
            except (TjrsCredencialError, ValueError) as exc:
                ultimo = f'credencial: {str(exc)[:120]}'
                self._credencial = None
                continue

            if resp.status_code in (401, 429):
                # Credencial esgotada/recusada: a SPA faz o mesmo (regenera no
                # 401/429). Não é culpa do IP — não marca o proxy.
                ultimo = f'credencial {resp.status_code}'
                self._credencial = None
                if resp.status_code == 401:
                    recusadas += 1
                    if recusadas >= self.MAX_CREDENCIAIS_RECUSADAS:
                        raise TjrsEnricherError(
                            f'API recusou {recusadas} credenciais novas (401) — '
                            'fonte instável, não é "não encontrado"')
                continue
            if resp.status_code == 403:
                ultimo = 'bloqueado 403'
                if proxy and proxy != cortex:
                    self.pool.mark_bad(proxy)
                time.sleep(min(tentativa, 3))
                continue
            if resp.status_code >= 500:
                ultimo = f'servidor {resp.status_code}'
                continue
            try:
                corpo = resp.json()
            except ValueError:
                ultimo = f'corpo não-JSON ({len(resp.content)} bytes)'
                continue
            if resp.status_code == 404:
                return 404, corpo
            if resp.status_code != 200:
                ultimo = f'HTTP {resp.status_code}: {str(corpo)[:120]}'
                continue
            return 200, corpo
        raise TjrsEnricherError(
            f'{len(tentados)} proxies tentados sem sucesso'
            + (f' (último: {ultimo})' if ultimo else ''))

    # ---------- Partes já promovidas do DJEN ----------

    @staticmethod
    def _chave(nome: str) -> str:
        import unicodedata
        nfkd = unicodedata.normalize('NFKD', nome or '')
        return re.sub(r'\s+', ' ', ''.join(c for c in nfkd if not unicodedata.combining(c))
                      ).strip().upper()

    def somar_partes_de_terceiros(self, process_id: int, partes: dict) -> dict:
        """Mantém o que o DJEN/diário já trouxe e esta fonte NÃO tem.

        O drainer apaga TODAS as `ProcessoParte` do processo e regrava as do
        evento. Para os outros tribunais isso é o certo — o enricher traz mais
        que o DJEN. No TJRS não: a consulta pública não publica advogado
        (`{nome: null, oab: null}`), e o DJEN do TJRS traz o advogado com a OAB
        no cabeçalho de toda intimação. Regravar só as partes da consulta
        apagaria todos os advogados do processo — dado coletado pela metade,
        que vale menos que zero.

        Então as linhas de outra procedência (`fonte` não nula) voltam no
        evento com a MESMA identidade e a MESMA `fonte`: advogados sempre;
        demais partes só quando o nome não está entre as da consulta (aí a da
        consulta vence, porque traz o papel). Sem vínculo `representa` — o DJEN
        não o tem, e inventar seria chute.
        """
        from tribunals.models import ProcessoParte

        nomes = {self._chave(p['nome']) for lista in partes.values() for p in lista}
        linhas = (ProcessoParte.objects
                  .filter(processo_id=process_id, fonte__isnull=False, representa__isnull=True)
                  .select_related('parte'))
        for pp in linhas:
            parte = pp.parte
            advogado = parte.tipo == 'advogado' or bool(parte.oab) or 'ADVOGAD' in (pp.papel or '')
            if not advogado and self._chave(parte.nome) in nomes:
                continue
            polo = pp.polo if pp.polo in partes else 'outros'
            partes[polo].append({
                'nome': parte.nome, 'documento': parte.documento or '',
                'tipo_documento': parte.tipo_documento or '', 'oab': parte.oab or '',
                'papel': pp.papel or '', 'tipo': parte.tipo or 'desconhecido',
                'fonte': pp.fonte, 'representantes': [],
            })
        return partes

    # ---------- Parsing ----------

    def extrair_dados(self, item: dict) -> dict:
        out: dict = {}
        classe = (item.get('classeCNJ') or item.get('nomeClasse') or '').strip()
        if classe:
            # Só o NOME: a API não publica o código TPU. O drainer atualiza o
            # nome e preserva o código que veio do DJEN/Datajud.
            out['classe'] = classe
        assunto = assunto_folha(item.get('assuntoCNJ') or item.get('nomeNatureza') or '')
        if assunto:
            out['assunto'] = assunto
        orgao = ((item.get('orgaoJulgador') or {}).get('nome') or '').strip()
        if orgao:
            out['orgao_julgador'] = orgao
        julgador = (((item.get('orgaoJulgador') or {}).get('julgador') or {})
                    .get('nome') or '').strip()
        if julgador:
            out['juizo'] = julgador
        autuacao = _data_br(item.get('dataDistribuicao') or '')
        if autuacao:
            out['data_autuacao'] = autuacao
        if item.get('segredoJustica') is not None:
            out['segredo_justica'] = bool(item.get('segredoJustica'))
        # `valor_causa`: a API pública não publica — fica vazio.
        return out

    def _polo_de(self, papel: str) -> str:
        p = (papel or '').strip().upper()
        if p in self._TIPOS_ATIVO:
            return 'ativo'
        if p in self._TIPOS_PASSIVO:
            return 'passivo'
        return 'outros'

    def extrair_partes(self, item: dict) -> dict[str, list[dict]]:
        """Cada `parte` vira um principal; advogado só entra se a fonte der
        NOME (no eproc vem `{nome: null, oab: null}` — nada a criar)."""
        polos: dict[str, list[dict]] = {'ativo': [], 'passivo': [], 'outros': []}
        for parte in ((item.get('partes') or {}).get('parte') or []):
            nome = re.sub(r'\s+', ' ', (parte.get('nome') or '')).strip()
            if not nome:
                continue
            papel = re.sub(r'\s+', ' ', (parte.get('descricaoTipo') or '')).strip().upper()
            principal = {
                'nome': nome[:255],
                'documento': '',
                'tipo_documento': '',
                'oab': '',
                'papel': papel[:120],
                'tipo': classificar_tipo_parte('', '', '', papel),
                'representantes': [],
            }
            for adv in ((parte.get('advogados') or {}).get('advogado') or []):
                nome_adv = re.sub(r'\s+', ' ', (adv.get('nome') or '')).strip()
                if not nome_adv:
                    continue
                bruto = re.sub(r'[^0-9A-Za-z]', '', adv.get('oab') or '').upper()
                oab = (canonizar_oab(bruto) or bruto) if bruto else ''
                principal['representantes'].append({
                    'nome': nome_adv[:255], 'documento': '', 'tipo_documento': '',
                    'oab': oab[:20], 'papel': 'ADVOGADO', 'tipo': 'advogado',
                })
            polos[self._polo_de(papel)].append(principal)
        return polos
