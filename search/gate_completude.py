"""Gate de completude do índice de PROCESSOS — contagem dos DOIS lados.

## Por que ele existe

Em 31/08/2026, no fim do backfill do sinal do TJSP, o Postgres fechou com **0**
processos sem `tem_sinal_precatorio` e o índice ainda tinha **524.945** docs do
TJSP com o campo em `null`. Os três instrumentos de saúde do
`search/sync_incremental.py` diziam "tudo certo" ao mesmo tempo:

    watermark `sync_es:wm:proc_ts` .... 1 min 23 s de atraso  (em dia)
    fila `es_index` .................. 0                     (drenada)
    FailedJobRegistry(es_index) ...... 0                     (nada falhou)

Nenhum dos três é um gate de completude, e nenhum dos três podia sê-lo:

  · **idade de watermark** mede o cursor, não o acervo. Se o cursor pulou (perda
    da chave do Redis, re-ancoragem, `atualizado_em` de transação longa que faz
    COMMIT abaixo dele), ele fica jovem e saudável justamente porque abandonou
    o passado;
  · **profundidade de fila** mede o que foi ENFILEIRADO. O que nunca entrou na
    fila conta zero — foi assim que 179.490.613 publicações ficaram fora do
    índice com `es_index` marcando 0;
  · **FailedJobRegistry** mede o que falhou barulhento. Job que nunca nasceu não
    falha.

Este módulo não olha para nenhum dos três. Ele conta o Postgres, conta o
Elasticsearch, e subtrai.

## Como ele é barato

Varre por FAIXA DE PK, uma faixa por passada, com cursor próprio e **durável**
(`search/watermarks.py`, tabela `search_watermark` — o gate não pode morrer da
mesma doença que veio diagnosticar). Em cada faixa:

  1. `count(*)` no Postgres pela chave primária — index-only, sem heap;
  2. `_count` no ES com `range` no campo `id`;
  3. **CONTROLE**: `_count` na mesma faixa com `exists: proc`. `proc` é o número
     CNJ e existe em todo documento de processo: se este não der 100% do total
     do ES, a régua está lendo o índice ou o campo errado e a medição inteira é
     descartada, não publicada (regra do plano nº 2 — foi um controle em 0,0%
     que pegou uma régua torta em 30/08). O `exists` do ES conta string vazia
     como valor presente (regra nº 4), então este controle pega régua torta, não
     campo oco — quem exige CONTEÚDO é a amostra do passo 4;
  4. **amostra de CAMPO**: `_mget` de N ids da faixa, comparando o valor do
     documento com o do Postgres. Contagem de documento não pega doc PRESENTE e
     DESATUALIZADO — que é exatamente o caso dos 524.945.

⚠️ A amostra usa `src.get(campo) is not None`, **nunca** `campo in src`. Doc
antigo tem a CHAVE gravada com valor `null` e passa no teste de presença: a
mesma medição deu 60/60 com `in` e 38/60 com `.get(...) is not None`.

## O "antes", medido em 31/08/2026 em produção

Oito faixas de 1 M de pk espalhadas por todo o acervo (topo = 106.326.832),
`reparar=False`:

    faixa                       PG          ES     delta   controle   amostra
    [  2.126.536, +1 M)     997.576     997.576       0     100,0%    500 · 0 div
    [ 15.949.024, +1 M)     997.290     997.290       0     100,0%    500 · 0 div
    [ 31.898.049, +1 M)     999.428     999.428       0     100,0%    500 · 0 div
    [ 47.847.074, +1 M)     999.484     999.484       0     100,0%    500 · 0 div
    [ 63.796.099, +1 M)     995.863     995.863       0     100,0%    500 · 0 div
    [ 79.745.124, +1 M)     999.419     999.419       0     100,0%    500 · 0 div
    [ 95.694.148, +1 M)     992.419     992.419       0     100,0%    500 · 0 div
    [104.731.929, +1 M)     992.066     992.066       0     100,0%    500 · 0 div
    ------------------------------------------------------------------------
    total                 7.973.545   7.973.545       0     100,0%  4.000 · 0

Delta zero em 7,5% do acervo e campo de controle em 100% nas oito — a régua
está calibrada e, no recorte medido, o índice está em dia (o buraco de 31/08 já
tinha sido reenfileirado no mesmo dia). Isto é o "antes": o gate nasce sabendo
qual é o número quando está tudo certo, para que a primeira faixa que NÃO fechar
seja achado, e não ruído.

## O que ele faz com o que acha

Divergência é ERRO registrado com o número real (regra nº 2) e, se o reparo
estiver ligado, os ids faltantes/divergentes vão para a fila `es_index` em lote
— o mesmo caminho que, em 31/08/2026, levou os 524.945 a zero em minutos.
Reparo tem TETO; teto atingido é outro ERRO, nunca um corte mudo.
"""
import logging
import os

from django.db import connection, transaction

from search import gate
from search import watermarks as wm_store

logger = logging.getLogger('voyager.search.gate')

#: cursor da varredura — durável, igual às watermarks do sync.
WM_FAIXA = 'sync_es:gate:faixa_proc'

#: largura da faixa de pk por passada. 1 M de pks é um `count(*)` index-only
#: curto e um `_count` de `range` no ES, e a 1 passada/h fecha os ~106 M do
#: acervo em ~4,4 dias — cadência de gate, não de alarme de incêndio.
FAIXA = int(os.environ.get('GATE_ES_FAIXA', 1_000_000))

#: quantos ids da faixa entram na amostra de CAMPO (`_mget` realtime).
AMOSTRA = int(os.environ.get('GATE_ES_AMOSTRA', 500))

#: teto de ids lidos do Postgres para achar QUEM falta, depois que a contagem
#: já acusou. Bater nele é ERRO — a faixa fica marcada como "não fechada".
TETO_REPARO = int(os.environ.get('GATE_ES_TETO_REPARO', 200_000))

#: tetos de espera. Nada no caminho do scheduler sem teto (regra nº 7).
TIMEOUT_PG = os.environ.get('GATE_ES_TIMEOUT_PG', '60s')
TIMEOUT_ES = int(os.environ.get('GATE_ES_TIMEOUT_ES', 60))

#: campo booleano do doc de processo cuja defasagem já custou 524.945 docs.
#: Chave = campo no ES; valor = coluna no Postgres. Os nomes coincidem aqui, e
#: isso NÃO é regra da casa: `voyager-acervo` usa `classe_codigo` enquanto
#: `voyager-processos` usa `codigo_classe`, invertidos. Por isso o par é
#: explícito em vez de adivinhado.
CAMPOS_AMOSTRA = {'tem_sinal_precatorio': 'tem_sinal_precatorio'}

#: campo de CONTROLE: tem que dar 100% em toda faixa, sempre.
CAMPO_CONTROLE = 'proc'


def _pg(sql: str, params: list, tmo: str = TIMEOUT_PG):
    """Consulta com teto de espera. Levanta — quem chama decide abster."""
    with transaction.atomic(), connection.cursor() as c:
        c.execute('SET LOCAL statement_timeout = %s', [tmo])
        c.execute(sql, params)
        return c.fetchall()


def _faixa_query(ini: int, fim: int) -> dict:
    return {'range': {'id': {'gte': ini, 'lt': fim}}}


def contar_no_pg(ini: int, fim: int) -> int | None:
    try:
        return _pg('SELECT count(*) FROM tribunals_process '
                   'WHERE id >= %s AND id < %s', [ini, fim])[0][0]
    except Exception:
        logger.warning('gate completude: count no PG estourou %s na faixa '
                       '[%s, %s) — abstendo.', TIMEOUT_PG, ini, fim,
                       exc_info=True)
        return None


def contar_no_es(ini: int, fim: int, com_controle: bool = False) -> int | None:
    consulta = _faixa_query(ini, fim)
    if com_controle:
        consulta = {'bool': {'filter': [consulta,
                                        {'exists': {'field': CAMPO_CONTROLE}}]}}
    try:
        return gate._es().count(index=gate.indice_processos(), query=consulta,
                                request_timeout=TIMEOUT_ES)['count']
    except Exception:
        logger.warning('gate completude: ES mudo na faixa [%s, %s) — abstendo.',
                       ini, fim, exc_info=True)
        return None


def amostrar_campos(ini: int, fim: int, n: int = AMOSTRA) -> dict | None:
    """Compara o VALOR dos campos numa amostra da faixa. Pega doc desatualizado.

    Contagem de documento não vê a doença de 31/08/2026: o doc estava lá, com o
    campo em `null`. Aqui os dois lados são lidos e comparados de verdade.
    """
    colunas = sorted(set(CAMPOS_AMOSTRA.values()))
    try:
        linhas = _pg(
            'SELECT id, ' + ', '.join(colunas) + ' FROM tribunals_process '
            'WHERE id >= %s AND id < %s ORDER BY id LIMIT %s', [ini, fim, n])
    except Exception:
        logger.warning('gate completude: amostra no PG estourou na faixa '
                       '[%s, %s) — abstendo.', ini, fim, exc_info=True)
        return None
    if not linhas:
        return {'n': 0, 'controle': None, 'divergentes': [], 'ausentes': []}

    por_id = {r[0]: dict(zip(colunas, r[1:])) for r in linhas}
    fonte = [CAMPO_CONTROLE] + sorted(CAMPOS_AMOSTRA)
    try:
        r = gate._es().mget(index=gate.indice_processos(),
                            ids=[str(i) for i in por_id], source=fonte,
                            request_timeout=TIMEOUT_ES)
    except Exception:
        logger.warning('gate completude: ES mudo no _mget da faixa [%s, %s) — '
                       'abstendo.', ini, fim, exc_info=True)
        return None

    achados = controle_ok = 0
    ausentes: list[int] = []
    divergentes: list[int] = []
    for d in r['docs']:
        pid = int(d['_id'])
        if not d.get('found'):
            ausentes.append(pid)
            continue
        achados += 1
        src = d.get('_source') or {}
        # `'proc' in src` MENTE: doc antigo tem a chave com valor `null` e passa
        # no teste de presença. A mesma amostra deu 60/60 com `in` e 38/60 com
        # `.get(...) is not None`.
        #
        # E aqui o teste é de VALOR, não de presença, porque o `exists` do lado
        # da contagem conta string vazia como valor presente (regra nº 4). A
        # amostra é o único lugar onde dá para exigir conteúdo de verdade.
        if src.get(CAMPO_CONTROLE):
            controle_ok += 1
        for campo, coluna in CAMPOS_AMOSTRA.items():
            no_pg = por_id[pid][coluna]
            no_es = src.get(campo)
            # abstenção do lado do PG (`NULL`) não é divergência: o gate mede
            # atraso de ENTREGA, não decide o valor do campo.
            if no_pg is not None and no_es != no_pg:
                divergentes.append(pid)
                break
    controle = None if not achados else round(100.0 * controle_ok / achados, 2)
    return {'n': len(por_id), 'achados': achados, 'controle': controle,
            'ausentes': ausentes, 'divergentes': divergentes}


def conferir_faixa(ini: int, fim: int, reparar: bool = True) -> dict:
    """Uma faixa de pk conferida dos DOIS lados. Nunca levanta."""
    saida: dict = {'faixa': [ini, fim]}
    pg = contar_no_pg(ini, fim)
    es = contar_no_es(ini, fim)
    controle_es = contar_no_es(ini, fim, com_controle=True)
    saida.update({'pg': pg, 'es': es})

    if pg is None or es is None or controle_es is None:
        saida['abstido'] = 'contagem não fechou dos dois lados'
        logger.warning('gate completude: faixa [%s, %s) ABSTIDA — pg=%s es=%s '
                       'controle=%s.', ini, fim, pg, es, controle_es)
        return saida

    # CONTROLE: `proc` existe em todo doc de processo. Se ele não der 100% do
    # que o índice diz ter, a régua está errada e a medição não se publica.
    saida['controle_pct'] = None if not es else round(100.0 * controle_es / es, 2)
    if es and controle_es != es:
        logger.error(
            'gate completude: CONTROLE FALHOU na faixa [%s, %s) — o índice diz '
            'ter %d docs mas só %d têm `%s`. A régua está lendo o campo ou o '
            'índice errado; a medição desta faixa vai FORA.',
            ini, fim, es, controle_es, CAMPO_CONTROLE)
        saida['abstido'] = 'controle abaixo de 100%'
        return saida

    saida['delta'] = pg - es
    amostra = amostrar_campos(ini, fim)
    saida['amostra'] = amostra

    faltando: list[int] = []
    if amostra and amostra.get('controle') not in (None, 100.0):
        logger.error(
            'gate completude: CONTROLE da amostra em %.2f%% na faixa [%s, %s) — '
            'a amostra vai fora.', amostra['controle'], ini, fim)
        saida['abstido'] = 'controle da amostra abaixo de 100%'
        return saida

    suspeita = saida['delta'] > 0 or bool(
        amostra and (amostra['ausentes'] or amostra['divergentes']))
    if not suspeita:
        return saida

    logger.error(
        'gate completude: faixa [%s, %s) NÃO fecha — Postgres %d × ES %d '
        '(delta %d). Amostra de campo: %s ausentes, %s divergentes de %s. Isso '
        'NÃO aparece na idade da watermark nem na profundidade da fila.',
        ini, fim, pg, es, saida['delta'],
        'n/d' if not amostra else len(amostra['ausentes']),
        'n/d' if not amostra else len(amostra['divergentes']),
        'n/d' if not amostra else amostra['n'])

    if not reparar:
        return saida

    # Só agora sai caro: ler os ids da faixa para saber QUEM falta.
    try:
        ids = [r[0] for r in _pg(
            'SELECT id FROM tribunals_process WHERE id >= %s AND id < %s '
            'ORDER BY id LIMIT %s', [ini, fim, TETO_REPARO + 1])]
    except Exception:
        logger.error('gate completude: leitura dos ids da faixa [%s, %s) '
                     'estourou %s — faixa NÃO reparada e NÃO fechada.',
                     ini, fim, TIMEOUT_PG, exc_info=True)
        saida['reparo'] = 'leitura estourou'
        return saida
    if len(ids) > TETO_REPARO:
        # Teto é alerta, nunca corte mudo (regra nº 2).
        logger.error(
            'gate completude: TETO de %d ids atingido na faixa [%s, %s) — '
            'reparo PARCIAL, o resto da faixa continua sem conferência fina.',
            TETO_REPARO, ini, fim)
        saida['teto_reparo'] = True
        ids = ids[:TETO_REPARO]

    try:
        for i in range(0, len(ids), gate.BLOCO_TERMS):
            faltando.extend(gate.ausentes_no_bloco(ids[i:i + gate.BLOCO_TERMS],
                                                   gate.indice_processos()))
    except Exception:
        logger.error('gate completude: ES mudo ao listar ausentes da faixa '
                     '[%s, %s) — nada enfileirado.', ini, fim, exc_info=True)
        saida['reparo'] = 'ES mudo'
        return saida

    alvo = sorted(set(faltando) | set((amostra or {}).get('divergentes') or []))
    saida['ausentes'] = len(faltando)
    try:
        saida['reenfileirados'] = gate.enfileirar_processos(alvo)
    except Exception:
        logger.error('gate completude: enqueue dos %d ausentes da faixa '
                     '[%s, %s) FALHOU — o cursor NÃO avança.', len(alvo), ini,
                     fim, exc_info=True)
        saida['reparo'] = 'enqueue falhou'
        return saida
    logger.error('gate completude: faixa [%s, %s) — %d processos estavam no '
                 'Postgres e FORA do índice; reenfileirados.',
                 ini, fim, len(alvo))
    return saida


def _topo() -> int | None:
    try:
        return _pg('SELECT max(id) FROM tribunals_process', [], '10s')[0][0]
    except Exception:
        logger.warning('gate completude: não consegui ler max(id).', exc_info=True)
        return None


def tick_gate_completude(reparar: bool = True) -> dict:
    """Uma faixa por passada, varrendo o acervo em ciclo. Nunca levanta.

    O cursor é DURÁVEL de propósito: um gate que perde a posição no restart do
    Redis re-começa do zero (ou, pior, do topo) e vira o mesmo silêncio verde
    que ele existe para matar.
    """
    topo = _topo()
    if topo is None:
        return {'abstido': 'topo não medido'}

    ini, estado = wm_store.obter(WM_FAIXA)
    if estado == 'indisponivel':
        return {'abstido': 'cursor não legível'}
    if estado in ('primeiro', 'perdida'):
        # Cursor perdido é barato: o gate recomeça do MENOR pk e varre tudo de
        # novo. Nenhuma faixa é abandonada — é o oposto da watermark do sync.
        ini = 0
        logger.info('gate completude: cursor %s — recomeçando a varredura do '
                    'pk 0.', estado)
    ini = int(ini or 0)
    if ini > topo:
        ini = 0

    fim = min(ini + FAIXA, topo + 1)
    saida = conferir_faixa(ini, fim, reparar=reparar)

    # Faixa que não fechou (abstenção ou reparo incompleto) NÃO avança o cursor:
    # ela volta na próxima passada. Não fechar é diferente de estar em dia.
    if saida.get('abstido') or saida.get('reparo') or saida.get('teto_reparo'):
        saida['cursor'] = ini
        return saida
    proximo = 0 if fim > topo else fim
    wm_store.gravar(WM_FAIXA, proximo)
    saida['cursor'] = proximo
    if proximo == 0:
        logger.info('gate completude: volta completa no acervo (topo=%s).', topo)
    return saida


def agendar_gate_completude() -> dict:
    """Enfileira UMA passada, com `job_id` fixo. Chamado pelo scheduler.

    Fila `default` e nunca `es_index`: durante um backfill a fila da indexação
    tem dezenas de milhares de jobs à frente, e gate que roda tarde é gate que
    não roda. `job_id` determinístico impede empilhamento.
    """
    import django_rq

    django_rq.get_queue('default').enqueue(
        tick_gate_completude, job_id='search:gate_completude', job_timeout=1800)
    return {'enfileirado': True}
