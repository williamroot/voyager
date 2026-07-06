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


def _cronograma_pagamento(ano_ordem) -> dict | None:
    """Modelo T = cronograma constitucional (determinístico, NÃO ML): precatório
    inscrito no orçamento do ano Y é pago até 31/dez/Y (EC 114/2021). A data de
    pagamento observada não existe estruturada no Juriscope, então a estimativa
    honesta é o prazo orçamentário. Ressalva: entes em regime especial pagam com
    atraso (modelagem de atraso exige histórico de pagamento não disponível)."""
    import datetime as _dt
    from django.utils import timezone
    try:
        ano = int(str(ano_ordem)[:4])
    except (TypeError, ValueError):
        return None
    if ano < 2000 or ano > 2100:
        return None
    hoje = timezone.now().date()
    prazo = _dt.date(ano, 12, 31)
    return {
        'ano_orcamento': ano,
        'prazo_constitucional': prazo,
        'meses_ate_prazo': round((prazo - hoje).days / 30.44),
        'em_atraso': prazo < hoje,
        'fonte': 'cronograma constitucional (ano_ordem_orcamentaria · EC 114/2021)',
    }


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
    """Sinais de precatório do processo + dados estruturados do Juriscope.

    Voyager dá a classificação (é lead?); o Juriscope dá o conteúdo real do
    precatório (natureza, valor corrigido, ente devedor, posição na fila, datas).
    """
    from . import juriscope_client, survival_precatorio
    is_lead = proc.classificacao in ('PRECATORIO', 'PRE_PRECATORIO')
    tem_exped = Movimentacao.objects.filter(processo_id=proc.pk).filter(
        texto__iregex=r'precat|requisit|expedi').exists()
    js = juriscope_client.dados_precatorio(proc.numero_cnj)
    # Marco homologação de cálculos — sinal on-demand no texto dos movs DJEN
    # (a coluna calculos_homologados do Juriscope é esparsa). Bounded ao processo.
    homolog_mov = (Movimentacao.objects.filter(processo_id=proc.pk)
                   .filter(texto__iregex=r'homolog').filter(texto__iregex=r'c[aá]lculo')
                   .order_by('-data_disponibilizacao')
                   .values_list('data_disponibilizacao', flat=True).first())
    homologacao = {'ocorreu': bool(homolog_mov), 'data': homolog_mov}
    # Sobrevivência DC→precatório (só faz sentido pra quem ainda NÃO virou);
    # modelo T (→pagamento) pra quem JÁ é precatório.
    _nat = (js or {}).get('natureza')
    _ente = (js or {}).get('ente_nome') or (js or {}).get('devedora')
    sobrevivencia = None
    pagamento = None
    if proc.classificacao == 'DIREITO_CREDITORIO':
        sobrevivencia = survival_precatorio.prever(proc.tribunal_id, _nat, _ente)
    if proc.classificacao in ('PRECATORIO', 'PRE_PRECATORIO'):
        pagamento = _cronograma_pagamento((js or {}).get('ano_ordem_orcamentaria'))
    return {
        'is_lead': is_lead,
        'classificacao': proc.classificacao,
        'score': round(proc.classificacao_score or 0, 3),
        'versao': proc.classificacao_versao,
        'valor_causa': proc.valor_causa,
        'tem_sinal_expedicao': tem_exped,
        'juriscope': js,  # natureza/valor/ente/ordem/datas (ou {} se indisponível)
        'sobrevivencia': sobrevivencia,  # chance/tempo de virar precatório (DC)
        'homologacao': homologacao,      # marco: cálculos homologados (mov-text)
        'pagamento': pagamento,          # modelo T: cronograma de pagamento (precatório)
        'meta': {'fonte': 'classificacao v6 + movimentações DJEN + juriscope/falcon',
                 'tipo': 'modelo + estruturado'},
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


def _dossie_juriscope(cnj: str) -> dict | None:
    """Monta o dossiê a partir do Juriscope quando o processo NÃO está no acervo
    Voyager (busca live por CNJ, sem proxy). None se também não está no Juriscope."""
    from . import juriscope_client, survival_precatorio
    js = juriscope_client.dados_precatorio(cnj)
    if not js or not js.get('encontrado'):
        return None
    ente = js.get('ente_nome') or js.get('devedora')
    valor = js.get('valor_acao_corrigido') or js.get('valor_acao')
    return {
        'cnj': cnj,
        'fonte_dados': 'juriscope',
        'cabecalho': {
            'tribunal': js.get('tribunal') or '—',
            'classe_codigo': '', 'classe_nome': 'Precatório (via Juriscope)',
            'assunto_nome': (js.get('natureza') or '—'),
            'orgao_julgador': '—',
            'valor_causa': valor,
            'data_autuacao': js.get('data_oficio'),
            'enriquecimento_status': 'via Juriscope (fora do acervo Voyager)',
            'total_movimentacoes': 0, 'pk': None,
        },
        'polos': {'ativo': [], 'passivo': ([{'nome': ente, 'papel': 'ente devedor', 'polo': 'passivo'}] if ente else []), 'outros': []},
        'precatorio': {
            'is_lead': True, 'classificacao': 'PRECATORIO', 'score': None, 'versao': None,
            'valor_causa': valor, 'tem_sinal_expedicao': True,
            'juriscope': js,
            'sobrevivencia': None,
            'homologacao': None,
            'pagamento': _cronograma_pagamento(js.get('ano_ordem_orcamentaria')),
            'meta': {'fonte': 'juriscope/falcon (live)', 'tipo': 'estruturado'},
        },
        'jurimetria_tipo': {'disponivel': False, 'motivo': 'processo fora do acervo Voyager (dados via Juriscope)'},
        'precedentes': _precedentes_termos(js.get('natureza') or 'precatório'),
    }


def _precedentes_termos(termos: str, limite: int = 4) -> dict:
    from . import zordon_client
    res = zordon_client.buscar(query=(termos or '')[:200], limit=limite)
    return {'itens': (res or {}).get('results') or [], 'query': termos, 'erro': (res or {}).get('erro'),
            'meta': {'fonte': 'zordon hybrid_search', 'tipo': 'RAG'}}


def montar_dossie(cnj_raw: str) -> dict:
    """Orquestra o dossiê completo por CNJ. Nunca levanta — devolve erro no dict."""
    cnj = normalizar_cnj(cnj_raw)
    if not cnj:
        return {'erro': 'CNJ inválido. Use o formato 0000000-00.0000.0.00.0000.'}
    proc = (Process.objects.select_related('tribunal')
            .filter(numero_cnj=cnj).order_by('-ultima_movimentacao_em').first())
    if not proc:
        # Fallback em tempo real: não está no acervo Voyager → tenta o Juriscope
        # (query live, sem proxy). Se estiver lá, monta o dossiê de precatório dali.
        jd = _dossie_juriscope(cnj)
        if jd:
            return jd
        return {'erro': (f'Processo {cnj} não encontrado no acervo Voyager nem no '
                         f'Juriscope. Busca em tempo real nas fontes do tribunal '
                         f'(DJEN/enricher) depende do pipeline de ingestão.'), 'cnj': cnj}

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
