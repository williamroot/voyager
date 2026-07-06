"""Dossiê de jurimetria por CNJ (M3).

Recebe um número de processo e orquestra, de forma DETERMINÍSTICA e auditável:
  1. Cabeçalho do processo (Voyager: classe, assunto, órgão, valor, partes, classificação)
  2. Jurimetria do TIPO (agregação sobre processos da mesma classe+tribunal)
  3. Bloco precatório (se o processo é lead PRECATORIO/PRE_PRECATORIO)
  4. Precedentes relevantes (Zordon RAG — degrada se indisponível)

Cada bloco carrega proveniência (`meta`) pra UI exibir n/fonte. LLM (narração) é
módulo posterior (M5); aqui os números vêm todos de SQL/serviços.
"""
from __future__ import annotations

import re

from django.db import connection

from tribunals.models import Movimentacao, Process, ProcessoParte

_CNJ_RE = re.compile(r'\d{7}-?\d{2}\.?\d{4}\.?\d\.?\d{2}\.?\d{4}')
_EXPED_RE = re.compile(r'precat[óo]rio|ofício requisit[óo]rio|expedi', re.IGNORECASE)


def normalizar_cnj(raw: str) -> str | None:
    s = (raw or '').strip()
    m = _CNJ_RE.search(s)
    if not m:
        return None
    d = re.sub(r'\D', '', m.group(0))
    if len(d) != 20:
        return None
    return f'{d[0:7]}-{d[7:9]}.{d[9:13]}.{d[13]}.{d[14:16]}.{d[16:20]}'


def _jurimetria_do_tipo(proc: Process) -> dict:
    """Conta processos do mesmo TIPO (classe+tribunal) e a taxa de precatório.

    Só counts indexados (classe_codigo, tribunal) — barato mesmo em classe grande.
    """
    if not proc.classe_codigo:
        return {'disponivel': False, 'motivo': 'processo sem classe estruturada'}
    base = Process.objects.filter(tribunal_id=proc.tribunal_id,
                                  classe_codigo=proc.classe_codigo)
    with connection.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout='15000'")
    total = base.count()
    precat = base.filter(classificacao='PRECATORIO').count()
    pre = base.filter(classificacao='PRE_PRECATORIO').count()
    return {
        'disponivel': True,
        'classe_codigo': proc.classe_codigo,
        'classe_nome': proc.classe_nome,
        'total': total,
        'precatorio': precat,
        'pre_precatorio': pre,
        'taxa_precatorio': round(100 * (precat + pre) / total, 1) if total else 0.0,
        'meta': {'fonte': 'tribunals_process (classe_codigo+tribunal)',
                 'tipo': 'descritivo', 'n': total},
    }


def _bloco_precatorio(proc: Process) -> dict:
    """Sinais de precatório do próprio processo (determinístico)."""
    is_lead = proc.classificacao in ('PRECATORIO', 'PRE_PRECATORIO')
    tem_exped = Movimentacao.objects.filter(processo_id=proc.pk).filter(
        texto__iregex=r'precat|requisit|expedi').exists()
    return {
        'is_lead': is_lead,
        'classificacao': proc.classificacao,
        'score': round(proc.classificacao_score or 0, 3),
        'versao': proc.classificacao_versao,
        'valor_causa': proc.valor_causa,
        'tem_sinal_expedicao': tem_exped,
        'meta': {'fonte': 'classificacao v6 + movimentações DJEN', 'tipo': 'modelo'},
    }


def _precedentes(proc: Process, limite: int = 4) -> dict:
    """Precedentes relevantes via RAG do Zordon (degrada se indisponível)."""
    from . import zordon_client
    termos = ' '.join(t for t in [proc.classe_nome, proc.assunto_nome] if t)[:200]
    if not termos:
        return {'itens': [], 'meta': {'fonte': 'zordon RAG', 'query': ''}}
    res = zordon_client.buscar(query=termos, limit=limite)
    itens = (res or {}).get('results') or []
    return {'itens': itens[:limite], 'query': termos, 'erro': (res or {}).get('erro'),
            'meta': {'fonte': 'zordon hybrid_search (bge-m3+rerank)', 'tipo': 'RAG'}}


def montar_dossie(cnj_raw: str) -> dict:
    """Orquestra o dossiê completo por CNJ. Nunca levanta — devolve erro no dict."""
    cnj = normalizar_cnj(cnj_raw)
    if not cnj:
        return {'erro': 'CNJ inválido. Use o formato 0000000-00.0000.0.00.0000.'}
    proc = (Process.objects.select_related('tribunal')
            .filter(numero_cnj=cnj).order_by('-ultima_movimentacao_em').first())
    if not proc:
        return {'erro': f'Processo {cnj} não encontrado no acervo.', 'cnj': cnj}

    participacoes = (ProcessoParte.objects.filter(processo=proc)
                     .select_related('parte').order_by('polo', 'papel'))
    polos: dict = {'ativo': [], 'passivo': [], 'outros': []}
    for pp in participacoes:
        polos.setdefault(pp.polo, []).append(
            {'nome': pp.parte.nome, 'papel': pp.papel, 'polo': pp.polo})

    return {
        'cnj': cnj,
        'cabecalho': {
            'tribunal': proc.tribunal_id,
            'classe_codigo': proc.classe_codigo,
            'classe_nome': proc.classe_nome or '—',
            'assunto_nome': proc.assunto_nome or '—',
            'orgao_julgador': proc.orgao_julgador_nome or '—',
            'valor_causa': proc.valor_causa,
            'data_autuacao': proc.data_autuacao,
            'enriquecimento_status': proc.enriquecimento_status,
            'total_movimentacoes': proc.total_movimentacoes,
            'pk': proc.pk,
        },
        'polos': polos,
        'precatorio': _bloco_precatorio(proc),
        'jurimetria_tipo': _jurimetria_do_tipo(proc),
        'precedentes': _precedentes(proc),
    }
