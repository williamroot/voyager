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
    out['estoque_fmt'] = _humano(precat)
    out['rcl_fmt'] = _humano(rcl)
    out['pagamento_valor_fmt'] = _humano(out.get('pagamento_anual_estimado_valor'))
    cache.set(ck, out, timeout=86400)
    return out


def _humano(v) -> str | None:
    """Número grande → 'R$ 1,2 bi' / 'R$ 340 mi'."""
    if not v:
        return None
    v = float(v)
    if v >= 1e9:
        return f'R$ {v / 1e9:.1f} bi'.replace('.', ',')
    if v >= 1e6:
        return f'R$ {v / 1e6:.0f} mi'
    return f'R$ {v:,.0f}'.replace(',', '.')


# ───────────────────────── CAPAG (rating do ente) ─────────────────────────
_CAPAG_PKG = 'https://www.tesourotransparente.gov.br/ckan/api/3/action/package_show?id=capag-estados'
_CAPAG_NOTA = {'A': 'ótima', 'B': 'boa', 'C': 'insuficiente', 'D': 'crítica'}


def capag_rating(uf: str) -> dict:
    """Rating oficial de Capacidade de Pagamento (CAPAG) do ESTADO: nota A/B/C/D do
    Tesouro (endividamento, poupança corrente, liquidez). C/D = solvência reprovada →
    red flag pra pagar precatório. Fonte: Tesouro Transparente (CSV, ano mais recente)."""
    uf = (uf or '').strip().upper()
    if len(uf) != 2:
        return {'erro': 'uf inválida'}
    ck = f'capag:estados:{uf}'
    cached = cache.get(ck)
    if cached is not None:
        return cached
    try:
        res = _get(_CAPAG_PKG, timeout=25).json().get('result', {}).get('resources', [])
        csvs = [x for x in res if 'csv' in (x.get('format') or '').lower()]
        # ano mais recente pelo nome/url
        def _ano(x):
            m = re.search(r'20\d\d', (x.get('name') or '') + (x.get('url') or ''))
            return int(m.group()) if m else 0
        csvs.sort(key=_ano, reverse=True)
        if not csvs:
            return {'erro': 'sem CSV CAPAG'}
        ano = _ano(csvs[0])
        r = _get(csvs[0]['url'], timeout=40)
        r.encoding = 'utf-8-sig'
        txt = r.text
        delim = ';' if txt.split('\n', 1)[0].count(';') > txt.split('\n', 1)[0].count(',') else ','
        for row in csv.DictReader(io.StringIO(txt), delimiter=delim):
            norm = {re.sub(r'[^a-z0-9]', '', (k or '').lower()): v for k, v in row.items()}
            if (norm.get('uf') or '').strip().upper() == uf:
                nota = next((v for k, v in norm.items() if 'classifica' in k and v), None)
                out = {'uf': uf, 'ano': ano, 'nota': (nota or '').strip(),
                       'significado': _CAPAG_NOTA.get((nota or '').strip(), ''),
                       'endividamento': norm.get('indicador1'), 'poupanca': norm.get('indicador2'),
                       'liquidez': norm.get('indicador3'), 'fonte': 'Tesouro/CAPAG'}
                cache.set(ck, out, timeout=30 * 86400)
                return out
        return {'erro': f'UF {uf} não encontrada no CAPAG'}
    except Exception as exc:  # noqa: BLE001
        logger.warning('capag %s: %s', uf, str(exc)[:120])
        return {'erro': str(exc)[:120]}


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


# ───────────────────────── Índices econômicos (BCB SGS) ─────────────────────────
# Séries validadas ao vivo. Todas = variação % no mês. IPCA-E(7478)=Tema 810/EC 136;
# Selic(4390)=EC 113 (correção+juros num índice); TR(7811)=EC 62/pré-2015.
_SGS = {'IPCA-E': 7478, 'IPCA': 433, 'INPC': 188, 'SELIC': 4390, 'TR': 7811}


def atualizar_valor(valor: float, data_inicial: str, data_final: str,
                    indice: str = 'IPCA-E') -> dict:
    """Corrige um valor entre duas datas (DD/MM/AAAA) pelo índice do BCB (SGS). Índices:
    IPCA-E (Tema 810/EC 136), SELIC (EC 113, já com juros), TR (EC 62), IPCA, INPC.
    Devolve valor corrigido + fator. NÃO soma juros de mora (exceto Selic, que já embute)."""
    cod = _SGS.get((indice or 'IPCA-E').upper().replace('IPCAE', 'IPCA-E'))
    if not cod:
        return {'erro': f'índice desconhecido: {indice}'}
    try:
        valor = float(valor)
        url = (f'https://api.bcb.gov.br/dados/serie/bcdata.sgs.{cod}/dados'
               f'?formato=json&dataInicial={data_inicial}&dataFinal={data_final}')
        serie = _get(url, timeout=30).json()
        fator = 1.0
        for m in serie:
            fator *= 1 + float(m['valor']) / 100
        return {'indice': indice, 'de': data_inicial, 'ate': data_final,
                'fator': round(fator, 8), 'valor_original': valor,
                'valor_corrigido': round(valor * fator, 2), 'variacao_pct': round((fator - 1) * 100, 2),
                'meses': len(serie), 'fonte': f'BCB SGS {cod}'}
    except Exception as exc:  # noqa: BLE001
        logger.warning('atualizar_valor %s: %s', indice, str(exc)[:120])
        return {'erro': str(exc)[:120]}


def _selic_meta_aa() -> float:
    """Selic meta atual (% a.a.) via SGS 432. Cacheada; fallback 10.5 se indisponível."""
    v = cache.get('bcb:selic_meta')
    if v is not None:
        return v
    try:
        d = _get('https://api.bcb.gov.br/dados/serie/bcdata.sgs.432/dados/ultimos/1?formato=json',
                 timeout=15).json()
        v = float(d[-1]['valor'])
        cache.set('bcb:selic_meta', v, timeout=86400)
        return v
    except Exception:  # noqa: BLE001
        return 10.5


def valor_presente(valor_face: float, anos_ate_pagamento: float,
                   taxa_desconto_aa: float | None = None, ipca_aa: float = 4.5) -> dict:
    """Valor JUSTO hoje de um precatório (determinístico, 100% público). Projeta o valor
    de face pela correção EC 136 — min(IPCA+2%, Selic) a.a. — até o pagamento e desconta
    pela taxa de oportunidade (Selic por padrão). Devolve VP + deságio implícito. NÃO é
    cotação de mercado; é âncora determinística (mercado costuma cotar deságio ≥ este)."""
    try:
        valor_face = float(valor_face)
        anos = max(float(anos_ate_pagamento), 0.0)
    except (TypeError, ValueError):
        return {'erro': 'valor_face/anos inválidos'}
    selic = _selic_meta_aa()
    taxa = (taxa_desconto_aa if taxa_desconto_aa is not None else selic) / 100
    corr = min(ipca_aa / 100 + 0.02, selic / 100)  # EC 136: IPCA+2% com teto Selic
    fv = valor_face * (1 + corr) ** anos
    vp = fv / (1 + taxa) ** anos if anos > 0 else valor_face
    return {
        'valor_face': round(valor_face, 2), 'anos_ate_pagamento': round(anos, 1),
        'valor_projetado_pagamento': round(fv, 2), 'valor_presente': round(vp, 2),
        'desagio_implicito_pct': round((1 - vp / valor_face) * 100, 1) if valor_face else None,
        'correcao_aa_pct': round(corr * 100, 2), 'taxa_desconto_aa_pct': round(taxa * 100, 2),
        'selic_meta_aa_pct': selic,
        'fonte': 'modelo determinístico (EC 136 + desconto Selic/BCB) — não é cotação de mercado',
    }


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
