"""Clients de FONTES PÚBLICAS (sem login/captcha) pra enriquecer a jurimetria de
precatórios e alimentar o RAG/agente. Cada função é fail-closed: devolve dict com
`erro` (ou {}) em falha — nunca levanta pro caller, a tela/agente degrada.

Fontes (validadas ao vivo, HTTP 200):
- SICONFI/Tesouro   — fiscal do ente devedor (estoque precatório, DCL, RCL). Define,
                      sob a EC 136/2025, quanto o ente paga por ano (banda 1–5% da RCL).
- CNPJ público      — cadastro do ente/PJ (Minha Receita → BrasilAPI fallback).
- STJ Dados Abertos — temas repetitivos + teses firmadas (CKAN, CSV).
- DJEN/Comunica     — publicações por CNJ/CPF-CNPJ/OAB (texto integral).
- SGT/TPU (CNJ)     — decodifica códigos de classe/assunto/movimento do DataJud.
- Querido Diário    — diários municipais (fila de precatório municipal).

Nada é inventado: o que a fonte não der, volta ausente. Cache curto no Redis (as bases
mudam devagar) pra não martelar as APIs.
"""
from __future__ import annotations

import csv
import io
import logging
import re

import requests
from django.core.cache import cache

logger = logging.getLogger('voyager.fontes_publicas')

_UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
       'Chrome/120 Safari/537.36')
_H = {'User-Agent': _UA, 'Accept-Language': 'pt-BR,pt;q=0.9'}


def _get(url: str, *, timeout: float = 25, headers: dict | None = None, **kw):
    return requests.get(url, headers={**_H, **(headers or {})}, timeout=timeout,
                        verify=False, **kw)


# ───────────────────────── SICONFI (ente devedor) ─────────────────────────
_SICONFI = 'https://apidatalake.tesouro.gov.br/ords/siconfi/tt'


# UF → código IBGE (2 díg) — pra consultar precatório ESTADUAL pela sigla.
_UF_IBGE = {'RO': 11, 'AC': 12, 'AM': 13, 'RR': 14, 'PA': 15, 'AP': 16, 'TO': 17,
            'MA': 21, 'PI': 22, 'CE': 23, 'RN': 24, 'PB': 25, 'PE': 26, 'AL': 27,
            'SE': 28, 'BA': 29, 'MG': 31, 'ES': 32, 'RJ': 33, 'SP': 35, 'PR': 41,
            'SC': 42, 'RS': 43, 'MS': 50, 'MT': 51, 'GO': 52, 'DF': 53}


def ente_fiscal(id_ente: str | int | None = None, ano: int = 2023, *,
                uf: str | None = None, periodo: int = 3) -> dict:
    """Saúde fiscal do ente devedor via SICONFI (RGF-Anexo 02): estoque de precatórios
    vencidos, Dívida Consolidada Líquida (DCL) e RCL. Passe `uf` (ex. 'SP') pro estado, ou
    `id_ente` IBGE (município=7 díg). Devolve valores + banda de pagamento anual EC 136 (1–5% RCL)."""
    if id_ente is None and uf:
        id_ente = _UF_IBGE.get(uf.strip().upper())
    if id_ente is None:
        return {'erro': 'informe uf (estado) ou id_ente (IBGE)'}
    ck = f'siconfi:rgf:{id_ente}:{ano}:{periodo}'
    cached = cache.get(ck)
    if cached is not None:
        return cached
    esfera = 'E' if len(str(id_ente)) <= 2 else 'M'
    url = (f'{_SICONFI}/rgf?an_exercicio={ano}&in_periodicidade=Q&nr_periodo={periodo}'
           f'&co_tipo_demonstrativo=RGF&no_anexo=RGF-Anexo%2002&co_poder=E'
           f'&co_esfera={esfera}&id_ente={id_ente}')
    try:
        r = _get(url, timeout=30)
        if r.status_code != 200:
            return {'erro': f'HTTP {r.status_code}'}
        itens = r.json().get('items', [])
    except Exception as exc:  # noqa: BLE001
        logger.warning('siconfi %s: %s', id_ente, str(exc)[:120])
        return {'erro': str(exc)[:120]}

    def _soma(frag):
        return sum(i.get('valor') or 0 for i in itens
                   if frag.lower() in (i.get('conta') or '').lower())

    precat = _soma('Precatórios') or None
    dcl = next((i.get('valor') for i in itens
                if (i.get('conta') or '').strip().upper().startswith('DÍVIDA CONSOLIDADA LÍQUIDA')), None)
    rcl = next((i.get('valor') for i in itens
                if 'RECEITA CORRENTE LÍQUIDA' in (i.get('conta') or '').upper()), None)
    out = {'id_ente': id_ente, 'ano': ano, 'estoque_precatorio_vencido': precat,
           'divida_consolidada_liquida': dcl, 'rcl': rcl, 'fonte': 'SICONFI/Tesouro RGF-Anexo 02'}
    # banda de pagamento anual da EC 136/2025: estoque/RCL → % da RCL/ano
    if precat and rcl:
        raz = precat / rcl
        pct = 1 if raz <= 0.15 else (5 if raz > 0.85 else round(1 + 4 * (raz - 0.15) / 0.70, 1))
        out['razao_estoque_rcl'] = round(raz, 3)
        out['pagamento_anual_estimado_pct_rcl'] = pct
        out['pagamento_anual_estimado_valor'] = round(rcl * pct / 100, 2)
    cache.set(ck, out, timeout=86400)
    return out


# ───────────────────────── CNPJ público ─────────────────────────
def consultar_cnpj(cnpj: str) -> dict:
    """Cadastro RFB por CNPJ (razão social, situação, natureza jurídica, CNAE, porte,
    município). Minha Receita → BrasilAPI fallback. Útil pro ente devedor e partes PJ."""
    cnpj = re.sub(r'\D', '', cnpj or '')
    if len(cnpj) != 14:
        return {'erro': 'CNPJ inválido'}
    ck = f'cnpj:{cnpj}'
    cached = cache.get(ck)
    if cached is not None:
        return cached
    for nome, url, mapa in (
        ('minhareceita', f'https://minhareceita.org/{cnpj}',
         {'razao': 'razao_social', 'situacao': 'descricao_situacao_cadastral',
          'natureza': 'natureza_juridica', 'municipio': 'municipio', 'uf': 'uf', 'porte': 'porte'}),
        ('brasilapi', f'https://brasilapi.com.br/api/cnpj/v1/{cnpj}',
         {'razao': 'razao_social', 'situacao': 'descricao_situacao_cadastral',
          'natureza': 'natureza_juridica', 'municipio': 'municipio', 'uf': 'uf', 'porte': 'porte'}),
    ):
        try:
            r = _get(url, timeout=25)
            if r.status_code != 200:
                continue
            d = r.json()
            out = {k: d.get(v) for k, v in mapa.items()}
            out.update({'cnpj': cnpj, 'fonte': nome})
            cache.set(ck, out, timeout=7 * 86400)
            return out
        except Exception as exc:  # noqa: BLE001
            logger.info('cnpj %s via %s: %s', cnpj, nome, str(exc)[:80])
            continue
    return {'erro': 'CNPJ não resolvido em nenhuma fonte'}


# ───────────────────────── STJ temas repetitivos (CKAN) ─────────────────────────
_STJ_TEMAS_CSV = ('https://dadosabertos.web.stj.jus.br/dataset/4238da2f-c07b-4c1a-b345-'
                  '4402accacdcf/resource/df29da13-7d6b-41ba-ad96-cd1a5bbd191c/download/temas.csv')


def _stj_temas_todos() -> list[dict]:
    cached = cache.get('stj:temas:all')
    if cached is not None:
        return cached
    try:
        r = _get(_STJ_TEMAS_CSV, timeout=40)
        r.encoding = 'utf-8'
        head = r.text.split('\n', 1)[0]
        delim = ';' if head.count(';') > head.count(',') else ','
        rows = list(csv.DictReader(io.StringIO(r.text), delimiter=delim))
        cache.set('stj:temas:all', rows, timeout=7 * 86400)
        return rows
    except Exception as exc:  # noqa: BLE001
        logger.warning('stj temas csv: %s', str(exc)[:120])
        return []


def stj_temas_repetitivos(assunto: str, limit: int = 8) -> dict:
    """Busca temas repetitivos/teses firmadas do STJ (CKAN) por termo no assunto/tese.
    Devolve os precedentes qualificados reais (nunca inventa)."""
    termo = (assunto or '').strip().lower()
    if not termo:
        return {'erro': 'assunto vazio'}
    rows = _stj_temas_todos()
    if not rows:
        return {'erro': 'base STJ indisponível'}
    palavras = [w for w in re.split(r'\W+', termo) if len(w) > 2]
    hits = []
    for row in rows:
        blob = ' '.join(str(v) for v in row.values() if v).lower()
        if (palavras and all(w in blob for w in palavras)) or (not palavras and termo in blob):
            hits.append({
                'numero': (row.get('numeroPrecedente') or '').strip(),
                'tipo': (row.get('tipoPrecedente') or '').strip(),
                'tese': (row.get('teseFirmada') or '').strip()[:700],
                'questao': (row.get('questaoSubmetidaAJulgamento') or '').strip()[:300],
                'situacao': (row.get('situacao') or '').strip(),
                'sumula': (row.get('sumulaOriginada') or row.get('referenciaSumular') or '').strip()[:120],
                'tema_stf': (row.get('numeroRepercussaoGeralSTF') or '').strip(),
                'assuntos': (row.get('Assuntos') or '').strip()[:120],
            })
    # teses firmadas primeiro (mais úteis que controvérsias pendentes)
    hits.sort(key=lambda h: (0 if h['tese'] else 1))
    return {'assunto': assunto, 'n': len(hits), 'temas': hits[:limit],
            'fonte': 'STJ Dados Abertos (Precedentes Qualificados)'}


# ───────────────────────── DJEN / Comunica (publicações on-demand) ─────────────────────────
_COMUNICA = 'https://comunicaapi.pje.jus.br/api/v1/comunicacao'


def djen_publicacoes(*, numero_processo: str | None = None, documento: str | None = None,
                     numero_oab: str | None = None, uf_oab: str | None = None,
                     limit: int = 20) -> dict:
    """Publicações do DJEN por CNJ, CPF/CNPJ da parte, ou OAB (texto integral). On-demand
    (não é a ingestão bulk). Fonte pública, sem auth."""
    params = {'itensPorPagina': min(limit, 100), 'pagina': 1}
    if numero_processo:
        params['numeroProcesso'] = re.sub(r'\D', '', numero_processo)
    if documento:
        params['numeroDocumento'] = re.sub(r'\D', '', documento)
    if numero_oab:
        params['numeroOab'] = re.sub(r'\D', '', numero_oab)
        if uf_oab:
            params['ufOab'] = uf_oab.upper()
    if len(params) <= 2:
        return {'erro': 'informe numero_processo, documento ou OAB'}
    try:
        r = _get(_COMUNICA, params=params, timeout=30)
        if r.status_code != 200:
            return {'erro': f'HTTP {r.status_code}'}
        d = r.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning('djen pub: %s', str(exc)[:120])
        return {'erro': str(exc)[:120]}
    itens = [{
        'data': it.get('data_disponibilizacao'), 'tribunal': it.get('siglaTribunal'),
        'orgao': it.get('nomeOrgao'), 'tipo': it.get('tipoComunicacao'),
        'classe': it.get('nomeClasse'), 'cnj': it.get('numero_processo'),
        'texto': re.sub(r'<[^>]+>', ' ', it.get('texto') or '')[:1200],
    } for it in (d.get('items') or [])]
    return {'count': d.get('count'), 'n': len(itens), 'publicacoes': itens, 'fonte': 'DJEN/Comunica (PJe-CNJ)'}


# ───────────────────────── SGT / TPU (decode de códigos) ─────────────────────────
_SGT = 'https://www.cnj.jus.br/sgt/sgt_ws.php'


def sgt_decodificar(tipo_tabela: str, codigo: int | str) -> dict:
    """Traduz um código do DataJud (classe/assunto/movimento/documento) → descrição.
    tipo_tabela ∈ {C,A,M,D}. Usa o WS SOAP público do SGT/CNJ."""
    tt = (tipo_tabela or 'C')[0].upper()
    ck = f'sgt:{tt}:{codigo}'
    cached = cache.get(ck)
    if cached is not None:
        return cached
    envelope = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:sgt="urn:sgt_ws"><soapenv:Body><sgt:pesquisarItemPublicoWS>'
        f'<tipoTabela>{tt}</tipoTabela><tipoPesquisa>C</tipoPesquisa>'
        f'<valorPesquisa>{codigo}</valorPesquisa>'
        '</sgt:pesquisarItemPublicoWS></soapenv:Body></soapenv:Envelope>')
    try:
        r = requests.post(_SGT, data=envelope.encode('utf-8'),
                          headers={**_H, 'Content-Type': 'text/xml; charset=utf-8', 'SOAPAction': ''},
                          timeout=25, verify=False)
        nome = re.search(r'<nome[^>]*>([^<]+)</nome>', r.text)
        pai = re.search(r'<cod_item_pai[^>]*>([^<]+)</cod_item_pai>', r.text)
        if not nome:
            return {'erro': 'código não encontrado'}
        out = {'tipo': tt, 'codigo': str(codigo), 'nome': nome.group(1).strip(),
               'cod_pai': pai.group(1).strip() if pai else None, 'fonte': 'SGT/TPU CNJ'}
        cache.set(ck, out, timeout=30 * 86400)
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning('sgt %s %s: %s', tt, codigo, str(exc)[:100])
        return {'erro': str(exc)[:100]}


# ───────────────────────── Querido Diário (municipal) ─────────────────────────
def querido_diario(termo: str, *, municipio_ibge: str | None = None, limit: int = 10) -> dict:
    """Busca em diários oficiais MUNICIPAIS (Querido Diário). Útil pra fila/editais de
    precatório municipal. Devolve trechos + link do .txt integral."""
    params = {'querystring': termo, 'size': min(limit, 20), 'excerpt_size': 300, 'number_of_excerpts': 2}
    if municipio_ibge:
        params['territory_ids'] = municipio_ibge
    try:
        r = _get('https://api.queridodiario.ok.org.br/gazettes', params=params, timeout=25)
        if r.status_code != 200:
            return {'erro': f'HTTP {r.status_code}'}
        d = r.json()
    except Exception as exc:  # noqa: BLE001
        return {'erro': str(exc)[:120]}
    itens = [{
        'municipio': g.get('territory_name'), 'uf': g.get('state_code'), 'data': g.get('date'),
        'txt_url': g.get('txt_url'), 'trechos': g.get('excerpts') or [],
    } for g in (d.get('gazettes') or [])]
    return {'total': d.get('total_gazettes'), 'n': len(itens), 'diarios': itens, 'fonte': 'Querido Diário'}
