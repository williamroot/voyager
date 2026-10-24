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

TJMT (SRP): o precatório pendente ausente nasce direto (classe 1265, grau G2;
`tjmt_2g`), e no classificador `fora_da_fila`/`outro` REBAIXAM com motivo
anotado em vez de se abster (ver `tribunals/classificador.py`).
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
SEGMENTO = {'TJAL': '8.02', 'TJMT': '8.11', 'TJPA': '8.14'}
CLASSE_PRECATORIO = ('1265', 'Precatório')
_RE_FORO_PRESIDENCIA = re.compile(r'\.9003$')
#: TJMT: o precatório é autuado no PJe de 2º grau, foro 0000 (os 17.885 da
#: fila SRP de 23/10/2026, sem exceção). Nasce direto, classe 1265, grau G2:
#: o enricher do TJMT lê a consulta pública, que não tem o 2º grau (7.090
#: `nao_encontrado` medidos), e o DJEN do precatório é "Processo sigiloso".
_RE_PRECATORIO_TJMT = re.compile(r'\.8\.11\.0000$')
#: TJPA: o mesmo desenho — precatório autuado no PJe 2º grau (`08xxxxx-DD.AAAA.
#: 8.14.0000`, os 3.756 da lista do Power BI de 23/10/2026, sem exceção). Não
#: publica no DJEN (0 de 314 consultados), não está no Datajud (0 de 2) e a
#: consulta unificada o filtra: nasce direto, classe 1265, grau G2.
_RE_PRECATORIO_TJPA = re.compile(r'\.8\.14\.0000$')
#: Tribunais cujo precatório nasce no 2º grau (foro 0000): regex e tipo.
_PRECATORIO_2G = {'TJMT': (_RE_PRECATORIO_TJMT, 'tjmt'), 'TJPA': (_RE_PRECATORIO_TJPA, 'tjpa')}
#: Tipos de número que nascem direto (sem Datajud), com o grau a gravar.
_NASCE_DIRETO = {'foro_9003': '', 'tjmt_2g': 'G2', 'tjpa_2g': 'G2'}
#: Número que a fonte de 2º grau deu fora do foro 0000: não se adivinha.
_OUTRO_NUMERO = {'tjmt_outro_numero', 'tjpa_outro_numero'}

_CAMPOS = ('fonte', 'entidade', 'entidade_devedora', 'situacao', 'situacao_raw',
           'chave_e_origem', 'id_externo', 'valor_original', 'valor_atualizado',
           'ano_orcamento', 'natureza', 'origem_cnj', 'ordem', 'regime', 'n_itens',
           'valor_pago', 'preferencia')


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


def tipo_do_numero(chave: str, chave_e_origem: bool, sigla: str = 'TJAL') -> str:
    if sigla in _PRECATORIO_2G:
        regex, pref = _PRECATORIO_2G[sigla]
        return f'{pref}_2g' if regex.search(chave) else f'{pref}_outro_numero'
    if chave_e_origem:
        return 'origem_1grau'
    if _RE_FORO_PRESIDENCIA.search(chave):
        return 'foro_9003'
    return 'comarca_050'


def _criar_direto(chave: str, tribunal: Tribunal, grau: str = '') -> Process:
    cod, nome = CLASSE_PRECATORIO
    campos = {'numero_cnj': chave, 'tribunal': tribunal,
              'classe_codigo': cod, 'classe_nome': nome}
    if grau:
        campos['grau'] = grau
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
    tipo = tipo_do_numero(item.chave, item.chave_e_origem, tribunal.sigla)
    if tipo in _NASCE_DIRETO:
        proc = _criar_direto(item.chave, tribunal, _NASCE_DIRETO[tipo])
    elif tipo in _OUTRO_NUMERO:
        # Nunca visto (0 em 17.885 no TJMT, 0 em 3.756 no TJPA): número fora do
        # foro 0000. Não se adivinha o que é — fica registrado e conta como
        # falha no run.
        logger.warning('fila oficial %s: número %s fora do 2º grau; não criado',
                       tribunal.sigla, item.chave)
        placar.falhas += 1
        return None, False
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
                if reg.saiu_em is not None:
                    # Voltou à fila: a saída anotada deixa de valer (o histórico
                    # fica no ClassificacaoLog do processo).
                    reg.saiu_em, reg.motivo_saida = None, ''
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
    return _sair_da_fila(qs, inicio, placar, classificar,
                         f'ausente da coleta completa da entidade {entidade}')


def marcar_fora_da_fila_tribunal(tribunal: Tribunal, inicio, placar: Placar,
                                 classificar: bool = True) -> int:
    """Pendente do tribunal que NENHUMA entidade listou na coleta iniciada em
    `inicio` — o ente que saiu do cadastro da fonte ou mudou de nome. Só
    chamar com a coleta inteira completa (todas as entidades, nenhuma pulada)."""
    qs = PrecatorioFilaOficial.objects.filter(
        tribunal=tribunal, situacao=SITUACAO_PENDENTE, visto_em__lt=inicio)
    return _sair_da_fila(qs, inicio, placar, classificar,
                         'ausente da coleta completa do tribunal')


def _sair_da_fila(qs, inicio, placar: Placar, classificar: bool, onde: str) -> int:
    """Marca `fora_da_fila` e ANOTA o motivo (nunca apaga registro nem processo)."""
    agora = timezone.now()
    pids = list(qs.exclude(processo__isnull=True).values_list('processo_id', flat=True))
    motivo = (f'saiu da fila oficial sem pagamento registrado pela fonte: {onde} '
              f'de {timezone.localtime(inicio):%d/%m/%Y %H:%M}')[:500]
    n = qs.update(situacao=SITUACAO_FORA_DA_FILA, saiu_em=agora, motivo_saida=motivo,
                  atualizado_em=agora)
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
                   saida=None, entidade: str | None = None) -> None:
    """Dry-run: o que `gravar_entidade` faria, sem escrever nada.

    `saida(dict)`: recebe uma linha por item (sem dado pessoal) para auditoria.
    `entidade` (só quando a coleta dela veio COMPLETA): conta em
    `placar.fora_da_fila` os pendentes já registrados que sairiam da fila.
    """
    if entidade is not None:
        vistas = {it.chave for it in itens}
        sairiam = [c for c in PrecatorioFilaOficial.objects.filter(
            tribunal=tribunal, entidade=entidade, situacao=SITUACAO_PENDENTE,
        ).values_list('chave', flat=True) if c not in vistas]
        placar.fora_da_fila += len(sairiam)
        if saida is not None:
            for c in sairiam:
                saida({'chave': c, 'entidade': entidade, 'acao': 'sairia_da_fila'})
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
            tipo = tipo_do_numero(it.chave, it.chave_e_origem, tribunal.sigla)
            if tipo in _OUTRO_NUMERO:
                placar.falhas += 1
                linha['acao'] = 'nao_criaria'
            else:
                placar.criados[tipo] += 1
                linha['acao'] = 'criaria'
            linha['tipo'] = tipo
        else:
            linha['acao'] = 'nao_pendente'
        if saida is not None:
            saida(linha)
