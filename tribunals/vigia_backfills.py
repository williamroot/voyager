"""Vigia dos backfills longos — cobertura MEDIDA DOS DADOS, não do container.

## O defeito que este módulo conserta

Em 02/09/2026 três backfills longos estavam em produção e ninguém sabia dizer,
sem `ssh` e `docker ps -a`, se algum ainda andava:

    r105_fase_1..4     4 shards de `backfill_fase`   (Postgres)
    r106_proc_digits   `es_backfill_proc_digits`     (Elasticsearch)
    r111_validar       `auditar_schema --validar-fks` (Postgres, 5 FKs)

Dois estavam vivos **no host errado** (`voyager-workers`, .102) e por isso
constavam como "desaparecidos" para quem olhou o `.103`. Um estava em **laço de
reinício** (17 reinícios, progresso líquido zero, saindo com código 0 a cada
volta). Um tinha sido **cancelado** (`pg_cancel_backend`) e nunca voltou.

Nenhum desses três estados acende luz nenhuma hoje, e os três são
indistinguíveis de "terminou": **run verde, log limpo, número redondo** — a
assinatura exata das três perdas da tabela do `CLAUDE.md`.

## O contrato

Um tique agendado que, a cada passada:

* **recalcula a cobertura A PARTIR DOS DADOS** (índice do ES, tabela do
  Postgres, `pg_constraint`) — nunca de cursor, de arquivo, de log ou do
  estado do container. Reinício, deploy, queda de Redis e `docker rm` não
  perdem lugar nenhum, porque não há lugar guardado;
* **publica a data da medição**. O retrato tem TTL maior que o tique de
  propósito: o que a tela mostra quando o tique morre é um número VELHO com a
  idade em letra grande, não um card vazio. Card vazio some da vista;
* **grita com número real** quando o pendente não anda (regra nº 2);
* **auto-cura o que dá para auto-curar**: as FKs. Ver abaixo por que só elas.

É o mesmo desenho de `djen/recuperacao.py` (Fase 3), e de propósito: virou o
padrão da casa em 02/09/2026.

## Por que as FKs são dirigidas pelo tique e os outros dois não

A pergunta não é "o que é mais bonito", é "qual o tamanho da unidade de
trabalho e onde mora o estado":

| trabalho | unidade | estado | cabe em job? |
|---|---|---|---|
| `VALIDATE CONSTRAINT` | 1 FK, ~40 min | `pg_constraint.convalidated` | **sim** |
| `proc_digits` | 1 fatia-dia, até ~10 h | `must_not exists` no ES | não |
| `fase_codigo` | faixa de pk, horas | `fase_em IS NULL` no Postgres | não |

As FKs são um conjunto FINITO (5), cada unidade cabe folgada no
`DEFAULT_TIMEOUT` de 3600 s da fila `djen_audit`, e o estado já está no
catálogo do Postgres — validar de novo o que já foi validado é um `SELECT`
barato. É o caso de livro do tique que enfileira.

Os outros dois **não ganham nada** virando job de fila: já são idempotentes e
retomáveis por construção (o `must_not exists` do ES e o `fase_em IS NULL` do
Postgres são o cursor), rodam contra recursos cujo paralelismo seguro é **1**
(o nó do ES divide disco com a busca do site; o Postgres é disk-I/O-bound), e
uma fatia de 10 h estouraria qualquer timeout de fila razoável. O que faltava
neles nunca foi retomabilidade — era **alguém reparar que pararam**. Então
para eles o tique é vigia, não motorista.

## O `update_by_query` do ES NÃO mora no container (medido 02/09/2026)

A armadilha mais cara deste incidente inteiro. O `r106_proc_digits` disparou o
`_update_by_query` com `wait_for_completion=False`: quem varre o índice é o
**Elasticsearch**, e a tarefa continua rodando depois que o container morre. Às
14:21 UTC, com o container fora do `docker ps -a`, o ES estava assim:

    task BmIw6rzPSxy5_QCB1mIdQA:305727375 — update-by-query [voyager-movimentacoes]
    rodando há 1h04 · 8 fatias filhas escrevendo bulk[1000]
    lote atual: 13.572.000 de 27.888.685 (48,7%)

⇒ a régua "o container está de pé?" dá **falso negativo** aqui, e o falso
negativo é pior que o falso positivo: alguém religa por cima e **dobra a
carga** num nó de ES que já derrubou a tela de estado com 503. Por isso o tique
lê `_tasks?actions=*byquery*` e **nunca** declara `proc_digits` parado enquanto
houver tarefa viva — o veredito nesse caso é `escrevendo`, com o `updated/total`
da tarefa e o `requests_per_second` dela na tela.

O `requests_per_second` vai no retrato de propósito: em 02/09 a tarefa foi
estrangulada para **500 docs/s** (`_rethrottle`) porque a agregação de
`/dashboard/overview/estado/<UF>/` custa ~20 s a frio contra um teto de cliente
de 30 s, e a soma dava `ConnectionTimeout`. Quem for desestrangular precisa ver
o número que está valendo — e medir a tela junto. Medição do dia: baixar de 800
para 200 docs/s mudou a agregação de 22,16 s para 20,95 s, **quase nada**. Os
20 s são custo próprio da agregação; estrangular o vizinho não conserta o
vizinho.

## As duas armadilhas de medição que este módulo evita

1. **`exists` do ES conta string vazia como valor presente** (regra nº 4). O
   tique publica os dois números: o `_count` (exato e barato) e uma AMOSTRA
   conferida pelo CONTEÚDO. Se os dois divergirem além do erro amostral, isso
   é ERRO — "o `exists` voltou a mentir" — e não um arredondamento. Medido em
   02/09/2026, `proc_digits`: `exists` 57,73%, amostra de 3.000 docs 57,07%,
   com **0 vazios e 0 malformados**. Aqui, hoje, ele não mente; o teste é para
   saber no dia em que passar a mentir.

   A amostra usa `s.get('proc_digits') is not None`, nunca
   `'proc_digits' in s`: doc antigo tem a CHAVE com valor `null` e passaria.

2. **Cobertura de 100% não é o alvo do `fase_codigo`**. A fase só existe para
   processo que tem publicação em diário com classe (`meio <> 'datajud'`).
   Medido em 02/09/2026 por amostra: **94,16%** dos processos sem fase têm uma
   publicação qualificável, ou seja, o teto alcançável é ~98,9% e não 100%. O
   tique remede esse teto a cada 24 h e publica os dois números lado a lado —
   sem isso o card mostraria "18% faltando" para sempre e ninguém saberia que
   uma parte é inalcançável por construção.

## A amostragem do Postgres tem erro, e ele vai declarado

`TABLESAMPLE SYSTEM` sorteia PÁGINAS, não linhas: as linhas de uma página são
vizinhas em pk e altamente correlacionadas na cobertura (o backfill anda por
faixa de pk). O n efetivo é o número de páginas, não o de linhas. Por isso o
tique publica `fase_pct_erro_pp` (±2σ calculado sobre PÁGINAS) e só declara
PARADO quando o pendente estimado está confortavelmente acima do ruído
(`FASE_PISO_ALARME`). Abaixo disso o veredito é `reta_final`, não `parado` —
alarme que acende sozinho no fim é alarme gasto.
"""
from __future__ import annotations

import datetime
import logging
import math
import os

from django.core.cache import cache
from django.db import connection, transaction
from django.utils import timezone
from django_rq import job

logger = logging.getLogger('voyager.tribunals.vigia_backfills')

# ── Chaves no Redis. Kill switch mora aqui, não em env: parar tem que custar
# segundos, não um deploy. O Redis de prod tem AOF desde 31/08/2026.
CHAVE_ESTADO = 'vigia:backfills:v1'
CHAVE_HIST = 'vigia:backfills:hist:v1'
CHAVE_OFF = 'vigia:backfills:off'
CHAVE_FK_OFF = 'vigia:backfills:fk_off'
CHAVE_TETO_FASE = 'vigia:backfills:teto_fase:v1'

#: TTL do retrato. MAIOR que o tique de propósito — ver a docstring.
ESTADO_TTL_S = 12 * 3600
HIST_TTL_S = 8 * 24 * 3600
HIST_MAX = 400

#: Janela em que se julga progresso. Curta demais e o ruído amostral do
#: Postgres decide; longa demais e o alarme chega tarde.
JANELA_PROGRESSO_H = float(os.environ.get('VIGIA_JANELA_PROGRESSO_H', 6))
#: Antes disto não há amostra suficiente para julgar — o veredito é `aquecendo`.
MIN_JANELA_H = float(os.environ.get('VIGIA_MIN_JANELA_H', 2))

#: Abaixo deste pendente estimado o `fase` está na reta final e a amostragem
#: não distingue "parado" de "quase pronto". 3 M ≈ 6× o ±2σ medido.
FASE_PISO_ALARME = int(os.environ.get('VIGIA_FASE_PISO_ALARME', 3_000_000))
#: O `proc_digits` é contado EXATO pelo `_count` — não tem ruído amostral, e o
#: piso existe só para não gritar no último milhão.
DIGITS_PISO_ALARME = int(os.environ.get('VIGIA_DIGITS_PISO_ALARME', 500_000))

#: Fração de páginas do `TABLESAMPLE SYSTEM`. 0,1% de `tribunals_process`
#: (131 GB) ≈ 131 MB por passada — o preço de saber, a cada 15 min, se um
#: backfill de 19 M de linhas ainda anda.
FASE_AMOSTRA_PCT = float(os.environ.get('VIGIA_FASE_AMOSTRA_PCT', 0.1))
#: Amostra do teto (quantos dos SEM fase são alcançáveis). Cara: faz um EXISTS
#: em `tribunals_movimentacao` por linha. Por isso roda no máximo 1×/24 h.
TETO_AMOSTRA = int(os.environ.get('VIGIA_TETO_AMOSTRA', 2_000))
TETO_TTL_S = 24 * 3600

#: Amostra de conteúdo do ES por passada. Pequena de propósito: ela não é a
#: medida (o `_count` é), é o CONTROLE de que o `_count` ainda diz a verdade.
ES_AMOSTRA = int(os.environ.get('VIGIA_ES_AMOSTRA', 300))
#: Semente declarada — medição sem semente não se repete, e o que não se
#: repete não é régua.
ES_SEMENTE = 119

#: Tetos de espera. Nada no caminho de nada sem teto (regra nº 7).
SQL_TIMEOUT_S = int(os.environ.get('VIGIA_SQL_TIMEOUT_S', 120))
SQL_TETO_TIMEOUT_S = int(os.environ.get('VIGIA_SQL_TETO_TIMEOUT_S', 300))
ES_TIMEOUT_S = int(os.environ.get('VIGIA_ES_TIMEOUT_S', 240))

#: Janela do `detected_at` que contém 100% dos faltantes de `proc_digits`.
#: IMPORTADA do comando, nunca copiada: se a premissa da fatia mudar lá, o
#: alarme daqui muda junto. Uma segunda cópia produziria o pior resultado —
#: o comando trabalhando numa janela e o vigia conferindo outra.
def _janela_digits() -> tuple[datetime.date, datetime.date]:
    from search.management.commands.es_backfill_proc_digits import (
        JANELA_FIM, JANELA_INICIO)
    return JANELA_INICIO, JANELA_FIM


# ─────────────────────────────────────────────────────────────────────────────
# Medição
# ─────────────────────────────────────────────────────────────────────────────

def _com_teto(fn, segundos: int = SQL_TIMEOUT_S):
    """`SET LOCAL` DENTRO de uma transação — o pgbouncer roda em transaction
    mode e um `SET` solto vai para uma conexão enquanto a query vai para outra.
    Mesmo padrão de `djen/recuperacao.py::_com_teto`."""
    with transaction.atomic(), connection.cursor() as c:
        c.execute('SET LOCAL statement_timeout = %s', [segundos * 1000])
        return fn(c)


def medir_proc_digits() -> dict:
    """Cobertura de `proc_digits` no índice de movimentações.

    Publica TRÊS coisas: o `_count` (exato), a amostra por conteúdo (o
    controle de que o `_count` não está mentindo) e os faltantes FORA da
    janela de `detected_at` (que TÊM que ser 0 — se não forem, a premissa que
    faz o backfill fatiar por dia caiu e ele deixaria buraco).
    """
    from search.client import get_es, index_name
    es = get_es()
    idx = index_name('movimentacoes')

    def conta(q, t=ES_TIMEOUT_S):
        return int(es.count(index=idx, query=q, request_timeout=t)['count'])

    total = conta({'match_all': {}}, t=60)
    falta = conta({'bool': {'must_not': [{'exists': {'field': 'proc_digits'}}]}})
    de, ate = _janela_digits()
    fora = conta({'bool': {'must_not': [
        {'exists': {'field': 'proc_digits'}},
        {'range': {'detected_at': {'gte': de.isoformat(), 'lt': ate.isoformat()}}},
    ]}})

    r = es.search(index=idx, size=ES_AMOSTRA, request_timeout=ES_TIMEOUT_S,
                  source_includes=['proc', 'proc_digits'],
                  query={'function_score': {
                      'query': {'match_all': {}},
                      'random_score': {'seed': ES_SEMENTE, 'field': '_seq_no'}}})
    hits = r['hits']['hits']
    ok = vazio = ausente = errado = 0
    for h in hits:
        s = h['_source']
        # `.get(...) is not None`: `'campo' in _source` MENTE em doc que tem a
        # chave com valor null — já derrubou uma medição nesta casa (60/60
        # virou 38/60).
        d = s.get('proc_digits')
        esperado = ''.join(c for c in (s.get('proc') or '') if c.isdigit())
        if d is None:
            ausente += 1
        elif d == '':
            vazio += 1
        elif len(d) == 20 and d == esperado:
            ok += 1
        else:
            errado += 1
    n = len(hits)
    return {
        'indice': idx, 'docs': total, 'faltam': falta,
        'tarefas': _tarefas_byquery(es),
        'pct_exists': round(100.0 * (total - falta) / total, 2) if total else None,
        'fora_da_janela': fora,
        'amostra_n': n, 'amostra_ok': ok, 'amostra_vazio': vazio,
        'amostra_ausente': ausente, 'amostra_errado': errado,
        'pct_amostra': round(100.0 * ok / n, 2) if n else None,
    }


def _tarefas_byquery(es) -> list:
    """Tarefas `*byquery` vivas no ES — a ÚNICA prova de que o backfill do
    índice ainda anda.

    Devolve lista vazia em caso de erro, e o chamador trata isso como
    "não sei", nunca como "não tem". A diferença importa: `[]` por erro
    somado a "o pendente não caiu" produziria exatamente o falso "PAROU" que
    faz alguém religar por cima de uma tarefa viva.

    Só as tarefas RAÍZ (`parent_task_id` ausente): um `update_by_query` com
    `slices=8` aparece como 1 pai + 8 filhas, e contar as filhas faria "1
    backfill" parecer "9 backfills".
    """
    saida = []
    try:
        t = es.tasks.list(actions='*byquery*', detailed=True,
                          request_timeout=60)
    except Exception as exc:  # noqa: BLE001
        logger.warning('vigia: não li as tarefas do ES (%s) — o veredito de '
                       '`proc_digits` fica ABSTIDO, nunca "parado"', str(exc)[:120])
        return saida
    for node in (t.get('nodes') or {}).values():
        for tid, tk in (node.get('tasks') or {}).items():
            if tk.get('parent_task_id'):
                continue
            st = tk.get('status') or {}
            total = st.get('total') or 0
            feito = (st.get('updated') or 0) + (st.get('created') or 0)
            saida.append({
                'id': tid,
                'acao': tk.get('action'),
                'updated': feito,
                'total': total,
                'pct': round(100.0 * feito / total, 1) if total else None,
                'rps': st.get('requests_per_second'),
                'minutos': round((tk.get('running_time_in_nanos') or 0) / 1e9 / 60, 1),
            })
    return saida


def medir_fase() -> dict:
    """Cobertura de `fase_codigo` em `tribunals_process`, por amostra de páginas.

    `fase_codigo <> ''` e não `IS NOT NULL`: a coluna tem default `''`, e
    contar string vazia como preenchida é a versão em Postgres do `exists` que
    mente. Medido em 02/09/2026: dos 18,34% sem fase, **100%** são `''` e
    nenhum é NULL — quem contasse por `IS NOT NULL` publicaria 100,00%.
    """
    def _q(c):
        c.execute("SELECT reltuples::bigint, relpages::bigint FROM pg_class "
                  "WHERE relname = 'tribunals_process'")
        est, pgs = c.fetchone()
        c.execute('SELECT setseed(0.119)')
        c.execute(f"""
            SELECT count(*),
                   count(*) FILTER (WHERE fase_codigo IS NOT NULL AND fase_codigo <> ''),
                   count(*) FILTER (WHERE fase_codigo = ''),
                   count(*) FILTER (WHERE fase_codigo IS NULL)
              FROM tribunals_process TABLESAMPLE SYSTEM ({FASE_AMOSTRA_PCT})
        """)
        return (est, pgs) + tuple(c.fetchone())

    est, pgs, n, com, vazia, nula = _com_teto(_q)
    pct = (100.0 * com / n) if n else None
    # ±2σ sobre PÁGINAS (o n efetivo do TABLESAMPLE SYSTEM), não sobre linhas.
    pgs_amostra = max(1, int((pgs or 0) * FASE_AMOSTRA_PCT / 100.0))
    p = (com / n) if n else 0.0
    erro_pp = 200.0 * math.sqrt(max(p * (1 - p), 1e-9) / pgs_amostra)
    faltam = int(est * (1 - p)) if est else None
    return {
        'linhas': est, 'amostra_n': n, 'com_fase': com,
        'string_vazia': vazia, 'nulos': nula,
        'pct': round(pct, 2) if pct is not None else None,
        'pct_erro_pp': round(erro_pp, 2),
        'faltam_estimado': faltam,
        'paginas_amostradas': pgs_amostra,
    }


def medir_teto_fase(forcar: bool = False) -> dict | None:
    """Quantos dos processos SEM fase são ALCANÇÁVEIS.

    Sem este número o card mostraria "18% faltando" para sempre, porque uma
    parte do resíduo é inalcançável por construção: processo que só existe pela
    porta do Datajud não tem publicação em diário com classe, e a fase não sai
    de lugar nenhum. Regra nº 5 — medir a completude dos DOIS lados.

    Cara (um EXISTS em `tribunals_movimentacao` por linha), então roda no
    máximo 1×/24 h e o resultado carrega a própria data.
    """
    if not forcar:
        guardado = cache.get(CHAVE_TETO_FASE)
        if guardado:
            return guardado

    def _q(c):
        c.execute('SELECT setseed(0.219)')
        c.execute("""
          WITH am AS (
            SELECT id, fase_codigo FROM tribunals_process TABLESAMPLE SYSTEM (0.01)
          ), sem AS (
            SELECT id FROM am WHERE fase_codigo IS NULL OR fase_codigo = '' LIMIT %s
          )
          SELECT count(*),
                 count(*) FILTER (WHERE EXISTS (
                   SELECT 1 FROM tribunals_movimentacao m
                    WHERE m.processo_id = s.id AND m.codigo_classe <> ''
                      AND m.meio <> 'datajud'))
            FROM sem s
        """, [TETO_AMOSTRA])
        return c.fetchone()

    n, alcancavel = _com_teto(_q, segundos=SQL_TETO_TIMEOUT_S)
    r = {'amostra_sem_fase': n, 'alcancavel': alcancavel,
         'pct_alcancavel': round(100.0 * alcancavel / n, 2) if n else None,
         'medido_em': timezone.now()}
    try:
        cache.set(CHAVE_TETO_FASE, r, TETO_TTL_S)
    except Exception:  # noqa: BLE001 — medir não pode derrubar o tique
        logger.warning('vigia: não guardei o teto do fase', exc_info=True)
    return r


def medir_fks() -> dict:
    """FKs declaradas e ainda `NOT VALID`, e quem está validando agora.

    Catálogo puro: instantâneo, exato e imune a container que sumiu. É o
    exemplo mais limpo do princípio deste módulo — o estado do trabalho está
    no BANCO, não no processo que o executa.
    """
    def _q(c):
        c.execute("""SELECT conrelid::regclass::text, conname
                       FROM pg_constraint
                      WHERE contype = 'f' AND NOT convalidated
                      ORDER BY 1, 2""")
        pend = [[t, n] for t, n in c.fetchall()]
        c.execute("""SELECT pid, EXTRACT(epoch FROM now() - query_start)::int,
                            state, coalesce(wait_event_type, '')
                       FROM pg_stat_activity
                      WHERE pid <> pg_backend_pid()
                        AND query ILIKE '%%VALIDATE CONSTRAINT%%'""")
        act = [[p, s, st, w] for p, s, st, w in c.fetchall()]
        return pend, act

    pend, act = _com_teto(_q, segundos=30)
    return {'pendentes': pend, 'validando': act}


# ─────────────────────────────────────────────────────────────────────────────
# Auto-cura das FKs
# ─────────────────────────────────────────────────────────────────────────────

@job('djen_audit', timeout=3600)
def validar_uma_fk(tabela: str, nome: str) -> dict:
    """`VALIDATE CONSTRAINT` de UMA FK. A unidade de trabalho do tique.

    ⚠️ `VALIDATE CONSTRAINT` pega `SHARE UPDATE EXCLUSIVE`, **que conflita
    consigo mesmo**. Dois em paralelo não se ajudam: o segundo espera e depois
    REFAZ a varredura inteira — medido em 01/09/2026, 37 min de espera para
    revarrer 131 GB. Por isso a guarda em `pg_stat_activity` roda dentro do
    job, e não só no tique: entre enfileirar e executar, alguém pode ter
    começado à mão.

    ⚠️ Recheca `convalidated` antes de gastar a varredura. O job é idempotente
    por construção — validar de novo o que já foi validado seria uma varredura
    de 131 GB para não fazer nada.
    """
    def _estado(c):
        c.execute("""SELECT convalidated FROM pg_constraint co
                       JOIN pg_class t ON t.oid = co.conrelid
                      WHERE co.conname = %s AND t.relname = %s""", [nome, tabela])
        linha = c.fetchone()
        c.execute("""SELECT count(*) FROM pg_stat_activity
                      WHERE pid <> pg_backend_pid() AND state = 'active'
                        AND query ILIKE '%%VALIDATE CONSTRAINT%%'""")
        return (linha[0] if linha else None), c.fetchone()[0]

    validada, ocupados = _com_teto(_estado, segundos=30)
    if validada is None:
        logger.error('vigia fk: %s.%s não existe no catálogo — ou o nome mudou, '
                     'ou a FK nunca foi criada. Não valido o que não sei o que é',
                     tabela, nome)
        return {'skip': 'inexistente', 'fk': nome}
    if validada:
        logger.info('vigia fk: %s.%s já está VALIDADA — nada a fazer', tabela, nome)
        return {'skip': 'ja_validada', 'fk': nome}
    if ocupados:
        logger.warning('vigia fk: já há %d VALIDATE ativo(s) — não abro um '
                       'segundo. Dois VALIDATE não se ajudam: o segundo espera '
                       'e refaz a varredura inteira', ocupados)
        return {'skip': 'ja_validando', 'fk': nome}

    import time
    t0 = time.monotonic()
    logger.info('vigia fk: VALIDATE %s.%s — começou', tabela, nome)
    # 55 min: abaixo do timeout de 3600 s do job, para o erro sair do Postgres
    # com nome e não do RQ como "work-horse morto".
    with transaction.atomic(), connection.cursor() as c:
        c.execute("SET LOCAL statement_timeout = '3300s'")
        c.execute(f'ALTER TABLE "{tabela}" VALIDATE CONSTRAINT "{nome}"')
    dur = time.monotonic() - t0
    logger.info('vigia fk: VALIDADA %s.%s em %.0fs (0 violações — o ALTER teria '
                'falhado com a linha ofensora)', tabela, nome, dur)
    return {'fk': nome, 'tabela': tabela, 'segundos': round(dur, 1)}


def _enfileirar_fk(pendentes: list, validando: list) -> dict:
    """Enfileira NO MÁXIMO uma FK, e só se ninguém estiver validando."""
    r = {'enfileirada': None, 'motivo': None}
    if not pendentes:
        return r
    try:
        if cache.get(CHAVE_FK_OFF):
            r['motivo'] = 'fk_pausada'
            return r
    except Exception:  # noqa: BLE001
        logger.warning('vigia: não li o kill switch das FKs — sigo LIGADO')
    if validando:
        r['motivo'] = 'ja_validando'
        return r

    import django_rq
    fila = django_rq.get_queue('djen_audit')
    tabela, nome = pendentes[0]
    job_id = f'vigia:fk:{nome}'
    # id determinístico: se o job desta FK já está na fila ou rodando, não
    # abrimos um segundo. `fetch_job` devolve None para id que não existe mais,
    # que é justamente o caso em que queremos reenfileirar (auto-cura).
    try:
        existente = fila.fetch_job(job_id)
    except Exception:  # noqa: BLE001
        existente = None
    if existente is not None and existente.get_status(refresh=True) in (
            'queued', 'started', 'deferred', 'scheduled'):
        r['motivo'] = 'ja_em_voo'
        return r
    if existente is not None:
        try:
            existente.delete()          # sobra de execução anterior
        except Exception:  # noqa: BLE001
            pass
    fila.enqueue(validar_uma_fk, tabela, nome, job_id=job_id, job_timeout=3600)
    r['enfileirada'] = nome
    logger.info('vigia fk: enfileirada %s.%s (%d FKs ainda NOT VALID)',
                tabela, nome, len(pendentes))
    return r


# ─────────────────────────────────────────────────────────────────────────────
# Progresso e histórico
# ─────────────────────────────────────────────────────────────────────────────

def _historico() -> list:
    try:
        return list(cache.get(CHAVE_HIST) or [])
    except Exception:  # noqa: BLE001
        return []


def _anexar_historico(agora, digits_falta, fase_falta, fks_pend) -> list:
    h = _historico()
    h.append({'em': agora, 'digits': digits_falta, 'fase': fase_falta,
              'fks': fks_pend})
    h = h[-HIST_MAX:]
    try:
        cache.set(CHAVE_HIST, h, HIST_TTL_S)
    except Exception:  # noqa: BLE001
        logger.warning('vigia: não guardei o histórico', exc_info=True)
    return h


def _progresso(hist: list, campo: str, agora) -> dict:
    """Quanto o pendente de `campo` caiu na janela. Sem estado próprio: sai do
    histórico, e histórico perdido só custa uma janela de espera, não o lugar.
    """
    limite = agora - datetime.timedelta(hours=JANELA_PROGRESSO_H)
    minimo = agora - datetime.timedelta(hours=MIN_JANELA_H)
    antigos = [x for x in hist
               if x.get('em') and x['em'] <= minimo and x.get(campo) is not None]
    candidatos = [x for x in antigos if x['em'] >= limite] or antigos
    if not candidatos:
        return {'veredito': 'aquecendo', 'delta': None, 'horas': None}
    base = min(candidatos, key=lambda x: x['em'])
    atual = next((x[campo] for x in reversed(hist) if x.get(campo) is not None), None)
    if atual is None:
        return {'veredito': 'sem_medida', 'delta': None, 'horas': None}
    horas = (agora - base['em']).total_seconds() / 3600.0
    return {'veredito': None, 'delta': base[campo] - atual,
            'horas': round(horas, 1), 'base': base[campo], 'atual': atual}


# ─────────────────────────────────────────────────────────────────────────────
# O tique
# ─────────────────────────────────────────────────────────────────────────────

def estado() -> dict | None:
    """Último retrato. `None` = o tique nunca rodou (ou o retrato venceu)."""
    try:
        return cache.get(CHAVE_ESTADO)
    except Exception:  # noqa: BLE001
        return None


@job('default', timeout=900)
def tick_vigia_backfills() -> dict:
    """Mede os três, grita quem parou, e enfileira UMA FK por vez.

    Toda medição vai dentro do seu próprio `try`: uma que falha (ES fora,
    Postgres em contenção) não pode apagar as outras duas do retrato — retrato
    que some é exatamente o defeito que este módulo existe para consertar.
    """
    agora = timezone.now()
    r: dict = {'medido_em': agora, 'erros': [], 'pausado': False}

    try:
        r['pausado'] = bool(cache.get(CHAVE_OFF))
    except Exception:  # noqa: BLE001
        logger.warning('vigia: não li o kill switch — sigo LIGADO')

    # ── as três medições, independentes ──────────────────────────────────
    for nome, fn in (('proc_digits', medir_proc_digits),
                     ('fase', medir_fase),
                     ('fks', medir_fks)):
        try:
            r[nome] = fn()
        except Exception as exc:  # noqa: BLE001
            r[nome] = None
            r['erros'].append(f'{nome}: {str(exc)[:200]}')
            logger.error('vigia: a medição de %s FALHOU (%s) — o retrato sai '
                         'sem ela, e "sem medida" NÃO é "está tudo bem"',
                         nome, str(exc)[:200])
    try:
        r['teto_fase'] = medir_teto_fase()
    except Exception as exc:  # noqa: BLE001
        r['teto_fase'] = None
        r['erros'].append(f'teto_fase: {str(exc)[:200]}')

    dig = r.get('proc_digits') or {}
    fase = r.get('fase') or {}
    fks = r.get('fks') or {}
    hist = _anexar_historico(agora, dig.get('faltam'), fase.get('faltam_estimado'),
                             len(fks.get('pendentes') or []) if fks else None)

    r['progresso_digits'] = _progresso(hist, 'digits', agora)
    r['progresso_fase'] = _progresso(hist, 'fase', agora)
    r['janela_h'] = JANELA_PROGRESSO_H

    # ── alarmes ──────────────────────────────────────────────────────────
    r['parados'] = []
    # ⚠️ Tarefa `*byquery` viva no ES = o backfill do índice ESTÁ andando,
    # ainda que o `_count` não tenha mexido na janela (throttle de 500 d/s +
    # refresh do índice). Declarar "parado" aqui faria alguém religar por cima
    # e DOBRAR a carga no nó — ver a docstring.
    tarefas = dig.get('tarefas')
    if tarefas:
        r['parados'].append({
            'o_que': 'proc_digits', 'veredito': 'escrevendo',
            'faltam': dig.get('faltam'), 'tarefas': tarefas})
        logger.info('vigia: `proc_digits` com %d tarefa(s) viva(s) no ES '
                    '(%s) — não é parado', len(tarefas),
                    '; '.join(f"{x['pct']}% a {x['rps']} d/s há {x['minutos']}min"
                              for x in tarefas))
    else:
        _alerta_parado(r, 'proc_digits', dig.get('faltam'), r['progresso_digits'],
                       DIGITS_PISO_ALARME, 0,
                       'es_backfill_proc_digits --rodar')
    _alerta_parado(r, 'fase', fase.get('faltam_estimado'), r['progresso_fase'],
                   FASE_PISO_ALARME,
                   int((fase.get('pct_erro_pp') or 0) / 100.0 * (fase.get('linhas') or 0)),
                   'backfill_fase --de <lo> --ate <hi>')
    _alerta_controles(r, dig)

    # ── auto-cura das FKs ────────────────────────────────────────────────
    pend = fks.get('pendentes') or []
    r['fk_acao'] = {'enfileirada': None, 'motivo': 'pausado' if r['pausado'] else None}
    if pend and not r['pausado']:
        try:
            r['fk_acao'] = _enfileirar_fk(pend, fks.get('validando') or [])
        except Exception as exc:  # noqa: BLE001
            r['fk_acao'] = {'enfileirada': None, 'motivo': f'erro: {str(exc)[:120]}'}
            logger.error('vigia fk: não consegui enfileirar (%s)', str(exc)[:200])
    if pend and not fks.get('validando') and not (r['fk_acao'] or {}).get('enfileirada'):
        logger.error(
            '%d FK(s) continuam NOT VALID, ninguém está validando e nada foi '
            'enfileirado neste tique (motivo: %s). Enquanto NOT VALID, a FK '
            'vale para linha nova mas o passado segue sem prova: %s',
            len(pend), (r['fk_acao'] or {}).get('motivo'),
            [f'{t}.{n}' for t, n in pend[:5]])

    _guardar(r)
    return r


def _alerta_parado(r: dict, rotulo: str, faltam, prog: dict, piso: int,
                   ruido: int, comando: str) -> None:
    """Pendente > 0 e nenhum progresso na janela = ERRO com o número real.

    O `ruido` é o ±2σ da amostragem: sem ele o alarme dispararia sozinho na
    reta final, quando o progresso real fica abaixo do erro de medição. Alarme
    que acende sempre é alarme gasto (lição do detector de frota, 21/08/2026).
    """
    if faltam is None:
        return
    if faltam <= max(piso, ruido):
        r['parados'].append({'o_que': rotulo, 'veredito': 'reta_final',
                             'faltam': faltam})
        return
    if prog.get('veredito'):
        r['parados'].append({'o_que': rotulo, 'veredito': prog['veredito'],
                             'faltam': faltam})
        return
    delta = prog.get('delta') or 0
    if delta > max(ruido, 0):
        return
    r['parados'].append({'o_que': rotulo, 'veredito': 'parado', 'faltam': faltam,
                         'delta': delta, 'horas': prog.get('horas')})
    logger.error(
        'BACKFILL PARADO: `%s` tem %s pendentes e NÃO andou em %.1f h '
        '(variação de %s, ruído da medição ±%s). Nada no sistema estava '
        'dizendo isso — retome com `%s`',
        rotulo, f'{faltam:,}', prog.get('horas') or 0, f'{delta:,}',
        f'{ruido:,}', comando)


def _alerta_controles(r: dict, dig: dict) -> None:
    """Os controles que TÊM que dar certo. Régua sem controle não se publica."""
    if not dig:
        return
    fora = dig.get('fora_da_janela')
    if fora:
        logger.error(
            '%s faltantes de `proc_digits` estão FORA da janela de '
            '`detected_at` que o backfill fatia. A premissa caiu: fatiar por '
            'dia deixaria buraco. Refaça a janela antes de continuar',
            f'{fora:,}')
        r['parados'].append({'o_que': 'proc_digits', 'veredito': 'janela_furada',
                             'faltam': fora})
    if dig.get('amostra_vazio') or dig.get('amostra_errado'):
        logger.error(
            'o `exists` do ES VOLTOU A MENTIR em `proc_digits`: a amostra de '
            '%d docs achou %d vazios e %d malformados. O %s%% publicado pelo '
            '`_count` é teto, não valor',
            dig.get('amostra_n'), dig.get('amostra_vazio'),
            dig.get('amostra_errado'), dig.get('pct_exists'))
        r['parados'].append({'o_que': 'proc_digits', 'veredito': 'exists_mente',
                             'faltam': None})
    pe, pa = dig.get('pct_exists'), dig.get('pct_amostra')
    n = dig.get('amostra_n') or 0
    if pe is not None and pa is not None and n:
        erro_pp = 200.0 * math.sqrt(max((pa / 100) * (1 - pa / 100), 1e-9) / n)
        r['divergencia_pp'] = round(abs(pe - pa), 2)
        r['divergencia_erro_pp'] = round(erro_pp, 2)
        if abs(pe - pa) > erro_pp:
            logger.warning(
                '`exists` diz %.2f%% e a amostra de %d docs diz %.2f%% — '
                'divergência de %.2f pp acima do erro amostral de ±%.2f pp. '
                'Confira o conteúdo antes de usar o número do `_count`',
                pe, n, pa, abs(pe - pa), erro_pp)


def _guardar(r: dict) -> None:
    """Retrato para a tela. Nunca propaga erro — medir não derruba o tique."""
    try:
        cache.set(CHAVE_ESTADO, r, ESTADO_TTL_S)
    except Exception:  # noqa: BLE001
        logger.warning('vigia: não guardei o retrato no cache', exc_info=True)
