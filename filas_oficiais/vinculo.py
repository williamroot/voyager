"""Vínculo precatório (2º grau) → processo de origem (1º grau) pelo DJEN.

No TJPA a fila oficial só traz o número do precatório no PJe 2º grau
(`08xxxxx-DD.AAAA.8.14.0000`); o ofício — e o credor — estão no processo de 1º
grau, que o JURISCOPE já lê. A ponte de maior precisão é a publicação de 1º
grau que CITA o número do precatório: "determino o cancelamento do Ofício
Precatório nº 771/2025 ..., devendo ser oficiado à CPREC junto ao processo
0827902-54.2025.8.14.0000", "dando-se ciência à Coordenadoria de Precatórios
através de Ofício junto ao processo n. 0816980-85.2024.8.14.0000".

Medido em 23/10/2026: nos 1.457 movimentos de 63 origens conhecidas, 18 citavam
um `8.14.0000` (4 processos); nos 2 casos com par conhecido por outra via, o
vínculo bateu. Cobertura baixa (~5% das origens), precisão alta. A ponte por
VALOR (valor de face = valor do ofício) NÃO entra aqui: colide em valores
iguais (7 de 52) e precisa de conferência por amostra antes.

Regra:
  * o CNJ `8.14.0000` tem de vir até 80 caracteres DEPOIS de "precat", "CPREC"
    ou "Coordenadoria" (o `8.14.0000` sozinho também é agravo e mandado de
    segurança);
  * só vincula a item que JÁ está na fila oficial e ainda sem origem; origem
    diferente da gravada não sobrescreve (conta como conflito no placar);
  * a origem nunca é processo de 2º grau.

Leitura: só pelo índice `(processo_id, data)` — a lista de processos sai de
`tribunals_process` (tribunal + classificação + última movimentação), e o texto
é lido só desses processos, em lotes.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import timedelta

from django.db import connection, transaction
from django.utils import timezone

from tribunals.classificador import classificar_e_persistir
from tribunals.models import PrecatorioFilaOficial, Process

logger = logging.getLogger('voyager.filas_oficiais.vinculo')

#: Regex (Postgres ARE) por tribunal: grupo 1 = CNJ do precatório citado.
CITACAO_PRECATORIO_SQL = {
    'TJPA': r'(?:precat|cprec|coordenadoria)[^\n]{0,80}?(\d{7}-\d{2}\.\d{4}\.8\.14\.0000)',
}
SUFIXO_2G = {'TJPA': '.8.14.0000'}
#: Onde procurar: o 1º grau que pode ter expedido precatório.
CLASSIFICACOES_ALVO = ('PRECATORIO', 'PRE_PRECATORIO', 'DIREITO_CREDITORIO')
LOTE = 500

_ALVO_SQL = """
    SELECT id FROM tribunals_process
    WHERE tribunal_id = %(trib)s AND classificacao = ANY(%(classifs)s)
      AND numero_cnj NOT LIKE %(sufixo)s
      AND (%(desde)s::timestamptz IS NULL OR ultima_movimentacao_em >= %(desde)s)
"""
_CITACOES_SQL = """
    SELECT m.processo_id, (regexp_matches(m.texto, %(re)s, 'gi'))[1]
    FROM tribunals_movimentacao m
    WHERE m.processo_id = ANY(%(pids)s) AND m.texto ~* %(re)s
"""


def citacoes(tribunal: str, desde=None, sql_timeout: int = 300):
    """Gera (pid da origem, CNJ do precatório citado), sem repetição."""
    regex = CITACAO_PRECATORIO_SQL[tribunal]
    with connection.cursor() as cur:
        cur.execute('SET statement_timeout = %s', [f'{sql_timeout}s'])
        cur.execute(_ALVO_SQL, {'trib': tribunal, 'classifs': list(CLASSIFICACOES_ALVO),
                                'sufixo': f'%{SUFIXO_2G[tribunal]}', 'desde': desde})
        pids = sorted(r[0] for r in cur.fetchall())
        vistos = set()
        for i in range(0, len(pids), LOTE):
            cur.execute(_CITACOES_SQL, {'re': regex, 'pids': pids[i:i + LOTE]})
            for pid, cnj in cur.fetchall():
                if (pid, cnj) not in vistos:
                    vistos.add((pid, cnj))
                    yield pid, cnj


def vincular_origens(tribunal: str = 'TJPA', desde=None, escrever: bool = False,
                     classificar: bool = True) -> dict:
    """Grava `processo_origem`/`origem_cnj` dos itens da fila citados no DJEN.

    `escrever=False` só mede. Devolve o placar (vai para o run e o comando).
    """
    placar = Counter()
    por_chave: dict[str, set[int]] = {}
    for pid, cnj in citacoes(tribunal, desde=desde):
        placar['citacoes'] += 1
        por_chave.setdefault(cnj, set()).add(pid)
    regs = {r.chave: r for r in PrecatorioFilaOficial.objects.filter(
        tribunal_id=tribunal, chave__in=list(por_chave))}
    numeros = dict(Process.objects.filter(
        pk__in={p for ps in por_chave.values() for p in ps}).values_list('pk', 'numero_cnj'))
    a_classificar = []
    for cnj, pids in sorted(por_chave.items()):
        reg = regs.get(cnj)
        if reg is None:
            placar['citado_fora_da_fila'] += 1
            continue
        if len(pids) > 1:
            # Duas origens citando o mesmo precatório (cumprimento + incidente,
            # ou processo apensado): não se escolhe no escuro.
            placar['ambiguo'] += 1
            continue
        pid = next(iter(pids))
        if reg.processo_origem_id == pid:
            placar['ja_vinculado'] += 1
            continue
        if reg.processo_origem_id is not None:
            placar['conflito'] += 1
            logger.warning('vínculo %s: origem gravada %s, DJEN cita %s — mantida a gravada',
                           cnj, reg.processo_origem_id, pid)
            continue
        placar['vinculados'] += 1
        if escrever:
            with transaction.atomic():
                PrecatorioFilaOficial.objects.filter(pk=reg.pk).update(
                    processo_origem_id=pid, origem_cnj=numeros.get(pid, '')[:25],
                    atualizado_em=timezone.now())
            a_classificar.append(pid)
    if escrever and classificar:
        for proc in Process.objects.filter(pk__in=a_classificar):
            try:
                cat, _ = classificar_e_persistir(proc, registrar_log=True)
                placar[f'classificado_{cat}'] += 1
            except Exception as e:
                logger.warning('classificação da origem pid=%s falhou: %s', proc.pk, e)
                placar['falhas'] += 1
    return dict(placar)


def vincular_recentes(tribunal: str = 'TJPA', dias: int = 3) -> dict:
    """O passo diário: só processos com movimentação nos últimos `dias`."""
    return vincular_origens(tribunal, desde=timezone.now() - timedelta(days=dias),
                            escrever=True)
