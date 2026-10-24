"""Coletor da fila oficial de precatórios do TJMT: o SRP (Sistema de Requisição
de Precatórios).

Fonte pública, sem login, sem captcha e sem fingerprint (achada em 23/10/2026
no `assets/config/config.json` da SPA `precatorios.tjmt.jus.br/consultaprecatorio`;
o bundle aponta para `localhost:1337`, o host certo só existe em runtime):

- `GET /srp/orgaos-devedores` — JSON (Spring Page) com os entes devedores
  (242 em 23/10/2026): id, nome, `idOrgaoPagador`, tipo de órgão, opção de
  regime, valor da superpreferência.
- `GET /srp/requisicao-precatorio/<id>` — o "Relatório de Ordem Cronológica"
  do ente em PDF (JasperReports 6.6), gerado na hora (CreationDate = a
  requisição). Ente sem itens devolve 200 com 0 bytes. Colunas: ordem, órgão
  devedor, situação, natureza, preferência, protocolo (= CNJ do precatório,
  `NNNNNNN-DD.AAAA.8.11.0000`, PJe 2º grau, classe 1265), data de envio,
  exercício de pagamento, valor requisitado, valor pago. **Sem credor, sem CPF
  e sem processo de origem** — não há dado pessoal a descartar.

Três fatos medidos nos 242 relatórios de 23/10/2026 (87.176 linhas) que
desenham este coletor:

1. **O relatório se repete pela conta do ente pagador.** As 8 autarquias
   estaduais (MTPREV, UNEMAT, DETRAN, INDEA, MT Saúde, Intermat, Jucemat, IPEM)
   devolvem a lista do Estado; Cuiabá-Prev, SANECAP, FUNED e LIMPURB a de
   Cuiabá; e assim por diante. O JSON diz quem paga quem (`idOrgaoPagador`):
   agrupando os relatórios pelo pagador (215 grupos), **nenhum** dos 17.883
   CNJs aparece em dois grupos. O grupo é a unidade de coleta, de deduplicação
   e de completude — e a `entidade` gravada é o nome do pagador.
2. **A superpreferência vem numa linha à parte.** Os 375 CNJs que "divergiam
   entre relatórios" (o relatório 05 da investigação) divergem DENTRO do mesmo
   relatório: são 975 pares (relatório, CNJ) com exatamente 2 linhas — a
   parcela superpreferencial (Idade/Deficiência/Doença Grave, art. 100, § 2º,
   da CF) com valor IGUAL ao `valorSuperpreferenciaCalculoPagamento` do ente
   (Estado 118.845,00; INSS 254.160,00; Sinop 23.358,06; Cuiabá 38.930,10), e
   o restante. Somando as linhas do mesmo CNJ no mesmo relatório, os relatórios
   do mesmo grupo passam a concordar em 100% (0 divergências). Regra: soma
   valor requisitado e valor pago, `n_itens` = linhas, ordem = a menor,
   preferência = a da parcela que tem.
3. **Saldo é a regra, não o rótulo.** "Provisionado" vem com R$ 0,00 / R$ 0,00
   (3.193 linhas); há "Pagamento Preferencial" e "Pago parcialmente" com valor
   pago maior que o requisitado (correção monetária). Só é PENDENTE o item com
   situação de fila conhecida E saldo (requisitado - pago) > 0. O resto fica
   `outro` — na fila, sem saldo que se prove — e o classificador não promove.

Leitura do PDF: PyMuPDF com coordenadas (a imagem não tem poppler). O
cabeçalho só existe na 1ª página; as colunas são as do cabeçalho, e cada linha
de texto vai para a coluna de centro mais próximo e para a ordem (âncora) de
altura mais próxima. Parser estrito: a ordem tem de ser 1..N contínua, senão o
relatório é dado como incompleto (nada sai da fila por causa dele).

Carga: uma requisição por vez, pausa de `FILA_OFICIAL_TJMT_PAUSA` (≥ 1 s).
242 relatórios ≈ 11 min e 26 MB (o do Estado: 2 MB, 13 s).
"""
from __future__ import annotations

import logging
import re
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings

from filas_oficiais.base import (
    SITUACAO_OUTRO,
    SITUACAO_PENDENTE,
    ItemFila,
    ResultadoEntidade,
    normalizar_chave,
    normalizar_entidade,
    segmento_tribunal,
)

logger = logging.getLogger('voyager.filas_oficiais.tjmt')

BASE_URL = 'https://hellsgate.tjmt.jus.br/precatorios-cms/srp'
TRIBUNAL = 'TJMT'
FONTE_SRP = 'tjmt_srp'
SEGMENTO_TJMT = '8.11'

#: Situações do relatório que são fila de pagamento (o precatório é devido).
#: Medidas nos 242 relatórios de 23/10/2026. "Pagamento em Controverso" quebra
#: em duas linhas no PDF. Situação fora daqui → `outro` + alerta no run.
SITUACOES_FILA = frozenset({
    'AGUARDANDO PAGAMENTO',
    'PAGAMENTO PREFERENCIAL',
    'AUTUADO',
    'EM TRAMITE DE QUITACAO',
    'PAGO PARCIALMENTE',
    'PAGAMENTO EM CONTROVERSO',
    'SUSPENSO',
    'SOBRESTADO',
    'AGUARDANDO REGULARIZACAO PROCESSUAL',
})
#: Situação conhecida que não é dívida aberta provada (R$ 0,00 / R$ 0,00).
SITUACOES_SEM_SALDO = frozenset({'PROVISIONADO'})

_RE_CNJ_TOPO = re.compile(r'^(\d{7})-$')
_RE_CNJ_BASE = re.compile(r'^(\d{2}\.\d{4}\.\d\.\d{2}\.\d{4})$')
_RE_VALOR = re.compile(r'^R\$\s*([\d.]+,\d{2})$')
_RE_DATA = re.compile(r'^\d{2}/\d{2}/\d{4}$')
_RE_HORA = re.compile(r'^\d{2}:\d{2}:\d{2}$')
_RE_RODAPE_DATA = re.compile(r'^\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}$')

# Rótulo do cabeçalho → coluna. "Exercício de" / "Pagamento" são duas linhas.
_CABECALHO = {
    'ORDEM': 'ordem',
    'ORGAO DEVEDOR': 'devedor',
    'SITUACAO': 'situacao',
    'NATUREZA': 'natureza',
    'PREFERENCIA': 'preferencia',
    'PROTOCOLO': 'protocolo',
    'DATA ENVIO': 'data_envio',
    'EXERCICIO DE': 'exercicio',
    'VALOR REQUISITADO': 'valor_requisitado',
    'VALOR PAGO': 'valor_pago',
}


def _norm(s: str) -> str:
    return normalizar_entidade(s)


def _decimal(s: str) -> Decimal | None:
    try:
        return Decimal(s.replace('.', '').replace(',', '.')).quantize(Decimal('0.01'))
    except (InvalidOperation, AttributeError):
        return None


@dataclass
class LinhaRelatorio:
    """Uma linha do Relatório de Ordem Cronológica, como o PDF a traz."""
    ordem: int
    cnj: str = ''
    situacao_raw: str = ''
    natureza: str = ''
    preferencia: str = ''
    data_envio: str = ''
    exercicio: int | None = None
    valor_requisitado: Decimal | None = None
    valor_pago: Decimal | None = None
    devedor: str = ''
    erro: str = ''


@dataclass
class Relatorio:
    linhas: list = field(default_factory=list)       # LinhaRelatorio válidas
    rejeitadas: list = field(default_factory=list)   # LinhaRelatorio com `erro`
    sequencia_ok: bool = True
    gerado_em: str = ''


class LayoutDesconhecido(ValueError):  # noqa: N818 — nome do domínio, como `Indisponivel`
    """O PDF não tem o cabeçalho do Relatório de Ordem Cronológica."""


def _linhas_da_pagina(page) -> list[tuple[float, float, float, str]]:
    """(x0, x1, y_centro, texto) de cada linha de texto da página."""
    out = []
    for bloco in page.get_text('dict').get('blocks', []):
        for linha in bloco.get('lines', []):
            texto = ''.join(s.get('text', '') for s in linha.get('spans', [])).strip()
            if not texto:
                continue
            x0, y0, x1, y1 = linha['bbox']
            out.append((x0, x1, (y0 + y1) / 2, texto))
    return out


def _colunas(linhas) -> tuple[dict[str, float], float]:
    """{coluna: x do centro} e o y onde o cabeçalho termina."""
    cols, fim = {}, None
    for x0, x1, y, texto in linhas:
        col = _CABECALHO.get(_norm(texto))
        if col and col not in cols:
            cols[col] = (x0 + x1) / 2
            fim = y if fim is None else max(fim, y)
    faltam = set(_CABECALHO.values()) - set(cols)
    if faltam:
        raise LayoutDesconhecido(f'cabeçalho sem as colunas {sorted(faltam)}')
    # "Pagamento", a 2ª linha de "Exercício de", fica logo abaixo do rótulo.
    return cols, fim + 8


def ler_relatorio(conteudo: bytes) -> Relatorio:
    """Linhas do Relatório de Ordem Cronológica (PDF do SRP).

    0 bytes = ente sem itens (o SRP responde assim). Levanta
    `LayoutDesconhecido` se o PDF não for o relatório.
    """
    rel = Relatorio()
    if not conteudo:
        return rel
    import fitz  # PyMuPDF — import tardio: só a coleta precisa

    cols = topo = None
    with fitz.open(stream=conteudo, filetype='pdf') as doc:
        for page in doc:
            linhas = _linhas_da_pagina(page)
            if cols is None:
                cols, topo = _colunas(linhas)
            else:
                topo = 0.0           # páginas seguintes não repetem o cabeçalho
            corpo = []
            for x0, x1, y, texto in linhas:
                if y <= topo:
                    continue
                if _RE_RODAPE_DATA.match(texto):
                    rel.gerado_em = texto
                    continue
                centro = (x0 + x1) / 2
                col = min(cols, key=lambda c: abs(cols[c] - centro))
                if col == 'valor_pago' and texto.isdigit() and centro > cols['valor_pago'] + 15:
                    continue         # número da página, no canto direito
                corpo.append((col, y, texto))
            ancoras = sorted((y, int(t)) for c, y, t in corpo if c == 'ordem' and t.isdigit())
            if not ancoras:
                continue
            celulas: dict[int, dict[str, list]] = {o: {} for _, o in ancoras}
            ys = [a[0] for a in ancoras]
            for col, y, texto in corpo:
                if col == 'ordem':
                    continue
                i = min(range(len(ys)), key=lambda k: abs(ys[k] - y))
                celulas[ancoras[i][1]].setdefault(col, []).append((y, texto))
            for _, ordem in ancoras:
                rel_linha = _montar(ordem, celulas[ordem])
                (rel.rejeitadas if rel_linha.erro else rel.linhas).append(rel_linha)
    ordens = sorted([x.ordem for x in rel.linhas] + [x.ordem for x in rel.rejeitadas])
    rel.sequencia_ok = ordens == list(range(1, len(ordens) + 1))
    return rel


def _juntar(celula) -> str:
    return re.sub(r'\s+', ' ', ' '.join(t for _, t in sorted(celula or []))).strip()


def _montar(ordem: int, c: dict[str, list]) -> LinhaRelatorio:
    linha = LinhaRelatorio(ordem=ordem)
    prot = [t for _, t in sorted(c.get('protocolo') or [])]
    topo = next((m.group(1) for t in prot if (m := _RE_CNJ_TOPO.match(t))), None)
    base = next((m.group(1) for t in prot if (m := _RE_CNJ_BASE.match(t))), None)
    if topo and base:
        linha.cnj = normalizar_chave(f'{topo}-{base}')
    linha.situacao_raw = _juntar(c.get('situacao'))
    linha.natureza = _juntar(c.get('natureza')).upper()
    linha.preferencia = _juntar(c.get('preferencia'))
    linha.devedor = _juntar(c.get('devedor'))
    datas = [t for _, t in sorted(c.get('data_envio') or [])]
    d = next((t for t in datas if _RE_DATA.match(t)), '')
    h = next((t for t in datas if _RE_HORA.match(t)), '')
    linha.data_envio = f'{d} {h}'.strip()
    exe = _juntar(c.get('exercicio'))
    linha.exercicio = int(exe) if re.fullmatch(r'\d{4}', exe) else None
    for campo in ('valor_requisitado', 'valor_pago'):
        m = _RE_VALOR.match(_juntar(c.get(campo)))
        setattr(linha, campo, _decimal(m.group(1)) if m else None)
    faltas = [n for n, v in (('cnj', linha.cnj), ('natureza', linha.natureza in ('COMUM', 'ALIMENTAR')),
                             ('exercicio', linha.exercicio),
                             ('valor_requisitado', linha.valor_requisitado is not None),
                             ('valor_pago', linha.valor_pago is not None)) if not v]
    if faltas:
        linha.erro = 'sem ' + ', '.join(faltas)
    return linha


def situacao_srp(situacao_raw: str, saldo: Decimal | None) -> str:
    """Situação normalizada. PENDENTE = situação de fila E saldo > 0."""
    s = _norm(situacao_raw)
    if s in SITUACOES_FILA and saldo is not None and saldo > 0:
        return SITUACAO_PENDENTE
    return SITUACAO_OUTRO


def agrupar_relatorio(linhas: list[LinhaRelatorio]) -> dict[str, dict]:
    """{cnj: registro} somando as parcelas do MESMO CNJ no MESMO relatório
    (superpreferência + restante, fato 2 do docstring do módulo)."""
    por_cnj: dict[str, dict] = {}
    for x in linhas:
        r = por_cnj.get(x.cnj)
        if r is None:
            por_cnj[x.cnj] = {
                'ordem': x.ordem, 'situacoes': [x.situacao_raw], 'natureza': x.natureza,
                'preferencia': x.preferencia, 'data_envio': x.data_envio,
                'exercicio': x.exercicio, 'valor_requisitado': x.valor_requisitado,
                'valor_pago': x.valor_pago, 'devedor': x.devedor, 'n_itens': 1}
            continue
        r['ordem'] = min(r['ordem'], x.ordem)
        r['situacoes'].append(x.situacao_raw)
        r['preferencia'] = r['preferencia'] or x.preferencia
        r['valor_requisitado'] += x.valor_requisitado
        r['valor_pago'] += x.valor_pago
        r['n_itens'] += 1
    return por_cnj


def _assinatura(r: dict) -> tuple:
    return (r['valor_requisitado'], r['valor_pago'], r['exercicio'],
            tuple(sorted(r['situacoes'])), r['natureza'])


def item_do_registro(cnj: str, r: dict, entidade: str, regime: str = '') -> ItemFila:
    saldo = r['valor_requisitado'] - r['valor_pago']
    # Mais de uma situação no mesmo CNJ: basta uma de fila (o saldo decide).
    raws = list(dict.fromkeys(s for s in r['situacoes'] if s))
    fila = [s for s in raws if _norm(s) in SITUACOES_FILA]
    raw = (fila or raws or [''])[0]
    return ItemFila(
        chave=cnj, fonte=FONTE_SRP, entidade=entidade,
        entidade_devedora=normalizar_entidade(r['devedor'])[:255],
        situacao=situacao_srp(raw, saldo), situacao_raw=raw[:80],
        valor_original=r['valor_requisitado'], valor_pago=r['valor_pago'],
        ano_orcamento=r['exercicio'],
        natureza=r['natureza'], preferencia=(r['preferencia'] or '')[:32],
        ordem=r['ordem'], regime=regime, n_itens=r['n_itens'],
    )


# ---------------------------------------------------------------------------
# Coletor
# ---------------------------------------------------------------------------

class Indisponivel(Exception):  # noqa: N818 — mesmo nome do coletor do TJAL
    """Sem resposta útil do SRP depois das tentativas."""


class ColetorTjmt:
    tribunal = TRIBUNAL
    #: Uma execução lê TODOS os entes do SRP: com tudo completo, o pendente que
    #: nenhum ente listou sai da fila (`execucao.executar`).
    cobertura_total = True

    def __init__(self, session=None, pausa=None, timeout=None, dormir=time.sleep):
        self.session = session or requests.Session()
        self.session.headers.setdefault(
            'User-Agent', 'Voyager/1.0 (fila oficial de precatorios)')
        self.pausa = max(1.0, float(pausa if pausa is not None
                                    else getattr(settings, 'FILA_OFICIAL_TJMT_PAUSA', 1.5)))
        self.timeout = timeout or getattr(settings, 'FILA_OFICIAL_TJMT_TIMEOUT', 300)
        self.dormir = dormir
        self.requisicoes = 0

    # -- HTTP --------------------------------------------------------------
    def _pedir(self, caminho):
        """Uma requisição por vez, com pausa; erro de rede/5xx tenta de novo
        duas vezes (30 s, 60 s)."""
        url = f'{BASE_URL}{caminho}'
        ultimo = None
        for espera in (0, 30, 60):
            if espera:
                self.dormir(espera)
            self.requisicoes += 1
            try:
                resp = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as e:
                ultimo = e
                logger.warning('GET %s: %s', caminho, e)
                self.dormir(self.pausa)
                continue
            self.dormir(self.pausa)
            if resp.status_code >= 500:
                ultimo = f'HTTP {resp.status_code}'
                logger.warning('GET %s: HTTP %s', caminho, resp.status_code)
                continue
            resp.raise_for_status()
            return resp
        raise Indisponivel(f'{caminho}: sem resposta depois de 3 tentativas ({ultimo})')

    def orgaos(self) -> list[dict]:
        """Os entes devedores. Página truncada é erro (regra nº 2), não corte."""
        d = self._pedir('/orgaos-devedores').json()
        conteudo = d.get('content') or []
        total = d.get('totalElements')
        if total is not None and int(total) != len(conteudo):
            raise Indisponivel(f'orgaos-devedores truncado: {len(conteudo)} de {total}')
        return conteudo

    def relatorio(self, orgao_id) -> Relatorio:
        return ler_relatorio(self._pedir(f'/requisicao-precatorio/{orgao_id}').content)

    # -- coleta ------------------------------------------------------------
    @staticmethod
    def grupos(orgaos: list[dict]) -> dict[int, list[dict]]:
        """{id do pagador: [entes]} — o pagador primeiro (fato 1 do módulo)."""
        por_id = {o['id']: o for o in orgaos}
        out: dict[int, list[dict]] = {}
        for o in sorted(orgaos, key=lambda x: x['id']):
            pag = o.get('idOrgaoPagador') or o['id']
            if pag not in por_id:
                pag = o['id']
            out.setdefault(pag, [])
            if o['id'] == pag:
                out[pag].insert(0, o)
            else:
                out[pag].append(o)
        return out

    def iterar(self, entidades=None,
               pular: Callable[[str], bool] | None = None,
               falhas: list | None = None) -> Iterator[ResultadoEntidade]:
        """Uma `ResultadoEntidade` por grupo pagador, na hora em que ele termina.

        `entidades`: ids (do pagador ou de qualquer ente do grupo) ou nomes.
        Completa = todos os relatórios do grupo vieram e passaram no parser
        estrito.
        """
        falhas = falhas if falhas is not None else []
        try:
            orgaos = self.orgaos()
        except (Indisponivel, requests.RequestException, ValueError) as e:
            falhas.append(f'orgaos-devedores: {e}')
            return
        grupos = self.grupos(orgaos)
        if entidades:
            alvo = {str(e) for e in entidades} | {normalizar_entidade(str(e)) for e in entidades}
            grupos = {p: ents for p, ents in grupos.items()
                      if any(str(o['id']) in alvo or normalizar_entidade(o.get('nome')) in alvo
                             for o in ents)}
        for pagador, ents in grupos.items():
            if pular and pular(f'srp:{pagador}'):
                continue
            yield self.coletar_grupo(pagador, ents)

    def _ler(self, oid, res: ResultadoEntidade) -> Relatorio | None:
        """O relatório do ente, ou None (falha registrada em `res`)."""
        try:
            return self.relatorio(oid)
        except (Indisponivel, requests.RequestException, LayoutDesconhecido) as e:
            res.falhas.append(f'relatório {oid}: {e}')
        except Exception as e:                    # PDF corrompido etc.
            res.falhas.append(f'relatório {oid}: {e!r}'[:300])
        res.info['relatorios'][oid] = 'falhou'
        return None

    def coletar_grupo(self, pagador: int, ents: list[dict]) -> ResultadoEntidade:
        principal = ents[0]
        nome = normalizar_entidade(principal.get('nome'))
        regime = f"srp_opcao_{principal.get('codigoOpcaoRegimePagamento') or ''}"[:16]
        res = ResultadoEntidade(entidade=nome, itens=[], completa=False,
                                info={'chave': f'srp:{pagador}', 'pagador': pagador,
                                      'relatorios': {}})
        completa = True
        registros: dict[str, dict] = {}       # cnj -> registro escolhido
        origem: dict[str, int] = {}           # cnj -> id do relatório
        tamanho: dict[int, int] = {}
        divergentes: set[str] = set()
        for o in ents:
            oid = o['id']
            rel = self._ler(oid, res)
            if rel is None:
                completa = False
                continue
            if not rel.sequencia_ok:
                completa = False
                res.falhas.append(f'relatório {oid}: ordem fora de sequência')
            do_tjmt = [x for x in rel.linhas if segmento_tribunal(x.cnj) == SEGMENTO_TJMT]
            if len(do_tjmt) != len(rel.linhas):
                res.falhas.append(f'relatório {oid}: {len(rel.linhas) - len(do_tjmt)} '
                                  'protocolos fora do TJMT')
            agrup = agrupar_relatorio(do_tjmt)
            tamanho[oid] = len(agrup)
            res.info['relatorios'][oid] = {
                'linhas': len(rel.linhas), 'cnjs': len(agrup),
                'rejeitadas': len(rel.rejeitadas), 'sequencia_ok': rel.sequencia_ok,
                'gerado_em': rel.gerado_em}
            for x in rel.rejeitadas:
                logger.info('relatório %s ordem %s rejeitada: %s', oid, x.ordem, x.erro)
            for cnj, r in agrup.items():
                atual = registros.get(cnj)
                if atual is None:
                    registros[cnj], origem[cnj] = r, oid
                elif _assinatura(atual) != _assinatura(r):
                    divergentes.add(cnj)
                    # O relatório do pagador (o 1º) manda; entre os outros, o maior.
                    if origem[cnj] != pagador and tamanho[oid] > tamanho[origem[cnj]]:
                        registros[cnj], origem[cnj] = r, oid
        if divergentes:
            res.falhas.append(f'grupo {pagador}: {len(divergentes)} CNJs divergem '
                              'entre relatórios do mesmo pagador')
            logger.warning('fila oficial TJMT grupo %s: %d CNJs divergentes (ex.: %s)',
                           pagador, len(divergentes), sorted(divergentes)[:5])
        res.itens = [item_do_registro(cnj, r, nome, regime) for cnj, r in registros.items()]
        sit = Counter(_norm(i.situacao_raw) for i in res.itens)
        desconhecidas = {s: n for s, n in sit.items()
                         if s not in SITUACOES_FILA and s not in SITUACOES_SEM_SALDO}
        if desconhecidas:
            # Abster > chutar: vira `outro` (não promove) e fica no run.
            res.falhas.append(f'grupo {pagador}: situações desconhecidas {desconhecidas}')
        res.info['itens'] = len(res.itens)
        res.info['divergentes'] = len(divergentes)
        res.completa = completa
        return res
