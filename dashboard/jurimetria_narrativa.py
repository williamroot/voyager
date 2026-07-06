"""Narrativa de jurimetria por IA — o LLM (Ollama) LÊ os dados determinísticos do
dossiê (diagnóstico, survival, cronograma, Juriscope, partes, movs, precedentes) e
ESCREVE uma análise estruturada em 6 seções (estilo Maritaca, turbinado com nossos
dados). O LLM narra/traduz — NUNCA calcula: todos os números vêm prontos daqui.

Assíncrono: chamado por um endpoint HTMX; o dossiê determinístico renderiza na hora,
a narrativa carrega depois. Fail-closed: sem LLM, o card some.
"""
from __future__ import annotations

import logging

from tribunals.models import Movimentacao, Process

logger = logging.getLogger(__name__)


def _ritmo_processual(proc: Process) -> dict:
    """Timeline + ritmo determinístico das movimentações (DJEN) — pra o LLM narrar
    sem inventar datas/contas."""
    movs = list(Movimentacao.objects.filter(processo_id=proc.pk, ativo=True)
                .order_by('data_disponibilizacao')
                .values_list('data_disponibilizacao', 'tipo_documento', 'nome_orgao', 'texto')[:60])
    if not movs:
        return {'n': 0, 'itens': []}
    primeira, ultima = movs[0][0], movs[-1][0]
    dias = (ultima - primeira).days or 1
    itens = [{'data': d.strftime('%d/%m/%Y'),
              'tipo': (tipo or '').strip()[:40],
              'orgao': (org or '').strip()[:50],
              'resumo': ' '.join((txt or '').split())[:120]} for d, tipo, org, txt in movs]
    return {
        'n': len(movs),
        'primeira': primeira.strftime('%d/%m/%Y'),
        'ultima': ultima.strftime('%d/%m/%Y'),
        'dias_span': dias,
        'ritmo_dias': round(dias / len(movs), 1),
        'itens': itens,
    }


def _contexto(dossie: dict, ritmo: dict) -> str:
    """Serializa os dados determinísticos num bloco de texto pro LLM narrar."""
    import json
    c = dossie.get('cabecalho', {})
    dg = dossie.get('diagnostico', {})
    pc = dossie.get('precatorio', {})
    js = pc.get('juriscope') or {}
    tipo = dossie.get('jurimetria_tipo', {})
    polos = dossie.get('polos', {})
    prec = dossie.get('precedentes', {})

    def _partes(lst):
        return [f"{p.get('nome')} ({p.get('papel')})" for p in (lst or [])][:20]

    dados = {
        'cnj': dossie.get('cnj'),
        'identificacao': {
            'tribunal': c.get('tribunal'), 'classe': c.get('classe_nome'),
            'assunto': c.get('assunto_nome'), 'orgao_julgador': c.get('orgao_julgador'),
            'data_autuacao': str(c.get('data_autuacao') or ''),
            'enriquecimento': c.get('enriquecimento_status'),
            'total_movimentacoes': c.get('total_movimentacoes'),
        },
        'diagnostico_deterministico': {
            'estagio': dg.get('estagio'), 'veredito': dg.get('veredito'),
            'recomendacao': (dg.get('recomendacao') or {}).get('label'),
            'sinais': dg.get('sinais'),
            'indicadores': dg.get('indicadores'),
            'chance_survival': dg.get('chance'),  # KM: chance_12m/24m, tempo_mediano_meses, estrato, n, eventos
        },
        'precatorio': {
            'classificacao': pc.get('classificacao'), 'valor_causa': str(pc.get('valor_causa') or ''),
            'tem_sinal_expedicao': pc.get('tem_sinal_expedicao'),
            'homologacao': pc.get('homologacao'), 'pagamento_cronograma': pc.get('pagamento'),
            'juriscope': {k: js.get(k) for k in ('natureza', 'ente_nome', 'devedora', 'valor_acao',
                          'valor_acao_corrigido', 'ordem_orcamentaria', 'ano_ordem_orcamentaria',
                          'data_oficio', 'files_downloaded')} if js else {},
        },
        'jurimetria_do_tipo': {k: tipo.get(k) for k in ('disponivel', 'taxa_precatorio', 'total',
                               'precatorio', 'pre_precatorio', 'classe_nome')} if tipo.get('disponivel') else {},
        'partes': {'ativo': _partes(polos.get('ativo')), 'passivo': _partes(polos.get('passivo')),
                   'outros': _partes(polos.get('outros'))},
        'ritmo_processual': ritmo,
        'precedentes_zordon': [{'cnj': it.get('numero_cnj'), 'tipo': it.get('doc_tipo'),
                                'trecho': (it.get('snippet') or '')[:200]}
                               for it in (prec.get('itens') or [])][:6],
    }
    # Enriquecimento via tools (pré-coleta determinística): casos similares
    # (venceu/perdeu), padrões de juízes/relatores e histórico da parte principal.
    try:
        from . import jurimetria_tools as jt
        assunto = c.get('assunto_nome') or ''
        trib = c.get('tribunal') or ''
        if assunto and assunto != '—':
            dados['casos_similares'] = jt.casos_similares(assunto, trib, limit=8)
            dados['padroes_juizes'] = jt.jurimetria_agregada('relatores', tema=assunto, tribunal=trib)
    except Exception as exc:  # noqa: BLE001
        logger.warning('narrativa: enriquecimento por tools falhou: %s', exc)
    return json.dumps(dados, ensure_ascii=False, indent=1, default=str)


_SYSTEM = """Você é um analista de jurimetria sênior especializado em precatórios e \
execuções contra a Fazenda Pública no Brasil. Recebe DADOS DETERMINÍSTICOS já \
calculados (classificação, modelo de sobrevivência Kaplan-Meier, cronograma \
constitucional EC 114/2021, dados do Juriscope, partes, movimentações, precedentes) e \
escreve uma ANÁLISE JURIMÉTRICA estruturada e acionável.

REGRAS DURAS:
- Você NARRA e INTERPRETA — NUNCA calcula nem inventa números. Todo número (chance, \
tempo, valor, ritmo, taxa) vem dos dados fornecidos. Se um dado não veio, diga \
"não disponível" — não estime por conta própria.
- Não invente jurisprudência, leis ou números de processo que não estejam nos dados. \
Você PODE trazer contexto jurídico geral conhecido (ex.: o que é a EC 114, art. 100 CF, \
natureza alimentar) mas SEM inventar fatos específicos do caso.
- Português jurídico claro e objetivo. Seja específico ao caso, não genérico.

FORMATO DE SAÍDA — HTML puro (sem markdown, sem ```), usando SOMENTE estas classes:
- Seções: <h3 class="text-base font-semibold mt-4 mb-2">N. Título</h3>
- Parágrafos: <p class="text-sm text-fg-subtle mb-2">...</p> (use <strong> pra destacar)
- Tabelas: <table class="w-full text-sm mb-2"><tr><td class="py-1 text-fg-subtle">Campo</td><td class="text-right">Valor</td></tr>...</table>
- Listas: <ul class="text-sm text-fg-subtle list-disc pl-5 mb-2"><li>...</li></ul>
- Destaque de conclusão: <div class="card bg-accent/10 text-sm mt-3"><strong>🔑 Conclusão:</strong> ...</div>

ESTRUTURA (6 seções, nesta ordem):
1. Identificação do Processo (tabela)
2. Linha do Tempo e Ritmo Processual (narrativa + ritmo; se poucas movs, diga que a \
visão via DJEN é parcial)
3. Natureza do Assunto e Precedentes (contexto jurídico do tema + o que os precedentes \
e os padrões de juízes/relatores em 'padroes_juizes' indicam)
4. Padrão Comparativo (fluxo típico até o pagamento + fatores de duração + desfechos de \
'casos_similares' — quantos do mesmo tema viraram precatório no acervo e os precedentes)
5. Diagnóstico e Prognóstico (tabela: fase atual, mérito, risco de reversão, chance/tempo \
de virar precatório se aplicável, previsão de pagamento do cronograma, valor)
6. Conclusão (bloco destacado)

Não repita o CNJ no título de cada seção. Comece direto no <h3> da seção 1."""


import re as _re

# CNJ NNNNNNN-DD.AAAA.J.TR.OOOO — pra linkificar citações na narrativa.
_CNJ_PAT = _re.compile(r'(?<![\w>./=-])(\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4})(?![\w<])')


def _linkify_cnj(html: str) -> str:
    """Torna todo CNJ citado na narrativa clicável → abre o dossiê daquele processo
    (que por sua vez leva ao original na fonte). Não mexe em CNJs já dentro de <a>."""
    def _sub(m):
        cnj = m.group(1)
        return (f'<a href="?cnj={cnj}" class="text-accent hover:underline font-mono" '
                f'title="Abrir dossiê de {cnj}">{cnj}</a>')
    # evita reprocessar dentro de tags <a ...>...</a>: split grosseiro por <a>
    partes = _re.split(r'(<a\b[^>]*>.*?</a>)', html, flags=_re.S)
    return ''.join(p if p.startswith('<a') else _CNJ_PAT.sub(_sub, p) for p in partes)


def _sanitiza(html: str) -> str:
    """Defesa contra XSS-via-LLM: remove <script>/<style>/<iframe>, handlers on*=,
    e URLs javascript:. O modelo é nosso, mas HTML cru vai pro innerHTML — não confiar."""
    html = _re.sub(r'(?is)<\s*(script|style|iframe|object|embed|link|meta)\b.*?(</\s*\1\s*>|$)', '', html)
    html = _re.sub(r'(?i)\son\w+\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)', '', html)
    html = _re.sub(r'(?i)(href|src)\s*=\s*(["\']?)\s*javascript:[^"\'>]*\2', r'\1=\2#\2', html)
    return html


def _limpa_fences(html: str) -> str:
    html = (html or '').strip()
    if html.startswith('```'):
        html = html.split('\n', 1)[-1]
        if html.rstrip().endswith('```'):
            html = html.rsplit('```', 1)[0]
    return _linkify_cnj(_sanitiza(html.strip()))


def gerar_stream(cnj: str):
    """Gera eventos {type,...} pro SSE: status | reasoning | content | done | error.
    Pré-coleta determinística + stream da síntese LLM (mostra o 'pensando')."""
    from core import llm
    from .jurimetria_dossie import montar_dossie
    if not llm.disponivel():
        yield {'type': 'error', 'text': 'Análise por IA indisponível (LLM não configurado).'}
        return
    yield {'type': 'status', 'text': 'Coletando dados do processo…'}
    dossie = montar_dossie(cnj)
    if dossie.get('erro') or dossie.get('processando') or not dossie.get('cabecalho'):
        yield {'type': 'error', 'text': 'Processo ainda sem dados suficientes para a análise.'}
        return
    yield {'type': 'status', 'text': 'Buscando precedentes, juízes e casos similares…'}
    proc = Process.objects.filter(numero_cnj=dossie['cnj']).first()
    ritmo = _ritmo_processual(proc) if proc else {'n': 0, 'itens': []}
    contexto = _contexto(dossie, ritmo)
    yield {'type': 'status', 'text': 'Gerando análise jurimétrica…'}
    buf = []
    for chunk in llm.chat_stream(
            [{'role': 'system', 'content': _SYSTEM},
             {'role': 'user', 'content': f'Faça a análise jurimétrica com estes dados:\n\n{contexto}'}],
            max_tokens=9000, temperature=0.3):
        if chunk['type'] == 'reasoning':
            yield {'type': 'reasoning', 'text': chunk['text']}
        else:
            buf.append(chunk['text'])
            yield {'type': 'content', 'text': chunk['text']}
    html = _limpa_fences(''.join(buf))
    yield {'type': 'done', 'html': html}


def gerar_html(cnj: str) -> str | None:
    """Gera o HTML da narrativa pro CNJ. None se LLM indisponível ou dossiê inválido."""
    from core import llm
    from .jurimetria_dossie import montar_dossie
    if not llm.disponivel():
        return None
    dossie = montar_dossie(cnj)
    if dossie.get('erro') or dossie.get('processando') or not dossie.get('cabecalho'):
        return None
    proc = Process.objects.filter(numero_cnj=dossie['cnj']).first()
    ritmo = _ritmo_processual(proc) if proc else {'n': 0, 'itens': []}
    contexto = _contexto(dossie, ritmo)
    resposta = llm.chat(
        [{'role': 'system', 'content': _SYSTEM},
         {'role': 'user', 'content': f'Faça a análise jurimétrica com estes dados:\n\n{contexto}'}],
        max_tokens=9000, temperature=0.3, timeout=240)
    if not resposta:
        return None
    return _limpa_fences(resposta)
