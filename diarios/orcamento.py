"""Orçamento da terceira porta — o teto que a fila `diarios` não tinha.

POR QUE ESTE ARQUIVO EXISTE
===========================
O `WATERMARK_POR_FONTE=200` do `diarios/jobs.py` limita a PROFUNDIDADE da fila,
não a VAZÃO: assim que os workers drenam, o tick reabastece. Com o agendamento
ligado, quem define o ritmo é o número de réplicas do `worker_diarios` — e esse
número foi escolhido por CPU, não por disco.

A conta que obriga a existir um teto de vazão, toda ela medida em produção em
24/08/2026, antes de ligar qualquer coisa:

    catálogo do `tjsp-dje` ............ 33.296 unidades (32.616 dentro da janela)
    itens medidos no dia 12/03/2025 ... 220.548 em 8 cadernos ⇒ 27.568/caderno
    projeção do backfill inteiro ...... ~7,3e8 linhas
    custo por doc no ES .............. 1,06 KB (1,5 TB / 1.517.713.033 docs)
    ⇒ o backfill do DJE/TJSP pede ~772 GB de índice
    ES hoje .......................... 1,8 TB usados de 2,9 TB — 1,0 TB livre

Ou seja: o backfill completo **não cabe** no nó de Elasticsearch de hoje, e o
jeito de descobrir isso NÃO pode ser o índice entrar em `flood_stage` e virar
read-only às 3 da manhã. O `read_only_allow_delete` do ES é global por índice e
derruba a ingestão das TRÊS portas, não só desta.

Daí as três guardas deste módulo:

1. **Guarda de disco do ES** — o tick não enfileira nada quando o nó passa de
   `DIARIOS_ES_DISCO_MAX_PCT`. É o critério de parada escrito ANTES virando
   código, em vez de virar um parágrafo de runbook que alguém lê depois do
   incidente.
2. **Guarda de FILA do índice** — o tick não enfileira quando a `es_index` já
   está funda. Nesta casa "coletado" só vale quando é BUSCÁVEL (§12 do
   `.ia/DIARIOS.md`), e uma edição que entra no fim de uma fila de horas está
   gravada e invisível. Medido em 24/08/2026, DURANTE a etapa 1 e com outro
   backfill (14,4 milhões de processos) no mesmo nó de ES: amostra aleatória
   declarada de 250 das 652 falhas do `FailedJobRegistry` da `es_index` deu
   **26 `indexar_movimentacoes_bulk` com `ConnectionTimeout`, todas nas 3 h
   anteriores** (as outras 224 são `indexar_processos_bulk|ValueError`, bug
   antigo e alheio). O `write` thread pool do ES estava com `active=16`,
   `queue=33`, `rejected=0`. Ou seja: o índice estava aceitando, mas no limite —
   e a terceira porta é a que deve ceder, porque é a única das três que pode
   esperar.
3. **Orçamento diário por fonte** — teto de unidades coletadas em 24 h. É o que
   permite "ligar em etapas" sem alguém de plantão: a fonte anda o que foi
   autorizado a andar e para sozinha.

DUAS DECISÕES QUE PARECEM ERRADAS E SÃO DE PROPÓSITO
-----------------------------------------------------
**A guarda de disco falha FECHADA.** Se o ES não responder dentro do teto, o
tick NÃO enfileira. Abster aqui não é "não sei, então tanto faz": o custo de
uma parada falsa é 10 minutos de atraso (o tick volta), e o custo de um "vai"
falso é encher um disco que está a 63% com uma porta que escreve ~980 mil
linhas/hora. Assimetria decidida com o número na mão.

**O teto é ALERTA, não corte mudo** (regra nº 2 do CLAUDE.md). Atingir o
orçamento devolve o motivo e o número no retorno do job e no log, e as unidades
continuam `pendente` — dívida visível, não desaparecimento. Nenhuma unidade é
descartada, nenhum `EdicaoDiario` muda de status.
"""

import datetime as dt
import logging

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger('voyager.diarios.orcamento')

#: Percentual de uso do disco do nó de ES a partir do qual o tick para de
#: enfileirar. 85% é o `cluster.routing.allocation.disk.watermark.low` padrão do
#: ES; 90% é o `high` e 95% o `flood_stage`, que marca o índice
#: `read_only_allow_delete` e derruba a escrita das TRÊS portas. Parar no `low`
#: dá 5 pontos percentuais de folga — a 2,9 TB de disco, ~145 GB, ou ~5 dias no
#: pior ritmo medido desta porta.
ES_DISCO_MAX_PCT_PADRAO = 85.0
#: Teto de espera da leitura do disco do ES. Regra nº 7: nada no caminho sem
#: teto de espera — e isto roda dentro do `tick`, que é um cron.
ES_TIMEOUT_DISCO = 10
#: Profundidade da fila `es_index` acima da qual o tick para. A conta, com os
#: números desta casa: cada job carrega 500 documentos e um `_bulk` de 500 leva
#: **4,13 s** (medido em 21/08/2026); a `.102` roda **24** `worker_es_index`.
#: 5.000 jobs = 2,5 milhões de documentos ≈ **15 min** de dreno com o índice
#: LIVRE — e horas com ele sob contenção, que é justamente quando isto importa.
#: Um caderno do DJE/TJSP rende ~55 jobs; enfileirar mais coleta contra uma fila
#: dessas é produzir invisibilidade. Muito abaixo do `FILA_ES_ALTA=150.000` do
#: `search/sync_incremental.py` de propósito: lá o freio protege o poller de si
#: mesmo, aqui a terceira porta cede a vez para as outras duas.
FILA_ES_MAX_PADRAO = 5_000


def _chave_env(fonte: str) -> str:
    """`tjsp-dje` → `TJSP_DJE`, o mesmo dialeto de `DIARIOS_RPS_<SLUG>`."""
    return fonte.upper().replace('-', '_')


def teto_diario(fonte: str) -> int:
    """Unidades por 24 h autorizadas para a fonte. `0` = sem teto.

    Lê `DIARIOS_TETO_UNIDADES_DIA_<SLUG>` e, na falta dele, o global
    `DIARIOS_TETO_UNIDADES_DIA`. Ajustável por env sem deploy, que é o que
    importa quando a decisão de acelerar ou frear é tomada às 2 da manhã.
    """
    especifico = getattr(settings, f'DIARIOS_TETO_UNIDADES_DIA_{_chave_env(fonte)}', None)
    if especifico is not None:
        return max(int(especifico), 0)
    return max(int(getattr(settings, 'DIARIOS_TETO_UNIDADES_DIA', 0) or 0), 0)


def coletadas_24h(fonte: str) -> int:
    """Unidades da fonte FECHADAS nas últimas 24 h.

    Conta `EdicaoDiario.coletado_em`, que só é preenchido em `ok`/`vazia` (ver
    `EdicaoDiario.marcar`). Unidade que falhou não consome orçamento de
    propósito: o custo dela para o banco foi zero, e cobrá-la faria uma fonte
    quebrada travar o orçamento das próprias tentativas de conserto.

    Recorte por `coletado_em` num índice que não existe? Não: o filtro tem
    `fonte` na frente e a tabela `diarios_edicaodiario` tem 8 linhas hoje e
    ordem de 33 mil no fim do backfill do TJSP — três ordens de grandeza abaixo
    do que exigiria índice próprio.
    """
    from .models import EdicaoDiario

    corte = timezone.now() - dt.timedelta(hours=24)
    return EdicaoDiario.objects.filter(fonte=fonte, coletado_em__gte=corte).count()


def folga_do_orcamento(fonte: str) -> int | None:
    """Quantas unidades ainda cabem no orçamento de 24 h. `None` = sem teto."""
    teto = teto_diario(fonte)
    if teto <= 0:
        return None
    return max(teto - coletadas_24h(fonte), 0)


def disco_do_es() -> dict | None:
    """Uso de disco do nó de ES. `None` = não deu para medir (e isso FECHA).

    Lê `_cat/allocation`, que é o mesmo número que o ES usa para decidir os
    próprios watermarks. `_nodes/stats/fs` daria bytes com mais precisão, mas
    devolve payload grande e o que interessa aqui é um percentual.
    """
    try:
        from search import gate

        linhas = gate._es().cat.allocation(format='json', request_timeout=ES_TIMEOUT_DISCO)
        for linha in linhas:
            pct = linha.get('disk.percent')
            if pct in (None, '', 'null'):
                continue                      # a linha UNASSIGNED não tem disco
            return {'usado_pct': float(pct), 'total': linha.get('disk.total'),
                    'livre': linha.get('disk.avail'), 'no': linha.get('node')}
    except Exception:
        logger.warning('orçamento: não consegui ler o disco do ES — abstendo', exc_info=True)
        return None
    return None


def fila_do_indice() -> int | None:
    """Profundidade da fila `es_index`. `None` = não deu para medir (FECHA).

    `Queue.count` é um `LLEN` no Redis: barato o bastante para o caminho de um
    cron de 10 minutos.
    """
    try:
        import django_rq

        return int(django_rq.get_queue('es_index').count)
    except Exception:
        logger.warning('orçamento: não consegui ler a fila es_index — abstendo', exc_info=True)
        return None


def guarda_do_indice() -> tuple[bool, str]:
    """A fila do índice aguenta mais coleta agora? `(pode, motivo)`.

    Existe porque nesta casa "coletado" e "buscável" não são a mesma palavra
    (§12 do `.ia/DIARIOS.md`). Enfileirar caderno contra uma `es_index` funda
    não acelera nada: só troca linha invisível no Postgres por linha invisível
    no Postgres com um job a mais esperando. Fecha quando não consegue medir,
    pelo mesmo motivo da guarda de disco.
    """
    teto = int(getattr(settings, 'DIARIOS_FILA_ES_MAX', FILA_ES_MAX_PADRAO))
    if teto <= 0:
        return True, 'guarda de fila do índice sem teto (DIARIOS_FILA_ES_MAX<=0)'
    n = fila_do_indice()
    if n is None:
        return False, ('não consegui medir a fila `es_index` — a guarda FECHA por '
                       'decisão declarada (ver diarios/orcamento.py)')
    if n >= teto:
        return False, (f'fila `es_index` com {n} jobs (teto {teto}) — coletar agora '
                       f'produziria linha gravada e não buscável')
    return True, f'fila `es_index` com {n} jobs (teto {teto})'


def guarda_de_disco() -> tuple[bool, str]:
    """Pode enfileirar coleta agora? Devolve `(pode, motivo)`.

    FECHA quando não consegue medir. Ver o cabeçalho do módulo para a
    assimetria que justifica isso: parada falsa custa 10 minutos, "vai" falso
    custa um índice em `read_only_allow_delete`.
    """
    if not bool(getattr(settings, 'DIARIOS_GUARDA_DISCO_ENABLED', True)):
        return True, 'guarda de disco desligada (DIARIOS_GUARDA_DISCO_ENABLED=0)'
    teto = float(getattr(settings, 'DIARIOS_ES_DISCO_MAX_PCT', ES_DISCO_MAX_PCT_PADRAO))
    if teto <= 0:
        return True, 'guarda de disco sem teto (DIARIOS_ES_DISCO_MAX_PCT<=0)'
    medida = disco_do_es()
    if medida is None:
        return False, ('não consegui medir o disco do ES — a guarda FECHA por decisão '
                       'declarada (ver diarios/orcamento.py)')
    if medida['usado_pct'] >= teto:
        return False, (f"disco do ES em {medida['usado_pct']:.0f}% (teto {teto:.0f}%, "
                       f"livre {medida['livre']} de {medida['total']})")
    return True, f"disco do ES em {medida['usado_pct']:.0f}% (teto {teto:.0f}%)"


def guarda_de_recursos() -> tuple[bool, str]:
    """As guardas de infraestrutura numa chamada só, na ordem mais barata.

    Disco antes de fila porque o disco é o teto ESTRUTURAL (não drena sozinho) e
    a fila é conjuntural. As duas juntas são o que o `tick` pergunta antes de
    tocar em qualquer coisa.
    """
    if not bool(getattr(settings, 'DIARIOS_GUARDA_DISCO_ENABLED', True)):
        return True, 'guardas de recurso desligadas (DIARIOS_GUARDA_DISCO_ENABLED=0)'
    pode, motivo = guarda_de_disco()
    if not pode:
        return False, motivo
    pode_fila, motivo_fila = guarda_do_indice()
    if not pode_fila:
        return False, motivo_fila
    return True, f'{motivo}; {motivo_fila}'
