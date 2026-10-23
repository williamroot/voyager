"""Da fila oficial para o acervo: registro, processo e classificação.

    coletor (uma entidade por vez)
        │
        ├─ 1. upsert em `PrecatorioFilaOficial` (sem CPF, sem nome)
        ├─ 2. PENDENTE sem processo no acervo → o processo nasce:
        │       · foro 9003 (Presidência): criado direto, classe 1265
        │         "Precatório". O Datajud não tem o 9003 (2 documentos em
        │         3,1 M do TJAL) e o enricher do e-SAJ consulta o cpopg, que
        │         não tem 2º grau — nenhum dos dois é chamado;
        │       · 050xxxx de comarca e número de origem: `hidratar_cnj`
        │         (esqueleto do Datajud + movimentos + enricher na fila do
        │         tribunal). Precatório de comarca sem esqueleto ganha a
        │         classe 1265; processo de origem fica com a classe que tiver.
        ├─ 3. classificação (`classificar_e_persistir`) de quem nasceu, foi
        │     vinculado agora ou mudou de situação. A regra da fila oficial
        │     mora no classificador (`_situacao_fila_oficial`), então toda
        │     reclassificação posterior a respeita.
        └─ 4. entidade COMPLETA: pendente que não veio nesta coleta vira
              `fora_da_fila` (e é reclassificado).

Item não pendente (pago, extinto, outro) NUNCA cria processo; se o processo já
existe, é vinculado — e pago/extinto o veta.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

from datajud.hidratacao import hidratar_cnj
from filas_oficiais.base import (
    SITUACAO_FORA_DA_FILA,
    SITUACAO_PENDENTE,
    ItemFila,
    segmento_tribunal,
)
from tribunals.catalogo import resolver as resolver_catalogo
from tribunals.classificador import classificar_e_persistir
from tribunals.models import PrecatorioFilaOficial, Process, Tribunal

logger = logging.getLogger('voyager.filas_oficiais.descoberta')

#: "J.TR" do CNJ de cada tribunal com fila oficial ligada.
SEGMENTO = {'TJAL': '8.02'}
CLASSE_PRECATORIO = ('1265', 'Precatório')
_RE_FORO_PRESIDENCIA = re.compile(r'\.9003$')

_CAMPOS = ('fonte', 'entidade', 'entidade_devedora', 'situacao', 'situacao_raw',
           'chave_e_origem', 'id_externo', 'valor_original', 'valor_atualizado',
           'ano_orcamento', 'natureza', 'origem_cnj', 'ordem', 'regime', 'n_itens')


@dataclass
class Placar:
    """Contagens de uma execução (vão para o `IngestionRun` e para a tela)."""
    itens: int = 0
    por_situacao: Counter = field(default_factory=Counter)
    registros_novos: int = 0
    registros_atualizados: int = 0
    ja_no_acervo: Counter = field(default_factory=Counter)   # por classificação
    criados: Counter = field(default_factory=Counter)        # por tipo de número
    falhas: int = 0
    fora_da_fila: int = 0
    classificados: Counter = field(default_factory=Counter)  # por categoria final
    outro_tribunal: int = 0

    def como_dict(self) -> dict:
        return {k: (dict(v) if isinstance(v, Counter) else v)
                for k, v in self.__dict__.items()}


def tipo_do_numero(chave: str, chave_e_origem: bool) -> str:
    if chave_e_origem:
        return 'origem_1grau'
    if _RE_FORO_PRESIDENCIA.search(chave):
        return 'foro_9003'
    return 'comarca_050'


def _criar_9003(chave: str, tribunal: Tribunal) -> Process:
    cod, nome = CLASSE_PRECATORIO
    campos = {'numero_cnj': chave, 'tribunal': tribunal,
              'classe_codigo': cod, 'classe_nome': nome}
    fk = resolver_catalogo('classe', cod)
    if fk:
        campos['classe_id'] = fk
    return Process.objects.create(**campos)


def garantir_processo(item: ItemFila, tribunal: Tribunal, placar: Placar,
                      com_enricher: bool = True) -> tuple[Process | None, bool]:
    """(processo, criado_agora). Só PENDENTE cria; os demais só vinculam."""
    proc = Process.objects.filter(numero_cnj=item.chave).first()
    if proc is not None:
        placar.ja_no_acervo[proc.classificacao or '(sem)'] += 1
        return proc, False
    if item.situacao != SITUACAO_PENDENTE:
        return None, False
    tipo = tipo_do_numero(item.chave, item.chave_e_origem)
    if tipo == 'foro_9003':
        proc = _criar_9003(item.chave, tribunal)
    else:
        try:
            out = hidratar_cnj(item.chave, com_enricher=com_enricher,
                               fila_enricher='tribunal')
        except Exception as e:
            # O `create` vem antes da chamada ao Datajud: se ele já aconteceu,
            # o processo existe e segue o fluxo; senão é falha contada.
            logger.warning('hidratação de %s falhou: %s', item.chave, e)
            out = {}
        proc = Process.objects.filter(numero_cnj=item.chave).first()
        if proc is None:
            placar.falhas += 1
            logger.warning('fila oficial: %s não nasceu (%s)', item.chave,
                           out.get('estado', 'erro'))
            return None, False
        if tipo == 'comarca_050' and not (proc.classe_codigo or '').strip():
            cod, nome = CLASSE_PRECATORIO
            campos = {'classe_codigo': cod, 'classe_nome': nome,
                      'atualizado_em': timezone.now()}
            fk = resolver_catalogo('classe', cod)
            if fk:
                campos['classe_id'] = fk
            Process.objects.filter(pk=proc.pk).update(**campos)
            proc.refresh_from_db()
    placar.criados[tipo] += 1
    return proc, True


def _origem(item: ItemFila) -> Process | None:
    if not item.origem_cnj:
        return None
    return Process.objects.filter(numero_cnj=item.origem_cnj).first()


def gravar_entidade(tribunal: Tribunal, itens: list[ItemFila], placar: Placar,
                    agora=None, com_enricher: bool = True,
                    classificar: bool = True) -> list[int]:
    """Grava os itens de UMA entidade, garante os processos e classifica.

    Devolve os pks classificados. Idempotente: rodar duas vezes no mesmo dia
    não cria nada de novo e não reclassifica quem não mudou.
    """
    agora = agora or timezone.now()
    segmento = SEGMENTO.get(tribunal.sigla)
    a_classificar: dict[int, Process] = {}
    for it in itens:
        placar.itens += 1
        placar.por_situacao[it.situacao] += 1
        if segmento and segmento_tribunal(it.chave) != segmento:
            placar.outro_tribunal += 1
            continue
        reg = (PrecatorioFilaOficial.objects
               .filter(tribunal=tribunal, chave=it.chave).first())
        proc, criado = garantir_processo(it, tribunal, placar, com_enricher=com_enricher)
        valores = {c: getattr(it, c) for c in _CAMPOS}
        valores['entidade'] = (valores['entidade'] or '')[:255]
        valores['entidade_devedora'] = (valores['entidade_devedora'] or '')[:255]
        valores['situacao_raw'] = (valores['situacao_raw'] or '')[:80]
        origem = _origem(it)
        with transaction.atomic():
            if reg is None:
                reg = PrecatorioFilaOficial.objects.create(
                    tribunal=tribunal, chave=it.chave, processo=proc,
                    processo_origem=origem, primeira_vez_em=agora, visto_em=agora,
                    **valores)
                placar.registros_novos += 1
                mudou = True
            else:
                mudou = (reg.situacao != it.situacao
                         or (proc is not None and reg.processo_id != proc.pk))
                for c, v in valores.items():
                    setattr(reg, c, v)
                reg.processo = proc or reg.processo
                reg.processo_origem = origem or reg.processo_origem
                reg.visto_em = agora
                reg.save()
                placar.registros_atualizados += 1
        # Já PRECATORIO e a fila diz pendente: a regra daria o mesmo — não gasta
        # a agregação de movimentos (o Estado sozinho tem ~6.800 itens, quase
        # todos já no acervo como PRECATORIO).
        ja_e = (proc is not None and it.situacao == SITUACAO_PENDENTE
                and proc.classificacao == Process.CLASSIF_PRECATORIO)
        if proc is not None and not ja_e and (mudou or criado or proc.classificacao is None):
            a_classificar[proc.pk] = proc
    if not classificar:
        return []
    for proc in a_classificar.values():
        try:
            cat, _ = classificar_e_persistir(proc, registrar_log=True)
            placar.classificados[cat] += 1
        except Exception as e:
            logger.warning('classificação de pid=%s falhou: %s', proc.pk, e)
            placar.falhas += 1
    return list(a_classificar)


def marcar_fora_da_fila(tribunal: Tribunal, entidade: str, inicio, placar: Placar,
                        classificar: bool = True) -> int:
    """Pendente da entidade que não apareceu na coleta COMPLETA iniciada em
    `inicio` sai da fila. Só chamar quando a entidade veio completa."""
    qs = PrecatorioFilaOficial.objects.filter(
        tribunal=tribunal, entidade=entidade, situacao=SITUACAO_PENDENTE,
        visto_em__lt=inicio)
    pids = list(qs.exclude(processo__isnull=True).values_list('processo_id', flat=True))
    n = qs.update(situacao=SITUACAO_FORA_DA_FILA, atualizado_em=timezone.now())
    placar.fora_da_fila += n
    if classificar:
        for proc in Process.objects.filter(pk__in=pids):
            try:
                cat, _ = classificar_e_persistir(proc, registrar_log=True)
                placar.classificados[cat] += 1
            except Exception as e:
                logger.warning('classificação de pid=%s falhou: %s', proc.pk, e)
                placar.falhas += 1
    return n


def medir_entidade(tribunal: Tribunal, itens: list[ItemFila], placar: Placar,
                   saida=None) -> None:
    """Dry-run: o que `gravar_entidade` faria, sem escrever nada.

    `saida(dict)`: recebe uma linha por item (sem dado pessoal) para auditoria.
    """
    segmento = SEGMENTO.get(tribunal.sigla)
    chaves = [it.chave for it in itens]
    existentes = dict(Process.objects.filter(numero_cnj__in=chaves)
                      .values_list('numero_cnj', 'classificacao'))
    for it in itens:
        placar.itens += 1
        placar.por_situacao[it.situacao] += 1
        linha = {'chave': it.chave, 'entidade': it.entidade, 'situacao': it.situacao,
                 'fonte': it.fonte, 'chave_e_origem': it.chave_e_origem,
                 'origem_cnj': it.origem_cnj, 'ano_orcamento': it.ano_orcamento}
        if segmento and segmento_tribunal(it.chave) != segmento:
            placar.outro_tribunal += 1
            linha['acao'] = 'outro_tribunal'
        elif it.chave in existentes:
            placar.ja_no_acervo[existentes[it.chave] or '(sem)'] += 1
            linha['acao'] = 'ja_no_acervo'
            linha['classificacao'] = existentes[it.chave]
        elif it.situacao == SITUACAO_PENDENTE:
            tipo = tipo_do_numero(it.chave, it.chave_e_origem)
            placar.criados[tipo] += 1
            linha['acao'] = 'criaria'
            linha['tipo'] = tipo
        else:
            linha['acao'] = 'nao_pendente'
        if saida is not None:
            saida(linha)
