"""Coletor da fila oficial de precatórios do TJPA: o Power BI público da
Coordenadoria de Precatórios.

A página `tjpa.jus.br/PortalExterno/institucional/Precatorios/1401291-lista-
cronologica.xhtml` embute um relatório "publicar na web" do Power BI. A API
pública dele devolve a tabela crua do modelo, sem login, sem captcha e sem
cookie (medido em 23/10/2026):

- `GET  /public/reports/<chave>/modelsAndExploration?preferReadOnlySession=true`
  → o modelo (`id`, `dbName`, `LastRefreshTime`, `NextRefreshTime`);
- `POST /public/reports/conceptualschema` `{"modelIds": [id]}` → as tabelas e
  colunas do modelo;
- `POST /public/reports/querydata?synchronous=true` com uma
  `SemanticQueryDataShapeCommand` → a tabela, em DSR (o formato comprimido do
  Power BI: dicionários de valores, bitmask `R` de "repete o anterior" e `Ø` de
  nulo).

Tabelas usadas: `BI_PREC_LISTAORDEMCRONOLOGICA` (ordem por ente, `nr_processo`
= CNJ do precatório no PJe 2º grau `08xxxxx-DD.AAAA.8.14.0000`, ente,
natureza, número do precatório `00383/2026`, autuação, `vl_causa` = valor de
face, exercício) e `BI_PREC_LISTASSUPERPREFERENCIA` (a modalidade da
superpreferência deferida). Em 23/10/2026: 3.770 linhas, 3.756 processos, 128
entes, R$ 1,13 bi, `IC=true` (resposta completa) numa requisição de 340 KB. O
dataset atualiza todo dia às 07:00 UTC. **Sem credor, sem CPF e sem processo
de origem** — não há dado pessoal a descartar.

Parser ESTRITO (regra nº 2: teto é alerta, nunca corte mudo). Qualquer coisa
fora do formato medido levanta `LayoutDesconhecido` e a coleta inteira falha
alto — o run fica `failed`, nenhum item sai da fila e nada é dado como vazio:
  * mais de um modelo, tabela ou coluna faltando no `conceptualschema`;
  * a resposta não diz `IC=true` (truncada), traz colunas em outra ordem ou
    nome, ou sobra valor no `C` de uma linha (bitmask mal lida);
  * zero linhas, ou menos que `PISO_RELATIVO` dos pendentes já registrados;
  * linha com CNJ inválido (DV) ou fora do foro `8.14.0000`, ordem não
    inteira, valor ou exercício impossíveis.

Unidade de coleta: uma requisição traz o tribunal inteiro; o coletor parte por
ENTE (a `entidade` gravada) só para o placar e a completude por entidade. O
mesmo CNJ pode vir em duas linhas: dois números de precatório no mesmo
processo (8 casos; somados, `n_itens` = 2) ou o devedor solidário — o mesmo
precatório na lista do Estado e na do IGEPREV/IASEP (6 casos). Este fica no
ente de nome menor em ordem alfabética, com os dois em `entidade_devedora`,
para não trocar de entidade a cada coleta.

Carga: 4 requisições por dia, uma por vez, pausa ≥ 1 s.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings

from filas_oficiais.base import (
    SITUACAO_PENDENTE,
    ItemFila,
    ResultadoEntidade,
    normalizar_chave,
    normalizar_entidade,
)
from tribunals.cnj import dv_valido

logger = logging.getLogger('voyager.filas_oficiais.tjpa')

TRIBUNAL = 'TJPA'
FONTE_POWERBI = 'tjpa_powerbi'
API = 'https://wabi-brazil-south-api.analysis.windows.net/public/reports'
#: Chave do "publicar na web" (`r=` da página da lista, em base64). Se o TJPA
#: recriar o link, ela muda e a coleta falha alto (401/404).
CHAVE_RELATORIO = 'aed10e46-256d-40fe-8631-7a841b28af95'
SUFIXO_2G = '.8.14.0000'

ENTIDADE_LISTA = 'BI_PREC_LISTAORDEMCRONOLOGICA'
COLUNAS_LISTA = ('ordem', 'nr_processo', 'ente', 'natureza', 'nr_precatorio',
                 'dt_autuacao', 'vl_causa', 'exercicio_precatorio')
ENTIDADE_SUPER = 'BI_PREC_LISTASSUPERPREFERENCIA'
COLUNAS_SUPER = ('nr_processo', 'nr_precatorio_certidao_inscricao', 'modalidade')
#: Linhas por consulta. 3.770 em 23/10/2026; a resposta tem de vir `IC=true`.
JANELA = 30000
#: A lista não pode encolher mais que isto de uma coleta para a outra sem
#: alerta: 0,5 = menos da metade dos pendentes já registrados é falha.
PISO_RELATIVO = 0.5

#: Modalidade da superpreferência (art. 100, § 2º, CF) → `preferencia`, já
#: normalizada (sem acento, caixa alta). Medidas em 24/10/2026 nos 655 da
#: lista: IDOSO(A) 640, PORTADORES DE DOENÇAS GRAVES 12, PESSOA COM DEFICIÊNCIA 3.
PREFERENCIAS = {'IDOSO(A)': 'Idade', 'IDOSO': 'Idade',
                'PORTADORES DE DOENCAS GRAVES': 'Doença Grave', 'DOENCA GRAVE': 'Doença Grave',
                'PESSOA COM DEFICIENCIA': 'Deficiência', 'PCD': 'Deficiência'}


class Indisponivel(Exception):  # noqa: N818 — mesmo nome do coletor do TJMT
    """A fonte não respondeu (rede, 5xx) depois das retentativas."""


class LayoutDesconhecido(Exception):  # noqa: N818
    """A resposta não tem o formato medido: modelo, colunas ou DSR mudaram."""


# ---------------------------------------------------------------------------
# DSR
# ---------------------------------------------------------------------------
def consulta(entidade: str, colunas: tuple[str, ...], model_id: int, dataset_id: str) -> dict:
    """Corpo do `querydata` que seleciona `colunas` de `entidade`, linha a linha."""
    sel = [{'Column': {'Expression': {'SourceRef': {'Source': 't'}}, 'Property': c},
            'Name': f't.{c}'} for c in colunas]
    q = {'Commands': [{'SemanticQueryDataShapeCommand': {
        'Query': {'Version': 2, 'From': [{'Name': 't', 'Entity': entidade, 'Type': 0}],
                  'Select': sel},
        'Binding': {'Primary': {'Groupings': [{'Projections': list(range(len(colunas)))}]},
                    'DataReduction': {'DataVolume': 4, 'Primary': {'Window': {'Count': JANELA}}},
                    'Version': 1}}}]}
    return {'version': '1.0.0', 'cancelQueries': [], 'modelId': model_id,
            'queries': [{'Query': q, 'QueryId': '',
                         'ApplicationContext': {'DatasetId': dataset_id,
                                                'Sources': [{'ReportId': 'voyager'}]}}]}


def _valor(v, s: dict, dicts: dict):
    """Um valor da linha: índice de dicionário (`DN`) ou literal."""
    dn = s.get('DN')
    if dn is not None and isinstance(v, int) and not isinstance(v, bool):
        lst = dicts.get(dn)
        if lst is None or not 0 <= v < len(lst):
            raise LayoutDesconhecido(f'índice {v} fora do dicionário {dn}')
        return lst[v]
    return v


def decodificar_dsr(resposta: dict, colunas: tuple[str, ...]) -> list[dict]:  # noqa: PLR0912
    """As linhas da resposta, como dicts `{coluna: valor}`. Estrito: ver o módulo."""
    try:
        res = resposta['results'][0]['result']
        if 'error' in res or 'data' not in res:
            raise LayoutDesconhecido(f'querydata com erro: {str(res)[:300]}')
        data = res['data']
        nomes = tuple(x['Name'].split('.', 1)[1] for x in data['descriptor']['Select'])
        dss = data['dsr']['DS']
    except (KeyError, IndexError, TypeError) as e:
        raise LayoutDesconhecido(f'resposta fora do formato: {e!r}') from e
    if nomes != colunas:
        raise LayoutDesconhecido(f'colunas {nomes} != {colunas}')
    if len(dss) != 1:
        raise LayoutDesconhecido(f'{len(dss)} DataSets na resposta')
    ds = dss[0]
    if ds.get('IC') is not True:
        raise LayoutDesconhecido('resposta incompleta (IC != true): a lista veio truncada')
    if ds.get('RT') or ds.get('DW'):
        raise LayoutDesconhecido('resposta com continuação (RT/DW): a lista veio truncada')
    dicts = ds.get('ValueDicts') or {}
    linhas: list[dict] = []
    esquema = None
    anterior = None
    for ph in ds.get('PH') or []:
        for chave, lst in ph.items():
            if chave != 'DM0':
                raise LayoutDesconhecido(f'membro inesperado no DSR: {chave}')
            for r in lst:
                if 'S' in r:
                    esquema = r['S']
                if esquema is None or len(esquema) != len(colunas):
                    raise LayoutDesconhecido('linha sem esquema (S) ou com outra largura')
                rep, nul = r.get('R', 0), r.get('Ø', 0)
                # Forma nomeada ({"G0": v, ...}) nas tabelas de 1 linha.
                vals = iter(r['C'] if 'C' in r else [r[s['N']] for s in esquema if s['N'] in r])
                linha = []
                for i, s in enumerate(esquema):
                    if rep >> i & 1:
                        if anterior is None:
                            raise LayoutDesconhecido('repetição (R) na primeira linha')
                        linha.append(anterior[i])
                    elif nul >> i & 1:
                        linha.append(None)
                    else:
                        try:
                            linha.append(_valor(next(vals), s, dicts))
                        except StopIteration:
                            raise LayoutDesconhecido('linha com menos valores que colunas') from None
                if list(vals):
                    raise LayoutDesconhecido('sobra valor no C: bitmask mal lida')
                anterior = linha
                linhas.append(dict(zip(colunas, linha, strict=True)))
    return linhas


# ---------------------------------------------------------------------------
# Linhas → itens
# ---------------------------------------------------------------------------
def _decimal(v) -> Decimal | None:
    if v is None:
        return None
    try:
        d = Decimal(str(v)).quantize(Decimal('0.01'))
    except InvalidOperation:
        return None
    return d if d >= 0 else None


def validar_linha(r: dict) -> str:  # noqa: PLR0911
    """'' se a linha da lista está no formato medido; senão, o motivo."""
    chave = normalizar_chave(str(r.get('nr_processo') or ''))
    if not chave or chave != str(r.get('nr_processo')).strip():
        return f'nr_processo fora do formato CNJ: {r.get("nr_processo")!r}'
    if not chave.endswith(SUFIXO_2G):
        return f'{chave}: fora do PJe 2º grau do TJPA ({SUFIXO_2G})'
    if not dv_valido(chave):
        return f'{chave}: dígito verificador inválido'
    o = r.get('ordem')
    if not isinstance(o, int) or isinstance(o, bool) or o < 1:
        return f'{chave}: ordem {o!r} não é inteiro positivo'
    if _decimal(r.get('vl_causa')) is None:
        return f'{chave}: valor {r.get("vl_causa")!r}'
    ex = r.get('exercicio_precatorio')
    if not isinstance(ex, int) or not 2000 <= ex <= 2100:
        return f'{chave}: exercício {ex!r}'
    if not (r.get('ente') or '').strip():
        return f'{chave}: sem ente'
    return ''


def montar_itens(linhas: list[dict], super_por_cnj: dict[str, str]) -> dict[str, list[ItemFila]]:
    """{entidade: [ItemFila]} — uma chave (CNJ) por item, num ente só."""
    por_cnj: dict[str, list[dict]] = defaultdict(list)
    for r in linhas:
        por_cnj[r['nr_processo'].strip()].append(r)
    out: dict[str, list[ItemFila]] = defaultdict(list)
    for cnj, rs in por_cnj.items():
        entes = sorted({normalizar_entidade(r['ente']) for r in rs})
        # Mesmo precatório em mais de um ente = devedor solidário: conta uma vez.
        unicos = {(r['nr_precatorio'] or '', str(r['vl_causa'])): r for r in rs}
        rs_u = list(unicos.values())
        # "COMUM - PARCELADO" (6 em 24/10/2026) não cabe nos 16 caracteres.
        naturezas = sorted({re.sub(r'\s*-\s*PARCELADO$', '/PARC',
                                   (r['natureza'] or '').strip().upper())
                            for r in rs_u} - {''})
        item = ItemFila(
            chave=cnj, fonte=FONTE_POWERBI, entidade=entes[0],
            entidade_devedora=' / '.join(sorted({r['ente'].strip() for r in rs})),
            situacao=SITUACAO_PENDENTE, situacao_raw='lista cronológica',
            id_externo=';'.join(sorted({r['nr_precatorio'] or '' for r in rs_u}))[:32],
            valor_original=sum((_decimal(r['vl_causa']) for r in rs_u), Decimal('0.00')),
            ano_orcamento=min(r['exercicio_precatorio'] for r in rs_u),
            natureza='/'.join(naturezas)[:16],
            preferencia=super_por_cnj.get(cnj, '')[:32],
            ordem=min(r['ordem'] for r in rs_u),
            n_itens=len(rs_u),
        )
        out[entes[0]].append(item)
    return out


class ColetorTjpa:
    tribunal = TRIBUNAL
    #: Uma requisição traz o tribunal inteiro: com tudo completo, o pendente
    #: que sumiu da lista sai da fila (`execucao.executar`).
    cobertura_total = True

    def __init__(self, session=None, pausa=None, timeout=None, dormir=time.sleep,
                 pendentes_registrados: int | None = None):
        self.session = session or requests.Session()
        self.session.headers.update({
            'User-Agent': 'Voyager/1.0 (fila oficial de precatorios)',
            'Accept': 'application/json', 'X-PowerBI-ResourceKey': CHAVE_RELATORIO})
        self.pausa = max(1.0, float(pausa if pausa is not None
                                    else getattr(settings, 'FILA_OFICIAL_TJPA_PAUSA', 1.5)))
        self.timeout = timeout or getattr(settings, 'FILA_OFICIAL_TJPA_TIMEOUT', 180)
        self.dormir = dormir
        self.requisicoes = 0
        self._pendentes = pendentes_registrados
        self.modelo: dict = {}

    # -- HTTP --------------------------------------------------------------
    def _pedir(self, metodo: str, caminho: str, corpo=None) -> dict:
        """Uma requisição por vez, com pausa; rede/5xx tenta de novo 2 vezes."""
        url = f'{API}{caminho}'
        ultimo = None
        for espera in (0, 30, 60):
            if espera:
                self.dormir(espera)
            self.requisicoes += 1
            try:
                resp = self.session.request(
                    metodo, url, json=corpo, timeout=self.timeout,
                    headers={'ActivityId': str(uuid.uuid4()), 'RequestId': str(uuid.uuid4())})
            except requests.RequestException as e:
                ultimo = e
                logger.warning('%s %s: %s', metodo, caminho, e)
                self.dormir(self.pausa)
                continue
            self.dormir(self.pausa)
            if resp.status_code >= 500:
                ultimo = f'HTTP {resp.status_code}'
                continue
            if resp.status_code != 200:
                # 401/403/404: chave do relatório trocada ou link despublicado.
                raise LayoutDesconhecido(f'{caminho}: HTTP {resp.status_code}')
            try:
                return resp.json()
            except ValueError as e:
                raise LayoutDesconhecido(f'{caminho}: resposta não é JSON') from e
        raise Indisponivel(f'{caminho}: sem resposta depois de 3 tentativas ({ultimo})')

    def ler_modelo(self) -> dict:
        """Modelo + colunas. Diverge do medido → `LayoutDesconhecido`."""
        mae = self._pedir('GET', f'/{CHAVE_RELATORIO}/modelsAndExploration'
                                 '?preferReadOnlySession=true')
        modelos = mae.get('models') or []
        if len(modelos) != 1:
            raise LayoutDesconhecido(f'{len(modelos)} modelos no relatório (esperado 1)')
        m = modelos[0]
        if not m.get('id') or not m.get('dbName'):
            raise LayoutDesconhecido('modelo sem id/dbName')
        esquema = self._pedir('POST', '/conceptualschema', {'modelIds': [m['id']]})
        try:
            ents = {e['Name']: {p['Name'] for p in e.get('Properties', [])}
                    for e in esquema['schemas'][0]['schema']['Entities']}
        except (KeyError, IndexError, TypeError) as e:
            raise LayoutDesconhecido(f'conceptualschema fora do formato: {e!r}') from e
        for ent, cols in ((ENTIDADE_LISTA, COLUNAS_LISTA), (ENTIDADE_SUPER, COLUNAS_SUPER)):
            faltam = set(cols) - ents.get(ent, set())
            if faltam:
                raise LayoutDesconhecido(f'{ent}: colunas ausentes no modelo {sorted(faltam)}')
        self.modelo = {'model_id': m['id'], 'dataset_id': m['dbName'],
                       'atualizado_em': m.get('LastRefreshTime'),
                       'proxima_atualizacao': m.get('NextRefreshTime')}
        return self.modelo

    def ler_tabela(self, entidade: str, colunas: tuple[str, ...]) -> list[dict]:
        corpo = consulta(entidade, colunas, self.modelo['model_id'], self.modelo['dataset_id'])
        return decodificar_dsr(self._pedir('POST', '/querydata?synchronous=true', corpo), colunas)

    # -- coleta ------------------------------------------------------------
    def _piso(self) -> int:
        if self._pendentes is None:
            from tribunals.models import PrecatorioFilaOficial
            self._pendentes = PrecatorioFilaOficial.objects.filter(
                tribunal_id=TRIBUNAL, situacao=SITUACAO_PENDENTE).count()
        return int(self._pendentes * PISO_RELATIVO)

    def coletar(self) -> dict[str, list[ItemFila]]:
        """A lista inteira, já em itens por ente. Falha alto (ver o módulo)."""
        self.ler_modelo()
        linhas = self.ler_tabela(ENTIDADE_LISTA, COLUNAS_LISTA)
        if not linhas:
            raise LayoutDesconhecido('lista cronológica vazia')
        ruins = [m for m in (validar_linha(r) for r in linhas) if m]
        if ruins:
            raise LayoutDesconhecido(f'{len(ruins)} linhas fora do formato (ex.: {ruins[:3]})')
        cnjs = {r['nr_processo'].strip() for r in linhas}
        piso = self._piso()
        if len(cnjs) < piso:
            raise LayoutDesconhecido(
                f'lista encolheu: {len(cnjs)} processos contra piso de {piso} '
                f'({PISO_RELATIVO:.0%} dos pendentes registrados)')
        sup = {}
        desconhecidas = set()
        for r in self.ler_tabela(ENTIDADE_SUPER, COLUNAS_SUPER):
            mod = normalizar_entidade(r.get('modalidade') or '')
            cnj = str(r.get('nr_processo') or '').strip()
            if cnj in cnjs and mod:
                if mod not in PREFERENCIAS:
                    desconhecidas.add(mod)
                sup[cnj] = PREFERENCIAS.get(mod, mod.title())
        if desconhecidas:
            logger.warning('fila oficial TJPA: modalidades desconhecidas %s', sorted(desconhecidas))
        self.modelo.update({'linhas': len(linhas), 'processos': len(cnjs),
                            'superpreferencias': len(sup),
                            'coletado_em': datetime.now(UTC).isoformat()})
        return montar_itens(linhas, sup)

    def iterar(self, entidades=None,
               pular: Callable[[str], bool] | None = None,
               falhas: list | None = None) -> Iterator[ResultadoEntidade]:
        """Uma `ResultadoEntidade` por ente. Falha da fonte = nenhum resultado
        e a falha em `falhas` (o run fecha `failed`, nada sai da fila)."""
        falhas = falhas if falhas is not None else []
        try:
            por_ente = self.coletar()
        except (Indisponivel, LayoutDesconhecido, requests.RequestException) as e:
            logger.error('fila oficial TJPA: coleta falhou: %s', e)
            falhas.append(f'powerbi: {e}'[:500])
            return
        if entidades:
            alvo = {normalizar_entidade(str(e)) for e in entidades}
            por_ente = {k: v for k, v in por_ente.items() if k in alvo}
        for ente, itens in sorted(por_ente.items()):
            chave = f'powerbi:{ente}'
            if pular and pular(chave):
                continue
            yield ResultadoEntidade(entidade=ente, itens=itens, completa=True,
                                    info={'chave': chave, **self.modelo})
