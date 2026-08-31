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

from . import telemetria
from .client import DatajudClient, DatajudPaginaGrandeError

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

#: PISO da página adaptativa. Abaixo disto o custo por requisição (rpm, que é o
#: recurso escasso) domina o custo por byte e a puxada deixa de caber em horas.
PISO_PAGINA = 100

#: Sonda: a PRIMEIRA requisição de cada passada pede poucos docs só para PESAR o
#: tribunal antes de comprometer memória. Foi a falta disso que produziu o OOM da
#: coleta do DJEN — lá o comentário dizia "3 KB por publicação" e a medição deu
#: 56 KB, 27× de erro. Aqui o `_source` é curto, mas o multiplicador (10.000 docs
#: por página) é grande o bastante para que a diferença entre prever e MEDIR seja
#: a diferença entre 4 MB e um worker morto.
PAGINA_SONDA = 500

#: Quanto a página pode CRESCER de uma calibração para a outra. Encolher é
#: imediato (o risco é agora); crescer é gradual (o ganho pode esperar).
CRESCIMENTO_MAX = 4.0

#: Peso lembrado decai a cada página, senão um único outlier prende a página no
#: piso para sempre — o erro que a coleta do DJEN cometeu com o "máximo de todos
#: os tempos".
DECAIMENTO_PESO = 0.75


def _bytes_alvo() -> int:
    """ORÇAMENTO de bytes por resposta: a previsão que dimensiona a página.

    Lido tarde (não no import) para que `override_settings` funcione e para que
    um cliente montado sem `__init__` continue tendo orçamento.
    """
    return int(getattr(settings, 'DATAJUD_VARREDURA_BYTES_ALVO', 16 * 1024 * 1024))



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
                 pagina: int = PAGINA, es=None, teto_ms: int = TETO_JANELA,
                 escrever: bool = True, parar=None, telemetria_ativa: bool = True):
        self.sigla = sigla.upper()
        self.client = client or DatajudClient(prefer_cortex=False)
        #: TETO da página. O tamanho REAL de cada requisição sai de
        #: `_proxima_pagina()`, que divide o orçamento de bytes pelo peso MEDIDO
        #: do doc deste tribunal. Nunca é um teto de QUANTAS páginas: a
        #: paginação vai até a página voltar incompleta, sempre — o
        #: `for pagina in range(1, 11)` do `CLAUDE.md` não volta por aqui.
        self.pagina = pagina
        self.teto_ms = teto_ms
        self.es = es or get_es()
        self.indice = index_name(INDICE)
        #: dry-run de verdade: mede a fonte sem tocar no índice
        self.escrever = escrever
        #: kill switch: chamada a cada página; `True` para a passada e salva o
        #: cursor. Ver `varrer_tribunal`.
        self.parar = parar
        self.telemetria_ativa = telemetria_ativa
        self.lidos = 0
        self.gravados = 0
        self.perdidos = 0        # docs que um empate de ms nos impediu de ver
        self.paginas = 0
        self.esperas = 0         # quantas vezes a cota compartilhada segurou
        self.requisicoes = 0     # toda ida ao Datajud, inclusive as que falharam
        self.bytes = 0           # corpo lido da fonte, acumulado
        self.peso_doc = None     # bytes por doc MEDIDOS neste tribunal
        self.pagina_atual = None  # size da última requisição de página
        self.erros = {}          # {tipo: n} — o mesmo que a telemetria publica

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
                self.requisicoes += 1
                # o teto duro de bytes vem junto com a cota `varredura` (ver
                # `DatajudClient._post`) — nada de kwarg solto que um cliente de
                # terceiro não conheça
                d = self.client._post(self.sigla, body, cota='varredura')
                self.bytes += int(getattr(self.client, 'ultimos_bytes', 0) or 0)
                return d
            except DatajudPaginaGrandeError:
                raise            # quem encolhe a página é `_buscar`, não aqui
            except DatajudClientError as e:
                if 'rate-limit' not in str(e) or tentativa == TENTATIVAS_COTA:
                    self._erro('cota' if 'rate-limit' in str(e) else 'api', str(e))
                    raise
                espera = min(60, 5 * tentativa)
                self.esperas += 1
                self._erro('cota_espera', str(e))
                logger.info('varredura %s: cota cheia, esperando %ss (%d/%d)',
                            self.sigla, espera, tentativa, TENTATIVAS_COTA)
                time.sleep(espera)
        raise AssertionError('inalcançável')

    def _erro(self, tipo: str, detalhe: str = '') -> None:
        """Conta um erro POR TIPO — aqui e na telemetria, no mesmo instante."""
        self.erros[tipo] = self.erros.get(tipo, 0) + 1
        if self.telemetria_ativa:
            telemetria.registrar_erro(self.sigla, tipo, detalhe)

    # -- orçamento de BYTES (nunca de páginas) ------------------------------ #

    def _proxima_pagina(self) -> int:
        """Quantos docs pedir na próxima requisição, em BYTES e não em itens.

        Enquanto o tribunal não foi pesado (`peso_doc is None`), o tamanho é a
        SONDA — poucos docs, só para medir. Depois disso o tamanho é o orçamento
        dividido pelo peso medido, preso entre `PISO_PAGINA` e o teto do
        `max_result_window`.

        Piso e teto respeitam um `pagina` menor que o piso (os dublês dos
        testes usam 2 ou 3), senão o próprio teste deixaria de exercitar a
        borda que ele existe para cercar.
        """
        teto = self.pagina
        piso = min(PISO_PAGINA, teto)
        if self.peso_doc is None:
            return min(PAGINA_SONDA, teto)
        cabem = int(_bytes_alvo() // max(1, self.peso_doc))
        return max(piso, min(teto, cabem))

    def _calibrar(self, n_hits: int, bytes_lidos: int) -> None:
        """Recalcula bytes/doc com o que a ÚLTIMA resposta realmente pesou.

        Encolhe na hora (o peso novo vale imediatamente) e cresce devagar: o
        peso lembrado só decai `DECAIMENTO_PESO` por página. Um cliente que não
        informa bytes (os dublês dos testes) deixa a calibração desligada, e a
        página volta a ser o teto — degradar para o comportamento antigo é
        melhor que travar a página num número inventado.
        """
        if not n_hits or bytes_lidos <= 0:
            return
        medido = bytes_lidos / n_hits
        anterior = self.peso_doc
        if anterior is None:
            self.peso_doc = medido
        else:
            # o máximo PURO prenderia a página no pior outlier de sempre; com o
            # decaimento, um doc gordo isolado encolhe a página agora e a
            # liberação volta em algumas páginas (≤1,33× por página).
            self.peso_doc = max(medido, anterior * DECAIMENTO_PESO)

    def _buscar(self, query: dict, size: int | None = None,
                desde: int = 0) -> list[dict]:
        """Uma página, com TETO DURO de bytes e re-leitura do mesmo ponto.

        Se a resposta estourar `DATAJUD_VARREDURA_BYTES_MAX`, a página é
        cortada pela metade e o MESMO `query`/`from` é pedido de novo. Nada é
        descartado: a paginação é por `range gte`, então reler o mesmo ponto é
        idempotente — a diferença é só o tamanho do balde.
        """
        pedido = size or self._proxima_pagina()
        while True:
            body = {
                'size': pedido,
                'from': desde,
                '_source': CAMPOS,
                'sort': [{'@timestamp': {'order': 'asc'}}],
                'query': query,
            }
            if not desde:
                body.pop('from')
            try:
                d = self._pedir(body)
            except DatajudPaginaGrandeError as e:
                if pedido <= min(PISO_PAGINA, self.pagina):
                    # já estamos no piso: pedir menos não é opção, e recusar
                    # deixaria o tribunal sem varrer — que é o pecado que este
                    # projeto não comete. Lê, mas DECLARA no run.
                    self._erro('resposta_grande_no_piso', str(e))
                    logger.error(
                        'varredura %s: resposta de %.1f MB no piso de %d docs — '
                        'lendo assim mesmo e registrando', self.sigla,
                        e.bytes_lidos / 1048576, pedido)
                    raise
                novo = max(min(PISO_PAGINA, self.pagina), pedido // 2)
                self._erro('resposta_grande', str(e))
                logger.warning(
                    'varredura %s: resposta de %.1f MB acima do teto — página '
                    '%d → %d, relendo o MESMO ponto', self.sigla,
                    e.bytes_lidos / 1048576, pedido, novo)
                # o peso medido explica o estouro e sobrevive à próxima página
                self.peso_doc = max(self.peso_doc or 0, e.bytes_lidos / max(1, pedido))
                pedido = novo
                continue
            hits = (d.get('hits') or {}).get('hits') or []
            self.pagina_atual = pedido
            self._calibrar(len(hits), int(getattr(self.client, 'ultimos_bytes', 0) or 0))
            return hits

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
        if not self.escrever:
            # dry-run de verdade: contamos o que ENTRARIA, sem tocar no índice
            return len(acoes)
        ok, erros = bulk(self.es, acoes, raise_on_error=False, stats_only=False)
        if erros:
            self._erro('es_recusou', str(erros[0])[:160])
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
            size = min(self._proxima_pagina(), self.teto_ms - desde)
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

        Três saídas que NÃO são "acabou", e por que cada uma é diferente:

          * `pausado`  — o kill switch foi acionado. O cursor volta no resumo e
            a próxima passada retoma dali. Não é erro.
          * `max_paginas` — bateu um teto que NÓS impusemos. É ERRO registrado,
            com o número real do que ficou de fora medido na fonte (regra nº 2
            do `CLAUDE.md`): teto é alerta, nunca corte mudo.
          * `sem_sort` — a fonte devolveu página sem chave de ordenação. Sem ela
            não há como avançar o cursor sem pular documento; abortar e gritar é
            a única saída honesta.
        """
        if self.escrever:
            ensure_index(INDICE)
        cursor = int(cursor or 0)
        t0 = time.monotonic()
        parou_por = 'fim'

        def _faixa(c):
            f = {'range': {'@timestamp': {'gte': c}}}
            return {'bool': {'filter': [f, filtro]}} if filtro else f

        while True:
            # KILL SWITCH — conferido a cada página (~10 s), não só na largada.
            # Antes disto a pausa só era vista quando um job NOVO começava: uma
            # varredura de 20 h em curso ignorava o switch até terminar, que é o
            # oposto de "parar em segundos".
            if self.parar and self.parar():
                parou_por = 'pausado'
                break
            if max_paginas and self.paginas >= max_paginas:
                parou_por = 'max_paginas'
                break

            query = _faixa(cursor)
            hits = self._buscar(query)
            self.paginas += 1
            if not hits:
                break

            self.lidos += len(hits)
            self.gravados += self._gravar(hits)
            ultimo = (hits[-1].get('sort') or [None])[0]
            pedido = self.pagina_atual or self.pagina

            if ultimo is None:
                self._erro('sem_sort')
                logger.error('varredura %s: página sem `sort` — abortando', self.sigla)
                parou_por = 'sem_sort'
                break

            if ultimo == cursor and len(hits) >= pedido:
                # página inteira dentro do mesmo ms: o cursor não avançaria nunca
                self._desempatar_ms(cursor)
                cursor += 1
            elif len(hits) < pedido:
                cursor = ultimo + 1
                parou_por = 'fim'
                self._publicar(cursor, time.monotonic() - t0)
                break                       # última página do recorte
            else:
                cursor = ultimo             # relê a cauda de propósito (idempotente)

            self._publicar(cursor, time.monotonic() - t0)
            if on_page:
                on_page(self, cursor)

        dt = time.monotonic() - t0
        resumo = {
            'tribunal': self.sigla, 'cursor': cursor, 'paginas': self.paginas,
            'lidos': self.lidos, 'gravados': self.gravados,
            'perdidos': self.perdidos, 'esperas': self.esperas,
            'requisicoes': self.requisicoes, 'bytes': self.bytes,
            'bytes_por_doc': round(self.peso_doc, 1) if self.peso_doc else None,
            'pagina_final': self.pagina_atual,
            'segundos': round(dt, 1),
            'docs_por_s': round(self.lidos / dt, 1) if dt else 0,
            'parou_por': parou_por,
            'erros': dict(self.erros),
            'restante_declarado': None,
        }
        if parou_por in ('max_paginas', 'pausado', 'sem_sort'):
            resumo['restante_declarado'] = self._quanto_ficou_de_fora(
                _faixa(cursor), parou_por)
        if self.perdidos:
            self._erro('perdidos_no_ms', f'{self.perdidos} docs')
        # os erros são copiados DEPOIS das duas linhas acima: as duas registram
        # erro, e um snapshot tirado antes devolveria `erros: {}` num run que
        # acabou de bater no teto — a mentira exata que este campo desfaz
        resumo['erros'] = dict(self.erros)
        if self.telemetria_ativa:
            telemetria.fechar(self.sigla, resumo)
        return resumo

    def _publicar(self, cursor: int, decorrido: float) -> None:
        """Publica o estado da passada. Nunca derruba a varredura."""
        if not self.telemetria_ativa:
            return
        telemetria.registrar_pagina(
            self.sigla, requisicoes=self.requisicoes, paginas=self.paginas,
            lidos=self.lidos, gravados=self.gravados, perdidos=self.perdidos,
            esperas=self.esperas, bytes_lidos=self.bytes,
            bytes_por_doc=round(self.peso_doc, 1) if self.peso_doc else None,
            pagina_atual=self.pagina_atual, cursor=cursor, decorrido=decorrido)

    def _quanto_ficou_de_fora(self, query: dict, motivo: str) -> int | None:
        """Conta NA FONTE o que sobrou depois do cursor — o número real.

        Um teto que devolve `return` discreto é o pecado do
        `for pagina in range(1, 11)`: run verde, log limpo, 43,6% do TJSP fora
        por 17 meses. Aqui o teto custa UMA requisição a mais e paga com o
        número que o operador precisa para decidir se retoma ou aborta.

        `None` quando nem contar foi possível — abster, nunca chutar zero: um
        `0` inventado diria "acabou" exatamente onde não acabou.
        """
        try:
            restante = self._contar(query)
        except Exception as exc:                       # noqa: BLE001
            self._erro('contagem_restante', str(exc))
            logger.error('varredura %s: parou por %s e NÃO consegui medir o '
                         'que ficou de fora: %s', self.sigla, motivo, str(exc)[:160])
            return None
        nivel = logger.error if motivo == 'max_paginas' else logger.warning
        nivel('varredura %s: parou por %s com %s docs ainda na fonte depois do '
              'cursor — a varredura NÃO está completa', self.sigla, motivo,
              f'{restante:,}')
        if motivo == 'max_paginas' and restante:
            self._erro('teto_max_paginas', f'{restante} docs fora')
        return restante


def deve_parar(sigla: str) -> bool:
    """O kill switch, lido do cache — um GET por página, ~1 a cada 10 s.

    Duas alavancas: a PARADA GLOBAL (uma chave, para a frota inteira) e a lista
    de siglas pausadas. Falha do cache NÃO para a varredura: um Redis fora do ar
    viraria um `stop` fantasma que ninguém pediu, e perder 20 h de puxada por
    causa disso seria trocar o produto pelo painel.
    """
    from .jobs import varredura_parada, varredura_pausados
    try:
        return varredura_parada() or sigla.upper() in varredura_pausados()
    except Exception as exc:                                # noqa: BLE001
        logger.warning('kill switch ilegível (%s) — a varredura segue',
                       str(exc)[:120])
        return False


def medir_alvo(sigla: str, client: DatajudClient | None = None) -> dict:
    """Quantos docs este tribunal DEVERIA render nesta passada, dos dois lados.

    Custa 1 requisição ao CNJ (`size: 0`, 190 bytes de resposta) e 1 `_count` no
    nosso índice. É o que dá sentido ao ETA — sem os dois lados, "faltam X" é
    contagem própria, que não prova nada (regra nº 5).

    Abster quando qualquer um dos lados falhar: ETA errado é pior que ETA
    nenhum, porque vira base de decisão.
    """
    client = client or DatajudClient(prefer_cortex=False)
    saida = {'declarado': None, 'nosso': None, 'invalidos': 0, 'alvo': None,
             'erro': None}
    try:
        d = client._post(sigla.upper(), {'size': 0, 'track_total_hits': True,
                                         'query': {'match_all': {}}},
                         cota='varredura')
        saida['declarado'] = ((d.get('hits') or {}).get('total') or {}).get('value')
    except Exception as exc:                                # noqa: BLE001
        saida['erro'] = f'declarado: {str(exc)[:120]}'
        return saida
    try:
        saida['nosso'] = get_es().options(request_timeout=30).count(
            index=index_name(INDICE),
            query={'term': {'tribunal': sigla.upper()}})['count']
    except Exception as exc:                                # noqa: BLE001
        saida['erro'] = f'nosso: {str(exc)[:120]}'
        return saida

    bruto = max(0, saida['declarado'] - saida['nosso'])
    if bruto:
        # O `_count` do CNJ inclui linhas que NÃO são processo: `numeroProcesso`
        # nulo, `classe: {codigo: "-1", nome: "Inválido"}`. Medido em
        # 31/08/2026: 5.337.680 no TJSP (7,15% do que ele declara), e são
        # EXATAMENTE as mesmas linhas que não têm `@timestamp` — sem chave de
        # ordenação, a paginação por `range @timestamp` nunca poderia alcançá-las.
        # Sem este desconto o alvo do TJSP seria 5,6 M para sempre, e o ETA
        # prometeria trazer o que não existe.
        try:
            d = client._post(sigla.upper(), {'size': 0, 'track_total_hits': True,
                                             'query': {'bool': {'must_not': [
                                                 {'exists': {'field': 'numeroProcesso'}}]}}},
                             cota='varredura')
            saida['invalidos'] = ((d.get('hits') or {}).get('total') or {}).get('value') or 0
        except Exception as exc:                            # noqa: BLE001
            # abster: alvo BRUTO e o motivo dito, nunca um desconto inventado
            saida['erro'] = f'invalidos: {str(exc)[:120]}'
            saida['invalidos'] = None
    saida['alvo'] = max(0, bruto - (saida['invalidos'] or 0))
    return saida


def varrer_tribunal(sigla: str, retomar: bool = True, max_paginas: int | None = None,
                    filtro: dict | None = None, parar=None,
                    medir: bool = True) -> dict:
    """Varre um tribunal salvando o watermark no `Tribunal` a cada passada.

    `retomar=False` recomeça do zero (varredura completa); `True` continua de
    onde parou, que no regime permanente é o sync incremental (F5).

    `parar` é o kill switch (default: `deve_parar`). `medir=False` desliga a
    medição do alvo — 1 requisição a menos, e a telemetria fica sem ETA.
    """
    trib = Tribunal.objects.filter(sigla=sigla.upper()).first()
    if not trib:
        raise ValueError(f'tribunal desconhecido: {sigla}')

    cursor = trib.datajud_varredura_cursor if retomar else 0
    v = Varredura(trib.sigla, parar=deve_parar if parar is None else parar)
    alvo = medir_alvo(trib.sigla, client=v.client) if medir else {}
    telemetria.abrir(trib.sigla, alvo=alvo.get('alvo'),
                     declarado=alvo.get('declarado'), cursor=cursor,
                     filtrada=bool(filtro))
    if alvo.get('erro'):
        v._erro('alvo_nao_medido', alvo['erro'])
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
        telemetria.registrar_erro(trib.sigla, 'excecao', str(e))
        telemetria.fechar(trib.sigla, {'parou_por': 'erro'}, estado_final='erro')
        raise
    # o cursor só avança se a passada foi do acervo INTEIRO: uma passada
    # filtrada (nicho) veria só parte do tempo e envenenaria o watermark
    # o status carrega o motivo E o tamanho da falta: "fim" e "max_paginas
    # (5.607.865 fora)" não podem se parecer na tela
    status = resumo['parou_por']
    if resumo.get('restante_declarado'):
        status = f"{status} ({resumo['restante_declarado']:,} fora)"
    campos = {
        'datajud_varredura_em': djtz.now(),
        'datajud_varredura_docs': (trib.datajud_varredura_docs or 0) + resumo['gravados'],
        'datajud_varredura_status': status[:100],
    }
    if not filtro:
        campos['datajud_varredura_cursor'] = resumo['cursor']
    Tribunal.objects.filter(pk=trib.pk).update(**campos)
    # INCREMENTAL CEGO — o defeito que este bloco existe para não deixar passar.
    # `@timestamp` é a única chave que o Datajud aceita ordenar, e o cursor
    # termina sempre em `máximo da fonte + 1`. Se a fonte ganhar documento com
    # `@timestamp` MENOR que o cursor (o CNJ reescreve `dataHoraUltimaAtualizacao`
    # em lote: medido em 31/08/2026, meses inteiros do TJSP mudaram de bucket), a
    # passada incremental devolve `fim` com ZERO documentos e status verde —
    # enquanto a medição dos dois lados diz que falta gente.
    # Sozinho, o `parou_por='fim'` é auto-confirmatório: o laço acaba exatamente
    # onde a fonte acaba. Isto aqui é a segunda opinião.
    if alvo.get('alvo') and not resumo['gravados'] and not filtro:
        logger.error(
            'varredura %s: passada incremental trouxe 0 documentos, mas a '
            'medição dos dois lados diz que faltam %s (declarado %s − inválidos '
            '%s − nosso %s). O watermark NÃO alcança esse buraco: só '
            '`--do-zero` alcança.', sigla, f"{alvo['alvo']:,}",
            f"{alvo.get('declarado') or 0:,}", f"{alvo.get('invalidos') or 0:,}",
            f"{alvo.get('nosso') or 0:,}")
        telemetria.registrar_erro(
            trib.sigla, 'incremental_cego',
            f"faltam {alvo['alvo']:,} e o cursor não alcança — só `--do-zero`")
        resumo['incremental_cego'] = alvo['alvo']
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
