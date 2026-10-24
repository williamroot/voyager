"""Uma execução da porta da fila oficial: coleta + descoberta, com registro.

Usado pelo cron (`filas_oficiais.jobs.coletar_fila_oficial`) e pelo comando
`fila_oficial_coletar` (dry-run por padrão). Cada entidade é coletada,
gravada e classificada antes da próxima (regra nº 1: itere, não acumule).

Registro: um `IngestionRun(fonte='fila_oficial')` por execução real. O dry-run
não grava NADA — nem run, nem marca de retomada.

Retomada: entidade concluída hoje fica marcada no Redis por 2 dias; com
`retomar=True` a execução seguinte do mesmo dia a pula.

Zumbi: o `watchdog_ingestao` não mexe nos runs desta fonte (a coleta inteira
leva ~2 h e ele mata run de mais de 1 h); quem marca o run morto é a própria
execução seguinte (`_matar_zumbis_da_fonte`).
"""
from __future__ import annotations

import logging
from datetime import timedelta
from importlib import import_module

from django.core.cache import cache
from django.utils import timezone

from filas_oficiais.descoberta import (
    Placar,
    gravar_entidade,
    marcar_fora_da_fila,
    marcar_fora_da_fila_tribunal,
    medir_entidade,
)
from tribunals.models import IngestionRun, PrecatorioFilaOficial, Tribunal

logger = logging.getLogger('voyager.filas_oficiais')

FONTE = PrecatorioFilaOficial.FONTE_RUN
COLETORES = {
    'TJAL': 'filas_oficiais.fontes.tjal.ColetorTjal',
    'TJMT': 'filas_oficiais.fontes.tjmt.ColetorTjmt',
    'TJPA': 'filas_oficiais.fontes.tjpa.ColetorTjpa',
}
#: Run desta fonte em `running` há mais que isto é zumbi.
ZUMBI_HORAS = 8
_LOCK = 'lock:fila_oficial:%s'
_LOCK_TTL_S = 8 * 3600


def coletor_do_tribunal(sigla: str, **kwargs):
    caminho = COLETORES.get(sigla.upper())
    if not caminho:
        raise ValueError(f'tribunal sem coletor de fila oficial: {sigla}')
    modulo, classe = caminho.rsplit('.', 1)
    return getattr(import_module(modulo), classe)(**kwargs)


def _chave_retomada(sigla: str, chave_ent: str) -> str:
    return f'fila_oficial:{sigla}:{timezone.localdate().isoformat()}:{chave_ent}'


def _matar_zumbis_da_fonte(tribunal: Tribunal) -> int:
    corte = timezone.now() - timedelta(hours=ZUMBI_HORAS)
    return IngestionRun.objects.filter(
        tribunal=tribunal, fonte=FONTE, status=IngestionRun.STATUS_RUNNING,
        started_at__lt=corte,
    ).update(status=IngestionRun.STATUS_FAILED, finished_at=timezone.now(),
             erros=['zumbi: execução da fila oficial não terminou'])


def executar(sigla: str = 'TJAL', escrever: bool = False, retomar: bool = True,  # noqa: PLR0912
             entidades=None, com_enricher: bool = True, coletor=None,
             saida=None, ao_concluir_entidade=None) -> dict:
    """Roda a porta. `escrever=False` é o dry-run: só mede.

    `saida(dict)`: linhas por item no dry-run (auditoria, sem dado pessoal).
    `ao_concluir_entidade(res, placar)`: progresso (o comando imprime).
    """
    sigla = sigla.upper()
    tribunal = Tribunal.objects.get(sigla=sigla)
    coletor = coletor or coletor_do_tribunal(sigla)
    placar = Placar()
    falhas_globais: list = []
    entidades_info: dict = {}
    incompletas: list = []
    run = None

    if escrever:
        if not cache.add(_LOCK % sigla, 1, _LOCK_TTL_S):
            return {'skip': f'outra execução da fila oficial {sigla} em andamento'}
        _matar_zumbis_da_fonte(tribunal)
        hoje = timezone.localdate()
        run = IngestionRun.objects.create(
            tribunal=tribunal, fonte=FONTE, status=IngestionRun.STATUS_RUNNING,
            janela_inicio=hoje, janela_fim=hoje)
    inicio = timezone.now()

    puladas: list = []

    def pular(chave_ent):
        p = bool(escrever and retomar and cache.get(_chave_retomada(sigla, chave_ent)))
        if p:
            puladas.append(chave_ent)
        return p

    try:
        for res in coletor.iterar(entidades=entidades, pular=pular, falhas=falhas_globais):
            if escrever:
                gravar_entidade(tribunal, res.itens, placar, agora=inicio,
                                com_enricher=com_enricher)
                if res.completa:
                    marcar_fora_da_fila(tribunal, res.entidade, inicio, placar)
                    cache.set(_chave_retomada(sigla, res.info.get('chave', res.entidade)),
                              1, 2 * 86400)
            else:
                medir_entidade(tribunal, res.itens, placar, saida=saida,
                               entidade=res.entidade if res.completa else None)
            if not res.completa:
                incompletas.append(res.entidade)
            entidades_info[res.entidade] = {**res.info, 'itens': len(res.itens),
                                            'completa': res.completa}
            falhas_globais.extend(res.falhas)
            if ao_concluir_entidade:
                ao_concluir_entidade(res, placar)
    except Exception as e:
        logger.exception('fila oficial %s: execução interrompida', sigla)
        falhas_globais.append(f'interrompida: {e!r}'[:500])
        if run is not None:
            _fechar(run, placar, coletor, falhas_globais, ok=False)
            cache.delete(_LOCK % sigla)
        raise

    # Coletor que cobre o tribunal INTEIRO numa execução (TJMT: todos os entes
    # do SRP): com tudo completo e nada pulado, o pendente que nenhuma entidade
    # listou — ente que sumiu do cadastro ou mudou de nome — também sai da fila.
    varredura = (getattr(coletor, 'cobertura_total', False) and not entidades
                 and not puladas and not incompletas and not falhas_globais)
    if varredura and escrever:
        marcar_fora_da_fila_tribunal(tribunal, inicio, placar)
    if placar.falhas:
        falhas_globais.append(f'{placar.falhas} itens com falha de hidratação ou '
                              'classificação (ver o log do worker)')
    resumo = {
        'tribunal': sigla, 'escreveu': escrever,
        'entidades': len(entidades_info), 'entidades_incompletas': incompletas,
        'requisicoes': getattr(coletor, 'requisicoes', None),
        'falhas': falhas_globais, **placar.como_dict(),
    }
    if run is not None:
        ok = not incompletas and not falhas_globais
        _fechar(run, placar, coletor, falhas_globais, ok=ok)
        cache.delete(_LOCK % sigla)
        resumo['run_id'] = run.pk
    # Teto/lacuna é alerta, nunca corte mudo (regra nº 2).
    if incompletas or falhas_globais:
        logger.warning('fila oficial %s: %d entidades incompletas, %d falhas: %s',
                       sigla, len(incompletas), len(falhas_globais), falhas_globais[:10])
    logger.info('fila oficial %s: %s', sigla, {k: v for k, v in resumo.items()
                                              if k not in ('falhas',)})
    return resumo


def _fechar(run, placar: Placar, coletor, falhas: list, ok: bool) -> None:
    run.status = IngestionRun.STATUS_SUCCESS if ok else IngestionRun.STATUS_FAILED
    run.finished_at = timezone.now()
    run.paginas_lidas = getattr(coletor, 'requisicoes', 0) or 0
    run.processos_novos = sum(placar.criados.values())
    run.erros = [str(f)[:500] for f in falhas][:200]
    run.save(update_fields=['status', 'finished_at', 'paginas_lidas',
                            'processos_novos', 'erros'])
