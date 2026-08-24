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

Daí as duas guardas deste módulo:

1. **Guarda de disco do ES** — o tick não enfileira nada quando o nó passa de
   `DIARIOS_ES_DISCO_MAX_PCT`. É o critério de parada escrito ANTES virando
   código, em vez de virar um parágrafo de runbook que alguém lê depois do
   incidente.
2. **Orçamento diário por fonte** — teto de unidades coletadas em 24 h. É o que
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
