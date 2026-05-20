"""Metadados didáticos das features do classificador + builder de explicação.

Fonte única pra emoji, label curto, descrição (advogado-friendly), família e
peso de cada feature do v6/v7. Consumido por:

  - `/dashboard/processos/<pk>/` (card "Por que essa classificação")
  - `/dashboard/leads/algoritmo/` (página explicativa + sandbox CNJ)
  - testes e management commands

Mantém metadados separados dos pesos: pesos vivos em `ClassificadorVersao`
(hot reload), metadados aqui (mudam com deploy).
"""
from __future__ import annotations

from typing import Optional

# === Famílias agrupam features pra leitura humana =============================
FAMILIA_ESTRUTURAL = 'estrutural'
FAMILIA_TEXTO = 'texto'
FAMILIA_VOLUME = 'volume'
FAMILIA_RECENCIA = 'recencia'
FAMILIA_INTERACAO = 'interacao'
FAMILIA_V7 = 'v7'

FAMILIAS = [
    (FAMILIA_ESTRUTURAL, 'scale',       'Classe e tipo de movimentação',
     'Sinais estruturados que vêm direto dos campos do CNJ/Datajud — sem precisar ler texto.'),
    (FAMILIA_TEXTO,      'scroll-text', 'O que diz o texto das movimentações',
     'Robô procura palavras-chave nos textos públicos das movimentações (DJEN/Datajud).'),
    (FAMILIA_VOLUME,     'trending-up', 'Tamanho e idade do processo',
     'Quantas movimentações, quantos tipos, há quanto tempo está rolando.'),
    (FAMILIA_RECENCIA,   'history',     'Tempo da última mov e número de partes',
     'Processos antigos com pouca atividade ou ações coletivas tendem a não ser leads.'),
    (FAMILIA_INTERACAO,  'link-2',      'Combos entre sinais',
     'O robô também olha pares de sinais juntos — alguns combos valem mais que a soma das partes.'),
    (FAMILIA_V7,         'flask',       'Novos sinais do v7 (em testes)',
     'Cinco sinais adicionais que o v7 aprendeu a usar. Hoje não influenciam o score em produção.'),
]

# === Catálogo human-friendly por feature ======================================
# Cada entrada: emoji, label curto (chip), desc completa (parágrafo),
# familia (uma das constantes), regex_ou_criterio (técnico, pra <details>).
FEATURE_META: dict = {
    # --- Estruturais ---------------------------------------------------------
    'F1_cumprim': {
        'emoji': 'scale', 'familia': FAMILIA_ESTRUTURAL,
        'label': 'É Cumprimento de Sentença contra a Fazenda?',
        'desc': (
            'Olha a classe processual do CNJ. Se for uma das variantes de '
            '"Cumprimento de Sentença contra a Fazenda Pública" — códigos CNJ '
            '12078, 156, 15160, 15215, 12079 — esse é o caminho processual onde '
            'precatórios nascem. É o sinal positivo mais forte do modelo.'
        ),
        'criterio': 'classe_codigo IN {12078, 156, 15160, 15215, 12079}',
    },
    'F10_juizado_ANTI': {
        'emoji': 'ban', 'familia': FAMILIA_ESTRUTURAL,
        'label': 'É Juizado Especial ou Recurso Inominado?',
        'desc': (
            'Juizado Especial, Recurso Inominado e Procedimento Comum quase '
            'nunca terminam em precatório (a competência e o rito são outros). '
            'Quando o robô vê isso na classe, é sinal forte de que NÃO é lead.'
        ),
        'criterio': 'classe_nome contém "juizado especial" OU "recurso inominado" OU "procedimento comum"',
    },
    'F2_precat_tc': {
        'emoji': 'scroll-text', 'familia': FAMILIA_ESTRUTURAL,
        'label': 'Tem movimentação tipo "Expedição de precatório/RPV"?',
        'desc': (
            'Algumas movimentações vêm com um campo estruturado "tipo de '
            'comunicação". Se aparecer "Expedição de precatório/rpv" ou '
            '"Precatório", é evidência direta — sem precisar olhar texto.'
        ),
        'criterio': 'mov.tipo_comunicacao IN {"Expedição de precatório/rpv", "Precatório"}',
    },
    'F7_envTrib_tc': {
        'emoji': 'send', 'familia': FAMILIA_ESTRUTURAL,
        'label': 'Foi enviado ao Tribunal?',
        'desc': (
            'Movimentação tipo "Enviada ao Tribunal" ou "Preparada para Envio" '
            '— geralmente o ofício/precatório indo pro TRF. Sinal moderado.'
        ),
        'criterio': 'mov.tipo_comunicacao IN {"Enviada ao Tribunal", "Preparada para Envio"}',
    },
    # --- Texto ---------------------------------------------------------------
    'F11_precat_text': {
        'emoji': 'search', 'familia': FAMILIA_TEXTO,
        'label': 'Alguma mov menciona "precatório"?',
        'desc': (
            'Procura a palavra "precatório" no texto público das movimentações. '
            'Pega sinais que não estão nos campos estruturados — por exemplo, '
            'despachos do juiz citando o instituto.'
        ),
        'criterio': 'regex "precat[óo]rio" em mov.texto',
    },
    'F12_rpv_text': {
        'emoji': 'search', 'familia': FAMILIA_TEXTO,
        'label': 'Alguma mov menciona "RPV"?',
        'desc': (
            'Procura "RPV" (Requisição de Pequeno Valor) como palavra isolada '
            'no texto. Sinal moderado — RPVs interessam ao Juriscope tanto quanto precatórios.'
        ),
        'criterio': 'regex "\\mrpv\\M" em mov.texto (palavra inteira)',
    },
    'F13_reqPag_text': {
        'emoji': 'anomaly', 'familia': FAMILIA_TEXTO,
        'label': '"Requisição de pagamento" no texto (sinal contra)',
        'desc': (
            'Aparece muito em processos que NÃO são leads (ex: requerimentos '
            'administrativos comuns). O modelo aprendeu peso negativo no treino.'
        ),
        'criterio': 'regex "requisi[çc][ãa]o de pagamento" em mov.texto',
    },
    'F14_oficio_text': {
        'emoji': 'anomaly', 'familia': FAMILIA_TEXTO,
        'label': '"Ofício requisitório" no texto (sinal contra)',
        'desc': (
            'Apareceu mais em não-leads do que em leads no treino. Quando '
            'aparece sozinho (sem outros sinais), empurra o score pra baixo.'
        ),
        'criterio': 'regex "of[íi]cio requisit[óo]rio" em mov.texto',
    },
    # --- Volume / Cohort -----------------------------------------------------
    'F15_logMovs': {
        'emoji': 'trending-up', 'familia': FAMILIA_VOLUME,
        'label': 'Volume de movimentações',
        'desc': (
            'Quantas movimentações o processo já acumulou (em escala '
            'logarítmica). Leads tipicamente têm histórico longo — quanto '
            'mais movs, mais provável estar maduro. Esse é o sinal numérico '
            'mais forte de todos.'
        ),
        'criterio': 'log(1 + total_movs) / log(500)',
    },
    'F16_logTipos': {
        'emoji': 'tornado', 'familia': FAMILIA_VOLUME,
        'label': 'Variedade de tipos de movimentação (sinal contra)',
        'desc': (
            'Processos "diversificados" — com muitos tipos diferentes de mov — '
            'tendem a ser processos de conhecimento ativos, não cumprimento '
            'focado. O modelo aprendeu a usar isso como sinal de "não é lead".'
        ),
        'criterio': 'log(1 + tipos_distintos) / log(50)',
    },
    'F17_logN1count': {
        'emoji': 'target', 'familia': FAMILIA_VOLUME,
        'label': 'Quantas vezes "precatório/RPV/pagamento/ofício" aparecem',
        'desc': (
            'Soma das ocorrências das 4 palavras-chave de texto, em log. Não é '
            'só "se" mas "quantas vezes" — muitas menções ao longo do processo '
            'são sinal forte.'
        ),
        'criterio': 'log(1 + F11_n + F12_n + F13_n + F14_n) / log(20)',
    },
    'F18_anoZ': {
        'emoji': 'calendar', 'familia': FAMILIA_VOLUME,
        'label': 'Ano do processo (mais recente vale mais)',
        'desc': (
            'Ano de autuação do CNJ, normalizado pela média do treino. O '
            'modelo aprendeu leve preferência por processos mais recentes — '
            'provavelmente porque a cobertura de dados é melhor neles.'
        ),
        'criterio': '(ano_CNJ − 2019.66) / 6.49',
    },
    # --- Recência / Partes ---------------------------------------------------
    'F19_cancelado_ANTI': {
        'emoji': 'circle-x', 'familia': FAMILIA_RECENCIA,
        'label': 'Cancelamento/revogação de precatório (sinal contra)',
        'desc': (
            'Captura cancelamento ou revogação de precatório/RPV. Quando '
            'aparece, anula a hipótese de lead. Na prática, peso treinado é '
            'próximo de zero porque o termo é raro nas movs públicas — vive '
            'dentro dos autos completos.'
        ),
        'criterio': 'regex "cancelamento/revogação de precatório|rpv" em mov.texto',
    },
    'F20_exp_juriscope': {
        'emoji': 'circle-check', 'familia': FAMILIA_RECENCIA,
        'label': 'Termos exatos que o Juriscope confirma',
        'desc': (
            'Procura os termos exatos que o filtro do Juriscope usa pra '
            'confirmar expedição ("precatório expedido", "rpv expedida", etc). '
            'Raro nas movs DJEN/Datajud — esses termos costumam viver nos '
            'autos completos.'
        ),
        'criterio': 'regex de 8 termos exatos do filtro Juriscope',
    },
    'F21_diasUltMovZ': {
        'emoji': 'history', 'familia': FAMILIA_RECENCIA,
        'label': 'Há quanto tempo foi a última movimentação',
        'desc': (
            'Quantos dias atrás foi a última mov, normalizado. Leads tendem a '
            'ter histórico longo, então mov "antiga" (já completou o ciclo) '
            'ainda contribui positivo. Processos parados há pouco tempo '
            'também não são sinal contra.'
        ),
        'criterio': '(dias_desde_ultima_mov − 532.24) / 574.57',
    },
    'F23_logPartes': {
        'emoji': 'users', 'familia': FAMILIA_RECENCIA,
        'label': 'Número de partes (sinal contra ações coletivas)',
        'desc': (
            'Processos com muitas partes geralmente são ações coletivas — '
            'precatório individual raramente nasce deles. Peso negativo.'
        ),
        'criterio': 'log(1 + total_partes) / log(50)',
    },
    # --- Interações (combos) -------------------------------------------------
    'F1xF11': {
        'emoji': 'link-2', 'familia': FAMILIA_INTERACAO,
        'label': 'Combo: Cumprimento × "precatório" no texto',
        'desc': (
            'Combo: classe Cumprimento E menção a "precatório" no texto. O '
            'modelo aprendeu a desconto leve aqui pra não contar duas vezes '
            'o mesmo sinal (a palavra "precatório" é meio que esperada nesse '
            'tipo de classe).'
        ),
        'criterio': 'F1 × F11',
    },
    'F1xF15': {
        'emoji': 'link-2', 'familia': FAMILIA_INTERACAO,
        'label': 'Combo: Cumprimento × muitas movimentações',
        'desc': (
            'Combo MUITO forte: classe Cumprimento contra Fazenda E o processo '
            'já andou bastante. Esse é o perfil clássico do lead "maduro" — '
            'quem está prestes a virar precatório. Peso de combo +1.63 no v6.'
        ),
        'criterio': 'F1 × F15',
    },
    'F1xF20': {
        'emoji': 'link-2', 'familia': FAMILIA_INTERACAO,
        'label': 'Combo: Cumprimento × termos Juriscope',
        'desc': (
            'Combo: classe Cumprimento E menção aos termos exatos do filtro '
            'Juriscope. Ajuste fino — peso quase nulo na prática porque F20 '
            'é raro nas movs públicas.'
        ),
        'criterio': 'F1 × F20',
    },
    # --- v7 novas (F24-F28) — só usadas quando v7 vira ativa -----------------
    'F24_rpv_expedida_text': {
        'emoji': 'sparkles', 'familia': FAMILIA_V7,
        'label': '[v7] "RPV expedida" no texto',
        'desc': (
            'Novo no v7: regex específica pra "RPV expedida" como evento '
            '(diferente de só citar "RPV"). Sinal muito mais forte que F12.'
        ),
        'criterio': 'regex "\\bRPV\\s+expedid"',
    },
    'F25_pagamento_administrativo': {
        'emoji': 'sparkles', 'familia': FAMILIA_V7,
        'label': '[v7] Pagamento administrativo',
        'desc': (
            'Novo no v7: "pagamento administrativo" no texto. Sinal contra '
            'precatório (foi pago fora da fila do precatório).'
        ),
        'criterio': 'regex "pagamento\\s+administrativo"',
    },
    'F26_inscricao_ordem': {
        'emoji': 'sparkles', 'familia': FAMILIA_V7,
        'label': '[v7] Inscrição em ordem cronológica',
        'desc': (
            'Novo no v7: menção à inscrição na ordem cronológica de '
            'pagamento. Sinal forte — é etapa formal do precatório.'
        ),
        'criterio': 'regex "inscri[çc][ãa]o\\s+(?:na\\s+)?ordem\\s+cronol[óo]gica"',
    },
    'F27_transitado_julgado': {
        'emoji': 'sparkles', 'familia': FAMILIA_V7,
        'label': '[v7] Trânsito em julgado',
        'desc': (
            'Novo no v7: menção a "transitado em julgado" no texto. '
            'Pré-requisito formal pra cumprimento contra Fazenda virar precatório.'
        ),
        'criterio': 'regex "transitad[oa]\\s+em\\s+julgado"',
    },
    'F28_liquido_certo': {
        'emoji': 'sparkles', 'familia': FAMILIA_V7,
        'label': '[v7] Crédito líquido e certo',
        'desc': (
            'Novo no v7: menção a "líquido e certo" no texto. Linguagem '
            'típica de cálculos homologados → cumprimento iminente.'
        ),
        'criterio': 'regex "l[íi]quid[oa]\\s+(?:e\\s+)?cert[oa]"',
    },
}


def features_por_familia() -> list[tuple[str, str, str, str, list[str]]]:
    """Retorna [(familia_key, icone_alias, titulo, descricao, [feature_names])] na ordem de FAMILIAS."""
    grouped: dict[str, list[str]] = {f[0]: [] for f in FAMILIAS}
    for fname, meta in FEATURE_META.items():
        grouped[meta['familia']].append(fname)
    return [
        (key, icone, titulo, desc, grouped[key])
        for key, icone, titulo, desc in FAMILIAS
    ]


def construir_contribuicoes(
    features: dict,
    pesos_v6: dict,
    pesos_v7: Optional[dict] = None,
    top_n: int = 12,
) -> list[dict]:
    """Constrói lista ordenada de contribuições (peso × valor) pra exibição.

    Cada item: {feature, emoji, label, desc, peso, peso_v7, valor, contribuicao, familia}.
    Ordenada por |contribuicao| desc. Inclui só features com contribuição > 0.001
    (corta zeros pra não poluir).
    """
    contribs = []
    for fname, val in features.items():
        peso = pesos_v6.get(fname, 0.0)
        contrib = peso * val
        if abs(contrib) <= 0.001:
            continue
        meta = FEATURE_META.get(fname, {
            'emoji': '•', 'label': fname, 'desc': '',
            'familia': FAMILIA_ESTRUTURAL, 'criterio': '',
        })
        item = {
            'feature': fname,
            'emoji': meta['emoji'],
            'label': meta['label'],
            'desc': meta['desc'],
            'criterio': meta.get('criterio', ''),
            'familia': meta['familia'],
            'peso': round(peso, 3),
            'valor': round(val, 3),
            'contribuicao': round(contrib, 3),
        }
        if pesos_v7 is not None:
            item['peso_v7'] = round(pesos_v7.get(fname, 0.0), 3)
        contribs.append(item)
    contribs.sort(key=lambda x: -abs(x['contribuicao']))
    return contribs[:top_n]


def resumir_decisao(categoria: str, score: float, thresholds: dict) -> str:
    """Frase advogado-friendly explicando POR QUE a categoria foi escolhida."""
    t = thresholds
    if categoria == 'PRECATORIO':
        return (
            f'Confiança {score:.2f} ≥ {t["precatorio"]:.2f} E o robô viu '
            'menção explícita a precatório/RPV nas movimentações '
            '→ classificado como PRECATÓRIO (fila imediata).'
        )
    if categoria == 'PRE_PRECATORIO':
        return (
            f'Confiança {score:.2f} ≥ {t["pre"]:.2f} E a classe é Cumprimento '
            'contra Fazenda, mas o robô NÃO viu a palavra precatório/RPV ainda '
            '→ classificado como PRÉ-PRECATÓRIO (re-checar mensalmente).'
        )
    if categoria == 'DIREITO_CREDITORIO':
        return (
            f'Confiança {score:.2f} ≥ {t["direito"]:.2f} E há indícios de '
            'Cumprimento, mas o sinal ainda é fraco → classificado como '
            'DIREITO CREDITÓRIO (watch-list trimestral).'
        )
    return (
        f'Confiança {score:.2f} abaixo do mínimo OU a classe processual '
        'não bate com cumprimento contra Fazenda → NÃO É LEAD.'
    )


def explicar_processo(processo, *, top_n: int = 12) -> dict:
    """Builder all-in-one: roda compute_features + classificar + contribuicoes.

    Retorna dict pronto pro template `_algoritmo_explicacao.html`. Levanta
    `Process.DoesNotExist` se processo for None; se não houver pesos vivos,
    cai pro HARDCODED_WEIGHTS (mesmo comportamento de `classificar`).
    """
    from .classificador import (
        HARDCODED_WEIGHTS,
        THRESHOLD_DIREITO_CREDITORIO,
        THRESHOLD_PRE_PRECATORIO,
        THRESHOLD_PRECATORIO,
        _categorizar,
        _current_weights,
        compute_features,
        get_versao_ativa,
        predict_score,
    )
    from .models import ClassificadorVersao

    pesos_v6 = dict(_current_weights() or HARDCODED_WEIGHTS)
    pesos_v7 = None
    try:
        v7 = (
            ClassificadorVersao.objects.filter(versao='v7')
            .only('pesos').first()
        )
        if v7 and isinstance(v7.pesos, dict):
            pesos_v7 = v7.pesos
    except Exception:
        pesos_v7 = None

    features = compute_features(processo)
    score = predict_score(features, pesos=pesos_v6)
    categoria = _categorizar(
        score, features,
        tribunal_id=getattr(processo, 'tribunal_id', None),
        versao_modelo='v6',
    )

    thresholds = {
        'precatorio': THRESHOLD_PRECATORIO,
        'pre': THRESHOLD_PRE_PRECATORIO,
        'direito': THRESHOLD_DIREITO_CREDITORIO,
    }
    contribuicoes = construir_contribuicoes(
        features, pesos_v6=pesos_v6, pesos_v7=pesos_v7, top_n=top_n,
    )

    return {
        'processo': processo,
        'features': features,
        'score': round(score, 3),
        'categoria': categoria,
        'versao': get_versao_ativa(),
        'thresholds': thresholds,
        'resumo': resumir_decisao(categoria, score, thresholds),
        'contribuicoes': contribuicoes,
    }
