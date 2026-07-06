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


# Heurísticas do diagnóstico — quando a classificação ML não disparou (ex.: 0 movs
# DJEN), os dados enriquecidos (ente, órgão, assunto, partes) ainda concluem o estágio.
_ENTE_PUB_RE = re.compile(
    r'fazenda|estado d|munic[íi]pio|\buni[ãa]o\b|\bINSS\b|instituto de previd|autarqui|'
    r'prefeitura|governo d|distrito federal|departamento de|companhia de|caixa econ', re.I)
_ORGAO_FAZ_RE = re.compile(
    r'execu[çc][õo]es?\s+(?:contra|fisca|.*fazend)|fazenda\s+p[úu]blica|precat[óo]ri|'
    r'UPEFAZ|requisit[óo]ri|juizo auxiliar', re.I)
_ALIMENTAR_RE = re.compile(
    r'benef[íi]cio|previdenci|aposentad|pens[ãa]o|sal[áa]ri|vencimento|remunera|servidor|'
    r'trabalhist|ferrovi[áa]ri|alimentar|honor[áa]ri|indeniza', re.I)


def _diagnostico(proc: Process, precatorio: dict, tipo: dict, polos: dict) -> dict:
    """Camada analítica: sintetiza os sinais (classificação + Juriscope + dados
    enriquecidos: ente, órgão, assunto, partes) num VEREDITO + indicadores +
    recomendação. Funciona mesmo sem classificação ML (0 movs DJEN)."""
    from . import survival_precatorio
    js = precatorio.get('juriscope') or {}
    orgao = proc.orgao_julgador_nome or ''
    assunto = proc.assunto_nome or ''
    passivo_txt = ' '.join(p.get('nome', '') for p in polos.get('passivo', []))
    n_exequentes = sum(1 for p in polos.get('ativo', [])
                       if re.search(r'exeqte|reqte|requer|exequen', (p.get('papel') or ''), re.I))

    ente = js.get('ente_nome') or js.get('devedora')
    if not ente:  # tenta achar o ente público no polo passivo
        for p in polos.get('passivo', []):
            if _ENTE_PUB_RE.search(p.get('nome', '')):
                ente = p.get('nome'); break
    contra_fazenda = bool(ente) or bool(_ORGAO_FAZ_RE.search(orgao)) or bool(_ENTE_PUB_RE.search(passivo_txt))
    natureza = js.get('natureza') or ('ALIMENTAR' if _ALIMENTAR_RE.search(assunto) else None)

    ja_precatorio = bool(js.get('encontrado')) or proc.classificacao == 'PRECATORIO' \
        or bool(_ORGAO_FAZ_RE.search(orgao) and re.search(r'precat', orgao, re.I)) \
        or precatorio.get('tem_sinal_expedicao')

    sinais, indicadores = [], []
    if ente: sinais.append(f'Ente devedor: {ente}')
    if _ORGAO_FAZ_RE.search(orgao): sinais.append(f'Órgão de execução contra a Fazenda ({orgao[:60]})')
    if natureza == 'ALIMENTAR' or _ALIMENTAR_RE.search(assunto):
        sinais.append(f'Natureza alimentar (assunto: {assunto[:50]})')
    if n_exequentes >= 2: sinais.append(f'{n_exequentes} exequentes/requerentes (execução coletiva)')

    # --- estágio + veredito + recomendação ---
    chance = survival_precatorio.prever(proc.tribunal_id, natureza, ente) if (contra_fazenda and not ja_precatorio) else None
    if ja_precatorio:
        estagio, tom = 'PRECATÓRIO', 'ok'
        pag = precatorio.get('pagamento') or _cronograma_pagamento(js.get('ano_ordem_orcamentaria'))
        veredito = f'Precatório {("de natureza " + natureza.lower()) if natureza else ""} — requisição já no fluxo de pagamento.'
        recomendacao = {'label': '💰 Precatório expedido — ativo pronto', 'tom': 'ok'}
        if pag and pag.get('ano_orcamento'):
            indicadores.append({'label': 'Pagamento previsto', 'valor': pag['ano_orcamento'],
                                'sub': 'orçamento (até 31/dez)' if not pag.get('em_atraso') else 'em atraso'})
    elif contra_fazenda:
        estagio, tom = 'PRÉ-PRECATÓRIO', 'accent'
        veredito = (f'Execução contra a Fazenda Pública{" (" + ente + ")" if ente else ""} — '
                    f'caminho direto para precatório/RPV. Ainda não expedido.')
        recomendacao = {'label': '🔥 Lead quente — execução contra Fazenda', 'tom': 'accent'} \
            if (natureza == 'ALIMENTAR' or n_exequentes >= 2) else \
            {'label': '📌 Acompanhar — pré-precatório', 'tom': 'accent'}
    elif proc.classificacao == 'DIREITO_CREDITORIO':
        estagio, tom = 'DIREITO CREDITÓRIO', 'muted'
        veredito = 'Direito creditório em formação — ainda distante do precatório.'
        recomendacao = {'label': '🌱 Monitorar formação do crédito', 'tom': 'muted'}
    else:
        estagio, tom = 'INDEFINIDO', 'muted'
        veredito = ('Sem sinais suficientes de execução contra a Fazenda neste processo. '
                    'Pode não ser um caminho de precatório.')
        recomendacao = {'label': '⚪ Sem indício de precatório', 'tom': 'muted'}

    # --- indicadores da jurimetria (números) ---
    if chance:
        indicadores.insert(0, {'label': 'Chance de virar precatório',
                               'valor': f'{chance["chance_24m"]}%', 'sub': 'em 24 meses (Kaplan-Meier)'})
        if chance.get('tempo_mediano_meses'):
            indicadores.append({'label': 'Tempo estimado', 'valor': f'~{chance["tempo_mediano_meses"]}',
                                'sub': 'meses (mediana)'})
    if natureza:
        indicadores.append({'label': 'Natureza', 'valor': ('🟢 ' if natureza == 'ALIMENTAR' else '') + natureza,
                            'sub': 'prioridade alimentar' if natureza == 'ALIMENTAR' else 'comum'})
    val = js.get('valor_acao_corrigido') or js.get('valor_acao') or proc.valor_causa
    if val:
        try:
            val_num = float(str(val).replace('.', '').replace(',', '.')) if isinstance(val, str) else float(val)
            indicadores.append({'label': 'Valor', 'valor': f'R$ {val_num:,.0f}'.replace(',', '.'),
                                'sub': 'da ação/precatório'})
        except (ValueError, TypeError):
            pass
    if n_exequentes >= 2:
        indicadores.append({'label': 'Beneficiários', 'valor': n_exequentes, 'sub': 'exequentes (execução coletiva)'})
    if tipo.get('disponivel'):
        indicadores.append({'label': 'Taxa do tipo', 'valor': f'{tipo["taxa_precatorio"]}%',
                            'sub': f'viram precatório (n={tipo["total"]:,})'.replace(',', '.')})

    return {
        'estagio': estagio, 'tom': tom, 'veredito': veredito,
        'recomendacao': recomendacao, 'indicadores': indicadores,
        'sinais': sinais, 'chance': chance,
        'contra_fazenda': contra_fazenda, 'ja_precatorio': ja_precatorio,
        'meta': {'fonte': 'síntese jurimetria (classificação + Juriscope + enriquecido) + Kaplan-Meier',
                 'tipo': 'conclusivo'},
    }


def _diagnostico_safe(proc, precatorio, tipo, polos):
    """Blindagem: um bug no diagnóstico NUNCA pode derrubar o dossiê (500). Em falha,
    devolve None (o card some) e loga."""
    try:
        return _diagnostico(proc, precatorio, tipo, polos)
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).exception('diagnostico falhou p/ %s', proc.numero_cnj)
        return None


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


# CNJ NNNNNNN-DD.AAAA.J.TR.OOOO → sigla, pros tribunais que têm enricher (fetch
# em tempo real). J=8 estadual, J=4 federal. TR = número do tribunal.
_CNJ_SIGLA = {
    '8.01': 'TJAC', '8.02': 'TJAL', '8.03': 'TJAP', '8.04': 'TJAM', '8.05': 'TJBA',
    '8.06': 'TJCE', '8.07': 'TJDFT', '8.08': 'TJES', '8.09': 'TJGO', '8.10': 'TJMA',
    '8.11': 'TJMT', '8.12': 'TJMS', '8.13': 'TJMG', '8.14': 'TJPA', '8.15': 'TJPB',
    '8.16': 'TJPR', '8.17': 'TJPE', '8.18': 'TJPI', '8.19': 'TJRJ', '8.20': 'TJRN',
    '8.21': 'TJRS', '8.22': 'TJRO', '8.23': 'TJRR', '8.24': 'TJSC', '8.25': 'TJSE',
    '8.26': 'TJSP', '8.27': 'TJTO',
    '4.01': 'TRF1', '4.02': 'TRF2', '4.03': 'TRF3', '4.04': 'TRF4', '4.05': 'TRF5', '4.06': 'TRF6',
}


def _sigla_de_cnj(cnj: str) -> str | None:
    d = re.sub(r'\D', '', cnj or '')
    if len(d) < 16:
        return None
    return _CNJ_SIGLA.get(f'{d[13]}.{d[14:16]}')


def _processando(cnj: str, sigla: str, pk, msg: str) -> dict:
    return {'cnj': cnj, 'processando': True, 'tribunal': sigla, 'pk': pk, 'msg': msg}


def _buscar_tempo_real(cnj: str) -> dict:
    """Processo fora do acervo e sem dados no Juriscope → busca em tempo real na
    fonte do tribunal: cria o Process (entra no acervo) + enfileira o enricher.
    Os dados aparecem no reload."""
    from tribunals.models import Tribunal
    from enrichers.jobs import _ENRICHERS, enqueue_enriquecimento_manual
    sigla = _sigla_de_cnj(cnj)
    if not sigla or sigla not in _ENRICHERS:
        return {'erro': (f'Processo {cnj} não está no acervo nem no Juriscope, e não há '
                         f'enricher em tempo real para {sigla or "esse tribunal"}.'), 'cnj': cnj}
    trib = Tribunal.objects.filter(sigla=sigla).first()
    if not trib:
        return {'erro': f'Tribunal {sigla} não cadastrado.', 'cnj': cnj}
    proc, _ = Process.objects.get_or_create(
        tribunal=trib, numero_cnj=cnj,
        defaults={'enriquecimento_status': 'pendente'})
    enqueue_enriquecimento_manual(proc.pk)
    return _processando(cnj, sigla, proc.pk,
                        (f'Processo não estava no acervo — adicionado e buscando dados em tempo '
                         f'real na fonte ({sigla}). Recarregue em ~15s.'))


def _reenriquecer_se_vazio(proc, cnj: str):
    """Blindagem contra dossiê vazio: se o Process existe mas NÃO tem partes e o
    status indica tentativa incompleta/falha (pendente/erro), re-dispara o enrich
    e devolve 'processando' — em vez de mostrar um dossiê vazio. Se já concluiu
    como nao_encontrado, devolve mensagem clara (não uma tela vazia). None quando
    o processo tem dados e o dossiê normal deve seguir."""
    from enrichers.jobs import _ENRICHERS, enqueue_enriquecimento_manual
    n_partes = ProcessoParte.objects.filter(processo=proc).count()
    if n_partes > 0:
        return None  # tem dados → dossiê normal
    sigla = proc.tribunal_id
    status = proc.enriquecimento_status
    if status in ('pendente', 'processando', 'erro') and sigla in _ENRICHERS:
        enqueue_enriquecimento_manual(proc.pk)
        return _processando(cnj, sigla, proc.pk,
                            (f'Buscando dados em tempo real na fonte ({sigla}). '
                             f'Recarregue em ~15s.'))
    if status == 'nao_encontrado':
        return {'cnj': cnj, 'pk': proc.pk, 'erro': (
            f'Processo {cnj} não encontrado na consulta pública do {sigla} '
            f'(pode não existir lá, estar em segredo de justiça, ou a fonte estar '
            f'instável). O re-enriquecimento automático tentará de novo.')}
    return None  # ok mas sem partes (ex.: processo sem partes públicas) → dossiê normal


def _dossie_juriscope(cnj: str) -> dict | None:
    """Monta o dossiê a partir do Juriscope quando o processo NÃO está no acervo
    Voyager (busca live por CNJ, sem proxy). None se também não está no Juriscope."""
    from . import juriscope_client, survival_precatorio
    js = juriscope_client.dados_precatorio(cnj)
    if not js or not js.get('encontrado'):
        return None
    # Registro "casca" (CNJ existe mas sem NENHUM dado estruturado) → trata como
    # não-encontrado pra cair na busca em tempo real na fonte do tribunal.
    if not any([js.get('natureza'), js.get('valor_acao'), js.get('valor_acao_corrigido'),
                js.get('entity_id'), js.get('ordem_orcamentaria'), js.get('data_oficio'),
                js.get('files_downloaded')]):
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
        # Nem no acervo, nem dados no Juriscope → busca em tempo real na fonte do
        # tribunal (cria Process + enfileira enricher).
        return _buscar_tempo_real(cnj)

    # Process existe mas pode estar vazio por uma tentativa anterior falha (ex.:
    # proxy que resetou). Nunca mostrar vazio: re-dispara o enrich / mensagem clara.
    blindagem = _reenriquecer_se_vazio(proc, cnj)
    if blindagem is not None:
        return blindagem

    participacoes = (ProcessoParte.objects.filter(processo=proc)
                     .select_related('parte').order_by('polo', 'papel'))
    polos: dict = {'ativo': [], 'passivo': [], 'outros': []}
    for pp in participacoes:
        polos.setdefault(pp.polo, []).append(
            {'nome': pp.parte.nome, 'papel': pp.papel, 'polo': pp.polo})

    precatorio = _bloco_precatorio(proc)
    tipo = _jurimetria_do_tipo(proc)
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
        'diagnostico': _diagnostico_safe(proc, precatorio, tipo, polos),
        'polos': polos,
        'precatorio': precatorio,
        'jurimetria_tipo': tipo,
        'precedentes': _precedentes(proc),
    }
