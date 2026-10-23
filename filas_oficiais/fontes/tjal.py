"""Coletor da fila oficial de precatórios do TJAL: SAPRE + listas em PDF.

As duas fontes são públicas, sem login e sem captcha (base
`https://precatorios.tjal.jus.br`, achada no bundle da página
`tjal.jus.br/servicos/precatorios` em 23/10/2026):

- SAPRE, `POST /api/sapre/precatorios/por-entidade?page=N` — JSON paginado de
  10 em 10 por entidade, com CNJ do precatório, origem, valor de inscrição,
  natureza, situação e ano. Só devolve a fila pendente (em 3.626 linhas
  coletadas, só as situações 3 "Cadastrado" e 21 "Autuado"). Lento (~10 s por
  página) e o ESTADO DE ALAGOAS (id 28) dá 504 até fatiado pela busca.
- Listas em PDF por regime, `GET /api/precatorios/por-regime?regime=e|g` +
  `GET /api/precatorios/baixar/<id>` — a "Lista Unificada de Precatórios
  Pendentes de Pagamento" de cada entidade, com valor atualizado e ordem.

Nenhuma das duas sozinha é a fila: 0500371-61.2022.8.02.9003 e
0500028-42.2023.8.02.0043 estão na lista de Delmiro Gouveia e não no SAPRE; o
Estado só sai pelo PDF. A união é a verdade (Delmiro: 121 chaves, 119 do SAPRE
e 2 só da lista).

Mesma lógica do coletor validado no JURISCOPE
(`datamodel/completude/tjal.py`), com três diferenças: o credor (nome e CPF)
é descartado na leitura (LGPD, ver `filas_oficiais/base.py`); a coleta é um
gerador por entidade (regra nº 1 do CLAUDE.md: itere, não acumule); e cada
entidade diz se veio COMPLETA (todas as fontes dela responderam), que é o que
autoriza marcar quem saiu da fila.

Carga: uma requisição por vez e pausa de `FILA_OFICIAL_TJAL_PAUSA` (≥ 1 s).
"""
from __future__ import annotations

import hashlib
import logging
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings

from filas_oficiais.base import (
    SITUACAO_EXTINTO,
    SITUACAO_OUTRO,
    SITUACAO_PAGO,
    SITUACAO_PENDENTE,
    ItemFila,
    ResultadoEntidade,
    normalizar_chave,
    normalizar_entidade,
    segmento_tribunal,
)

logger = logging.getLogger('voyager.filas_oficiais.tjal')

BASE_URL = 'https://precatorios.tjal.jus.br'
TRIBUNAL = 'TJAL'
FONTE_SAPRE = 'tjal_sapre'
FONTE_LISTA = 'tjal_lista_pdf'
FONTE_UNIAO = 'tjal_sapre+lista'
SEGMENTO_TJAL = '8.02'

# `situacaoprecatorio` do SAPRE → situação normalizada. A tabela é a `Va` do
# bundle da página (Precatorios-*.js, 23/10/2026). Conferido contra as listas:
# os itens em 3 "Cadastrado" estão nas listas de PENDENTES (Delmiro 80 de 80,
# Maceió 734 de 734, Canapi 46 de 46); os 50 da lista de PAGOS de Maceió que o
# SAPRE ainda dá como 3 são parcela superpreferencial ("P I", R$ 85.800,00
# cada) — o saldo continua na fila, e pendente é o certo. Parcial e
# provisionamento ainda devem; sequestro é cobrança em curso.
SITUACOES_SAPRE = {
    0: (SITUACAO_OUTRO, 'Importado do Saj'),
    1: (SITUACAO_PENDENTE, 'Pendente'),
    2: (SITUACAO_PENDENTE, 'Pré-cadastrado'),
    3: (SITUACAO_PENDENTE, 'Cadastrado'),
    4: (SITUACAO_EXTINTO, 'Extinto'),
    5: (SITUACAO_OUTRO, 'Com pagamento'),       # ambíguo (total ou parcial)
    6: (SITUACAO_PENDENTE, 'Com pagamento parcial'),
    7: (SITUACAO_EXTINTO, 'Recusado'),
    8: (SITUACAO_EXTINTO, 'Excluído'),
    9: (SITUACAO_OUTRO, 'Agrupado'),
    10: (SITUACAO_PENDENTE, 'Sequestro'),
    11: (SITUACAO_PENDENTE, 'Manifestação de cálculo'),
    12: (SITUACAO_PENDENTE, 'Reserva de valores'),
    13: (SITUACAO_PENDENTE, 'Pagamento parcial - retenções a recolher'),
    14: (SITUACAO_PENDENTE, 'Pagamento parcial - portaria 381/2012'),
    15: (SITUACAO_PENDENTE, 'Pagamento parcial - preferencial'),
    16: (SITUACAO_PAGO, 'Pago'),
    17: (SITUACAO_PENDENTE, 'Suspenso'),
    18: (SITUACAO_PENDENTE, 'Reserva de valores - pagamento parcial'),
    19: (SITUACAO_PENDENTE, 'Requisitado o valor'),
    20: (SITUACAO_OUTRO, 'Desmembrado'),
    21: (SITUACAO_PENDENTE, 'Autuado'),
    22: (SITUACAO_PENDENTE, 'Aguardando assinatura magistrado'),
    23: (SITUACAO_PENDENTE, 'Provisionamento total'),
    24: (SITUACAO_PENDENTE, 'Provisionamento parcial'),
    25: (SITUACAO_PENDENTE, 'Provisionamento p/ acordo'),
    26: (SITUACAO_PAGO, 'Pagamento acordo'),
    27: (SITUACAO_EXTINTO, 'Baixado com saldo'),
    28: (SITUACAO_PAGO, 'Com pagamento total'),
    29: (SITUACAO_PENDENTE, 'Aguardando análise magistrado'),
    30: (SITUACAO_PENDENTE, 'Requisição expedida'),
    31: (SITUACAO_PENDENTE, 'Intimação das partes'),
}
NATUREZAS_SAPRE = {0: 'ALIMENTAR', 1: 'COMUM'}


def situacao_sapre(codigo) -> tuple[str, str]:
    try:
        return SITUACOES_SAPRE[int(codigo)]
    except (TypeError, ValueError, KeyError):
        return SITUACAO_OUTRO, f'desconhecida ({codigo})'


def _decimal(valor) -> Decimal | None:
    if valor in (None, ''):
        return None
    s = str(valor).strip()
    if ',' in s:                       # "1.305.178,15" (PDF)
        s = s.replace('.', '').replace(',', '.')
    try:
        return Decimal(s).quantize(Decimal('0.01'))
    except InvalidOperation:
        return None


def e_numero_de_origem(chave: str) -> bool:
    """O "número do precatório" é o de um processo de 1º grau.

    Precatório do TJAL é autuado no foro 9003 (Presidência) ou, na comarca,
    na numeração 050xxxx. Fora disso a fonte está dando o próprio processo de
    origem (precatório antigo, sem autuação própria — classe 156/7).
    """
    if not chave:
        return False
    return not (chave.endswith('.9003') or chave.startswith('050'))


def _dedup(documento: str, valor) -> tuple:
    doc = re.sub(r'\D', '', documento or '')
    # Só o hash, e só em memória: o documento não sai desta função.
    return (hashlib.sha256(doc.encode()).hexdigest()[:16] if doc else '', valor)


# ---------------------------------------------------------------------------
# SAPRE
# ---------------------------------------------------------------------------

def item_do_sapre(linha: dict, entidade: str) -> ItemFila | None:
    """Uma linha do SAPRE como `ItemFila`. Nome e CPF do requerente ficam fora."""
    chave = normalizar_chave(linha.get('nuprocessosaj'))
    if not chave:
        return None
    situacao, raw = situacao_sapre(linha.get('situacaoprecatorio'))
    origem = normalizar_chave(linha.get('numprocessoexecucao')
                              or linha.get('numprocessoconhecimento'))
    origens = {normalizar_chave(linha.get('numprocessoexecucao')),
               normalizar_chave(linha.get('numprocessoconhecimento'))}
    devedora = ((linha.get('entidade_devedora') or {}).get('nome') or '')
    ordem = (linha.get('ultima_classificacao_orcamentaria') or {}).get('ordemclassificacao')
    try:
        ano = int(linha.get('anoorcamento')) if linha.get('anoorcamento') else None
    except (TypeError, ValueError):
        ano = None
    valor = _decimal(linha.get('valorglobal'))
    return ItemFila(
        chave=chave, fonte=FONTE_SAPRE, entidade=entidade,
        entidade_devedora=normalizar_entidade(devedora),
        situacao=situacao, situacao_raw=raw,
        chave_e_origem=chave in origens or e_numero_de_origem(chave),
        id_externo=str(linha.get('id') or ''),
        valor_original=valor,
        ano_orcamento=ano,
        natureza=NATUREZAS_SAPRE.get(linha.get('naturezaprecatorio'), ''),
        origem_cnj='' if origem == chave else origem,
        ordem=ordem if isinstance(ordem, int) else None,
        _dedup=_dedup(linha.get('cpfcnpjrequerente'), valor),
    )


def agrupar_por_chave(itens: list[ItemFila]) -> list[ItemFila]:
    """Uma linha por chave, somando as requisições da mesma chave.

    O SAPRE repete linhas idênticas (mesmo documento e valor) e lista o
    precatório antigo uma vez por beneficiário com o número da origem. A
    idêntica sai; as demais somam valor e contam em `n_itens`. Pendente vence:
    basta uma requisição devida para a chave estar na fila.
    """
    grupos: dict[str, list[ItemFila]] = {}
    for it in itens:
        grupos.setdefault(it.chave, []).append(it)
    out = []
    for lista in grupos.values():
        vistos, unicos = set(), []
        for it in lista:
            if it._dedup in vistos:
                continue
            vistos.add(it._dedup)
            unicos.append(it)
        base = unicos[0]
        if len(unicos) > 1:
            base.n_itens = len(unicos)
            valores = [u.valor_original for u in unicos if u.valor_original is not None]
            base.valor_original = sum(valores, Decimal('0')) if valores else None
            if any(u.situacao == SITUACAO_PENDENTE for u in unicos):
                base.situacao = SITUACAO_PENDENTE
                base.situacao_raw = next(u.situacao_raw for u in unicos
                                         if u.situacao == SITUACAO_PENDENTE)
        out.append(base)
    return out


# ---------------------------------------------------------------------------
# Listas em PDF
# ---------------------------------------------------------------------------

_RE_CNJ_QUEBRADO = re.compile(r'(\d{7})-\s*(\d{2}\.\d{4}\.\d\.\d{2}\.\d{4})')
_RE_NATUREZA = re.compile(r'(Alimentar|Comum)\s+(\d{4})(?:\s+(TJ-?AL|TRT-?\s?\d{2}|TRF-?\s?\d))?(?!\d)')
_RE_VALOR = re.compile(r'(?:R\$\s*([\d.]+,\d{2})|([\d.]+,\d{2})\s*R\$)')
_RE_ORDINAL = re.compile(r'(?:^|\n|\s)(?=\d{1,5}º\s)')


def ler_lista_pdf(texto: str, entidade: str, regime: str = '') -> list[ItemFila]:
    """Itens do PRÓPRIO TJAL de uma lista de pendentes (texto do PDF).

    O layout muda de entidade para entidade (com e sem coluna de origem, CNJ
    quebrado em duas linhas, linha repetida na virada de página), então a
    leitura é por registro, não por linha: cada registro começa no ordinal
    ("12º") e tem, nesta ordem, o número, a natureza seguida do orçamento e o
    valor; a origem, quando a coluna existe, vem depois da natureza. O nome do
    credor, que a lista também traz, não é lido.

    Repasses de outros tribunais (TRT19, TRF5 — a lista é unificada) e linhas
    sem CNJ ficam de fora: não são precatórios do TJAL.
    """
    itens: dict[str, ItemFila] = {}
    for bloco in _RE_ORDINAL.split(texto or ''):
        m = re.match(r'(\d{1,5})º\s', bloco)
        if not m:
            continue
        nat = _RE_NATUREZA.search(bloco)
        if not nat:
            continue
        num = _RE_CNJ_QUEBRADO.search(bloco[:nat.start()])
        if not num:
            continue
        chave = normalizar_chave(f'{num.group(1)}-{num.group(2)}')
        if segmento_tribunal(chave) != SEGMENTO_TJAL:
            continue
        depois = bloco[nat.end():]
        valor = _RE_VALOR.search(depois)
        origem = ''
        if not nat.group(3):
            o = _RE_CNJ_QUEBRADO.search(depois)
            if o:
                origem = normalizar_chave(f'{o.group(1)}-{o.group(2)}')
        if chave in itens:          # linha repetida na virada de página
            continue
        itens[chave] = ItemFila(
            chave=chave, fonte=FONTE_LISTA, entidade=entidade,
            situacao=SITUACAO_PENDENTE, situacao_raw='Lista de pendentes',
            chave_e_origem=e_numero_de_origem(chave),
            valor_atualizado=_decimal((valor.group(1) or valor.group(2)) if valor else None),
            ano_orcamento=int(nat.group(2)),
            natureza=nat.group(1).upper(),
            origem_cnj='' if origem == chave else origem,
            ordem=int(m.group(1)), regime=regime,
        )
    return list(itens.values())


def texto_do_pdf(conteudo: bytes) -> str:
    """Texto do PDF na ordem do conteúdo.

    `pdftotext -raw` quando o binário existe; senão o PyMuPDF (`get_text()`
    sem `sort`, que também segue a ordem do conteúdo). NÃO o pdfplumber: nestas
    listas ele monta a célula quebrada em duas linhas EM VOLTA da linha do
    ordinal e duplica o texto em negrito — medido no JURISCOPE em 23/10/2026,
    Estado, Maceió e Canapi davam 0 itens. As duas leituras aceitas dão o mesmo
    conjunto de chaves nas 8 listas baixadas em 23/10/2026 (ver
    `tests/test_fila_oficial_tjal.py` e a seção da fila oficial em
    `.ia/INGESTION.md`).
    """
    if shutil.which('pdftotext'):
        r = subprocess.run(['pdftotext', '-raw', '-enc', 'UTF-8', '-', '-'],
                           input=conteudo, capture_output=True, timeout=300, check=False)
        if r.returncode == 0:
            return r.stdout.decode('utf-8', errors='replace')
        logger.warning('pdftotext falhou (%s): %s', r.returncode, r.stderr[:200])
    import fitz  # PyMuPDF — import tardio: só a coleta de PDF precisa

    with fitz.open(stream=conteudo, filetype='pdf') as doc:
        return '\n'.join(page.get_text() for page in doc)


def unir(sapre: list[ItemFila], lista: list[ItemFila]) -> list[ItemFila]:
    """SAPRE + lista, uma linha por chave (união).

    O SAPRE é o sistema vivo: situação, natureza, origem, valor de inscrição e
    entidade vêm dele. A lista acrescenta o valor atualizado, a ordem e o
    regime. Item só da lista entra como pendente — ela é a lista de pendentes.
    """
    por_chave = {it.chave: it for it in sapre}
    for it in lista:
        s = por_chave.get(it.chave)
        if s is None:
            por_chave[it.chave] = it
            continue
        s.fonte = FONTE_UNIAO
        s.valor_atualizado = it.valor_atualizado
        s.ordem = it.ordem if it.ordem is not None else s.ordem
        s.regime = it.regime or s.regime
        s.origem_cnj = s.origem_cnj or it.origem_cnj
    return list(por_chave.values())


# ---------------------------------------------------------------------------
# Coletor
# ---------------------------------------------------------------------------

class Indisponivel(Exception):  # noqa: N818 — mesmo nome do coletor validado no JURISCOPE
    """504/timeout: a consulta pesada demais para o SAPRE (fatiar)."""


class ForaDoAr(Exception):  # noqa: N818
    """5xx que não é 504, ou rede, depois das retentativas: a fonte caiu.

    NÃO se fatia (fatiar uma fonte fora do ar multiplica a espera por 100 —
    medido em 23/10/2026: o SAPRE respondeu 500 "Algo deu errado" em todo
    `por-entidade` e em `entidades` por mais de uma hora, com as listas em PDF
    no ar). A entidade fica incompleta e o SAPRE é pulado no resto da coleta.
    """


class ColetorTjal:
    tribunal = TRIBUNAL

    def __init__(self, session=None, pausa=None, timeout=None, dormir=time.sleep,
                 profundidade=None, so_pdf=None):
        self.session = session or requests.Session()
        self.session.headers.setdefault(
            'User-Agent', 'Voyager/1.0 (fila oficial de precatorios)')
        self.pausa = max(1.0, float(pausa if pausa is not None
                                    else getattr(settings, 'FILA_OFICIAL_TJAL_PAUSA', 1.5)))
        self.timeout = timeout or getattr(settings, 'FILA_OFICIAL_TJAL_TIMEOUT', 120)
        self.dormir = dormir
        self.profundidade = (profundidade if profundidade is not None
                             else getattr(settings, 'FILA_OFICIAL_TJAL_PROFUNDIDADE', 2))
        self.so_pdf = set(so_pdf if so_pdf is not None
                          else getattr(settings, 'FILA_OFICIAL_TJAL_SO_PDF', [28]))
        self.requisicoes = 0
        self.sapre_fora = False

    # -- HTTP --------------------------------------------------------------
    def _pedir(self, metodo, caminho, **kw):
        """Uma requisição por vez, com pausa. 504/timeout viram `Indisponivel`;
        outro erro de rede tenta de novo duas vezes (30 s, 60 s)."""
        url = f'{BASE_URL}{caminho}'
        for espera in (0, 30, 60):
            if espera:
                self.dormir(espera)
            self.requisicoes += 1
            try:
                resp = self.session.request(metodo, url, timeout=self.timeout, **kw)
            except requests.Timeout as e:
                self.dormir(self.pausa)
                raise Indisponivel(str(e)) from e
            except requests.RequestException as e:
                logger.warning('%s %s: %s', metodo, caminho, e)
                self.dormir(self.pausa)
                continue
            self.dormir(self.pausa)
            if resp.status_code == 504:
                raise Indisponivel(f'504 em {caminho}')
            if resp.status_code >= 500:
                logger.warning('%s %s: HTTP %s', metodo, caminho, resp.status_code)
                continue
            resp.raise_for_status()
            return resp
        raise ForaDoAr(f'{caminho}: sem resposta depois de 3 tentativas')

    def entidades(self) -> list[dict]:
        return self._pedir('GET', '/api/sapre/precatorios/entidades').json()

    def _pagina(self, entidade_id, busca, pagina):
        corpo = {'idEntidadePrincipal': entidade_id, 'idEntidadeDevedora': None,
                 'busca': busca, 'cpf': None}
        return self._pedir('POST', f'/api/sapre/precatorios/por-entidade?page={pagina}',
                           json=corpo).json()

    def _filhos(self, busca):
        """Fatias da busca (substring de `nuprocessosaj`): primeiro o dígito
        verificador ("-07."), depois um dígito a mais à esquerda por nível."""
        if busca is None:
            return [f'-{i:02d}.' for i in range(100)]
        nivel = len(busca.split('-')[0])
        if nivel >= self.profundidade:
            return None
        return [f'{d}{busca}' for d in range(10)]

    def linhas_da_entidade(self, entidade_id) -> tuple[list[dict], bool]:
        """Todas as linhas do SAPRE da entidade. (linhas, completo).

        Consulta inteira; no 504, fatia pela busca (Maceió saiu inteira com 182
        consultas em 23/10/2026). Fatia que ainda dá 504 no último nível deixa
        a entidade incompleta — é registrado, nunca corte mudo.
        """
        linhas, completo = {}, True
        if self.sapre_fora:
            return [], False
        pendentes = [None]
        while pendentes:
            busca = pendentes.pop(0)
            try:
                d = self._pagina(entidade_id, busca, 1)
                for p in range(2, int(d.get('last_page') or 1) + 1):
                    for r in self._pagina(entidade_id, busca, p).get('data') or []:
                        linhas[r['id']] = r
                for r in d.get('data') or []:
                    linhas[r['id']] = r
            except Indisponivel:
                filhos = self._filhos(busca)
                if filhos is None:
                    logger.warning('entidade %s: fatia %r indisponível', entidade_id, busca)
                    completo = False
                    continue
                pendentes.extend(filhos)
            except ForaDoAr as e:
                logger.warning('SAPRE fora do ar na entidade %s: %s', entidade_id, e)
                self.sapre_fora = True
                return list(linhas.values()), False
        return list(linhas.values()), completo

    def listas_pendentes(self) -> dict[str, dict]:
        """{entidade normalizada: {'id', 'data', 'regime', 'nome'}} — a lista de
        pendentes mais recente de cada entidade, nos dois regimes."""
        out = {}
        for regime, rotulo in (('e', 'especial'), ('g', 'geral')):
            indice = self._pedir('GET', '/api/precatorios/por-regime',
                                 params={'regime': regime}).json() or {}
            for nome, bloco in indice.items():
                docs = ((bloco or {}).get('dados') or {}).get('pendente') or []
                melhor = None
                for doc in docs:
                    try:
                        quando = datetime.strptime(doc.get('data_envio') or '', '%d/%m/%Y')
                    except ValueError:
                        quando = datetime.min
                    if doc.get('id') and (melhor is None or quando > melhor[0]):
                        melhor = (quando, doc)
                if melhor:
                    out[normalizar_entidade(nome)] = {
                        'id': melhor[1]['id'], 'data': melhor[1].get('data_envio'),
                        'regime': rotulo, 'nome': nome}
        return out

    def itens_da_lista(self, lista: dict, entidade: str) -> list[ItemFila]:
        conteudo = self._pedir('GET', f'/api/precatorios/baixar/{lista["id"]}').content
        return ler_lista_pdf(texto_do_pdf(conteudo), entidade, lista.get('regime', ''))

    # -- coleta ------------------------------------------------------------
    def _lista(self, listas, nome, res: ResultadoEntidade) -> tuple[list, bool]:
        if nome not in listas:
            return [], True             # entidade sem lista: nada a faltar
        try:
            itens = self.itens_da_lista(listas[nome], nome)
        except (Indisponivel, ForaDoAr, requests.RequestException, ValueError) as e:
            res.falhas.append(f'lista {nome}: {e}')
            return [], False
        res.info['lista'] = {'id': listas[nome]['id'], 'data': listas[nome]['data'],
                             'itens': len(itens)}
        return itens, True

    def iterar(self, entidades=None,  # noqa: PLR0912
               pular: Callable[[str], bool] | None = None,
               falhas: list | None = None) -> Iterator[ResultadoEntidade]:
        """Uma `ResultadoEntidade` por entidade, na hora em que ela termina.

        `entidades`: ids do SAPRE ou nomes (normalizados) para restringir.
        `pular(chave_da_entidade)`: retomada — True pula a entidade.
        `falhas`: recebe as falhas que não são de uma entidade só (índices).
        """
        falhas = falhas if falhas is not None else []
        try:
            listas = self.listas_pendentes()
            listas_ok = True
        except (Indisponivel, ForaDoAr, requests.RequestException, ValueError) as e:
            logger.warning('índice das listas indisponível: %s', e)
            listas, listas_ok = {}, False
            falhas.append(f'índice das listas: {e}')
        try:
            ents = self.entidades()
        except (Indisponivel, ForaDoAr, requests.RequestException, ValueError) as e:
            falhas.append(f'entidades do SAPRE: {e}')
            ents = []
            self.sapre_fora = True
        if entidades:
            alvo = {str(e) for e in entidades} | {normalizar_entidade(str(e)) for e in entidades}
            ents = [e for e in ents if str(e.get('id')) in alvo
                    or normalizar_entidade(e.get('nome')) in alvo]
            nomes = {normalizar_entidade(e.get('nome')) for e in ents}
            listas = {k: v for k, v in listas.items() if k in nomes or k in alvo}

        cobertas = set()
        for ent in ents:
            eid, nome = ent.get('id'), normalizar_entidade(ent.get('nome'))
            cobertas.add(nome)
            chave_ent = f'sapre:{eid}'
            if pular and pular(chave_ent):
                continue
            res = ResultadoEntidade(entidade=nome, itens=[], completa=False,
                                    info={'chave': chave_ent, 'id_sapre': eid})
            sapre_itens, sapre_ok = [], False
            if eid in self.so_pdf:
                res.info['sapre'] = 'só PDF (504 no SAPRE)'
            else:
                linhas, sapre_ok = self.linhas_da_entidade(eid)
                sapre_itens = agrupar_por_chave(
                    [i for i in (item_do_sapre(r, nome) for r in linhas) if i])
                res.info['sapre'] = len(sapre_itens) if sapre_ok else f'{len(sapre_itens)} (incompleto)'
                if not sapre_ok:
                    res.falhas.append(f'entidade {eid} {nome}: SAPRE incompleto')
            lista_itens, lista_ok = self._lista(listas, nome, res)
            if eid in self.so_pdf and nome not in listas:
                lista_ok = False
                res.falhas.append(f'entidade {eid} {nome}: só PDF e sem lista no índice')
            # Completa = toda fonte que a entidade tem respondeu inteira (e o
            # índice das listas respondeu: sem ele não se sabe se há lista).
            res.completa = ((sapre_ok or eid in self.so_pdf) and lista_ok and listas_ok)
            res.itens = unir(sapre_itens, lista_itens)
            yield res
        # Entidade que só tem lista (nome que não casa com o SAPRE).
        for nome in listas:
            if nome in cobertas:
                continue
            chave_ent = f'lista:{nome}'
            if pular and pular(chave_ent):
                continue
            res = ResultadoEntidade(entidade=nome, itens=[], completa=False,
                                    info={'chave': chave_ent, 'sapre': None})
            itens, ok = self._lista(listas, nome, res)
            # Sem a relação de entidades do SAPRE não há como saber se esta
            # lista tem par no SAPRE: nunca completa (não marca fora_da_fila).
            res.itens, res.completa = itens, ok and not (self.sapre_fora and not ents)
            if self.sapre_fora and not ents:
                res.falhas.append(f'lista {nome}: SAPRE fora do ar, sem a relação de entidades')
            yield res
