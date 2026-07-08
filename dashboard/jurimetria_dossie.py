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
    r'trabalhist|ferrovi[áa]ri|alimentar|honor[áa]ri|indeniza|'
    # verbas salariais/funcionais (servidor público) — natureza alimentar:
    r'adicional|gratifica|hora[s]?\s+extra|verba|qu[íi]nqu[êe]ni|abono|aux[íi]lio|'
    r'proventos|reajuste|diferen[çc]a[s]?\s+salari|13[ºo\b]|f[ée]rias|insalubr|periculos|URV', re.I)


# Tooltips (hover) explicando cada indicador — pra plateia que não conhece os termos.
_TIPS = {
    'Fase do mérito': 'Cumprimento de sentença/execução = o mérito já foi julgado e o '
        'crédito reconhecido; resta executar. É o sinal mais forte de que o crédito existe.',
    'Chance de virar precatório': 'Probabilidade estimada (modelo Kaplan-Meier) de o direito '
        'creditório virar precatório em 24 meses, a partir do histórico de casos do mesmo estrato.',
    'Tempo estimado': 'Tempo mediano (meses) até virar precatório, pelo mesmo modelo de sobrevivência.',
    'Natureza': 'Natureza alimentar (salários, aposentadoria, verbas de servidor) tem prioridade '
        'de pagamento sobre precatórios comuns (art. 100, §1º, CF).',
    'Valor': 'Valor da ação/precatório — corrigido quando o Juriscope tem o dado, senão o valor da causa.',
    'Taxa do tipo': '% dos processos do mesmo assunto no nosso acervo que já viraram precatório.',
    'Beneficiários': 'Nº de exequentes/requerentes — execução coletiva costuma indicar verba de categoria.',
    'Pagamento previsto': 'Ano orçamentário em que o precatório deve ser pago (cronograma constitucional EC 114/136).',
}

_TIPS_PILAR = {
    'Certeza do crédito': 'Quão certo é que o crédito existe: precatório expedido > cálculos '
        'homologados > título constituído (cumprimento de sentença) > execução contra Fazenda.',
    'Prioridade (natureza)': 'Natureza alimentar fura a fila dos precatórios comuns (art. 100 CF). Peso 15%.',
    'Solvência do ente': 'Capacidade do ente pagar: rating CAPAG (A/B/C/D) do Tesouro + estoque de '
        'precatórios vs. RCL. Peso 25%.',
    'Liquidez (prazo)': 'Quão rápido o crédito vira dinheiro: quanto menor o prazo até o pagamento, '
        'maior. Peso 20%.',
    'Margem (deságio)': 'Deságio justo disponível (desconto a valor presente) — margem de oportunidade '
        'na aquisição. Peso 15%.',
}


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

    # Título judicial constituído: cumprimento de sentença / execução = mérito JÁ
    # decidido e crédito reconhecido (o credor venceu e está executando). Sinal forte.
    classe = proc.classe_nome or ''
    titulo_constituido = bool(re.search(
        r'cumprimento de senten|execu[çc][ãa]o de t[íi]tulo|execu[çc][ãa]o de senten|'
        r'^execu[çc][ãa]o|liquida[çc][ãa]o de senten', classe, re.I))

    sinais, indicadores = [], []
    if titulo_constituido:
        sinais.append(f'Mérito já reconhecido — {classe.title()} (título judicial constituído, em execução)')
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
        _merito = 'com mérito já reconhecido (cumprimento de sentença)' if titulo_constituido \
            else 'ainda não expedido'
        veredito = (f'Execução contra a Fazenda Pública{" (" + ente + ")" if ente else ""} — '
                    f'caminho direto para precatório/RPV, {_merito}.')
        recomendacao = {'label': '🔥 Lead quente — execução contra Fazenda', 'tom': 'accent'} \
            if (natureza == 'ALIMENTAR' or n_exequentes >= 2 or titulo_constituido) else \
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
    if titulo_constituido:
        indicadores.insert(0, {'label': 'Fase do mérito', 'valor': '✓ decidido',
                               'sub': 'cumprimento de sentença — crédito reconhecido'})
    if tipo.get('disponivel'):
        indicadores.append({'label': 'Taxa do tipo', 'valor': f'{tipo["taxa_precatorio"]}%',
                            'sub': f'viram precatório (n={tipo["total"]:,})'.replace(',', '.')})

    for ind in indicadores:
        ind['tip'] = _TIPS.get(ind['label'], '')
    return {
        'estagio': estagio, 'tom': tom, 'veredito': veredito,
        'recomendacao': recomendacao, 'indicadores': indicadores,
        'sinais': sinais, 'chance': chance,
        'ente': ente, 'natureza': natureza,  # pro bloco Precatório cair aqui quando Juriscope vazio
        'contra_fazenda': contra_fazenda, 'ja_precatorio': ja_precatorio,
        'titulo_constituido': titulo_constituido, 'classe': classe,
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
    from django.db import transaction
    from django.db.models import Count, Q
    base = Process.objects.filter(tribunal_id=proc.tribunal_id,
                                  classe_codigo=proc.classe_codigo)
    # UMA query com contagens condicionais, sob statement_timeout REAL (atomic — o
    # SET LOCAL só vale dentro de transação; fora, autocommit ignorava e classes
    # federais com milhões de linhas TRAVAVAM o dossiê → timeout 500).
    try:
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout='6000'")
            agg = base.aggregate(
                total=Count('id'),
                precat=Count('id', filter=Q(classificacao='PRECATORIO')),
                pre=Count('id', filter=Q(classificacao='PRE_PRECATORIO')))
    except Exception:  # noqa: BLE001 — timeout/erro: tipo amplo demais
        return {'disponivel': False, 'motivo': 'tipo muito amplo para agregar em tempo hábil'}
    total, precat, pre = agg['total'], agg['precat'], agg['pre']
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
    # Capacidade fiscal do ente devedor (SICONFI) — só p/ precatório ESTADUAL (TJ→UF).
    # Fail-closed + cacheado; define a banda de pagamento anual da EC 136.
    # dispara pra qualquer precatório OU execução contra a Fazenda: classificação, Juriscope,
    # ou o órgão/classe indicando Fazenda/execução (ex.: TJAL não está no Juriscope mas é
    # execução contra o Estado). O ente_fiscal é cacheado (barato).
    _faz_txt = f"{proc.orgao_julgador_nome or ''} {proc.classe_nome or ''}".lower()
    parece_fazenda = ('fazenda' in _faz_txt or bool(_ORGAO_FAZ_RE.search(_faz_txt))
                      or bool(re.search(r'cumprimento de senten|execu[çc][ãa]o', _faz_txt)))
    eh_precatorio = (is_lead or bool((js or {}).get('encontrado'))
                     or bool((js or {}).get('valor_acao')) or parece_fazenda)
    ente_fiscal = None
    if eh_precatorio and (proc.tribunal_id or '').upper().startswith('TJ'):
        try:
            from . import fontes_publicas
            _uf = proc.tribunal_id.upper()[2:4]
            ef = fontes_publicas.ente_fiscal(uf=_uf)
            if ef and not ef.get('erro') and ef.get('rcl'):
                cap = fontes_publicas.capag_rating(_uf)
                if cap and not cap.get('erro'):
                    ef['capag'] = cap
                ente_fiscal = ef
        except Exception:  # noqa: BLE001
            pass

    # HERO: valor justo hoje + deságio implícito (valor_presente). Precisa de valor de
    # face + horizonte até o pagamento. Horizonte: do cronograma (ano_orçamento) OU da
    # banda de vazão do ente (estoque/RCL ÷ %/ano da EC 136). Determinístico, público.
    valor_justo = None
    _vface = (js or {}).get('valor_acao_corrigido') or (js or {}).get('valor_acao') or proc.valor_causa
    _anos = None
    _anos_estimado = False
    if pagamento and pagamento.get('ano_orcamento'):
        from django.utils import timezone
        _anos = max(pagamento['ano_orcamento'] - timezone.now().year, 0.5)
    elif ente_fiscal and ente_fiscal.get('razao_estoque_rcl') and ente_fiscal.get('pagamento_anual_estimado_pct_rcl'):
        _anos = min(round(ente_fiscal['razao_estoque_rcl'] / (ente_fiscal['pagamento_anual_estimado_pct_rcl'] / 100), 1), 20)
    elif sobrevivencia and sobrevivencia.get('tempo_mediano_meses'):
        # pré-precatório sem cronograma: tempo até virar precatório (KM) + lag típico de pagamento
        _anos = round(sobrevivencia['tempo_mediano_meses'] / 12 + 4, 1)
        _anos_estimado = True
    if _vface and _anos:
        try:
            from . import fontes_publicas
            vp = fontes_publicas.valor_presente(float(_vface), _anos)
            if vp and not vp.get('erro'):
                vp['valor_face_fmt'] = fontes_publicas._humano(vp.get('valor_face'))
                vp['valor_presente_fmt'] = fontes_publicas._humano(vp.get('valor_presente'))
                vp['horizonte_estimado'] = _anos_estimado
                valor_justo = vp
        except Exception:  # noqa: BLE001
            pass

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
        'ente_fiscal': ente_fiscal,      # SICONFI: capacidade de pagamento do ente (EC 136)
        'valor_justo': valor_justo,      # valor_presente: valor justo hoje + deságio
        'meta': {'fonte': 'classificacao v6 + movimentações DJEN + juriscope/falcon',
                 'tipo': 'modelo + estruturado'},
    }


def score_oportunidade(dossie: dict) -> dict | None:
    """Score de Oportunidade 0–100 do lead — 5 pilares ponderados, com breakdown
    auditável. Re-normaliza sobre os pilares COM dado (não zera o que falta)."""
    dg = dossie.get('diagnostico') or {}
    p = dossie.get('precatorio') or {}
    ef = p.get('ente_fiscal') or {}
    cap = (ef or {}).get('capag') or {}
    sb = p.get('sobrevivencia') or {}
    vj = p.get('valor_justo') or {}
    homolog = (p.get('homologacao') or {}).get('ocorreu')

    # S_titulo — certeza de que o crédito existe
    if dg.get('ja_precatorio'):
        s_t = 1.0
    elif homolog:
        s_t = 0.85
    elif dg.get('titulo_constituido'):
        s_t = 0.70
    elif dg.get('contra_fazenda'):
        s_t = 0.40
    elif sb.get('chance_24m') is not None:
        s_t = sb['chance_24m'] / 100
    else:
        s_t = None
    # S_natureza
    nat = dg.get('natureza')
    s_n = 1.0 if nat == 'ALIMENTAR' else (0.5 if nat else None)
    # S_ente — solvência
    s_e = None
    if cap.get('nota'):
        s_e = {'A': 1.0, 'B': 0.8, 'C': 0.4, 'D': 0.15}.get(cap['nota'][:1].upper(), 0.5)
    elif ef.get('rcl'):
        s_e = 0.6
    if s_e is not None and ef.get('razao_estoque_rcl'):
        s_e *= max(min(1 - 0.4 * max(ef['razao_estoque_rcl'] - 1, 0), 1), 0.3)
    # S_tempo — liquidez
    anos = vj.get('anos_ate_pagamento')
    s_tempo = max(min(1 - anos / 10, 1), 0) if anos is not None else None
    # S_margem — deságio
    des = vj.get('desagio_implicito_pct')
    s_m = max(min(des / 50, 1), 0) if des is not None else None

    defs = [('Certeza do crédito', 0.25, s_t), ('Prioridade (natureza)', 0.15, s_n),
            ('Solvência do ente', 0.25, s_e), ('Liquidez (prazo)', 0.20, s_tempo),
            ('Margem (deságio)', 0.15, s_m)]
    disp = [(n, w, v) for n, w, v in defs if v is not None]
    if not disp:
        return None
    peso_tot = sum(w for _, w, _ in disp)
    score = 100 * sum(w * v for _, w, v in disp) / peso_tot
    pilares = [{'nome': n, 'peso': w, 'pct': round(v * 100) if v is not None else None,
                'contrib': round(w * v / peso_tot * 100, 1) if v is not None else None,
                'tip': _TIPS_PILAR.get(n, ''), 'ok': v is not None} for n, w, v in defs]
    faixa = 'forte' if score >= 70 else ('moderada' if score >= 40 else 'fraca')
    tom = 'ok' if score >= 70 else ('accent' if score >= 40 else 'muted')
    return {'score': round(score), 'faixa': faixa, 'tom': tom, 'pilares': pilares,
            'n_pilares': len(disp)}


def fontes_e_pesos(dossie: dict) -> list[dict]:
    """Transparência: todas as FONTES usadas no dossiê, com peso (papel no veredito) e o
    VALOR que cada uma trouxe pra este CNJ. Alimenta a modal 'Fontes & pesos'."""
    c = dossie.get('cabecalho') or {}
    p = dossie.get('precatorio') or {}
    js = p.get('juriscope') or {}
    ef = p.get('ente_fiscal') or {}
    cap = (ef or {}).get('capag') or {}
    sb = p.get('sobrevivencia') or {}
    tipo = dossie.get('jurimetria_tipo') or {}
    prec = dossie.get('precedentes') or {}
    polos = dossie.get('polos') or {}
    n_partes = sum(len(v) for v in polos.values())
    vj = p.get('valor_justo') or {}
    cnj_digits = (dossie.get('cnj') or '').replace('-', '').replace('.', '')
    return [
        {'fonte': 'Classificação ML (Voyager)', 'tipo': 'modelo próprio', 'peso': 'Alto',
         'ok': bool(p.get('classificacao')), 'url': None,
         'valor': f"{p.get('classificacao') or 'sem classe'} · score {p.get('score') or 0}"},
        {'fonte': 'Juriscope / Falcon', 'tipo': 'precatório estruturado', 'peso': 'Alto',
         'ok': bool(js.get('encontrado')),
         'url': f'https://esaj.tjsp.jus.br/cpopg/open.do' if js.get('encontrado') else None,
         'valor': (f"{js.get('n_precatorios') or 0} precatório(s) · "
                   f"{js.get('valor_acao_corrigido_fmt') or js.get('valor_acao_fmt') or '—'} · "
                   f"{js.get('natureza') or '—'}") if js.get('encontrado') else 'sem registro'},
        {'fonte': 'SICONFI / Tesouro', 'tipo': 'fiscal do ente (público)', 'peso': 'Alto',
         'ok': bool(ef.get('rcl')), 'url': ef.get('fonte_url'),
         'valor': (f"RCL {ef.get('rcl_fmt')} · paga ~{ef.get('pagamento_anual_estimado_pct_rcl')}%/ano (EC 136)")
                  if ef.get('rcl') else '—'},
        {'fonte': 'CAPAG / Tesouro', 'tipo': 'rating do ente (público)', 'peso': 'Médio',
         'ok': bool(cap.get('nota')), 'url': cap.get('fonte_url'),
         'valor': f"nota {cap.get('nota')} ({cap.get('significado')})" if cap.get('nota') else '—'},
        {'fonte': 'BCB / Selic (valor presente)', 'tipo': 'índice oficial (público)', 'peso': 'Médio',
         'ok': bool(vj), 'url': vj.get('fonte_url'),
         'valor': (f"deságio {vj.get('desagio_implicito_pct')}% · Selic {vj.get('selic_meta_aa_pct')}%") if vj else '—'},
        {'fonte': 'Kaplan-Meier', 'tipo': 'modelo preditivo', 'peso': 'Médio',
         'ok': bool(sb), 'url': None,
         'valor': f"{sb.get('chance_24m')}% em 24m · n={sb.get('n')}" if sb else 'n/a'},
        {'fonte': 'Cronograma EC 114/136', 'tipo': 'determinístico', 'peso': 'Médio',
         'ok': bool(p.get('pagamento')),
         'url': 'https://www.planalto.gov.br/ccivil_03/constituicao/emendas/emc/emc136.htm',
         'valor': f"orçamento {(p.get('pagamento') or {}).get('ano_orcamento')}" if p.get('pagamento') else '—'},
        {'fonte': 'Movimentações DJEN', 'tipo': 'ingestão nacional', 'peso': 'Médio',
         'ok': bool(c.get('total_movimentacoes')),
         'url': f'https://comunicaapi.pje.jus.br/api/v1/comunicacao?numeroProcesso={cnj_digits}' if cnj_digits else None,
         'valor': f"{c.get('total_movimentacoes') or 0} movimentações"},
        {'fonte': 'Zordon (precedentes)', 'tipo': 'RAG semântico', 'peso': 'Médio',
         'ok': bool(prec.get('itens')), 'url': None,
         'valor': f"{len(prec.get('itens') or [])} precedentes (bge-m3 + rerank)"},
        {'fonte': 'e-SAJ (partes/incidentes)', 'tipo': 'enricher público', 'peso': 'Baixo',
         'ok': n_partes > 0, 'url': 'https://esaj.tjsp.jus.br/cpopg/open.do', 'valor': f"{n_partes} partes"},
    ]


def _precedentes(proc: Process, limite: int = 4) -> dict:
    """Precedentes relevantes via RAG do Zordon (degrada se indisponível)."""
    from . import zordon_client
    termos = ' '.join(t for t in [proc.classe_nome, proc.assunto_nome] if t)[:200]
    if not termos:
        return {'itens': [], 'meta': {'fonte': 'zordon RAG', 'query': ''}}
    res = zordon_client.buscar(query=termos, limit=limite)
    itens = (res or {}).get('results') or []
    # POR QUE é relevante: o tema casado (assunto/classe) + o score de similaridade.
    assunto = proc.assunto_nome or ''
    for it in itens:
        sc = it.get('score')
        it['relevancia'] = (f'mesmo assunto: {assunto}' if assunto
                            else f'mesma classe: {proc.classe_nome or "tema similar"}')
        it['score_pct'] = round(sc * 100) if isinstance(sc, (int, float)) and sc <= 1 else (
            round(sc) if isinstance(sc, (int, float)) else None)
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
                         f'real na fonte ({sigla}).'))


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
                            f'Buscando dados em tempo real na fonte ({sigla}).')
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
