"""Jobs da busca ao vivo: um por tribunal, mais a hidratação do que for achado.

Fila PRÓPRIA (`busca_ao_vivo`), e isso é decisão, não detalhe: as filas
`enrich_*` carregam centenas de milhares de itens de backlog e a `manual` é do
enricher. Busca é ação de usuário esperando resposta — dividir fila com
trabalho em massa é como uma consulta de tela vira "rodou de madrugada".

Cada tribunal roda em seu próprio job porque as fontes têm ritmos muito
diferentes (0,2 s no e-SAJ do TJAL, 71 s numa página cheia do TJSP, 21 s num
POST do PJe): serializar os nove faria a resposta valer o pior deles.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict

import django_rq
from django.db import transaction
from django.utils import timezone
from django_rq import job

from .base import CriterioIndisponivel, FonteIndisponivel, RefinarBusca
from .registry import CATALOGO, TribunalSemBusca, buscador

logger = logging.getLogger('voyager.busca.jobs')

FILA = 'busca_ao_vivo'
FILA_HIDRATACAO = 'busca_hidratacao'

#: Teto de páginas por tribunal. No e-SAJ são 25 por página, então 10 páginas =
#: 250 processos; no PJe existe uma página só. Bater o teto é ERRO registrado
#: com o número real, e a resposta marca `truncado`.
TETO_PAGINAS = 10

#: Teto de tempo por tribunal. O e-SAJ já levou 71 s numa única página (CNPJ com
#: mil processos), então o teto não pode ser apertado; mas também não pode ser o
#: timeout do job, senão o run perde o que já tinha colhido.
TETO_TEMPO_S = 180

JOB_TIMEOUT = TETO_TEMPO_S + 120

#: Estados terminais de um tribunal dentro do run.
OK = 'ok'
VAZIO = 'vazio'
RECUSADO = 'criterio_indisponivel'
REFINAR = 'refinar'
INDISPONIVEL = 'fonte_indisponivel'
ERRO = 'erro'
TERMINAIS = frozenset({OK, VAZIO, RECUSADO, REFINAR, INDISPONIVEL, ERRO})


def iniciar(run) -> dict:
    """Fan-out: um job por tribunal do run. Devolve o que foi recusado na porta.

    A recusa por critério acontece AQUI, antes de qualquer requisição: se o
    TJPA não busca por nome de advogado, dizer isso custa zero e é a resposta
    certa — enfileirar o job para ele devolver vazio seria transformar "esta
    fonte não tem esse campo" em "não achei nada".
    """
    fila = django_rq.get_queue(FILA)
    recusados, enfileirados = [], []
    for sigla in run.tribunais:
        fonte = CATALOGO.get(sigla)
        if not fonte:
            recusados.append({'tribunal': sigla, 'motivo': 'tribunal_sem_busca'})
            continue
        if run.criterio not in fonte.criterios:
            recusados.append({'tribunal': sigla, 'motivo': RECUSADO})
            continue
        enfileirados.append(sigla)

    with transaction.atomic():
        travado = type(run).objects.select_for_update().get(pk=run.pk)
        for item in recusados:
            travado.por_tribunal[item['tribunal']] = {
                'status': item['motivo'],
                'mensagem': _mensagem_de_recusa(item['tribunal'], run.criterio),
                'encontrados': 0,
            }
        for sigla in enfileirados:
            travado.por_tribunal[sigla] = {'status': 'na_fila', 'encontrados': 0}
        travado.save(update_fields=['por_tribunal'])

    for sigla in enfileirados:
        fila.enqueue(buscar_no_tribunal, str(run.pk), sigla, job_timeout=JOB_TIMEOUT)

    if not enfileirados:
        _fechar_se_terminou(str(run.pk))
    return {'enfileirados': enfileirados, 'recusados': recusados}


def _mensagem_de_recusa(sigla: str, criterio: str) -> str:
    from .base import ROTULOS
    return (f'{sigla} não oferece busca por {ROTULOS.get(criterio, criterio)} '
            f'na consulta pública')


@job(FILA, timeout=JOB_TIMEOUT)
def buscar_no_tribunal(run_id: str, sigla: str) -> dict:
    """Consulta UMA fonte e vai gravando o que colhe, página a página.

    Gravar a cada página, e não no fim, é o que permite a tela mostrar
    resultado enquanto os tribunais lentos ainda respondem — e o que preserva o
    que já foi colhido se a fonte cair no meio.
    """
    from tribunals.models import BuscaTribunalRun

    run = BuscaTribunalRun.objects.get(pk=run_id)
    inicio = time.monotonic()
    _atualizar(run_id, sigla, {'status': 'buscando', 'paginas_lidas': 0})

    paginas, colhidos, truncado, motivo_truncagem = 0, [], False, ''
    estado = {'status': OK}
    try:
        motor = buscador(sigla)
        for pagina in motor.paginar(run.criterio, run.valor, teto_paginas=TETO_PAGINAS):
            paginas += 1
            colhidos.extend(pagina.itens)
            _atualizar(run_id, sigla, {
                'status': 'buscando',
                'paginas_lidas': paginas,
                'total_declarado': pagina.total_declarado,
                'total_e_teto': pagina.total_e_teto,
                'encontrados': len(colhidos),
                'aviso_fonte': pagina.aviso_fonte,
            }, itens=pagina.itens)

            if pagina.total_e_teto:
                truncado = True
                motivo_truncagem = (
                    f'a fonte limita a resposta a {motor.TETO_DA_FONTE} '
                    f'processos por consulta')
            elif (pagina.total_declarado or 0) > len(colhidos) and not pagina.tem_proxima:
                # A fonte CONTOU mais do que mostrou e não oferece continuação —
                # é o caso do TRF5, que conta 16 e renderiza uma linha. Sem esta
                # marca, a resposta entregaria 1 processo como se fosse tudo.
                truncado = True
                motivo_truncagem = (
                    f'a fonte contou {pagina.total_declarado} processos e '
                    f'devolveu {len(colhidos)}, sem oferecer página seguinte')
            if paginas >= TETO_PAGINAS and pagina.tem_proxima:
                truncado = True
                motivo_truncagem = f'teto de {TETO_PAGINAS} páginas por tribunal'
                logger.error('busca %s: teto de páginas atingido em %s '
                             '(colhidos=%d, a fonte declara=%s)',
                             run_id, sigla, len(colhidos), pagina.total_declarado)
                break
            if time.monotonic() - inicio > TETO_TEMPO_S:
                truncado = True
                motivo_truncagem = f'teto de {TETO_TEMPO_S}s por tribunal'
                logger.error('busca %s: teto de tempo atingido em %s '
                             '(colhidos=%d em %d páginas)',
                             run_id, sigla, len(colhidos), paginas)
                break

        estado['status'] = OK if colhidos else VAZIO

    except (CriterioIndisponivel, TribunalSemBusca) as exc:
        estado = {'status': RECUSADO, 'mensagem': str(exc)}
    except RefinarBusca as exc:
        # A fonte se recusou a responder porque a busca é ampla demais. É
        # resposta dela, não falha nossa — e a mensagem é dela, palavra por
        # palavra.
        estado = {'status': REFINAR, 'mensagem': str(exc)}
    except FonteIndisponivel as exc:
        estado = {'status': INDISPONIVEL, 'mensagem': str(exc)}
    except Exception as exc:
        logger.exception('busca %s: falha inesperada em %s', run_id, sigla)
        estado = {'status': ERRO, 'mensagem': f'{type(exc).__name__}: {str(exc)[:200]}'}

    estado.update({
        'paginas_lidas': paginas,
        'encontrados': len(colhidos),
        'truncado': truncado,
        'motivo_truncagem': motivo_truncagem,
        'levou_s': round(time.monotonic() - inicio, 1),
    })
    _atualizar(run_id, sigla, estado)
    _ingerir(run_id, [i.numero_cnj for i in colhidos])
    _fechar_se_terminou(run_id)
    return {'tribunal': sigla, **estado}


@job(FILA_HIDRATACAO, timeout=300)
def hidratar_achado(cnj: str) -> dict:
    """Traz para o acervo um processo que a busca encontrou.

    Reusa `hidratar_cnj` inteiro: cria o `Process`, puxa os movimentos do
    Datajud e enfileira o enricher do tribunal. Nada de novo é escrito por aqui.
    """
    from datajud.hidratacao import hidratar_cnj

    resultado = hidratar_cnj(cnj)
    logger.info('busca: hidratado %s -> %s', cnj, resultado.get('estado'))
    return resultado


# ── estado do run ────────────────────────────────────────────────────────────

def _atualizar(run_id: str, sigla: str, dados: dict, itens=()) -> None:
    """Escrita curta e travada: N jobs mexem no MESMO run ao mesmo tempo.

    Sem `select_for_update`, dois tribunais terminando juntos leriam o mesmo
    JSON e o último gravaria por cima do outro — o clássico read-modify-write
    perdido, que aqui apagaria o resultado de um tribunal inteiro.
    """
    from tribunals.models import BuscaTribunalRun

    with transaction.atomic():
        run = BuscaTribunalRun.objects.select_for_update().get(pk=run_id)
        estado = dict(run.por_tribunal.get(sigla) or {})
        estado.update(dados)
        run.por_tribunal[sigla] = estado
        campos = ['por_tribunal']
        if itens:
            run.resultados = [*run.resultados, *(asdict(i) for i in itens)]
            run.encontrados = len(run.resultados)
            campos += ['resultados', 'encontrados']
        run.save(update_fields=campos)


def _ingerir(run_id: str, numeros: list[str]) -> None:
    from tribunals.models import BuscaTribunalRun

    from .ingestao import enfileirar

    if not numeros:
        return
    run = BuscaTribunalRun.objects.get(pk=run_id)
    saida = enfileirar(numeros, ja_ingeridos=run.novos_no_acervo)

    with transaction.atomic():
        travado = BuscaTribunalRun.objects.select_for_update().get(pk=run_id)
        travado.novos_no_acervo += saida['enfileirados']
        if saida['fora_do_teto']:
            travado.erros = [*travado.erros, {
                'tipo': 'teto_de_ingestao',
                'mensagem': (f'{saida["fora_do_teto"]} processos encontrados '
                             f'ficaram fora do acervo: a busca ingere no máximo '
                             f'500 por consulta'),
            }]
        travado.save(update_fields=['novos_no_acervo', 'erros'])


def _fechar_se_terminou(run_id: str) -> None:
    """Fecha o run quando o último tribunal termina.

    `concluido` mesmo com tribunal indisponível: a busca ACONTECEU, e o que deu
    errado está dito por tribunal. Só vira `erro` quando NENHUM respondeu — aí
    sim não há resposta, e chamar isso de concluído seria entregar um vazio que
    parece resultado.
    """
    from tribunals.models import BuscaTribunalRun

    with transaction.atomic():
        run = BuscaTribunalRun.objects.select_for_update().get(pk=run_id)
        if run.status != BuscaTribunalRun.STATUS_RUNNING:
            return
        estados = [(e or {}).get('status') for e in run.por_tribunal.values()]
        if not estados or any(s not in TERMINAIS for s in estados):
            return
        respondeu = any(s in (OK, VAZIO, REFINAR, RECUSADO) for s in estados)
        run.status = (BuscaTribunalRun.STATUS_CONCLUIDO if respondeu
                      else BuscaTribunalRun.STATUS_ERRO)
        run.finalizado_em = timezone.now()
        run.save(update_fields=['status', 'finalizado_em'])
