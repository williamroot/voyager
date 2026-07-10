"""Tool `explicar_modelos` — expõe os MODELOS do Voyager com dados VIVOS do código.

Serve o chat/agente/MCP de jurimetria: pesos reais da regressão logística (via
`_current_weights()`, nunca cópia hardcoded em prosa), estratos do survival
Kaplan-Meier (artefato surv_strata), pilares do score de oportunidade e o
catálogo de fontes. Se o código mudar, a resposta muda junto.
"""
from __future__ import annotations

# Descrição de cada feature do classificador (o QUE ela mede; o peso vem vivo).
_FEATURES_DESC = {
    '_intercept_':        'intercepto da regressão logística',
    'F1_cumprim':         'classe é Cumprimento de Sentença/Execução contra a Fazenda (códigos TPU fixos)',
    'F2_precat_tc':       'alguma comunicação DJEN com tipo "precatório"',
    'F7_envTrib_tc':      'comunicação de envio/remessa ao tribunal',
    'F10_juizado_ANTI':   'classe anti-sinal: juizado especial / recurso inominado / procedimento comum',
    'F11_precat_text':    '"precatório" citado no texto das movimentações',
    'F12_rpv_text':       'RPV (requisição de pequeno valor) citada no texto',
    'F13_reqPag_text':    'requisição de pagamento citada no texto',
    'F14_oficio_text':    'ofício requisitório citado no texto',
    'F15_logMovs':        'log do nº total de movimentações (normalizado /log 500)',
    'F16_logTipos':       'log do nº de tipos distintos de movimentação (/log 50)',
    'F17_logN1count':     'log da soma de menções N1 (precatório+RPV+req.pag+ofício) (/log 20)',
    'F18_anoZ':           'ano do CNJ normalizado (z-score, média ~2019.7)',
    'F19_cancelado_ANTI': 'comunicação cancelada (anti-sinal; atua como regra, peso 0)',
    'F20_exp_juriscope':  'expedição de precatório/RPV confirmada via Juriscope',
    'F21_diasUltMovZ':    'dias desde a última movimentação (z-score)',
    'F23_logPartes':      'log do nº de partes (/log 50)',
    'F1xF11':             'interação: cumprimento × "precatório" no texto',
    'F1xF15':             'interação: cumprimento × volume de movimentações',
    'F1xF20':             'interação: cumprimento × expedição Juriscope',
}

_OVERRIDES_DESC = {
    'F24_pago_pos_exped_ANTI':
        'pagamento APÓS a expedição detectado no texto → lead já pago, rebaixa a NAO_LEAD. '
        'Peso LR 0 (atua como regra); gated por tribunal (PAGAMENTO_SINAL_TRIBUNAIS).',
    'F30_extinto_neg_ANTI':
        'desfecho terminal NEGATIVO (extinção sem mérito / improcedência / indeferimento '
        'da inicial / prescrição) sendo a última palavra (sem expedição posterior) → '
        'crédito não existe, rebaixa QUALQUER lead a NAO_LEAD (score 0,10). Override '
        'GLOBAL (todos os tribunais), fora dos pesos da LR.',
}


def _classificador() -> dict:
    from tribunals import classificador as clf
    pesos = clf._current_weights()  # noqa: SLF001 — leitura intencional dos pesos vivos
    return {
        'tipo': 'Regressão logística (sigmoide sobre soma ponderada de features)',
        'versao_ativa': clf.get_versao_ativa(),
        'metricas_treino': dict(clf.METRICAS),
        'pesos': {k: v for k, v in pesos.items()},
        'features': {k: _FEATURES_DESC.get(k, '?') for k in pesos},
        'overrides': dict(_OVERRIDES_DESC),
        'nota': 'Pesos carregados da versão ATIVA no banco (fallback hardcoded v6). '
                'Overrides F24/F30 não entram na soma — são regras de rebaixamento '
                'aplicadas depois do score.',
    }


def _survival() -> dict:
    from . import survival_precatorio as sp
    s = sp._strata() or {}  # noqa: SLF001
    estratos = []
    for chave, v in s.items():
        if not isinstance(v, dict):
            continue
        estratos.append({'estrato': chave, 'n': v.get('n'), 'eventos': v.get('eventos'),
                         'chance_12m': v.get('chance_12m'), 'chance_24m': v.get('chance_24m'),
                         'mediana_meses': v.get('tempo_mediano_meses') or v.get('mediana_meses')})
    return {
        'tecnica': 'Kaplan-Meier estratificado por {tipo de ente | natureza}, com fallback '
                   'pra {ente|*} e depois pro estrato geral (_overall). Cox multi-feature é '
                   'usado só no FIT offline (scripts/treinar_sobrevivencia_fit.py).',
        'pergunta_que_responde': 'dado um pré-precatório, qual a chance de virar precatório '
                                 'em 12/24 meses e o tempo mediano até lá',
        'artefato': 'dashboard/data/surv_strata(.live).json — re-treinado automaticamente',
        'n_estratos': len(estratos),
        'estratos': sorted(estratos, key=lambda e: -(e['n'] or 0))[:20],
    }


def _score_oportunidade() -> dict:
    return {
        'formula': 'score 0–100 = 100 × Σ(peso_i × valor_i) / Σ(pesos dos pilares COM dado) '
                   '— re-normaliza sobre o que existe, não zera pilar sem dado.',
        'pilares': [
            {'nome': 'Certeza do crédito', 'peso': 0.25,
             'como': 'desfecho terminal negativo=0.02 · pago=0.10 · precatório expedido=1.0 '
                     '· homologado=0.85 · sinal de expedição=0.70 · cumprimento=0.40 · '
                     'senão chance_24m do survival'},
            {'nome': 'Prioridade (natureza)', 'peso': 0.15, 'como': 'ALIMENTAR=1.0 · comum=0.5'},
            {'nome': 'Solvência do ente', 'peso': 0.25,
             'como': 'CAPAG A=1.0 B=0.8 C=0.4 D=0.15 (0.6 se só RCL); penaliza estoque/RCL>1'},
            {'nome': 'Liquidez (prazo)', 'peso': 0.20, 'como': '1 − anos_até_pagamento/10 (clamp 0..1)'},
            {'nome': 'Margem (deságio)', 'peso': 0.15, 'como': 'deságio_implícito/50% (clamp 0..1)'},
        ],
        'gate': 'Certeza < 0.15 (extinto/pago) TRAVA o score em ~certeza×100+5 — crédito '
                'inexistente não vira oportunidade por melhor que sejam ente/natureza.',
        'faixas': {'forte': '≥70', 'moderada': '40–69', 'fraca': '<40'},
        'onde': 'dashboard/jurimetria_dossie.py::score_oportunidade',
    }


def _fontes() -> dict:
    return {
        'nota': 'Catálogo das fontes do dossiê (o valor POR PROCESSO aparece na modal '
                '"Fontes & pesos" do dossiê e via tool dossie_jurimetrico).',
        'fontes': [
            {'fonte': 'DJEN/Comunica (movimentações)', 'tipo': 'oficial', 'papel': 'base do diagnóstico/estágio e do classificador'},
            {'fonte': 'e-SAJ/PJe (enriquecimento)', 'tipo': 'oficial', 'papel': 'capa do processo: classe, assunto, valor, partes'},
            {'fonte': 'Juriscope/Falcon', 'tipo': 'interna', 'papel': 'precatório real: natureza, ente, valor, ordem orçamentária'},
            {'fonte': 'SICONFI/Tesouro (RGF)', 'tipo': 'oficial', 'papel': 'saúde fiscal do ente: estoque, DCL, RCL, banda EC 136'},
            {'fonte': 'CAPAG/Tesouro', 'tipo': 'oficial', 'papel': 'rating A–D de capacidade de pagamento do ente'},
            {'fonte': 'BCB (SGS)', 'tipo': 'oficial', 'papel': 'índices IPCA-E/Selic/TR pra correção e valor presente'},
            {'fonte': 'STJ Dados Abertos', 'tipo': 'oficial', 'papel': 'temas repetitivos e teses firmadas'},
            {'fonte': 'Zordon (RAG)', 'tipo': 'interna', 'papel': 'precedentes semânticos + texto dos autos'},
            {'fonte': 'Survival KM (modelo)', 'tipo': 'modelo', 'papel': 'chance 12/24m de virar precatório'},
            {'fonte': 'Classificador LR (modelo)', 'tipo': 'modelo', 'papel': 'classificação PRECATORIO/PRE_PRECATORIO/DC/NAO_LEAD'},
        ],
    }


def explicar(topico: str = 'todos') -> dict:
    """Explica os modelos do Voyager. topico ∈ {classificador, survival,
    score_oportunidade, fontes_e_pesos, todos}."""
    partes = {
        'classificador': _classificador,
        'survival': _survival,
        'score_oportunidade': _score_oportunidade,
        'fontes_e_pesos': _fontes,
    }
    if topico in partes:
        return {topico: partes[topico]()}
    if topico not in ('todos', '', None):
        return {'erro': f'tópico inválido: {topico}', 'validos': [*partes, 'todos']}
    return {k: fn() for k, fn in partes.items()}
