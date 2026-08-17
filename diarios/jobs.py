"""Jobs RQ compartilhados por TODAS as fontes de diário.

Nenhuma fonte escreve job próprio: o ciclo catalogar → tick → coletar é o mesmo
para as quatro, e é onde moram as lições já pagas no DJEN (watermark por
tribunal, circuito aberto = adiar, ausência ≠ falha). Se a sua fonte precisa de
um job diferente, ela provavelmente está querendo furar um teto.
"""

import logging
from datetime import date, timedelta

from django_rq import job

from .base import ColetaPausada, FonteOcupada, catalogar_fonte, coletar_unidade, listar, obter
from .models import EdicaoDiario

logger = logging.getLogger('voyager.diarios.jobs')

#: teto de unidades pendentes POR FONTE na fila. Lição do incidente 2026-07-29:
#: teto só global não dá fairness — quem enche primeiro monopoliza a FIFO e as
#: outras fontes congelam. Aqui o limite operacional é por fonte.
WATERMARK_POR_FONTE = 200
LOTE_TICK = 100
#: unidade que falhou N vezes para de ser retentada e vira dívida visível na
#: dashboard. Retry infinito esconde problema de parser atrás de "pendente".
MAX_TENTATIVAS = 5
PREFIXO_JOB = 'dia'  # job_id determinístico dia:<fonte>:<chave> → dedupe entre ticks


@job('diarios', timeout=3600)
def catalogar(fonte: str, inicio: str, fim: str, sobrepor: bool = False) -> dict:
    """Materializa o catálogo do período como unidades pendentes."""
    return catalogar_fonte(obter(fonte), date.fromisoformat(inicio),
                           date.fromisoformat(fim), sobrepor=sobrepor)


@job('diarios', timeout=7200)
def coletar(fonte: str, chave: str, sobrepor: bool = False) -> dict:
    """Coleta UMA unidade. Unidade de trabalho atômica e retentável."""
    coletor = obter(fonte)
    edicao = EdicaoDiario.objects.filter(fonte=fonte, chave=chave).first()
    if edicao is None:
        return {'skip': 'unidade_desconhecida', 'chave': chave}
    if edicao.status in (EdicaoDiario.OK, EdicaoDiario.INEXISTENTE,
                         EdicaoDiario.FORA_DA_JANELA, EdicaoDiario.SEM_APROVEITAMENTO):
        return {'skip': edicao.status, 'chave': chave}
    try:
        return coletar_unidade(coletor, edicao, sobrepor=sobrepor)
    except (FonteOcupada, ColetaPausada) as exc:
        # ADIAR, não falhar: não empilha no FailedRegistry e não martela a fonte.
        # O tick re-enfileira quando o circuito fechar / a pausa sair.
        logger.warning('coleta adiada %s/%s: %s', fonte, chave, exc)
        return {'skip': 'adiado', 'chave': chave, 'motivo': str(exc)[:120]}


@job('default', timeout=300)
def tick(fonte: str) -> dict:
    """Alimenta a fila `diarios` com unidades pendentes, respeitando o teto.

    Mais recente → mais antigo, de propósito: o valor comercial decai com a
    idade, então os primeiros dias de backfill compram a parte mais útil do
    acervo. Quem quiser outra ordem passa por `diarios_coletar`, não muda isto.
    """
    import django_rq

    coletor = obter(fonte)
    fila = django_rq.get_queue('diarios')
    ids = fila.get_job_ids()
    meu_prefixo = f'{PREFIXO_JOB}:{fonte}:'
    meus = sum(1 for j in ids if j.startswith(meu_prefixo))
    if meus >= WATERMARK_POR_FONTE:
        return {'aguardando': True, 'na_fila': meus}

    pendentes = list(
        EdicaoDiario.objects
        .filter(fonte=fonte, status__in=[EdicaoDiario.PENDENTE, EdicaoDiario.FALHA],
                tentativas__lt=MAX_TENTATIVAS)
        .order_by('-data', 'chave')
        .values_list('chave', flat=True)[:min(LOTE_TICK, WATERMARK_POR_FONTE - meus)]
    )
    for chave in pendentes:
        fila.enqueue(coletar, fonte, chave, job_id=f'{meu_prefixo}{chave}', job_timeout=7200)

    restantes = EdicaoDiario.objects.filter(
        fonte=fonte, status__in=[EdicaoDiario.PENDENTE, EdicaoDiario.FALHA],
        tentativas__lt=MAX_TENTATIVAS,
    ).count()
    logger.info('tick %s: +%d enfileiradas, %d pendentes, janela=%s',
                fonte, len(pendentes), restantes,
                f'{coletor.janela_inicio}→{coletor.janela_fim}')
    return {'fonte': fonte, 'enfileiradas': len(pendentes), 'pendentes': restantes}


def fontes_agendadas() -> list[str]:
    """As fontes que o AGENDAMENTO pode tocar — recorte antes do kill switch.

    `DIARIOS_FONTES_AGENDADAS` vazio = todas as registradas. O recorte existe
    para ligar UMA fonte por vez em produção: o backfill destas portas é da
    ordem de centenas de milhões de linhas contra um Postgres já classificado
    como disk-I/O-bound, e "ligar tudo e ver o que acontece" não é plano.

    Fonte pausada (`diarios_pausar`) some daqui também: o kill switch já para no
    `checar_pausa`, mas não gastar o tick nela deixa o log legível durante um
    incidente — que é justamente quando alguém está lendo.
    """
    from django.conf import settings

    from .base import pausados

    escolhidas = [s for s in (getattr(settings, 'DIARIOS_FONTES_AGENDADAS', None) or listar())
                  if s in set(listar())]
    p = pausados()
    if '*' in p:
        return []
    return [s for s in escolhidas if s not in p]


@job('default', timeout=300)
def tick_todas() -> dict:
    """Um tick por fonte agendada. É o único cron de coleta que precisa existir."""
    return {f: tick(f) for f in fontes_agendadas()}


@job('default', timeout=600)
def catalogar_fronteira(dias: int = 7) -> dict:
    """Recataloga a fronteira recente de cada fonte (edição nova do dia).

    Só faz sentido para fontes cuja janela alcança hoje (DEJT pós-2024 publica
    pauta/ata que o DJEN não carrega; STF é fluxo corrente). Fonte puramente
    histórica não devolve nada e o custo é uma requisição.
    """
    fim = date.today()
    inicio = fim - timedelta(days=dias)
    out = {}
    for slug in fontes_agendadas():
        coletor = obter(slug)
        if coletor.janela_fim and coletor.janela_fim < inicio:
            out[slug] = {'skip': 'fonte histórica'}
            continue
        try:
            out[slug] = catalogar_fonte(coletor, inicio, fim)
        except Exception as exc:
            logger.warning('catalogar_fronteira %s falhou: %s', slug, exc)
            out[slug] = {'erro': str(exc)[:200]}
    return out
