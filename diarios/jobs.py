"""Jobs RQ compartilhados por TODAS as fontes de diário.

Nenhuma fonte escreve job próprio: o ciclo catalogar → tick → coletar é o mesmo
para as quatro, e é onde moram as lições já pagas no DJEN (watermark por
tribunal, circuito aberto = adiar, ausência ≠ falha). Se a sua fonte precisa de
um job diferente, ela provavelmente está querendo furar um teto.
"""

import logging
from datetime import date, timedelta

from django.utils import timezone
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


#: Quantos processos por job de promoção. `promover_lote` lê as 3
#: movimentações mais recentes de cada um pelo índice
#: `mov_processo_data_disp_idx` — medido em 1,26 s por 1.000 processos. 500 é o
#: mesmo lote da gravação, então um lote coletado vira um job de promoção.
LOTE_PARTES = 500


@job('default', timeout=1800)
def promover_partes(process_ids: list[int]) -> dict:
    """`Movimentacao.destinatarios` → `Parte`/`ProcessoParte`, para o que a
    terceira porta acabou de gravar.

    POR QUE ESTE JOB EXISTE (medido em 02/09/2026, e é o §12 outra vez)
    -------------------------------------------------------------------
    O coletor da DEPRE passou a extrair o ente devedor, e o JSONB provou que
    ele chegou ao banco: **2.568 de 2.568** movimentações da relação de
    10/03/2025 com `papel='ENTIDADE DEVEDORA'` e `polo='P'` em
    `Movimentacao.destinatarios`. E `ProcessoParte` desses processos:
    **ZERO linhas**.

    A promoção EXISTE (`tribunals/services/partes_djen.py`) mas é um backfill
    por FAIXA DE PK, disparado à mão. Conferido no Redis: nenhum checkpoint de
    shard; e as 218.068 `ProcessoParte` criadas nas 24 h anteriores eram todas
    do enricher (`fonte IS NULL`), nenhuma de `fonte='djen'`. Ou seja: processo
    que nasce HOJE de uma coleta de diário nunca é alcançado — a faixa de pk
    dele já ficou para trás.

    É exatamente a doença do §12 num campo diferente: "coletado" não era
    "buscável", e agora "extraído" não era "parte". A cura é a mesma —
    **entrega no `on_commit` da gravação**, não uma varredura que alguém
    precisa lembrar de rodar.

    Idempotente e conservador por construção, tudo herdado do serviço:
    `sem_processoparte` pula quem já tem parte (a do enricher é melhor que a
    nossa), e a constraint `uniq_processo_parte_polo_papel_principal` faz o
    `bulk_create(ignore_conflicts=True)` ser seguro entre workers.
    """
    from tribunals.services.partes_djen import promover_lote, sem_processoparte

    ids = [int(p) for p in (process_ids or [])]
    if not ids:
        return {'skip': 'lote vazio'}
    alvo = sem_processoparte(ids)
    if not alvo:
        return {'recebidos': len(ids), 'alvo': 0, 'linhas': 0,
                'motivo': 'todos já tinham ProcessoParte'}
    res = promover_lote(alvo)
    logger.info('promover_partes: %d recebidos, %d alvo, %d linhas confirmadas '
                '(%d partes, %d descartadas por segredo)',
                len(ids), len(alvo), res.linhas_confirmadas, res.partes_upsert,
                res.descartados_segredo)
    return {'recebidos': len(ids), 'alvo': len(alvo),
            'linhas': res.linhas_confirmadas, 'partes': res.partes_upsert,
            'segredo': res.descartados_segredo}


@job('default', timeout=300)
def tick(fonte: str) -> dict:
    """Alimenta a fila `diarios` com unidades pendentes, respeitando o teto.

    Mais recente → mais antigo, de propósito: o valor comercial decai com a
    idade, então os primeiros dias de backfill compram a parte mais útil do
    acervo. Quem quiser outra ordem passa por `diarios_coletar`, não muda isto.

    DOIS TETOS, e eles medem coisas diferentes. `WATERMARK_POR_FONTE` limita a
    PROFUNDIDADE da fila `diarios` (fairness entre fontes); o `diarios.orcamento`
    limita a VAZÃO em 24 h, o USO DE DISCO do ES e a PROFUNDIDADE DA FILA do
    índice. Sem o segundo, o ritmo real do backfill é o número de réplicas do
    `worker_diarios` — escolhido por CPU, não por disco — e a projeção medida em
    24/08/2026 é de ~772 GB de índice para um nó com 1,0 TB livre.
    Ver `diarios/orcamento.py`.
    """
    import django_rq

    from . import orcamento

    coletor = obter(fonte)

    # Guarda de recurso ANTES de qualquer trabalho: não adianta medir fila e
    # ler pendentes se o destino da escrita não tem para onde crescer.
    pode, motivo = orcamento.guarda_de_recursos()
    if not pode:
        # Teto é ALERTA (regra nº 2), nunca `return` discreto: o tick só volta
        # em 10 min e ninguém está olhando a fila às 3 da manhã.
        logger.error('tick %s BLOQUEADO por recurso: %s. As unidades continuam '
                     'pendentes — nada foi descartado.', fonte, motivo)
        return {'fonte': fonte, 'bloqueado': motivo, 'enfileiradas': 0}

    fila = django_rq.get_queue('diarios')
    ids = fila.get_job_ids()
    meu_prefixo = f'{PREFIXO_JOB}:{fonte}:'
    meus = sum(1 for j in ids if j.startswith(meu_prefixo))
    if meus >= WATERMARK_POR_FONTE:
        return {'aguardando': True, 'na_fila': meus}

    cabem = min(LOTE_TICK, WATERMARK_POR_FONTE - meus)
    folga = orcamento.folga_do_orcamento(fonte)
    if folga is not None:
        if folga <= 0:
            logger.warning('tick %s: orçamento de 24h ESGOTADO (teto=%d, coletadas=%d). '
                           'Nada enfileirado; as pendentes seguem pendentes.',
                           fonte, orcamento.teto_diario(fonte), orcamento.coletadas_24h(fonte))
            return {'fonte': fonte, 'orcamento_esgotado': True,
                    'teto_dia': orcamento.teto_diario(fonte), 'enfileiradas': 0}
        cabem = min(cabem, folga)

    pendentes = list(
        EdicaoDiario.objects
        .filter(fonte=fonte, status__in=[EdicaoDiario.PENDENTE, EdicaoDiario.FALHA],
                tentativas__lt=MAX_TENTATIVAS)
        .order_by('-data', 'chave')
        .values_list('chave', flat=True)[:cabem]
    )
    for chave in pendentes:
        fila.enqueue(coletar, fonte, chave, job_id=f'{meu_prefixo}{chave}', job_timeout=7200)

    restantes = EdicaoDiario.objects.filter(
        fonte=fonte, status__in=[EdicaoDiario.PENDENTE, EdicaoDiario.FALHA],
        tentativas__lt=MAX_TENTATIVAS,
    ).count()
    logger.info('tick %s: +%d enfileiradas, %d pendentes, folga24h=%s, %s, janela=%s',
                fonte, len(pendentes), restantes, folga, motivo,
                f'{coletor.janela_inicio}→{coletor.janela_fim}')
    return {'fonte': fonte, 'enfileiradas': len(pendentes), 'pendentes': restantes,
            'folga_24h': folga, 'disco': motivo}


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


#: Carência antes de conferir uma edição recém-coletada. A entrega ao índice é
#: assíncrona (fila `es_index`) e o dreno leva minutos: conferir na hora só
#: mediria a profundidade da fila e re-enfileiraria tudo de novo. Medido em
#: 21/08/2026: um `_bulk` de 500 documentos leva 4,13 s, e 220.544 linhas viram
#: 441 jobs.
CARENCIA_GATE_MIN = 20
#: Quantos (tribunal, dia) distintos por passada. Cada um custa duas contagens
#: baratas; só o que acusar diferença paga o reparo, que é caro.
LOTE_GATE = 20


@job('default', timeout=3600)
def conferir_indice(fonte: str | None = None, limite: int = LOTE_GATE,
                    carencia_min: int = CARENCIA_GATE_MIN, reparar: bool = True) -> dict:
    """Gate de completude do ÍNDICE: o dia coletado fecha contra o ES, ou não?

    O buraco que este job existe para fechar, medido em produção em 21/08/2026:
    os 8 cadernos do DJE/TJSP de 12/03/2025 fecharam `ok` com **220.544** linhas
    gravadas enquanto **27.619** delas ainda estavam FORA do Elasticsearch. O
    coletor não sabia, o `IngestionRun` não sabia, a tela não sabia. A única
    coisa que levava aquelas linhas ao índice era um poller de 10 minutos.

    Aqui a régua mede OS DOIS LADOS (regra nº 5) e a diferença vira ERRO
    registrado (regra nº 2) — e, quando `reparar=True`, vira também re-enfileiramento
    do que falta. Abstenção é explícita: se o ES ou o Postgres não responderem
    dentro do teto, o campo fica `None` e a edição NÃO recebe carimbo — nunca 0.

    Idempotente: só olha edição `ok` ainda não conferida, agrupada por
    (tribunal, dia) para não pagar a mesma medição 8 vezes no mesmo dia.
    """
    from .indice import conferir_dia, gate_ativo, reparar_dia

    if not gate_ativo():
        return {'skip': 'gate desligado (DIARIOS_GATE_INDICE_ENABLED=0)'}

    corte = timezone.now() - timedelta(minutes=carencia_min)
    qs = (EdicaoDiario.objects
          .filter(status=EdicaoDiario.OK, indice_conferido_em__isnull=True,
                  tribunal__isnull=False, coletado_em__lte=corte)
          .exclude(itens_gravados=0))
    if fonte:
        qs = qs.filter(fonte=fonte)
    # `.order_by()` NUA antes do distinct: `EdicaoDiario.Meta` tem
    # `ordering = ['-data', 'chave']` e o Django injeta as colunas do ORDER BY
    # no SELECT DISTINCT — sem isto o DISTINCT vale para a tupla inteira e
    # devolve uma linha por EDIÇÃO, que foi o bug dos "8 cartões iguais".
    dias = list(qs.order_by().values_list('tribunal_id', 'data').distinct()[:limite])

    saida: dict = {'dias': [], 'conferidos': 0, 'com_buraco': 0, 'abstidos': 0,
                   'reenfileiradas': 0}
    for tribunal_id, dia in dias:
        medida = conferir_dia(tribunal_id, dia)
        faltando = medida['faltando']
        reenfileiradas = None
        if faltando is None:
            # Não sei ≠ está fechado. Sem carimbo: a próxima passada tenta de novo.
            saida['abstidos'] += 1
            logger.warning('gate índice: ABSTENÇÃO em %s %s (pg=%s es=%s)',
                           tribunal_id, dia, medida['pg'], medida['es'])
            saida['dias'].append(medida)
            continue
        if faltando:
            saida['com_buraco'] += 1
            logger.error(
                'gate índice: %s %s tem %d linhas FORA do índice (PG=%d, ES=%d, %.2f%%). '
                'Coletado não é buscável — %s.',
                tribunal_id, dia, faltando, medida['pg'], medida['es'],
                100.0 * faltando / (medida['pg'] or 1),
                'reparando' if reparar else 'reparo DESLIGADO nesta passada',
            )
            if not reparar:
                # Achou buraco e não vai consertar ⇒ NÃO carimba. Carimbar aqui
                # tiraria o dia da fila do gate para sempre, com o buraco
                # aberto — "conferido" viraria selo de qualidade sobre perda.
                saida['dias'].append(medida)
                continue
            rep = reparar_dia(tribunal_id, dia)
            medida['reparo'] = rep
            reenfileiradas = rep['enfileiradas']
            saida['reenfileiradas'] += reenfileiradas
            if rep['teto_atingido']:
                # Teto = alerta. Sem carimbo: o dia continua em dívida.
                saida['dias'].append(medida)
                continue
        edicoes = EdicaoDiario.objects.filter(
            status=EdicaoDiario.OK, indice_conferido_em__isnull=True,
            tribunal_id=tribunal_id, data=dia,
        )
        if fonte:
            edicoes = edicoes.filter(fonte=fonte)
        for e in edicoes:
            e.carimbar_indice(no_es=medida['es'], faltando=faltando,
                              reenfileiradas=reenfileiradas)
        saida['conferidos'] += 1
        saida['dias'].append(medida)

    if saida['dias']:
        logger.info('gate índice: %s', saida)
    return saida


def agendar_conferencia_indice() -> dict:
    """Enfileira UMA conferência, com `job_id` fixo. Chamado inline pelo cron.

    O `job_id` determinístico é o que impede empilhamento: se o gate anterior
    ainda não rodou (a fila `default` também serve o `tick` e o
    `catalogar_fronteira`), o RQ substitui o job em vez de acrescentar mais um.
    Mesmo truque do `dia:<fonte>:<chave>` do `tick`, pelo mesmo motivo.

    Fila `default`, não `diarios`: durante um backfill a fila `diarios` tem até
    200 cadernos à frente, cada um de dezenas de segundos. O gate atrás disso
    demoraria horas para rodar — e gate que roda tarde é gate que não roda.
    """
    import django_rq

    from .indice import gate_ativo

    if not gate_ativo():
        return {'skip': 'gate desligado'}
    fila = django_rq.get_queue('default')
    fila.enqueue(conferir_indice, job_id='diarios:conferir_indice', job_timeout=3600)
    return {'enfileirado': True}


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
