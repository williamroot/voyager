"""Varredura do acervo declarado ao CNJ (Datajud) → índice `voyager-acervo`.

POR QUE ISSO EXISTE
-------------------
A única porta de entrada da base sempre foi o DJEN, que é um veículo de
**comunicação**, não um cadastro de processos: só aparece lá o processo que teve
intimação publicada em diário. Medido em 14/08/2026, com amostra aleatória de
300 CNJs por tribunal conferida um a um:

    TJSP 96,2% ausente · TJMG 85,7% · TJRJ 89,0% · TRF1 80,7%

e no recorte que é o nosso produto (classe 12078, Cumprimento de Sentença contra
a Fazenda Pública) a cobertura era **5,2% no TJSP** e 28,8% no TJMG. Somando os
60 tribunais que rastreamos, o CNJ declara **343.235.554** processos contra os
71,4M que tínhamos.

O Datajud não substitui o DJEN: ele **não traz parte, advogado nem valor**. O que
ele dá é o ESQUELETO — CNJ, classe, assunto, órgão julgador, datas. Isso basta
pra duas coisas que valem muito: (1) saber que o processo existe, e (2) ESCOLHER
quem mandar pro enricher, que é o gargalo real (112.820 processos/dia, e só 16
tribunais têm enricher — varrer 343M com ele levaria 8 anos).

COMO PAGINA (a parte não-óbvia)
-------------------------------
O índice do Datajud só aceita `sort` por `@timestamp`; ordenar por `_id` ou
`numeroProcesso` devolve 400 ("Fielddata is disabled"). Isso tem duas
consequências, e as duas mordem:

  1. `search_after` com chave NÃO-ÚNICA pula documento. Se a página termina no
     meio de um empate de milissegundo, os irmãos do empate somem pra sempre.
     Por isso paginamos com `range: {@timestamp: {gte: cursor}}` e RELEMOS a
     cauda de propósito — o `_id` do doc (`TJMG_G1_<cnj>`, dado pelo próprio
     Datajud) faz a reescrita ser idempotente.

  2. Se um único milissegundo tiver mais docs que a página inteira, o cursor
     nunca avança e o laço trava. `_desempatar_ms` cuida disso: fatia aquele
     milissegundo por `grau` e, se preciso, por `classe.codigo`, e só então
     empurra o cursor 1ms adiante. Quando nem assim couber, ele REGISTRA quantos
     ficaram de fora em vez de seguir fingindo que varreu tudo.

`@timestamp` é a data da última atualização do processo no Datajud, então o
mesmo cursor serve de watermark incremental: a passada seguinte pede
`gte último_cursor` e traz só o que nasceu ou mudou. Ver `.ia/INGESTION.md`.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from django.conf import settings
from django.utils import timezone as djtz
from elasticsearch.helpers import bulk

from search.client import get_es, index_name
from search.index import ensure_index
from tribunals.cnj import sigla_do_cnj
from tribunals.models import Tribunal

from .client import DatajudClient

logger = logging.getLogger('voyager.datajud.varredura')

#: teto do Elasticsearch por página (`index.max_result_window`). Medido:
#: 10.000 docs em ~10s, contra 100 docs em ~1s — 100× o dado pelo mesmo custo de
#: requisição. Como o limite que nos aperta é rpm (chave compartilhada) e não
#: banda, página cheia é o que torna a puxada nacional viável em horas.
PAGINA = 10_000

#: `index.max_result_window` do Datajud: `from + size` não passa disso. É o teto
#: de quanto dá pra ver dentro de UM milissegundo antes de precisar fatiar.
TETO_JANELA = 10_000

#: quantas vezes insistir quando a cota compartilhada do Datajud está cheia.
#: Repetir página é de graça (a paginação é idempotente), e desistir na primeira
#: negativa mataria um job de horas por um aperto de segundos.
TENTATIVAS_COTA = 12

#: `movimentos` fica DE FORA: são ~73 por processo (~15KB), o que faria cada
#: página pesar ~150MB em vez de ~4MB. Movimento vem na hidratação, por CNJ.
CAMPOS = [
    'numeroProcesso', 'grau', 'nivelSigilo', 'dataAjuizamento',
    'dataHoraUltimaAtualizacao', 'classe', 'assuntos', 'orgaoJulgador',
    'sistema', 'formato', 'tribunal', 'id',
]

INDICE = 'acervo'


def _fmt_cnj(digitos: str) -> str:
    d = digitos
    return f'{d[0:7]}-{d[7:9]}.{d[9:13]}.{d[13]}.{d[14:16]}.{d[16:20]}'


def _data(valor) -> str | None:
    """Datajud usa dois formatos de data e mistura os dois no mesmo doc.

    `dataAjuizamento` vem como `20221021175103` (14 dígitos, sem fuso) e
    `dataHoraUltimaAtualizacao` como ISO-8601. Devolver None em vez de arriscar
    um palpite: data errada em campo de data envenena agregação por ano.
    """
    if not valor:
        return None
    s = str(valor).strip()
    if len(s) == 14 and s.isdigit():
        try:
            return datetime.strptime(s, '%Y%m%d%H%M%S').replace(
                tzinfo=timezone.utc).isoformat()
        except ValueError:
            return None
    return s


def doc_do_datajud(src: dict) -> tuple[str, dict] | None:
    """`_source` do Datajud → (id, documento) do `voyager-acervo`.

    None quando o CNJ não tem 20 dígitos — sem CNJ o doc não casa com nada e
    seria lixo indexável.
    """
    num = (src.get('numeroProcesso') or '').strip()
    if len(num) != 20 or not num.isdigit():
        return None

    proc = _fmt_cnj(num)
    tribunal = (src.get('tribunal') or '').strip().upper()
    grau = (src.get('grau') or '').strip().upper()
    classe = src.get('classe') or {}
    orgao = src.get('orgaoJulgador') or {}
    assuntos = src.get('assuntos') or []
    if isinstance(assuntos, dict):            # visto no TJSP: objeto solto
        assuntos = [assuntos]
    # visto em campo: assuntos vindo como lista de listas
    plano = []
    for a in assuntos:
        plano.extend(a if isinstance(a, list) else [a])
    assuntos = [a for a in plano if isinstance(a, dict)]

    doc_id = src.get('id') or f'{tribunal}_{grau}_{num}'
    doc = {
        'proc': proc,
        'proc_digits': num,
        'tribunal': tribunal or (sigla_do_cnj(num) or ''),
        'uf': None,
        'grau': grau or None,
        'sistema': (src.get('sistema') or {}).get('nome'),
        'formato': (src.get('formato') or {}).get('nome'),
        'sigilo': src.get('nivelSigilo'),
        'classe_codigo': str(classe.get('codigo')) if classe.get('codigo') is not None else None,
        'classe_nome': classe.get('nome'),
        'assunto_codigos': [str(a['codigo']) for a in assuntos if a.get('codigo') is not None],
        'assunto_nomes': [a['nome'] for a in assuntos if a.get('nome')],
        'orgao_codigo': str(orgao.get('codigo')) if orgao.get('codigo') is not None else None,
        'orgao_nome': orgao.get('nome'),
        'municipio_ibge': (str(orgao.get('codigoMunicipioIBGE'))
                           if orgao.get('codigoMunicipioIBGE') is not None else None),
        'ajuizado_em': _data(src.get('dataAjuizamento')),
        'atualizado_em': _data(src.get('dataHoraUltimaAtualizacao')),
        'ano_cnj': int(num[9:13]),
        'varrido_em': djtz.now().isoformat(),
    }
    from search.geo import uf_do_tribunal          # import tardio: evita ciclo
    doc['uf'] = uf_do_tribunal(doc['tribunal'])
    return doc_id, {k: v for k, v in doc.items() if v is not None and v != []}


class Varredura:
    """Varre um tribunal do Datajud e grava o esqueleto no `voyager-acervo`."""

    def __init__(self, sigla: str, client: DatajudClient | None = None,
                 pagina: int = PAGINA, es=None, teto_ms: int = TETO_JANELA):
        self.sigla = sigla.upper()
        self.client = client or DatajudClient(prefer_cortex=False)
        self.pagina = pagina
        self.teto_ms = teto_ms
        self.es = es or get_es()
        self.indice = index_name(INDICE)
        self.lidos = 0
        self.gravados = 0
        self.perdidos = 0        # docs que um empate de ms nos impediu de ver
        self.paginas = 0
        self.esperas = 0         # quantas vezes a cota compartilhada segurou

    # -- Datajud ----------------------------------------------------------- #

    def _pedir(self, body: dict) -> dict:
        """Requisição ao Datajud tolerante a cota esgotada.

        A cota é compartilhada com o sync por processo, então em pico o token
        global demora mais que a espera máxima e o cliente levanta. Deixar isso
        subir MATA um job de horas por um aperto de segundos — foi o que
        aconteceu em 14-15/08/2026: 28 varreduras derrubadas por rate-limit.

        Como a paginação é idempotente (relê a cauda, `_id` do Datajud), repetir
        a mesma página é de graça. Espera e insiste; só desiste depois de
        `TENTATIVAS_COTA`, aí sim deixando o job falhar pro watchdog retomar do
        checkpoint.
        """
        from .client import DatajudClientError
        for tentativa in range(1, TENTATIVAS_COTA + 1):
            try:
                return self.client._post(self.sigla, body, cota='varredura')
            except DatajudClientError as e:
                if 'rate-limit' not in str(e) or tentativa == TENTATIVAS_COTA:
                    raise
                espera = min(60, 5 * tentativa)
                self.esperas += 1
                logger.info('varredura %s: cota cheia, esperando %ss (%d/%d)',
                            self.sigla, espera, tentativa, TENTATIVAS_COTA)
                time.sleep(espera)
        raise AssertionError('inalcançável')

    def _buscar(self, query: dict, size: int | None = None,
                desde: int = 0) -> list[dict]:
        body = {
            'size': size or self.pagina,
            'from': desde,
            '_source': CAMPOS,
            'sort': [{'@timestamp': {'order': 'asc'}}],
            'query': query,
        }
        if not desde:
            body.pop('from')
        d = self._pedir(body)
        return (d.get('hits') or {}).get('hits') or []

    def _contar(self, query: dict) -> int:
        d = self._pedir({'size': 0, 'track_total_hits': True, 'query': query})
        return ((d.get('hits') or {}).get('total') or {}).get('value') or 0

    # -- escrita ----------------------------------------------------------- #

    def _gravar(self, hits: list[dict]) -> int:
        acoes = []
        for h in hits:
            feito = doc_do_datajud(h.get('_source') or {})
            if not feito:
                continue
            doc_id, doc = feito
            acoes.append({'_op_type': 'index', '_index': self.indice,
                          '_id': doc_id, '_source': doc})
        if not acoes:
            return 0
        ok, erros = bulk(self.es, acoes, raise_on_error=False, stats_only=False)
        if erros:
            logger.warning('varredura %s: %d docs recusados pelo ES (ex: %s)',
                           self.sigla, len(erros), str(erros[0])[:200])
        return ok

    # -- empate de milissegundo -------------------------------------------- #

    def _varrer_recorte(self, query: dict) -> int:
        """Varre um recorte pequeno com `from`+`size` em vez de cursor.

        Só serve dentro de UM milissegundo, onde não há por onde avançar o
        cursor. O teto é o `index.max_result_window` do Datajud (10.000): além
        dele o `from` é recusado, e é aí que precisamos fatiar.
        """
        desde = 0
        lidos = 0
        while desde < self.teto_ms:
            size = min(self.pagina, self.teto_ms - desde)
            hits = self._buscar(query, size=size, desde=desde)
            if not hits:
                break
            self.lidos += len(hits)
            self.gravados += self._gravar(hits)
            lidos += len(hits)
            desde += len(hits)
            if len(hits) < size:
                break
        return lidos

    def _desempatar_ms(self, ms: int) -> int:
        """Puxa TUDO de um milissegundo que não coube numa página só.

        Primeiro tenta `from`+`size` (resolve até 10k docs no mesmo ms, que é o
        caso realista de uma carga em lote do tribunal). Se nem isso couber,
        fatia por `grau` e depois por `classe.codigo`. O que ainda assim sobrar
        é contabilizado em `self.perdidos` — a varredura prefere declarar a
        perda a mentir que varreu.
        """
        base = {'term': {'@timestamp': ms}}
        total = self._contar(base)
        logger.warning('varredura %s: %d docs no MESMO milissegundo (%s)',
                       self.sigla, total, ms)

        if total <= self.teto_ms:
            return self._varrer_recorte(base)

        vistos = 0
        lidos = 0
        for grau in ('G1', 'G2', 'JE', 'TR', None):
            q = {'bool': {'must': [base]}}
            if grau:
                q['bool']['filter'] = [{'term': {'grau': grau}}]
            else:
                q['bool']['must_not'] = [{'terms': {'grau': ['G1', 'G2', 'JE', 'TR']}}]
            n = self._contar(q)
            if not n:
                continue
            vistos += n
            if n <= self.teto_ms:
                lidos += self._varrer_recorte(q)
                continue
            # nem por grau coube: fatia por classe
            d = self._pedir({
                'size': 0, 'query': q,
                'aggs': {'c': {'terms': {'field': 'classe.codigo', 'size': 500}}},
            })
            cobertos = 0
            for b in (d.get('aggregations') or {}).get('c', {}).get('buckets', []):
                qc = {'bool': {'must': [base], 'filter': [
                    {'term': {'grau': grau}} if grau else {'match_all': {}},
                    {'term': {'classe.codigo': b['key']}}]}}
                lidos += self._varrer_recorte(qc)
                cobertos += min(b['doc_count'], self.teto_ms)
                if b['doc_count'] > self.teto_ms:
                    self.perdidos += b['doc_count'] - self.teto_ms
            self.perdidos += max(0, n - cobertos)
        self.perdidos += max(0, total - vistos)
        return lidos

    # -- laço principal ---------------------------------------------------- #

    def rodar(self, cursor: int | None = None, max_paginas: int | None = None,
              filtro: dict | None = None, on_page=None) -> dict:
        """Varre do `cursor` (epoch ms) em diante. Devolve o resumo da passada.

        `filtro` restringe o recorte (ex.: só a classe 12078) — é como a F4 puxa
        o nicho sem varrer o tribunal inteiro.
        """
        ensure_index(INDICE)
        cursor = int(cursor or 0)
        t0 = time.monotonic()
        parou_por = 'fim'

        while True:
            if max_paginas and self.paginas >= max_paginas:
                parou_por = 'max_paginas'
                break

            faixa = {'range': {'@timestamp': {'gte': cursor}}}
            query = ({'bool': {'filter': [faixa, filtro]}} if filtro else faixa)
            hits = self._buscar(query)
            self.paginas += 1
            if not hits:
                break

            self.lidos += len(hits)
            self.gravados += self._gravar(hits)
            ultimo = (hits[-1].get('sort') or [None])[0]

            if ultimo is None:
                logger.error('varredura %s: página sem `sort` — abortando', self.sigla)
                parou_por = 'sem_sort'
                break

            if ultimo == cursor and len(hits) >= self.pagina:
                # página inteira dentro do mesmo ms: o cursor não avançaria nunca
                self._desempatar_ms(cursor)
                cursor += 1
            elif len(hits) < self.pagina:
                cursor = ultimo + 1
                break                       # última página do recorte
            else:
                cursor = ultimo             # relê a cauda de propósito (idempotente)

            if on_page:
                on_page(self, cursor)

        dt = time.monotonic() - t0
        return {
            'tribunal': self.sigla, 'cursor': cursor, 'paginas': self.paginas,
            'lidos': self.lidos, 'gravados': self.gravados,
            'perdidos': self.perdidos, 'esperas': self.esperas,
            'segundos': round(dt, 1),
            'docs_por_s': round(self.lidos / dt, 1) if dt else 0,
            'parou_por': parou_por,
        }


def varrer_tribunal(sigla: str, retomar: bool = True, max_paginas: int | None = None,
                    filtro: dict | None = None) -> dict:
    """Varre um tribunal salvando o watermark no `Tribunal` a cada passada.

    `retomar=False` recomeça do zero (varredura completa); `True` continua de
    onde parou, que no regime permanente é o sync incremental (F5).
    """
    trib = Tribunal.objects.filter(sigla=sigla.upper()).first()
    if not trib:
        raise ValueError(f'tribunal desconhecido: {sigla}')

    cursor = trib.datajud_varredura_cursor if retomar else 0
    v = Varredura(trib.sigla)
    Tribunal.objects.filter(pk=trib.pk).update(datajud_varredura_status='rodando')

    # CHECKPOINT periódico. Sem isto, o watermark só era salvo no fim — e a
    # varredura do TJSP é um job de HORAS: um restart de worker (ou o
    # AbandonedJobError que o RQ dá quando o container morre) jogava fora todo o
    # progresso e a próxima passada recomeçava do zero. Aconteceu com o TJMG em
    # 14/08/2026, no deploy que subiu a frota de 4 pra 8 réplicas.
    # A cada 20 páginas = 200k docs ≈ 2 min de trabalho é o que se arrisca perder.
    # Só faz sentido na passada COMPLETA: filtrada não pode tocar o watermark.
    def checkpoint(v_, cursor_atual, _cada=20):
        if filtro or v_.paginas % _cada:
            return
        Tribunal.objects.filter(pk=trib.pk).update(
            datajud_varredura_cursor=cursor_atual,
            datajud_varredura_em=djtz.now(),
            datajud_varredura_status=f'rodando ({v_.lidos:,} lidos)')

    try:
        resumo = v.rodar(cursor=cursor, max_paginas=max_paginas, filtro=filtro,
                         on_page=checkpoint)
    except Exception as e:
        Tribunal.objects.filter(pk=trib.pk).update(
            datajud_varredura_status=f'erro: {str(e)[:80]}',
            datajud_varredura_em=djtz.now())
        raise
    # o cursor só avança se a passada foi do acervo INTEIRO: uma passada
    # filtrada (nicho) veria só parte do tempo e envenenaria o watermark
    campos = {
        'datajud_varredura_em': djtz.now(),
        'datajud_varredura_docs': (trib.datajud_varredura_docs or 0) + resumo['gravados'],
        'datajud_varredura_status': resumo['parou_por'],
    }
    if not filtro:
        campos['datajud_varredura_cursor'] = resumo['cursor']
    Tribunal.objects.filter(pk=trib.pk).update(**campos)
    logger.info('varredura %s: %s', sigla, resumo)
    return resumo


def marcar_no_acervo(sigla: str, lote: int = 5_000) -> dict:
    """Marca no `voyager-acervo` quem JÁ está no acervo rico.

    É o que a tela usa pra dizer "temos o processo" × "só sabemos que existe".
    Roda ES→ES, sem tocar no Datajud nem no Postgres.
    """
    es = get_es()
    alvo = index_name(INDICE)
    rico = index_name('processos')
    marcados = 0
    faltam = 0
    cursor = None
    while True:
        body = {'size': lote, 'sort': [{'proc': 'asc'}],
                'query': {'bool': {'filter': [{'term': {'tribunal': sigla.upper()}}]}},
                '_source': ['proc']}
        if cursor:
            body['search_after'] = cursor
        r = es.search(index=alvo, body=body)
        hits = r['hits']['hits']
        if not hits:
            break
        cursor = hits[-1]['sort']
        procs = [h['_source']['proc'] for h in hits]
        achados = set()
        for i in range(0, len(procs), 1000):
            fatia = procs[i:i + 1000]
            rr = es.search(index=rico, size=len(fatia), source=['proc'],
                           query={'terms': {'proc': fatia}})
            achados.update(h['_source']['proc'] for h in rr['hits']['hits'])
        acoes = [{'_op_type': 'update', '_index': alvo, '_id': h['_id'],
                  'doc': {'no_acervo': h['_source']['proc'] in achados}}
                 for h in hits]
        ok, _ = bulk(es, acoes, raise_on_error=False)
        marcados += ok
        faltam += sum(1 for p in procs if p not in achados)
    return {'tribunal': sigla.upper(), 'marcados': marcados, 'so_esqueleto': faltam}
