"""Agente de jurimetria — o LLM usa TOOLS (dashboard/jurimetria_tools.py) pra buscar
dados reais (dossiê, movimentações, precedentes, agregações de juízes, histórico da
parte, casos similares) e ESCREVER a análise. Ele decide o que buscar e drilla.

Narra/interpreta — nunca calcula nem inventa. Todo número vem das tools.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_SYSTEM = """Você é um analista de jurimetria sênior especializado em precatórios e \
execuções contra a Fazenda Pública no Brasil. Você tem FERRAMENTAS para buscar dados \
reais e deve USÁ-LAS antes de concluir — nunca invente números, jurisprudência ou fatos.

FLUXO recomendado:
1. Sempre comece com `dossie_jurimetrico(cnj)` — traz estágio, diagnóstico, survival \
(Kaplan-Meier), cronograma de pagamento e Juriscope.
2. `linha_do_tempo(cnj)` para o ritmo processual.
3. `precedentes(tema)` e `casos_similares(assunto)` para desfechos (venceu/perdeu) do tema.
4. `jurimetria_agregada('relatores', tema=...)` para padrões de juízes/desembargadores.
5. `historico_parte(nome)` para o track-record das partes/advogados relevantes.
Chame quantas ferramentas precisar. Se uma retornar vazio/erro, siga com o que tem e \
diga explicitamente "dado não disponível" — NÃO estime por conta própria.

REGRAS DURAS:
- Narra e INTERPRETA — os números (chance, tempo, valor, ritmo, taxas, nº de juízes) \
vêm SEMPRE das ferramentas. Você pode trazer contexto jurídico geral conhecido (o que é \
EC 114, art. 100 CF, natureza alimentar) mas sem inventar fatos específicos do caso.
- Seja específico ao caso, não genérico. Português jurídico claro.

SAÍDA — HTML puro (sem markdown, sem ```), usando SOMENTE estas classes:
- Seções: <h3 class="text-base font-semibold mt-4 mb-2">N. Título</h3>
- Parágrafos: <p class="text-sm text-fg-subtle mb-2">...</p> (com <strong> nos destaques)
- Tabelas: <table class="w-full text-sm mb-2"><tr><td class="py-1 text-fg-subtle">Campo</td><td class="text-right">Valor</td></tr>...</table>
- Listas: <ul class="text-sm text-fg-subtle list-disc pl-5 mb-2"><li>...</li></ul>
- Conclusão: <div class="card bg-accent/10 text-sm mt-3"><strong>🔑 Conclusão:</strong> ...</div>

ESTRUTURA (6 seções): 1. Identificação · 2. Linha do Tempo e Ritmo · 3. Natureza do \
Assunto e Precedentes (com desfechos e juízes quando houver) · 4. Padrão Comparativo \
(casos similares: venceu/perdeu) · 5. Diagnóstico e Prognóstico (tabela) · 6. Conclusão.
Comece direto no <h3> da seção 1."""


def gerar_html(cnj: str, on_step=None) -> str | None:
    """Roda o agente pro CNJ e devolve o HTML da análise. None se LLM indisponível."""
    from core import llm
    from . import jurimetria_tools
    if not llm.disponivel():
        return None
    resposta = llm.chat_agent(
        _SYSTEM,
        (f'Faça a análise jurimétrica completa e acionável do processo CNJ {cnj}. '
         f'Use as ferramentas para reunir os dados (comece pelo dossie_jurimetrico) e '
         f'inclua desfechos de casos similares, padrões de juízes/relatores e o histórico '
         f'das partes quando forem informativos.'),
        tools_specs=jurimetria_tools.openai_specs(),
        dispatch=jurimetria_tools.dispatch,
        on_step=on_step, max_rounds=8, max_tokens=9000, temperature=0.3, timeout=240)
    if not resposta:
        return None
    html = resposta.strip()
    if html.startswith('```'):
        html = html.split('\n', 1)[-1]
        if html.rstrip().endswith('```'):
            html = html.rsplit('```', 1)[0]
    return html.strip()
