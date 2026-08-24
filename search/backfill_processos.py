"""Censo e reparo do índice de PROCESSOS — o passivo que o write-through não cobre.

## O buraco, medido em 24/08/2026 (amostra ALEATÓRIA, seed 20260824)

O caminho de ESCRITA foi consertado no mesmo dia (`datajud/ingestion.py` entrega
no `on_commit`; `datajud/indice.py` confere a janela de escrita a cada 15 min).
Isso parou o buraco de CRESCER. O que sobrou é o passivo histórico: tudo que
entrou por `bulk_create`/`update()` antes do conserto — a varredura do Datajud e
a recuperação nacional do acervo — e que nunca teve `post_save` para disparar
`search/signals.py`.

Régua: 4.000 pks sorteados por faixa, os que EXISTEM no Postgres perguntados ao
Elasticsearch por `_mget` (resposta exata por documento, não estimativa):

    faixa de pk                     existem   fora do índice
    3.520-13.042.773                  3.987     0 (  0,00%)
    13.042.774-26.082.028             3.984     0 (  0,00%)
    26.082.029-39.121.283             3.988     0 (  0,00%)
    39.121.284-52.160.538             3.993     0 (  0,00%)
    52.160.539-65.199.793             3.989     0 (  0,00%)
    65.199.794-78.239.048             3.390 1.559 ( 45,99%)
    78.239.049-91.278.303             3.992   377 (  9,44%)
    91.278.304-104.317.558            3.978 2.444 ( 61,44%)
    ------------------------------------------------------
    TOTAL                            31.301 4.380 ( 13,99%)

Órfãos (doc no ES sem linha no Postgres): **0 em 699 pks de buraco de sequência
sorteados**. O índice é subconjunto do banco — o que existe lá existe aqui.

## `_cat/indices` MENTE em índice com `nested` — e por 16,9 milhões

`voyager-processos` tem `participacoes` como `nested`, e cada objeto aninhado é
um documento Lucene próprio. Por isso:

    _cat/indices  docs.count = 104.594.795   ← raízes + filhos nested
    _count match_all          =  87.709.209   ← raízes (processos de verdade)

A diferença, 16.885.586, não é acervo nenhum. Quem contar pelo `_cat` acha que o
índice tem MAIS processos do que o Postgres (102M) e conclui que não falta nada
— "número redondo" da regra nº 3 do CLAUDE.md, na forma mais cara: ele esconde
o buraco em vez de inventá-lo. Toda contagem daqui usa `_count`.

## Por que um módulo novo e não `reindexar_processos`

`reindexar_processos` reindexa TUDO no recorte: 102 milhões de leituras completas
do Postgres (linha + `participacoes` + `parte`) para consertar 14 milhões. O que
falta aqui é conhecido e barato de descobrir: ler só o `id` (index-only scan) e
perguntar ao ES quais desses ids ele NÃO tem — `search/gate.py::ausentes_no_bloco`,
a mesma primitiva dos gates do diário e do Datajud. Só o ausente paga a leitura
cara. Medido: o censo custa ~40 ms de ES por 10.000 ids e o Postgres devolve
10.000 ids contíguos em milissegundos.

E ele deixa o CENSO como subproduto: ao fim de uma passada completa sabemos
EXATAMENTE quantas linhas o Postgres tem e quantas estavam fora — sem
`reltuples`, que é estimativa.

## O ES é de UM nó e é I/O-bound: o backfill tem freio

Indexar em massa compete com a busca do site no mesmo nó (1,74 TB, heap em 71%).
Por isso este módulo mede a latência da busca REAL (as mesmas funções que a tela
chama) a cada N blocos e reduz a vazão sozinho. Teto atingido é ERRO registrado
(regra nº 2) e ES mudo é abstenção, nunca 0 (regra nº 6).
"""
from __future__ import annotations

import logging
import time

from django.core.cache import cache
from django.db import OperationalError, connection, transaction

from search import gate

logger = logging.getLogger('voyager.search.backfill')

#: Quantos ids por bloco de censo. `gate.BLOCO_TERMS` é 10.000 e o default de
#: `index.max_terms_count` do ES é 65.536 — cabe numa pergunta só.
BLOCO_CENSO = 10_000

#: Teto de espera do Postgres na leitura de ids. O bloco é index-only scan sobre
#: a PK; medido em produção, 10.000 ids contíguos custam milissegundos. 60 s é
#: folga larga. Existe porque o banco de prod NÃO tem `statement_timeout` global
#: (medido: `SHOW statement_timeout` = 0) e uma consulta pendurada segura conexão
#: do pgbouncer para sempre.
PG_TIMEOUT = '60s'

#: Checkpoint: até que pk o censo já fechou. `timeout=None` — perder isto é
#: recomeçar 102 milhões de ids do zero.
WM = 'search:backfill_proc:wm'
#: Telemetria da última passada (lida pela dashboard / pelo relatório).
ULTIMO = 'search:backfill_proc:ultimo'
#: Botão de parar, sem deploy: `cache.set('search:backfill_proc:off', True)`.
OFF = 'search:backfill_proc:off'

# ─────────────────────────────────────────────────────────────────────────────
# Freio — a busca do site tem prioridade sobre o backfill, sempre
# ─────────────────────────────────────────────────────────────────────────────
#: Os limiares são MÚLTIPLOS da baseline medida no INÍCIO de cada corrida, não
#: números absolutos: o mesmo cluster responde 10.116 ms frio e 83,9 ms quente
#: para a MESMA busca (medido), então um limiar absoluto ou freia sempre ou não
#: freia nunca. Piso e teto (abaixo) corrigem os dois extremos da baseline.
FATOR_FREIO = 2.0        # 2x a baseline ⇒ dobra o sleep
FATOR_PARADA = 4.0       # 4x a baseline ⇒ para e espera
#: Pisos absolutos: sem eles, uma baseline baixinha (a busca de processos mede
#: 340 ms) viraria um limiar que dispara com o ruído da JVM.
PISO_FREIO_MS = 1_000.0
PISO_PARADA_MS = 3_000.0
#: Tetos absolutos, e o motivo de existirem. Medido em produção em 24/08/2026,
#: com a rotação de termos e o backfill PARADO, a MEDIANA da busca de conteúdo
#: foi **7.109 ms** — termo frio em 1,74 TB num nó só é assim mesmo. O limiar
#: relativo de 4x daria 28.435 ms, que é praticamente o `ES_TIMEOUT` do
#: cliente: um freio calibrado ali só age depois que a busca já morreu. Os
#: tetos capam o limiar relativo em valores onde a tela ainda é utilizável.
TETO_FREIO_MS = 15_000.0
TETO_PARADA_MS = 25_000.0

#: Abortos, e por que eles mandam mais que a latência. O caminho
#: `busca_api.ids_por_texto` (listagem de movimentações e o filtro `q` da API
#: REST) tem `request_timeout=12 s` e levanta `BuscaIndisponivelError` acima
#: disso: o usuário NÃO vê um resultado lento, vê "a busca demorou mais que o
#: limite e foi interrompida". Latência acima de 12 s nesse caminho não é
#: espera, é FALHA — e falha é a unidade certa do limiar.
#:
#: Os limiares são somados à taxa de aborto da BASELINE, não comparados com
#: zero: se o cluster já aborta sozinho, exigir 0 faria o freio travar o
#: backfill por uma condição que ele não causou.
#: Com `N_SONDAS_FREIO = 3` a granularidade é de 33,3 pp, então os limiares são
#: escolhidos para casar com ela: 1 aborto em 3 (33%) FREIA, 2 em 3 (67%) PARA.
#: Uma busca em três falhando já é motivo para ceder vazão; duas em três é
#: motivo para sair da frente.
ABORTO_FREIO_PP = 20.0
ABORTO_PARADA_PP = 40.0

#: Sleep entre blocos: começa no valor pedido e sobe até este teto quando freia.
SLEEP_MAX = 4.0
#: Quanto espera quando a sonda pede PARADA, e quantas vezes tenta antes de
#: desistir. Desistir é ERRO registrado + checkpoint salvo — dívida VISÍVEL.
PAUSA_S = 60
PAUSA_TENTATIVAS = 10

#: De quantos em quantos blocos a sonda roda. Cada avaliação são 3 buscas de
#: processo + 3 de conteúdo, e a de conteúdo varre 1,74 TB — a sonda não é de
#: graça, ela também disputa o mesmo disco. 30 blocos de 10.000 ids é uma
#: avaliação a cada ~300 mil pks conferidos.
SONDA_A_CADA = 30
#: Quantas sondas por avaliação. Ímpar, para a mediana ser um valor medido; e a
#: taxa de aborto sai em terços (0 / 33 / 67 / 100%), granularidade suficiente
#: contra limiares de 15 e 30 pontos percentuais.
N_SONDAS_FREIO = 3


#: Termos da sonda. São ROTACIONADOS de propósito: a mesma busca repetida fica
#: quente e para de medir qualquer coisa. Medido em produção em 24/08/2026, 7
#: rodadas da MESMA busca de conteúdo, com o backfill parado:
#:
#:     83,9 · 88,7 · 136,5 · 289,7 · 346,4 · 2.545,2 · 10.116,5 ms
#:
#: São dois regimes, não ruído: o cluster é de UM nó, 1,74 TB, I/O-bound —
#: termo frio custa segundos, o mesmo termo repetido custa décimos. Uma sonda
#: de um termo só mediria "o disco já tem isto em cache", que é exatamente a
#: pergunta errada.
TERMOS_CONTEUDO = (
    'precatório', 'ofício requisitório', 'honorários sucumbenciais',
    'agravo de instrumento', 'penhora online', 'embargos de declaração',
    'cumprimento de sentença', 'sentença de mérito', 'astreintes',
)
TERMOS_PARTE = (
    ('instituto nacional do seguro social', 'SP'), ('municipio de sao paulo', 'SP'),
    ('estado de minas gerais', 'MG'), ('uniao federal', 'DF'),
    ('banco do brasil', 'RJ'), ('caixa economica federal', 'BA'),
    ('fazenda publica estadual', 'RS'), ('estado do parana', 'PR'),
    ('municipio de fortaleza', 'CE'),
)

#: Acima disto a busca não é "lenta", é inútil — o `timeout` do próprio body é
#: de 15 s (`busca_api.ES_QUERY_TIMEOUT`). Vale como alerta independente do
#: limiar relativo.
TETO_ABSURDO_MS = 20_000.0


def sondar(i: int = 0) -> dict:
    """Mede a latência da busca REAL — as mesmas funções que a tela chama.

    TRÊS sondas, porque a tela tem três caminhos com tolerâncias DIFERENTES:

      · `processos` — o índice que o backfill está ESCREVENDO (30 GB). É aqui
        que a contenção de merge/refresh aparece primeiro.
        (`dashboard/busca_views.py` → `busca_ui.buscar_processos_ui`)
      · `conteúdo`  — `voyager-movimentacoes-v2`, 1,74 TB. Não é escrito por
        este backfill, mas divide disco, CPU e heap com ele. É a busca frágil:
        83,9 ms quente contra 10.116,5 ms fria, medido. Tolera o `ES_TIMEOUT`
        do cliente (30 s em produção).
        (`dashboard/busca_views.py` → `busca_ui.buscar_conteudo_da_querystring`)
      · **`texto`** — o MESMO índice pelo caminho que **ABORTA aos 12 s**
        (`busca_api.ids_por_texto`, `IDS_TEXTO_TIMEOUT`), levantando
        `BuscaIndisponivelError(demorou=True)`. Aqui latência alta não é
        "usuário espera mais": é busca que **FALHA**, e a tela mostra "a busca
        demorou mais que o limite e foi interrompida".
        (`dashboard/views.py::1440` e `api/filters.py::105`)

    A terceira existe porque medir só as duas primeiras descreve uma
    experiência lenta onde a produção real já teria abortado — o p90 de 14,3 s
    que a sonda de conteúdo mediu, neste caminho, é 100% de falha. **O número
    que importa aqui é TAXA DE ABORTO, não percentil de latência.**

    `i` escolhe o termo da rotação — chamadas consecutivas medem buscas
    DIFERENTES, não a mesma esquentando.

    **Busca que estoura o timeout do cliente conta pelo tempo que gastou, não
    como erro de medição.** Aconteceu na primeira execução em produção
    (24/08/2026): a busca de conteúdo por um termo frio estourou o `ES_TIMEOUT`
    do cliente e derrubou o comando inteiro antes do primeiro bloco. Para quem
    está na tela isso não é "medição indisponível", é a pior latência possível
    — a busca não voltou. Contar como o teto é a leitura honesta e a
    conservadora ao mesmo tempo.
    """
    import datetime as dt

    from search import busca_api as ba

    hoje = dt.date.today()
    nome, uf = TERMOS_PARTE[i % len(TERMOS_PARTE)]
    saida: dict = {'i': i, 'erros': 0, 'abortos': 0}

    t0 = time.monotonic()
    try:
        ba.buscar_processos(parte=nome, filtros={'uf': uf}, size=20)
    except Exception as e:      # noqa: BLE001 — ver docstring
        saida['erros'] += 1
        saida['erro_processos'] = str(e)[:120]
    saida['processos_ms'] = round((time.monotonic() - t0) * 1000.0, 1)

    t0 = time.monotonic()
    try:
        ba.buscar_movimentacoes(
            q=TERMOS_CONTEUDO[i % len(TERMOS_CONTEUDO)],
            filtros={'publicado_gte': (hoje - dt.timedelta(days=31)).isoformat(),
                     'publicado_lte': hoje.isoformat()},
            size=20)
    except Exception as e:      # noqa: BLE001 — ver docstring
        saida['erros'] += 1
        saida['erro_conteudo'] = str(e)[:120]
    saida['conteudo_ms'] = round((time.monotonic() - t0) * 1000.0, 1)

    # O caminho que ABORTA: `ids_por_texto` passa `request_timeout=12 s` e
    # levanta `BuscaIndisponivelError`. Contamos o ABORTO, não os milissegundos.
    # `tribunais=[]` e janela de 31 d são o recorte que a própria docstring dele
    # manda usar (sem recorte a mediana medida foi 8,07 s).
    t0 = time.monotonic()
    try:
        ba.ids_por_texto(TERMOS_CONTEUDO[i % len(TERMOS_CONTEUDO)],
                         de=hoje - dt.timedelta(days=31), ate=hoje)
    except ba.BuscaIndisponivelError as e:
        saida['abortos'] += 1
        saida['texto_abortou'] = 'demorou' if getattr(e, 'demorou', False) else 'erro'
    except Exception as e:      # noqa: BLE001 - ver docstring
        saida['erros'] += 1
        saida['erro_texto'] = str(e)[:120]
    saida['texto_ms'] = round((time.monotonic() - t0) * 1000.0, 1)
    return saida


def _mediana(v: list[float]) -> float:
    v = sorted(v)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def baseline(n: int = 9, offset: int = 0) -> dict:
    """Roda a sonda `n` vezes, com termos DIFERENTES, e devolve a MEDIANA.

    Mediana, não média: com uma amostra que vai de 84 ms a 10.116 ms, a média
    é um número que nunca aconteceu — e vira um limiar que nunca freia.
    `n=9` cobre a rotação inteira uma vez.
    """
    proc, cont, texto, erros, abortos = [], [], [], 0, 0
    for k in range(n):
        s = sondar(offset + k)
        proc.append(s['processos_ms'])
        cont.append(s['conteudo_ms'])
        texto.append(s['texto_ms'])
        erros += s['erros']
        abortos += s['abortos']
    if erros:
        # A baseline foi medida com a busca já falhando. Não invalida a
        # corrida, mas quem lê o relatório precisa saber que o "antes" já
        # estava ruim - senão o "durante" parece culpa do backfill.
        logger.warning('backfill processos: a baseline teve %d busca(s) com '
                       'ERRO/timeout em %d sondas.', erros, 2 * n)
    if abortos:
        # A linha de base JÁ aborta. Isso não invalida a corrida - muda o
        # limiar: o freio compara a taxa de aborto DURANTE com esta, não com 0.
        logger.warning('backfill processos: a baseline teve %d de %d buscas de '
                       'texto ABORTADAS no teto de 12 s (%.1f%%).',
                       abortos, n, 100.0 * abortos / n)
    return {'processos_ms': _mediana(proc), 'conteudo_ms': _mediana(cont),
            'texto_ms': _mediana(texto), 'n': n, 'erros': erros,
            'abortos': abortos, 'aborto_pct': round(100.0 * abortos / n, 1),
            'processos_amostras': sorted(proc),
            'conteudo_amostras': sorted(cont), 'texto_amostras': sorted(texto),
            'processos_max': max(proc), 'conteudo_max': max(cont)}


def _limiares(base_ms: float) -> tuple[float, float]:
    """(freio, parada) para uma sonda, com piso e teto. Ver as constantes."""
    return (min(max(FATOR_FREIO * base_ms, PISO_FREIO_MS), TETO_FREIO_MS),
            min(max(FATOR_PARADA * base_ms, PISO_PARADA_MS), TETO_PARADA_MS))


class Freio:
    """Ajusta o `sleep` entre blocos pela latência medida da busca do site.

    Vigia as DUAS sondas, e freia por qualquer uma das duas:

      · **processos** é o sinal mais sensível — é o índice que o backfill está
        escrevendo, então merge e refresh batem nele primeiro. Baseline medida:
        340 ms de mediana ⇒ freio a 1.000 ms, parada a 3.000 ms.
      · **conteúdo** é o sinal mais caro para o usuário e o mais ruidoso: 1,74 TB
        num nó só, mediana medida de 7.109 ms com termos frios ⇒ freio a
        14.217 ms (capado em 15.000), parada a 25.000 ms.

    Vigiar só o conteúdo seria vigiar o ruído; vigiar só os processos seria
    ignorar o índice grande, que é quem divide o disco. As duas, então.

    **A sonda `texto` é vigiada pelos DOIS critérios: latência E aborto.** O
    aborto é indicador ATRASADO — quando ele acende, a busca do usuário já
    falhou. O número que obrigou isso, medido no A/B/A/B de 24/08/2026: na
    janela com o backfill escrevendo, o p90 da sonda `texto` foi **11.938 ms**
    contra o corte de 12.000 ms do `ids_por_texto`. **62 milissegundos de
    margem.** Zero abortos aconteceram, e zero abortos é a verdade — mas a
    distância até a primeira busca falhando não era margem, era sorte. O limiar
    de latência dela usa a MESMA fórmula relativa das outras duas
    (`_limiares`), não um número escolhido depois de ver o dado.

    Regra: a busca do site tem prioridade. Piorou ⇒ dobra a pausa. Piorou muito
    ⇒ PARA e espera. Depois de `PAUSA_TENTATIVAS` esperas sem melhora, desiste
    — com ERRO registrado e checkpoint salvo, para a corrida seguinte continuar
    de onde parou. Sonda que falha conta como "não sei" e freia (regra nº 6:
    abster é frear, nunca seguir a toda porque a medição falhou).
    """

    def __init__(self, base: dict, sleep_inicial: float,
                 freio_proc_ms: float | None = None):
        self.base = base
        self.sleep = sleep_inicial
        self.sleep_inicial = sleep_inicial
        self.freio_proc, self.parada_proc = _limiares(base['processos_ms'])
        if freio_proc_ms:
            # POLÍTICA de operação, mais apertada que o limiar declarado. O
            # freio é a última linha de defesa; se ele virar o modo normal de
            # funcionamento, a margem some. Medido em 24/08/2026 com o backfill
            # escrevendo: `processos` p50 = 846 ms, 85% do limiar de 1.000 ms.
            # Aperta só o FREIO (ceder vazão), nunca a PARADA declarada.
            self.freio_proc = min(self.freio_proc, freio_proc_ms)
        self.freio_cont, self.parada_cont = _limiares(base['conteudo_ms'])
        self.freio_texto, self.parada_texto = _limiares(base.get('texto_ms') or 0.0)
        self.aborto_base = base.get('aborto_pct') or 0.0
        self.aborto_freio = self.aborto_base + ABORTO_FREIO_PP
        self.aborto_parada = self.aborto_base + ABORTO_PARADA_PP
        self.pior_aborto_pct = 0.0
        self.freadas = 0
        self.paradas = 0
        self.pior_ms = 0.0
        self.pior_proc_ms = 0.0
        self.pior_texto_ms = 0.0
        self.rodada = 0

    def _medir(self) -> tuple[float, float]:
        """Mediana de 3 sondas com termos DIFERENTES: (processos_ms, conteudo_ms).

        Mediana de 3, não uma sonda só: neste cluster uma leitura fria isolada
        de 10 s é NORMAL (está na própria baseline). Uma amostra única faria o
        backfill parar por acaso — e um freio que dispara por acaso é desligado
        pelo primeiro operador que o vê, o que é pior do que não ter freio.
        """
        proc, cont, texto, abortos = [], [], [], 0
        for _ in range(N_SONDAS_FREIO):
            self.rodada += 1
            s = sondar(self.rodada)
            proc.append(s['processos_ms'])
            cont.append(s['conteudo_ms'])
            texto.append(s['texto_ms'])
            abortos += s['abortos']
            if s['conteudo_ms'] > TETO_ABSURDO_MS or s['erros']:
                logger.error(
                    'backfill processos: busca de conteúdo em %.0f ms%s — '
                    'termo %r. O timeout do próprio body é 15 s.',
                    s['conteudo_ms'], ' com ERRO' if s['erros'] else '',
                    TERMOS_CONTEUDO[self.rodada % len(TERMOS_CONTEUDO)])
        return (_mediana(proc), _mediana(cont), _mediana(texto),
                100.0 * abortos / N_SONDAS_FREIO)

    def avaliar(self) -> bool:
        """Mede, ajusta o sleep. Devolve False se a corrida deve ABORTAR."""
        for tentativa in range(PAUSA_TENTATIVAS + 1):
            try:
                m_proc, m_cont, m_texto, aborto_pct = self._medir()
            except Exception:      # noqa: BLE001 — "não sei" freia, não acelera
                logger.warning('backfill processos: sonda de latência FALHOU — '
                               'freando por precaução', exc_info=True)
                self.sleep = min(SLEEP_MAX, max(self.sleep, self.sleep_inicial) * 2)
                self.freadas += 1
                return True
            self.pior_ms = max(self.pior_ms, m_cont)
            self.pior_proc_ms = max(self.pior_proc_ms, m_proc)
            self.pior_aborto_pct = max(self.pior_aborto_pct, aborto_pct)
            self.pior_texto_ms = max(self.pior_texto_ms, m_texto)
            if (m_proc < self.parada_proc and m_cont < self.parada_cont
                    and m_texto < self.parada_texto
                    and aborto_pct < self.aborto_parada):
                break
            self.paradas += 1
            logger.error(
                'backfill processos: busca DEGRADADA — processos %.0f ms '
                '(limiar %.0f, baseline %.0f) · conteúdo %.0f ms (limiar %.0f, '
                'baseline %.0f) · texto %.0f ms (limiar %.0f, baseline %.0f) · '
                'buscas de texto ABORTADAS aos 12 s %.0f%% (limiar %.0f%%, '
                'baseline %.0f%%). Pausando %d s (tentativa %d/%d): a busca do '
                'site tem prioridade sobre o backfill.',
                m_proc, self.parada_proc, self.base['processos_ms'],
                m_cont, self.parada_cont, self.base['conteudo_ms'],
                m_texto, self.parada_texto, self.base.get('texto_ms') or 0.0,
                aborto_pct, self.aborto_parada, self.aborto_base,
                PAUSA_S, tentativa + 1, PAUSA_TENTATIVAS,
            )
            time.sleep(PAUSA_S)
        else:
            logger.error(
                'backfill processos: a busca não voltou ao normal depois de %d '
                'pausas de %d s — ABORTANDO a corrida. O checkpoint está salvo; '
                'a próxima corrida continua daqui. Isto é dívida VISÍVEL, não '
                'um fim silencioso.', PAUSA_TENTATIVAS, PAUSA_S,
            )
            return False

        if (m_proc > self.freio_proc or m_cont > self.freio_cont
                or m_texto > self.freio_texto or aborto_pct > self.aborto_freio):
            novo = min(SLEEP_MAX, max(self.sleep, 0.05) * 2)
            if novo != self.sleep:
                logger.warning(
                    'backfill processos: processos %.0f ms (limiar %.0f) · '
                    'conteúdo %.0f ms (limiar %.0f) · texto %.0f ms (limiar '
                    '%.0f) · aborto %.0f%% (limiar %.0f%%) — sleep %.2f s → '
                    '%.2f s',
                    m_proc, self.freio_proc, m_cont, self.freio_cont,
                    m_texto, self.freio_texto, aborto_pct, self.aborto_freio,
                    self.sleep, novo)
            self.sleep = novo
            self.freadas += 1
        elif (m_proc < self.freio_proc / 2 and m_cont < self.freio_cont / 2
                and m_texto < self.freio_texto / 2
                and aborto_pct <= self.aborto_base
                and self.sleep > self.sleep_inicial):
            self.sleep = max(self.sleep_inicial, self.sleep / 2)
        return True


# ─────────────────────────────────────────────────────────────────────────────
# Censo + reparo
# ─────────────────────────────────────────────────────────────────────────────
def _ids_do_bloco(depois_de: int, ate: int, tamanho: int) -> list[int] | None:
    """`tamanho` pks a partir de `depois_de` (exclusivo), até `ate` (inclusivo).

    Index-only scan sobre a PK. Devolve `None` quando o Postgres não respondeu
    dentro do teto — ABSTENÇÃO, não lista vazia: lista vazia significaria "acabou
    o banco" e o checkpoint pularia o resto do acervo.
    """
    try:
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute('SET LOCAL statement_timeout = %s', [PG_TIMEOUT])
            cur.execute(
                'SELECT id FROM tribunals_process '
                'WHERE id > %s AND id <= %s ORDER BY id LIMIT %s',
                [depois_de, ate, tamanho])
            return [r[0] for r in cur.fetchall()]
    except OperationalError:
        logger.error('backfill processos: leitura de ids estourou %s em id > %s '
                     '— ABSTENDO (o checkpoint não anda).', PG_TIMEOUT, depois_de)
        return None


def _topo() -> int:
    with transaction.atomic(), connection.cursor() as cur:
        cur.execute('SET LOCAL statement_timeout = %s', [PG_TIMEOUT])
        cur.execute('SELECT max(id) FROM tribunals_process')
        return cur.fetchone()[0] or 0


def _reparar(faltando: list[int], pk0: int, pk1: int, indexar) -> int:
    """Indexa os ausentes deste bloco e CONTA. Diferença vira ERRO registrado.

    Bulk que aceitou menos documentos do que mandamos é exatamente o formato da
    perda que este módulo existe para fechar. O checkpoint avança (o bloco foi
    medido e tentado), mas o número fica no log e no relatório - dívida
    visível, nunca `return` discreto.
    """
    if not faltando:
        return 0
    n = 0
    for i in range(0, len(faltando), gate.CHUNK_ENFILEIRA):
        n += indexar(faltando[i:i + gate.CHUNK_ENFILEIRA]) or 0
    if n < len(faltando):
        logger.error(
            'backfill processos: bloco id %s-%s tinha %d fora do índice e só '
            '%d entraram - %d NÃO indexados.',
            pk0, pk1, len(faltando), n, len(faltando) - n)
    return n


def rodar(de: int = 0, ate: int | None = None, sleep: float = 0.1,
          bloco: int = BLOCO_CENSO, reparar: bool = True,
          limite_blocos: int = 0, usar_checkpoint: bool = True,
          relatar=None, freio_proc_ms: float | None = None) -> dict:
    """Percorre os pks de `de` (exclusivo) até `ate`, mede e repara.

    Retomável: o checkpoint em `cache[WM]` guarda o último pk cujo bloco FECHOU.
    Um bloco que absteve (Postgres ou ES mudos) para o avanço ali mesmo — é a
    diferença entre "não sei" e "está tudo certo".

    Parável: `cache.set('search:backfill_proc:off', True)` encerra na virada do
    bloco seguinte, com checkpoint salvo.

    O reparo é SÍNCRONO de propósito. Enfileirar 14 milhões de processos na
    `es_index` seriam 28.600 jobs drenados por 24 workers em paralelo, ou seja
    24 leitores simultâneos num Postgres que é disk-I/O-bound — sem freio
    nenhum e competindo com a própria busca que este módulo promete não
    degradar. Aqui é um leitor só, com sleep ajustável e sonda de latência.
    """
    from search.jobs import indexar_processos_bulk

    if ate is None:
        ate = _topo()
    inicio = cache.get(WM) if (usar_checkpoint and not de) else de
    if inicio is None:
        inicio = de
    cursor = max(inicio, de)

    base = baseline()          # 9 sondas = a rotação inteira de termos
    freio = Freio(base, sleep, freio_proc_ms=freio_proc_ms)
    logger.info('backfill processos: baseline da busca — processos %.0f ms '
                '(freio %.0f / parada %.0f) · conteúdo %.0f ms (freio %.0f / '
                'parada %.0f)', base['processos_ms'], freio.freio_proc,
                freio.parada_proc, base['conteudo_ms'], freio.freio_cont,
                freio.parada_cont)

    tot = {'lidos': 0, 'fora': 0, 'indexados': 0, 'blocos': 0, 'abstidos': 0,
           'de': cursor, 'ate': cursor, 'parou_por': None,
           'baseline': base, 'sleep_final': freio.sleep}
    t0 = time.monotonic()
    while cursor < ate:
        if cache.get(OFF):
            tot['parou_por'] = 'kill-switch'
            logger.warning('backfill processos: PARADO pelo kill-switch em id=%s '
                           '(checkpoint salvo).', cursor)
            break
        if limite_blocos and tot['blocos'] >= limite_blocos:
            tot['parou_por'] = 'limite-blocos'
            break

        ids = _ids_do_bloco(cursor, ate, bloco)
        if ids is None:
            tot['abstidos'] += 1
            tot['parou_por'] = 'pg-mudo'
            break
        if not ids:
            cursor = ate
            tot['ate'] = ate
            break

        try:
            faltando = gate.ausentes_no_bloco(ids, gate.indice_processos())
        except Exception:      # noqa: BLE001 — ES mudo é "não sei", nunca "não falta"
            logger.warning('backfill processos: ES mudo no bloco id %s-%s — '
                           'abstendo (o checkpoint não anda).', ids[0], ids[-1],
                           exc_info=True)
            tot['abstidos'] += 1
            tot['parou_por'] = 'es-mudo'
            break

        n_ind = (_reparar(faltando, ids[0], ids[-1], indexar_processos_bulk)
                 if reparar else 0)

        tot['lidos'] += len(ids)
        tot['fora'] += len(faltando)
        tot['indexados'] += n_ind
        tot['blocos'] += 1
        cursor = ids[-1]
        tot['ate'] = cursor
        if usar_checkpoint:
            cache.set(WM, cursor, None)
        if relatar is not None:
            relatar(ids[0], ids[-1], len(ids), len(faltando), n_ind,
                    time.monotonic() - t0)

        if tot['blocos'] % SONDA_A_CADA == 0 and not freio.avaliar():
            tot['parou_por'] = 'busca-degradada'
            break
        if freio.sleep:
            time.sleep(freio.sleep)

    tot['segundos'] = round(time.monotonic() - t0, 1)
    tot['sleep_final'] = freio.sleep
    tot['freadas'] = freio.freadas
    tot['paradas'] = freio.paradas
    tot['pior_conteudo_ms'] = freio.pior_ms
    tot['pior_processos_ms'] = freio.pior_proc_ms
    tot['pior_aborto_pct'] = freio.pior_aborto_pct
    tot['pior_texto_ms'] = freio.pior_texto_ms
    cache.set(ULTIMO, tot, 7 * 24 * 3600)
    logger.info('backfill processos: %s', {k: v for k, v in tot.items()
                                           if k != 'baseline'})
    return tot


# ─────────────────────────────────────────────────────────────────────────────
# A régua — amostra aleatória por faixa de pk, com semente declarada
# ─────────────────────────────────────────────────────────────────────────────
#: Semente fixa: a mesma amostra antes e depois mede a MESMA coisa. Amostra por
#: `list(set(pks))[:200]` pega os MENORES pks e mede a parte do acervo que nunca
#: teve problema — já produziu alarme falso neste projeto.
SEED_PADRAO = 20260824
#: Candidatos sorteados por faixa. A densidade do espaço de pk é ~97,8% (o resto
#: é valor de sequência queimado por `bulk_create(ignore_conflicts=True)`), então
#: 4.000 candidatos viram ~3.900 processos reais.
N_AMOSTRA = 4_000
#: Acima disto a passada vira ERRO registrado: o buraco reabriu.
ALERTA_FORA_PCT = 1.0


def amostrar(faixas: int = 8, n: int = N_AMOSTRA, seed: int = SEED_PADRAO,
             teto: int | None = None) -> dict:
    """Quantos processos REAIS de cada faixa de pk estão fora do índice.

    Mede os dois lados com o MESMO critério (regra nº 5): os ids vêm do
    Postgres e são perguntados ao Elasticsearch um a um por `_mget`, que é
    realtime GET por `_id` — resposta exata por documento, não `_count`, não
    estimativa, sem janela de fuso para errar.

    `teto` FIXA o topo do espaço de pk. Sem ele as faixas são recalculadas a
    partir do `max(id)` do momento — e como a ingestão nunca para, a mesma
    semente sorteia pks diferentes a cada chamada e a amostra "depois" não
    compara com a "antes". O `max(id)` da medição de 24/08/2026 é
    **104.317.558**: é esse o valor a passar quando o objetivo for repetir
    aquela medição.

    Devolve `fora=None` na faixa em que o ES não respondeu. Nunca 0.
    """
    import random

    es = gate._es()
    idx = gate.indice_processos()
    with transaction.atomic(), connection.cursor() as cur:
        cur.execute('SET LOCAL statement_timeout = %s', [PG_TIMEOUT])
        cur.execute('SELECT min(id), max(id) FROM tribunals_process')
        minpk, maxpk = cur.fetchone()
    if not maxpk:
        return {'faixas': [], 'erro': 'tabela vazia'}
    topo_real = maxpk
    if teto:
        maxpk = min(maxpk, teto)

    rng = random.Random(seed)
    larg = (maxpk - minpk + 1) / faixas
    saida, tot_e, tot_f, abstidos = [], 0, 0, 0
    for f in range(faixas):
        lo = int(minpk + f * larg)
        hi = min(int(minpk + (f + 1) * larg) - 1, maxpk)
        cand = sorted(rng.sample(range(lo, hi + 1), min(n, hi - lo + 1)))
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute('SET LOCAL statement_timeout = %s', [PG_TIMEOUT])
            cur.execute('SELECT id FROM tribunals_process WHERE id = ANY(%s)',
                        [cand])
            existem = [r[0] for r in cur.fetchall()]
        fora: list[int] | None = []
        try:
            for i in range(0, len(existem), gate.BLOCO_MGET):
                r = es.mget(index=idx,
                            ids=[str(x) for x in existem[i:i + gate.BLOCO_MGET]],
                            source=False, request_timeout=gate.ES_TIMEOUT)
                fora.extend(int(d['_id']) for d in r['docs'] if not d.get('found'))
        except Exception:      # noqa: BLE001
            logger.warning('amostra: ES mudo na faixa %d — abstendo', f, exc_info=True)
            fora = None
            abstidos += 1
        item = {'faixa': f, 'lo': lo, 'hi': hi, 'candidatos': len(cand),
                'existem': len(existem),
                'fora': None if fora is None else len(fora),
                'pct': None if fora is None or not existem
                else round(100.0 * len(fora) / len(existem), 2)}
        saida.append(item)
        if fora is not None:
            tot_e += len(existem)
            tot_f += len(fora)
    pct = round(100.0 * tot_f / tot_e, 2) if tot_e else None
    return {'faixas': saida, 'seed': seed, 'existem': tot_e, 'fora': tot_f,
            'pct': pct, 'abstidos': abstidos, 'min_pk': minpk, 'max_pk': maxpk,
            'topo_real': topo_real, 'teto_fixado': bool(teto)}


def conferir_indice_processos() -> dict:
    """Sentinela: o buraco reabriu? Roda barato e GRITA quando sim.

    Existe porque o buraco que este módulo fechou (13,99% do acervo de
    processos fora da busca, 14,2 milhões de linhas) não deu sintoma nenhum
    durante meses: a fila `es_index` marcava zero, os runs fechavam verdes e o
    `_cat/indices` mostrava MAIS documentos do que o Postgres tem linhas.

    Amostra pequena de propósito (1.000 candidatos x 8 faixas ≈ 7.800
    processos, ~3 s): a régua grande é `manage.py es_backfill_processos
    --so-amostra`. Esta só precisa acender a luz.

    **Não fixa `teto` de propósito** — ao contrário da régua de antes/depois,
    que precisa comparar o MESMO espaço de pk. A sentinela tem que enxergar
    justamente o acervo que acabou de entrar, que é onde o buraco de
    24/08/2026 estava (0,00% nos cinco primeiros oitavos de pk, 45,99% /
    9,44% / 61,44% nos três últimos).
    """
    r = amostrar(faixas=8, n=1_000)
    cache.set('search:backfill_proc:amostra', r, 7 * 24 * 3600)
    if r.get('abstidos'):
        logger.warning('sentinela do índice de processos: %d faixa(s) sem '
                       'resposta do ES — medição INCOMPLETA.', r['abstidos'])
    if r.get('pct') is not None and r['pct'] >= ALERTA_FORA_PCT:
        piores = sorted((f for f in r['faixas'] if f['pct'] is not None),
                        key=lambda f: -f['pct'])[:3]
        logger.error(
            'sentinela do índice de processos: %.2f%% dos processos amostrados '
            '(%d de %d) estão FORA do índice — o buraco de 24/08/2026 reabriu. '
            'Piores faixas de pk: %s. Rode `manage.py es_backfill_processos`.',
            r['pct'], r['fora'], r['existem'],
            [(f['lo'], f['hi'], f['pct']) for f in piores])
    return r
