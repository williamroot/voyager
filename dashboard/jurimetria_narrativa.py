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


import json as _json
import re as _re

# O modelo SÓ PROCESSA: retorna JSON com o TEXTO de cada seção. NÓS renderizamos o HTML
# (confiável + sempre bonito). O LLM narra/interpreta, nunca calcula nem inventa.
_SYSTEM = """Você é um analista de jurimetria sênior (precatórios e execuções contra a \
Fazenda Pública no Brasil). Recebe DADOS DETERMINÍSTICOS já calculados (classificação, \
Kaplan-Meier, cronograma EC 114/2021, Juriscope, partes, movimentações, precedentes, \
padrões de juízes, casos similares) e produz uma análise jurimétrica.

RESPONDA APENAS COM UM OBJETO JSON (sem markdown, sem ```), com EXATAMENTE estas chaves \
(todas em texto corrido pt-BR, jurídico e específico ao caso — 1 a 3 frases cada):
{
 "sintese": "a conclusão principal em 1-2 frases (bottom line)",
 "identificacao": "o que é este processo: classe, órgão, partes, natureza do crédito",
 "ritmo": "análise do ritmo/tramitação e o que ele indica (se poucas movs, diga que a visão via DJEN é parcial)",
 "natureza": "natureza jurídica do tema + o que os precedentes e os padrões de juízes indicam",
 "comparativo": "casos similares (quantos do tema viram precatório / desfechos) + fluxo típico até o pagamento e fatores de duração",
 "prognostico": "diagnóstico e prognóstico: fase atual, risco de reversão, chance e tempo de virar precatório (se houver), previsão de pagamento do cronograma, valor",
 "conclusao": "conclusão acionável para o usuário (lead quente? acompanhar? ativo pronto?)"
}

REGRAS DURAS:
- INTERPRETA, nunca calcula nem inventa. Todo número (chance, tempo, valor, taxa) vem dos \
dados. Se um dado não veio, escreva "não disponível" — não estime.
- Não invente jurisprudência/leis/números de processo fora dos dados. Pode citar contexto \
jurídico geral conhecido (EC 114, art. 100 CF, natureza alimentar) sem inventar fatos do caso.
- Cite CNJs de precedentes reais quando estiverem nos dados.
- Objetivo e específico ao caso, não genérico. A RESPOSTA (o JSON) NUNCA pode vir vazia."""


_SECOES = [
    ('identificacao', '1. Identificação do Processo'),
    ('ritmo', '2. Linha do Tempo e Ritmo'),
    ('natureza', '3. Natureza do Assunto e Precedentes'),
    ('comparativo', '4. Padrão Comparativo'),
    ('prognostico', '5. Diagnóstico e Prognóstico'),
]


def _fmt(texto: str) -> str:
    """Texto puro do LLM → HTML seguro: escapa, linkifica CNJ, **negrito**, quebras."""
    from django.utils.html import escape
    t = escape((texto or '').strip())
    t = _re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', t)
    t = _linkify_cnj(t)
    t = t.replace('\n\n', '</p><p class="text-sm text-fg-soft leading-relaxed mb-2">').replace('\n', '<br>')
    return t


def _render_analise(d: dict) -> str:
    """Renderiza o JSON do LLM no NOSSO HTML (bonito + consistente)."""
    parts = []
    if d.get('sintese'):
        parts.append(f'<p class="text-base text-fg font-medium leading-snug mb-3">{_fmt(d["sintese"])}</p>')
    for chave, titulo in _SECOES:
        if d.get(chave):
            parts.append(f'<h3 class="text-sm font-semibold text-fg mt-4 mb-1">{titulo}</h3>'
                         f'<p class="text-sm text-fg-soft leading-relaxed mb-2">{_fmt(d[chave])}</p>')
    if d.get('conclusao'):
        parts.append(f'<div class="card bg-accent/10 border border-accent/20 text-sm mt-4">'
                     f'<strong class="text-accent-fg">🔑 Conclusão:</strong> '
                     f'<span class="text-fg-soft">{_fmt(d["conclusao"])}</span></div>')
    return ''.join(parts)


def _extrair_json(texto: str) -> dict | None:
    """Extrai o objeto JSON da resposta do LLM (tolera ``` e texto ao redor)."""
    if not texto:
        return None
    t = texto.strip()
    if '```' in t:
        m = _re.search(r'```(?:json)?\s*(\{.*?\})\s*```', t, _re.S)
        if m:
            t = m.group(1)
    i, j = t.find('{'), t.rfind('}')
    if i < 0 or j <= i:
        return None
    try:
        d = _json.loads(t[i:j + 1])
        return d if isinstance(d, dict) else None
    except (ValueError, TypeError):
        return None

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
    # O LLM retorna JSON (só o texto por seção); mostramos o 'pensando' (reasoning) ao
    # vivo e acumulamos o content (JSON) sem exibir cru. No fim, NÓS renderizamos o HTML.
    buf = []
    for chunk in llm.chat_stream(
            [{'role': 'system', 'content': _SYSTEM},
             {'role': 'user', 'content': _user_msg(contexto)}],
            max_tokens=13000, temperature=0.2):
        if chunk['type'] == 'reasoning':
            yield {'type': 'reasoning', 'text': chunk['text']}
        else:
            buf.append(chunk['text'])
    dados = _extrair_json(''.join(buf))
    if not dados or not _tem_conteudo(dados):
        # stream não entregou JSON útil → não-streaming (força a resposta)
        yield {'type': 'status', 'text': 'consolidando análise…'}
        dados = _analise_json(cnj, contexto)
    html = _render_analise(dados) if dados else ''
    yield {'type': 'done', 'html': html}


def _user_msg(contexto: str) -> str:
    return ('Analise o processo com estes dados e responda APENAS com o objeto JSON '
            f'especificado (chaves em pt-BR, texto corrido):\n\n{contexto}')


def _tem_conteudo(d: dict) -> bool:
    return bool(d) and sum(len(str(d.get(k) or '')) for k, _ in _SECOES) + len(str(d.get('conclusao') or '')) >= 120


def _analise_json(cnj: str, contexto: str | None = None) -> dict | None:
    """Chama o LLM (não-streaming) e devolve o dict JSON da análise. None se falhar."""
    from core import llm
    from .jurimetria_dossie import montar_dossie
    if not llm.disponivel():
        return None
    if contexto is None:
        dossie = montar_dossie(cnj)
        if dossie.get('erro') or dossie.get('processando') or not dossie.get('cabecalho'):
            return None
        proc = Process.objects.filter(numero_cnj=dossie['cnj']).first()
        ritmo = _ritmo_processual(proc) if proc else {'n': 0, 'itens': []}
        contexto = _contexto(dossie, ritmo)
    resposta = llm.chat(
        [{'role': 'system', 'content': _SYSTEM}, {'role': 'user', 'content': _user_msg(contexto)}],
        max_tokens=9000, temperature=0.2, timeout=240)
    return _extrair_json(resposta)


def gerar_html(cnj: str) -> str | None:
    """HTML da narrativa (não-streaming). None se LLM indisponível/falha."""
    dados = _analise_json(cnj)
    if not dados or not _tem_conteudo(dados):
        return None
    return _render_analise(dados)


# ---- geração assíncrona + poll (o LLM leva ~60-90s; NÃO cabe num request HTTP:
# gunicorn/nginx/cloudflare cortam. Gera numa thread do web — que tem LLM+Zordon —
# e cacheia; o front faz poll. Robusto, sem conexão longa). ----
import threading as _threading

_ERR = '__ERR__'


def iniciar_ou_obter(cnj: str) -> tuple[str | None, str]:
    """Poll da narrativa. Devolve (html, estado); estado ∈ 'pronto'|'gerando'|'erro'.
    Na 1ª chamada dispara a geração em background (thread) e cacheia o resultado."""
    from django.core.cache import cache
    key, genkey = f'narrjur:v1:{cnj}', f'narrjur:gen:{cnj}'
    val = cache.get(key)
    if val is not None:
        return (None, 'erro') if val == _ERR else (val, 'pronto')
    if cache.add(genkey, '1', timeout=200):  # só a 1ª requisição dispara a thread
        def _run():
            try:
                html = gerar_html(cnj)
                cache.set(key, html or _ERR, timeout=3600)
            except Exception:  # noqa: BLE001
                logger.exception('narrativa async falhou %s', cnj)
                cache.set(key, _ERR, timeout=300)
            finally:
                cache.delete(genkey)
        _threading.Thread(target=_run, daemon=True).start()
    return None, 'gerando'
