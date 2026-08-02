"""Tipos normalizados Jusbrasil/Digesto + classificador heurístico.

Tabela completa: https://api.jusbrasil.com.br/docs/referencias/andamento_publs.html
Cada tupla (tipo, subtipo) é um código numérico fixo. A função
`classificar_tipo_norm` mapeia tipo_comunicacao + texto da Movimentacao
para esses códigos via heurística regex/keywords. Retorna [] quando
incerto (compat spec: "lista vazia quando não disponível").
"""

import re

# Fixture completa: (tipo, subtipo, "nome")
TIPOS_NORM = [
    (0, 0, "Desconhecido"),
    (1, 1, "Agravo de Instrumento - Interposto"),
    (2, 1, "Audiência - Designada"),
    (2, 2, "Audiência - Redesignada"),
    (2, 3, "Audiência - Outros"),
    (2, 4, "Audiência - Cancelada"),
    (3, 1, "Deferida - Citação"),
    (3, 2, "Deferida - Conversão de busca e apreensão em execução"),
    (3, 3, "Deferida - Liminar recolhimento custas"),
    (3, 4, "Deferida - Liminar teor"),
    (3, 5, "Deferido - Arquivamento"),
    (3, 6, "Deferido - Bloqueio bem"),
    (3, 7, "Deferido - Pedido de penhora"),
    (3, 8, "Deferido - Suspensão 921"),
    (3, 9, "Deferido - Sobrestamento"),
    (3, 10, "Deferida - Liminar em parte"),
    (4, 0, "Manifestação contestação - Outros"),
    (4, 1, "Contatar Of.Just. mandado - Deferido"),
    (4, 2, "Contestação manifestação réu"),
    (5, 1, "Desbloqueado veículo Detran"),
    (6, 1, "Extinção - Sem Mérito I"),
    (6, 2, "Extinção - Sem Mérito II"),
    (6, 3, "Extinção - Sem Mérito III"),
    (6, 4, "Extinção - Sem Mérito VI"),
    (6, 5, "Extinção - Outros"),
    (7, 1, "Homologada a desistência - 485 VIII"),
    (7, 2, "Homologado acordo suspenso"),
    (7, 3, "Homologo acordo - 487"),
    (8, 1, "Indeferida a Liminar"),
    (9, 1, "Informa Núm.proc. e vara"),
    (11, 1, "Juntar - Minuta de acordo"),
    (11, 2, "Juntar - Planilha débito"),
    (12, 1, "Levantar depósito judicial"),
    (13, 0, "Manifestar nos autos - Outros"),
    (13, 1, "Manifestar autor - Andamento Feito"),
    (13, 2, "Manifestar autor - Certidão Oficial de Justiça"),
    (14, 0, "Manifestar provas - Outros"),
    (14, 1, "Produção de provas"),
    (15, 1, "Recolher custas - Diligência Of.Just."),
    (15, 2, "Recolher custas - Outros"),
    (16, 1, "Retirar ofícios"),
    (17, 1, "Sentença - Procedente"),
    (17, 2, "Sentença - Improcedente"),
    (17, 3, "Sentença - Parcialmente procedente"),
    (18, 1, "Transitado em julgado"),
    (19, 0, "Manifestar ofício - Outros"),
    (19, 1, "Verificar resposta ofícios"),
    (20, 1, "Iniciada - Liquidação"),
    (20, 2, "Iniciada - Execução"),
    (21, 0, "Publicação - Outros"),
    (21, 1, "Publicação realizada"),
    (21, 2, "Expedição comunicação via Sistema"),
    (22, 1, "Expedido alvará ao réu"),
    (22, 2, "Alvará e depósito"),
    (23, 0, "Disponibilizada intimação - Outros"),
    (23, 1, "Disponibilizada a Intimação"),
    (24, 1, "Expedida Notificação"),
    (25, 0, "Acórdão"),
    (25, 1, "Publicado o Acórdão"),
    (26, 1, "Conhecido o recurso - Não provido"),
    (27, 1, "Incluído em pauta"),
    (28, 1, "Revelia"),
    (29, 0, "Manifestar réu - Outros"),
    (29, 1, "Manifestar réu"),
    (30, 0, "Expedida intimação - Outros"),
    (30, 1, "Citação - Positiva"),
    (30, 2, "Citação - Intimação expedida"),
    (30, 3, "Citação - Citação expedida"),
    (31, 2, "Citação - AR negativo juntado"),
    (32, 1, "Prazo decorrido"),
    (33, 1, "Gratuidade - Deferida"),
    (33, 2, "Gratuidade - Indeferida"),
    (34, 1, "Antecipação de Tutela - Concedida"),
    (34, 2, "Antecipação de Tutela - Não concedida"),
    (35, 1, "Carta precatória - Expedição"),
    (36, 1, "Mandado - Expedição"),
    (38, 1, "Ofício requisitório"),
    (39, 1, "Contadoria"),
    (41, 1, "Decurso de prazo"),
]

# Heurísticas: lista de (regex_compilado, (tipo, subtipo)).
# Aplicadas em ordem; primeira que casa vence.
# Busca no texto normalizado (lowercase, sem acento).
_REGRAS = [
    # Intimação (tipo_comunicacao mais comum da DJEN)
    (re.compile(r'\bintimaca[ao]\b'), (23, 1)),
    (re.compile(r'\bdisponibilizada?\s+(a\s+)?intimac'), (23, 1)),
    # Citação
    (re.compile(r'\bcitaca[ao]\s+positiva\b'), (30, 1)),
    (re.compile(r'\bcitaca[ao]\s+expedida\b'), (30, 3)),
    (re.compile(r'\bcitaca[ao]\b.*\bexpedida\b'), (30, 3)),
    (re.compile(r'\bar\s+negativo\s+juntado\b'), (31, 2)),
    # Sentença
    (re.compile(r'\bsentenca\b.*\bprocedente\b'), (17, 1)),
    (re.compile(r'\bsentenca\b.*\bimprocedente\b'), (17, 2)),
    (re.compile(r'\bsentenca\b.*\bparcialmente\s+procedente\b'), (17, 3)),
    (re.compile(r'\bprocedente\b.*\bsentenca\b'), (17, 1)),
    # Acórdão
    (re.compile(r'\bacordao\b.*\bpublicado\b'), (25, 1)),
    (re.compile(r'\bpublicado?\s+(o\s+)?acordao\b'), (25, 1)),
    (re.compile(r'\bacordao\b'), (25, 0)),
    # Transitado em julgado
    (re.compile(r'\btransitado\s+em\s+julgado\b'), (18, 1)),
    # Audiência
    (re.compile(r'\baudiencia\b.*\bdesignada\b'), (2, 1)),
    (re.compile(r'\baudiencia\b.*\bredesignada\b'), (2, 2)),
    (re.compile(r'\baudiencia\b.*\bcancelada\b'), (2, 4)),
    # Despacho / Publicação
    (re.compile(r'\bpublicaca[ao]\s+realizada\b'), (21, 1)),
    (re.compile(r'\bexpedica[ao]\s+comunicaca[ao]\s+via\s+sistema\b'), (21, 2)),
    (re.compile(r'\bdespacho\b'), (21, 0)),
    # Extinção
    (re.compile(r'\bextint[ao]\b.*\bsem\s+merito\b'), (6, 5)),
    (re.compile(r'\bextinca[ao]\b'), (6, 5)),
    # Acordo
    (re.compile(r'\bhomolog[ao]\w*\s+.*\bacordo\b'), (7, 3)),
    (re.compile(r'\bconciliaca[ao]\b.*\bacordo\b'), (7, 3)),
    # Liminar
    (re.compile(r'\bliminar\b.*\bindeferida\b'), (8, 1)),
    (re.compile(r'\btutela\b.*\bconcedida\b'), (34, 1)),
    (re.compile(r'\btutela\b.*\bnao\s+concedida\b'), (34, 2)),
    (re.compile(r'\bliminar\b.*\bdeferida\b'), (3, 4)),
    # Revelia
    (re.compile(r'\brevelia\b'), (28, 1)),
    # Prazo
    (re.compile(r'\bdecurso\s+de\s+prazo\b'), (41, 1)),
    (re.compile(r'\bprazo\s+decorrido\b'), (32, 1)),
    # Ofício
    (re.compile(r'\boficio\s+requisitor[ioi]\b'), (38, 1)),
    # Alvará
    (re.compile(r'\balvara\b.*\bexpedido\b'), (22, 1)),
    (re.compile(r'\balvara\b'), (22, 2)),
    # Mandado
    (re.compile(r'\bmandado\b.*\bexpedica[ao]\b'), (36, 1)),
    # Decisão genérica (não casa nada específico)
]

_NORMALIZE_MAP = str.maketrans('àáâãäåçèéêëìíîïòóôõöùúûü', 'aaaaaaceeeeiiiiooooouuuu')


def _normalize(text: str) -> str:
    if not text:
        return ''
    return text.lower().translate(_NORMALIZE_MAP)


def classificar_tipo_norm(mov) -> list[tuple[int, int]]:
    """Classifica uma Movimentacao em tipos normalizados Jusbrasil.

    Heurística regex sobre tipo_comunicacao + texto (normalizado sem acento).
    Retorna lista de tuplas (tipo, subtipo). [] se incerto.
    """
    base = f'{mov.tipo_comunicacao or ""} {mov.texto or ""}'
    norm = _normalize(base)
    if not norm.strip():
        return []

    resultados = []
    for regex, (tipo, subtipo) in _REGRAS:
        if regex.search(norm):
            par = (tipo, subtipo)
            if par not in resultados:
                resultados.append(par)
            # Para após 3 classificações pra evitar ruído.
            if len(resultados) >= 3:
                break

    return resultados


def tipos_norm_as_json(mov) -> list[list[int]]:
    """Retorna assunto_norm no formato JSON (lista de [tipo, subtipo])."""
    return [[t, s] for t, s in classificar_tipo_norm(mov)]