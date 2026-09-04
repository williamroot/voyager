"""Dossiê de jurimetria de um magistrado — coleta, análise e PDF.

## O que este dossiê é, e o que ele recusa ser

É uma descrição de **padrão de atuação**: em que órgão o magistrado atua, que
classes julga, em que volume, e quais atos processuais aparecem no texto das
publicações. Serve para o advogado saber **o que esperar do rito**.

**Não é nota, ranking nem score de juiz**, e o desenho não pode sugerir isso.
Também **não é taxa de mérito**: 133 de 141 publicações medidas no caso que
originou este módulo são `Intimação`, que é a comunicação do ato e não o
inteiro teor. Dizer "taxa de condenação de X%" a partir de intimação seria
dado pela metade — que nesta casa vale menos que zero (princípio nº 1).

## As três decisões de método, e por que cada uma

**1. A unidade é o PROCESSO, não a publicação.** Um caso com 4 intimações
contaria 4× numa contagem por documento e distorceria qualquer frequência.
Medido no caso de origem: 141 publicações para 132 processos.

**2. A identidade é (tribunal, órgão, nome) — NUNCA o nome.** Medido em
03/09/2026: das 195 publicações que casam com `Rafaela Caldeira Gonçalves`,
**56 são de outros tribunais** (TJCE 18, TJRO 12, TJPE 4, TJPI 4, TJMA 4…) e
são **homônimos**. Uma ficha por nome misturaria quatro pessoas e pareceria
correta. Por isso `coletar()` exige `tribunal` e devolve a distribuição por
órgão para quem lê julgar o recorte.

**3. Os marcadores são VERBATIM, e coocorrem de propósito.** `condenacao` e
`absolvicao` podem aparecer no MESMO julgado — absolvição parcial é rotina
("condeno pelo art. X … por outro lado, ABSOLVO da imputação do art. Y").
Somar ou subtrair as duas frequências inventaria uma estatística. É por isso
que a análise publica a **tabela 2×2 inteira** de cada par, e não só o phi.

## O que a régua não cobre, dito aqui e na tela

O marcador diz que o TERMO aparece, não que o ato foi praticado naquele
processo — uma decisão pode citar "prisão preventiva" para negá-la. Para
distinguir seria preciso ler o dispositivo, que exige o inteiro teor.
"""
from __future__ import annotations

import datetime as _dt
import html as _html
import logging
import math
import re

logger = logging.getLogger('voyager.dashboard.dossie_magistrado')

#: Atos processuais procurados no texto. A chave é o rótulo técnico; o valor é
#: o regex VERBATIM. Nada aqui infere: se o termo não está escrito, não conta.
MARCADORES: dict[str, str] = {
    'condenacao':    r'\b(CONDENO|Condeno|condena[çc][ãa]o)\b',
    'absolvicao':    r'\b(ABSOLVO|Absolvo|absolvi[çc][ãa]o)\b',
    'protetiva':     r'medida[s]? protetiva',
    'extincao':      r'extin[çc][ãa]o da punibilidade',
    'preventiva':    r'pris[ãa]o preventiva',
    'flagrante':     r'flagrante',
    'arquivamento':  r'arquivamento|[Aa]rquive',
    'indeferimento': r'\b(INDEFIRO|Indefiro|indefiro)\b',
    'deferimento':   r'\b(DEFIRO|Defiro|defiro|CONCEDO|Concedo)\b',
}

ROTULO = {
    'condenacao': 'condenação', 'absolvicao': 'absolvição',
    'protetiva': 'medida protetiva', 'extincao': 'extinção da punibilidade',
    'preventiva': 'prisão preventiva', 'flagrante': 'flagrante',
    'arquivamento': 'arquivamento', 'indeferimento': 'indeferimento',
    'deferimento': 'deferimento',
}

#: Abaixo disto, a célula da 2×2 não sustenta inferência — o phi sai marcado
#: como não confiável em vez de sair bonito. É o mesmo motivo pelo qual um
#: gate desta casa exige n mínimo: amostra pequena EXAGERA, não atenua.
CELULA_MINIMA = 5

#: Teto de publicações trazidas do índice. Não é corte mudo: quando bate, o
#: dossiê declara que bateu (regra nº 2).
TETO_PUBLICACOES = 500


# ─────────────────────────────────────────────────────────────────────────────
# coleta
# ─────────────────────────────────────────────────────────────────────────────

def coletar(nome: str, tribunal: str, teto: int = TETO_PUBLICACOES) -> dict:
    """Publicações do magistrado no tribunal, agregadas POR PROCESSO.

    ⚠️ O campo de data no índice é `publish_date`. Pedir `data` devolve `None`
    em 100% dos documentos, **sem erro** — medido em 195 de 195 no caso de
    origem, e um gráfico por ano teria saído vazio parecendo ausência de dado.
    """
    from search.client import get_es, index_name

    # 240 s era teto de nada: o Cloudflare à frente corta em ~100 s, então quem
    # esperava via a página DELE, não o nosso aviso — e a thread do gunicorn
    # ficava presa 4 min depois de o leitor já ter ido embora. 75 s garante que
    # a resposta é sempre NOSSA. Medido em 04/09/2026: um `match_phrase` real
    # sobre `voyager-movimentacoes` levou 44 s com o índice ocupado pelos
    # backfills, então o teto tem folga sem virar espera infinita (regra nº 7).
    es = get_es().options(request_timeout=75)
    consulta = {'bool': {'filter': [
        {'match_phrase': {'body': nome}},
        {'term': {'tribunal': tribunal}},
    ]}}
    resp = es.search(
        index=index_name('movimentacoes'), query=consulta, size=teto,
        source=['proc', 'publish_date', 'nome_orgao', 'classe_nome', 'body'],
    )
    hits = resp['hits']['hits']
    total = resp['hits']['total']['value']

    processos: dict[str, dict] = {}
    datas_todas: list[str] = []
    for h in hits:
        s = h['_source']
        proc = s.get('proc')
        if not proc:
            continue
        d = processos.setdefault(proc, {
            'data': '', 'classe': '', 'orgao': '', 'marcadores': set(), 'pubs': 0,
        })
        d['pubs'] += 1
        data = str(s.get('publish_date') or '')[:10]
        if data:
            # TODAS as datas, não só a última: a linha do tempo mede ATIVIDADE
            # (publicações por mês). Um processo atravessa meses, então usar só
            # a data mais recente do caso desenharia um gráfico de "casos que
            # terminaram", que é outra coisa e ninguém pediu.
            datas_todas.append(data)
        if data > d['data']:
            d['data'] = data
        d['classe'] = d['classe'] or (s.get('classe_nome') or '')
        d['orgao'] = d['orgao'] or (s.get('nome_orgao') or '')
        texto = ' '.join(str(s.get('body') or '').split())
        for chave, rx in MARCADORES.items():
            if re.search(rx, texto):
                d['marcadores'].add(chave)

    return {
        'nome': nome, 'tribunal': tribunal,
        'publicacoes': len(hits), 'publicacoes_no_indice': total,
        'teto_batido': len(hits) >= teto and total > len(hits),
        'processos': processos, 'datas': datas_todas,
    }


# ─────────────────────────────────────────────────────────────────────────────
# coleta a partir do MODEL (o caminho bom) — ver `coletar()` para o legado
# ─────────────────────────────────────────────────────────────────────────────

def coletar_do_model(nome: str, tribunal: str) -> dict | None:
    """Dossiê a partir de `Magistrado`/`MagistradoAtuacao`. `None` se vazio.

    ## Por que este caminho substitui o `coletar()` por texto

    **1. Menção não é assinatura.** `match_phrase(body, nome)` acha a pessoa
    CITADA em decisão alheia — medido: *"reconhece-se a prevenção daquela
    Magistrada"* entra na conta, e citação de precedente faz um ministro do STJ
    virar autor de ato de vara. O extrator (`services/magistrados.py`) só
    atribui quando o marcador declara assinatura, e conta o resto como
    `citacao`.

    **2. A tripla `(tribunal, órgão, nome)` NÃO é a pessoa.** Este módulo
    agrupava por órgão e estava errado. Medido no backfill real de 147.592
    publicações: **2.549 linhas para 877 pessoas**, 32,6% com mais de um órgão,
    e **uma com 77 linhas** — porque `nome_orgao` no TJSP é a subseção do
    diário, com andar e sala. Filtrar pela tripla publicaria 77 fichas do mesmo
    desembargador. A ficha de uma PESSOA agrupa por `(tribunal_id, nome_chave)`,
    que é para o que o índice `mag_trib_nome_idx` existe.

    ⚠️ A normalização vem de `services.magistrados` e **não é reimplementada
    aqui**: normalizar diferente do escritor não dá erro — dá ficha vazia, que
    é indistinguível de "este magistrado não existe".
    """
    from tribunals.models import Magistrado, MagistradoAtuacao
    from tribunals.services.magistrados import normalizar_nome_magistrado

    chave = normalizar_nome_magistrado(nome)
    if not chave:
        return None
    linhas = list(Magistrado.objects.filter(
        tribunal_id=tribunal.upper(), nome_chave=chave))
    if not linhas:
        return None

    atuacoes = (MagistradoAtuacao.objects
                .filter(magistrado__in=linhas)
                .select_related('processo')
                .values('processo_id', 'publicado_em', 'formato', 'cargo',
                        'magistrado__orgao',
                        'processo__numero_cnj', 'processo__classe_nome'))

    processos: dict[str, dict] = {}
    datas: list[str] = []
    for a in atuacoes:
        proc = a['processo__numero_cnj'] or f"id:{a['processo_id']}"
        d = processos.setdefault(proc, {
            'data': '', 'classe': a['processo__classe_nome'] or '',
            'orgao': a['magistrado__orgao'] or '', 'marcadores': set(), 'pubs': 0,
        })
        d['pubs'] += 1
        if a['publicado_em']:
            iso = a['publicado_em'].isoformat()
            datas.append(iso)
            if iso > d['data']:
                d['data'] = iso
    return {
        'nome': linhas[0].nome, 'tribunal': tribunal.upper(),
        'publicacoes': len(atuacoes), 'publicacoes_no_indice': len(atuacoes),
        'teto_batido': False, 'processos': processos, 'datas': datas,
        # a ficha diz de quantos ÓRGÃOS ela foi montada: é a fan-out da tripla
        # aparecendo na tela em vez de virar surpresa
        'orgaos_do_magistrado': len(linhas),
        'origem': 'model',
    }


# ─────────────────────────────────────────────────────────────────────────────
# análise
# ─────────────────────────────────────────────────────────────────────────────

def _phi(processos: dict, a_chave: str, b_chave: str) -> tuple:
    """Coeficiente phi e a tabela 2×2 inteira.

    A tabela sai junto de propósito: phi sozinho esconde que uma das células
    tem 1 caso. Quem lê precisa poder desconfiar.
    """
    ambos = so_a = so_b = nenhum = 0
    for d in processos.values():
        tem_a = a_chave in d['marcadores']
        tem_b = b_chave in d['marcadores']
        if tem_a and tem_b:
            ambos += 1
        elif tem_a:
            so_a += 1
        elif tem_b:
            so_b += 1
        else:
            nenhum += 1
    den = math.sqrt((ambos + so_a) * (so_b + nenhum)
                    * (ambos + so_b) * (so_a + nenhum))
    phi = ((ambos * nenhum - so_a * so_b) / den) if den else None
    return phi, (ambos, so_a, so_b, nenhum)


def _serie_mensal(datas: list[str]) -> list[dict]:
    """Publicações por mês — a linha do tempo de ATIVIDADE.

    ⚠️ O mês corrente sai marcado `parcial`. É a lição do `buildVolumeChart`
    do `base.html`: bucket incompleto desenhado como completo sugere **queda**
    onde só há mês pela metade. O gráfico exclui o parcial da linha e o mostra
    tracejado, para o leitor ver que existe sem ler como tendência.
    """
    import collections

    if not datas:
        return []
    meses = collections.Counter(d[:7] for d in datas if d)
    if not meses:
        return []
    corrente = _dt.date.today().strftime('%Y-%m')
    ini, fim = min(meses), max(meses)
    # série CONTÍNUA: mês sem publicação é zero explícito, não buraco. Buraco
    # no eixo faz o olho interpolar e inventar atividade que não houve.
    saida, ano, mes = [], int(ini[:4]), int(ini[5:7])
    while f'{ano:04d}-{mes:02d}' <= fim:
        chave = f'{ano:04d}-{mes:02d}'
        saida.append({'mes': chave, 'n': meses.get(chave, 0),
                      'parcial': chave == corrente})
        mes += 1
        if mes > 12:
            mes, ano = 1, ano + 1
    return saida


def _matriz(procs: dict) -> dict:
    """Coocorrência de TODOS os pares — não só contra um eixo.

    O eixo único responde "o que anda com condenação". A matriz responde
    "o que anda com o quê", que é a pergunta da jurimetria e é onde aparece
    o par que ninguém pensou em procurar.
    """
    chaves = list(MARCADORES)
    presentes = [k for k in chaves
                 if any(k in d['marcadores'] for d in procs.values())]
    celulas = []
    for i, a in enumerate(presentes):
        for b in presentes[i + 1:]:
            phi, tab = _phi(procs, a, b)
            celulas.append({
                'a': a, 'b': b, 'rot_a': ROTULO.get(a, a), 'rot_b': ROTULO.get(b, b),
                'phi': phi, 'tabela': tab,
                'confiavel': min(tab) >= CELULA_MINIMA,
                'nunca_sozinho_b': tab[2] == 0 and tab[0] > 0,
            })
    fortes = sorted((c for c in celulas if c['confiavel'] and c['phi'] is not None),
                    key=lambda c: -abs(c['phi']))[:5]
    return {'chaves': presentes,
            'rotulos': [ROTULO.get(k, k) for k in presentes],
            'celulas': celulas, 'fortes': fortes}


def analisar(bruto: dict, eixo: str = 'condenacao') -> dict:
    """Frequências por processo, coocorrência com `eixo`, órgãos, classes e casos."""
    import collections

    procs = bruto['processos']
    n = len(procs)
    freq = collections.Counter(m for d in procs.values() for m in d['marcadores'])
    orgaos = collections.Counter(str(d['orgao']) for d in procs.values())
    classes = collections.Counter(str(d['classe']) for d in procs.values())
    anos = collections.Counter(d['data'][:4] for d in procs.values() if d['data'])

    correl = []
    for chave in MARCADORES:
        if chave == eixo:
            continue
        phi, tab = _phi(procs, eixo, chave)
        correl.append({
            'chave': chave, 'rotulo': ROTULO.get(chave, chave),
            'phi': phi, 'tabela': tab,
            'confiavel': min(tab) >= CELULA_MINIMA,
            # o caso que mais interessa: o marcador NUNCA aparece sozinho
            'nunca_sozinho': tab[2] == 0 and tab[0] > 0,
        })
    correl.sort(key=lambda c: -(c['phi'] or 0))

    casos = sorted(
        ({'proc': p, 'data': d['data'], 'classe': d['classe'],
          'orgao': d['orgao'], 'pubs': d['pubs'],
          'marcadores': sorted(d['marcadores'])} for p, d in procs.items()),
        key=lambda c: c['data'], reverse=True,
    )
    serie = _serie_mensal(bruto.get('datas') or [])
    ativos = [x for x in serie if not x['parcial']]
    destaque = {
        'orgao_principal': a_orgao[0] if (a_orgao := orgaos.most_common(1)) else None,
        'classe_principal': classes.most_common(1)[0] if classes else None,
        'ato_mais_frequente': freq.most_common(1)[0] if freq else None,
        'periodo': (serie[0]['mes'], serie[-1]['mes']) if serie else None,
        # média sobre meses COMPLETOS: incluir o corrente puxaria para baixo
        'media_mes': (round(sum(x['n'] for x in ativos) / len(ativos), 1)
                      if ativos else None),
    }
    return {
        **{k: v for k, v in bruto.items() if k not in ('processos', 'datas')},
        'n_processos': n, 'eixo': eixo,
        'serie': serie, 'matriz': _matriz(procs), 'destaque': destaque,
        'frequencia': [{'chave': k, 'rotulo': ROTULO.get(k, k), 'n': v,
                        'pct': 100.0 * v / n if n else 0}
                       for k, v in freq.most_common()],
        'orgaos': orgaos.most_common(6),
        'classes': classes.most_common(8),
        'anos': sorted(anos.items()),
        'correlacoes': correl,
        'casos': casos,
        'gerado_em': _dt.datetime.now().strftime('%d/%m/%Y %H:%M'),
    }


# ─────────────────────────────────────────────────────────────────────────────
# render
# ─────────────────────────────────────────────────────────────────────────────

def _svg_serie(serie: list[dict], largura: int = 520, altura: int = 110) -> str:
    """Linha do tempo em SVG puro, para o PDF.

    ⚠️ **WeasyPrint não roda JavaScript.** A tela usa ECharts; se o PDF
    dependesse dele, sairia com um retângulo vazio — e um relatório com o
    gráfico faltando é pior que um sem gráfico, porque parece defeito de
    impressão em vez de ausência deliberada. SVG é desenhado aqui, no servidor,
    e vive dentro do próprio arquivo.

    O mês PARCIAL sai como losango solto, fora da linha, pelo mesmo motivo da
    tela: bucket incompleto desenhado como cheio vira "queda" no olho de quem lê.
    """
    if not serie:
        return ''
    m_esq, m_dir, m_top, m_bai = 26, 6, 10, 16
    w = largura - m_esq - m_dir
    h = altura - m_top - m_bai
    topo = max((p['n'] for p in serie), default=0) or 1
    n = len(serie)
    passo = w / max(n - 1, 1)

    def xy(i, v):
        return (m_esq + i * passo, m_top + h - (v / topo) * h)

    completos = [(i, p) for i, p in enumerate(serie) if not p['parcial']]
    pts = ' '.join(f'{x:.1f},{y:.1f}' for x, y in (xy(i, p['n']) for i, p in completos))
    P = [f"<svg viewBox='0 0 {largura} {altura}' width='100%' height='{altura}' "
         f"xmlns='http://www.w3.org/2000/svg' role='img'>"]
    # eixo e duas guias horizontais — sem grade cheia, que compete com a linha
    for frac in (0, 0.5, 1.0):
        y = m_top + h - frac * h
        P.append(f"<line x1='{m_esq}' y1='{y:.1f}' x2='{largura - m_dir}' y2='{y:.1f}' "
                 f"stroke='#e6e6e6' stroke-width='0.6'/>")
        P.append(f"<text x='{m_esq - 4}' y='{y + 3:.1f}' font-size='7' fill='#999' "
                 f"text-anchor='end'>{int(topo * frac)}</text>")
    if pts:
        area = (f"{m_esq},{m_top + h:.1f} {pts} "
                f"{m_esq + completos[-1][0] * passo:.1f},{m_top + h:.1f}")
        P.append(f"<polygon points='{area}' fill='#b13' opacity='0.10'/>")
        P.append(f"<polyline points='{pts}' fill='none' stroke='#b13' "
                 f"stroke-width='1.4' stroke-linejoin='round'/>")
    for i, p in enumerate(serie):
        if not p['parcial']:
            continue
        x, y = xy(i, p['n'])
        P.append(f"<rect x='{x - 2.6:.1f}' y='{y - 2.6:.1f}' width='5.2' height='5.2' "
                 f"transform='rotate(45 {x:.1f} {y:.1f})' fill='#b13' opacity='0.45'/>")
    # rótulos: primeiro, meio e último — mais que isso vira borrão em 520px
    for i in {0, n // 2, n - 1}:
        x = m_esq + i * passo
        P.append(f"<text x='{x:.1f}' y='{altura - 4}' font-size='7' fill='#999' "
                 f"text-anchor='middle'>{serie[i]['mes']}</text>")
    P.append('</svg>')
    return ''.join(P)


def _e(v) -> str:
    return _html.escape(str(v if v is not None else ''))


def _barra(pct: float) -> str:
    return (f"<span class='bar'><span class='fill' style='width:{min(pct,100):.1f}%'>"
            f"</span></span>")


def render_html(a: dict) -> str:
    """HTML autocontido (CSS inline) para o WeasyPrint. Tudo escapado."""
    P: list[str] = []
    P.append("""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<style>
@page { size: A4; margin: 16mm 14mm 18mm 14mm;
        @bottom-center { content: counter(page) " / " counter(pages);
                         font: 8pt 'DejaVu Sans'; color: #888; } }
body { font: 9.5pt/1.45 'DejaVu Sans', sans-serif; color: #1a1a1a; }
h1 { font-size: 17pt; margin: 0 0 2mm; }
h2 { font-size: 11pt; margin: 7mm 0 2mm; padding-bottom: 1mm;
     border-bottom: 1.5pt solid #d9534f; color: #b13; }
h3 { font-size: 9.5pt; margin: 4mm 0 1.5mm; color: #444; }
.sub { color: #666; font-size: 8.5pt; margin: 0 0 4mm; }
table { width: 100%; border-collapse: collapse; font-size: 8.4pt; }
th { text-align: left; background: #f2f2f2; padding: 1.6mm 2mm;
     border-bottom: 1pt solid #ccc; font-weight: bold; }
td { padding: 1.3mm 2mm; border-bottom: 0.4pt solid #e6e6e6;
     vertical-align: top; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.aviso { border-left: 3pt solid #d9534f; background: #fdf3f2;
         padding: 2.5mm 3mm; margin: 3mm 0; font-size: 8.6pt; }
.nota { border-left: 3pt solid #999; background: #f6f6f6;
        padding: 2.5mm 3mm; margin: 3mm 0; font-size: 8.2pt; color: #444; }
.bar { display: inline-block; width: 26mm; height: 2.4mm; background: #e8e8e8;
       vertical-align: middle; }
.fill { display: block; height: 2.4mm; background: #b13; }
.tag { display: inline-block; font-size: 7.2pt; padding: 0.3mm 1.2mm;
       border: 0.4pt solid #bbb; border-radius: 1mm; margin-right: 1mm;
       color: #444; }
.tag.forte { border-color: #b13; color: #b13; }
.fraco { color: #999; }
.rodape { margin-top: 6mm; padding-top: 2mm; border-top: 0.4pt solid #ddd;
          font-size: 7.6pt; color: #777; }
</style></head><body>""")

    P.append(f"<h1>{_e(a['nome'])}</h1>")
    P.append(f"<p class='sub'>Dossiê de jurimetria · {_e(a['tribunal'])} · "
             f"{a['n_processos']} processos · {a['publicacoes']} publicações · "
             f"gerado em {_e(a['gerado_em'])}</p>")

    # ── o aviso vem ANTES do número. Quem lê tem que saber o que NÃO é. ──
    P.append("<div class='aviso'><b>O que este documento é — e o que não é.</b><br>"
             "Descreve <b>padrão de atuação</b>: onde o magistrado atua, que classes "
             "julga, em que volume e que atos aparecem no texto das publicações. "
             "<b>Não é</b> nota, ranking ou avaliação de magistrado, e <b>não mede "
             "mérito</b>: a base são intimações — a comunicação do ato, não o inteiro "
             "teor da decisão. Nenhum percentual aqui deve ser lido como "
             "&ldquo;taxa de condenação&rdquo;.</div>")

    if a.get('teto_batido'):
        P.append(f"<div class='aviso'><b>Teto atingido.</b> O índice tem "
                 f"{a['publicacoes_no_indice']:,} publicações e este dossiê leu "
                 f"{a['publicacoes']}. Os números abaixo descrevem a amostra lida, "
                 f"não o total.</div>".replace(',', '.'))

    # identidade
    P.append("<h2>Identificação e recorte</h2>")
    P.append("<div class='nota'>A identidade usada é <b>(tribunal, órgão, nome)</b>, "
             "nunca o nome sozinho: homônimos em outros tribunais são comuns e uma "
             "ficha por nome misturaria pessoas diferentes sem parecer errada. "
             "Confira abaixo se os órgãos correspondem ao magistrado procurado.</div>")
    P.append("<table><tr><th>Órgão julgador</th><th class='num'>Processos</th></tr>")
    for org, qtd in a['orgaos']:
        P.append(f"<tr><td>{_e(org) or '—'}</td><td class='num'>{qtd}</td></tr>")
    P.append("</table>")

    # linha do tempo — SVG, porque o WeasyPrint não roda JS
    if a.get('serie'):
        P.append("<h2>Atividade ao longo do tempo</h2>")
        P.append("<div class='nota'>Publicações por mês. Mês sem publicação é "
                 "<b>zero explícito</b>, não buraco. O mês corrente está "
                 "<b>incompleto</b> e sai como losango fora da linha — bucket "
                 "parcial desenhado como cheio sugere queda onde só há mês pela "
                 "metade.</div>")
        P.append(_svg_serie(a['serie']))
        dq = a.get('destaque') or {}
        if dq.get('media_mes') is not None:
            P.append(f"<p class='sub' style='margin-top:2mm'>Média de "
                     f"<b>{dq['media_mes']}</b> publicações por mês, sobre meses "
                     f"completos.</p>")

    # atuação
    P.append("<h2>O que julga</h2>")
    P.append("<table><tr><th>Classe processual</th><th class='num'>Processos</th></tr>")
    for cls, qtd in a['classes']:
        P.append(f"<tr><td>{_e(cls) or '—'}</td><td class='num'>{qtd}</td></tr>")
    P.append("</table>")
    if a['anos']:
        P.append("<h3>Distribuição por ano (data da publicação mais recente do caso)</h3>")
        P.append("<table><tr><th>Ano</th><th class='num'>Processos</th></tr>")
        for ano, qtd in a['anos']:
            P.append(f"<tr><td>{_e(ano)}</td><td class='num'>{qtd}</td></tr>")
        P.append("</table>")

    # marcadores
    P.append("<h2>Atos processuais mencionados</h2>")
    P.append("<div class='nota'>Contagem <b>verbatim</b>: o termo está escrito na "
             "publicação. Um mesmo processo pode ter vários marcadores — "
             "<b>condenação e absolvição coexistem</b> quando há absolvição parcial "
             "(&ldquo;condeno pelo art. X … por outro lado, absolvo da imputação do "
             "art. Y&rdquo;). Por isso os percentuais <b>não somam 100%</b> e não "
             "devem ser somados nem subtraídos.</div>")
    P.append("<table><tr><th>Ato</th><th class='num'>Processos</th>"
             "<th class='num'>%</th><th></th></tr>")
    for f in a['frequencia']:
        P.append(f"<tr><td>{_e(f['rotulo'])}</td><td class='num'>{f['n']}</td>"
                 f"<td class='num'>{f['pct']:.1f}%</td>"
                 f"<td>{_barra(f['pct'])}</td></tr>")
    P.append("</table>")

    # os pares mais fortes da matriz inteira
    fortes = (a.get('matriz') or {}).get('fortes') or []
    if fortes:
        P.append("<h2>Os pares que mais se associam</h2>")
        P.append(f"<div class='nota'>Entre os "
                 f"<b>{len((a.get('matriz') or {}).get('celulas') or [])}</b> pares "
                 f"possíveis, estes são os de maior associação <b>entre os que têm "
                 f"amostra suficiente</b> — os de célula pequena ficam de fora "
                 f"daqui em vez de liderarem o ranking por acaso de amostra.</div>")
        P.append("<table><tr><th>Par</th><th class='num'>juntos</th>"
                 "<th class='num'>φ</th></tr>")
        for c in fortes:
            P.append(f"<tr><td>{_e(c['rot_a'])} × {_e(c['rot_b'])}</td>"
                     f"<td class='num'>{c['tabela'][0]}</td>"
                     f"<td class='num'>{c['phi']:+.2f}</td></tr>")
        P.append("</table>")

    # correlação
    eixo_rot = ROTULO.get(a['eixo'], a['eixo'])
    P.append(f"<h2>Coocorrência com {_e(eixo_rot)}</h2>")
    P.append("<div class='nota'><b>φ</b> (phi) mede associação entre dois marcadores "
             "no mesmo processo: <b>+1</b> andam sempre juntos, <b>0</b> independentes, "
             "<b>−1</b> excludentes. A tabela 2×2 inteira sai ao lado <b>de propósito</b>: "
             "φ sozinho esconde que uma célula tem um único caso. Linhas em cinza têm "
             f"alguma célula abaixo de {CELULA_MINIMA} e <b>não sustentam inferência</b>.</div>")
    P.append(f"<table><tr><th>Marcador</th><th class='num'>φ</th>"
             f"<th class='num'>ambos</th><th class='num'>só {_e(eixo_rot)}</th>"
             f"<th class='num'>só o outro</th><th class='num'>nenhum</th>"
             f"<th>leitura</th></tr>")
    for c in a['correlacoes']:
        cls = '' if c['confiavel'] else " class='fraco'"
        phi = '—' if c['phi'] is None else f"{c['phi']:+.2f}"
        amb, so_e, so_o, nen = c['tabela']
        leitura = ''
        if c['nunca_sozinho']:
            leitura = (f"<b>nunca aparece sem {_e(eixo_rot)}</b> "
                       f"({so_o} casos isolados)")
        elif not c['confiavel']:
            leitura = 'amostra insuficiente'
        elif c['phi'] is not None and abs(c['phi']) < 0.1:
            leitura = 'praticamente independente'
        P.append(f"<tr{cls}><td>{_e(c['rotulo'])}</td><td class='num'>{phi}</td>"
                 f"<td class='num'>{amb}</td><td class='num'>{so_e}</td>"
                 f"<td class='num'>{so_o}</td><td class='num'>{nen}</td>"
                 f"<td>{leitura}</td></tr>")
    P.append("</table>")

    # casos
    P.append(f"<h2>Casos ({a['n_processos']}) — do mais recente ao mais antigo</h2>")
    P.append("<table><tr><th>Publicação</th><th>Processo</th><th>Classe</th>"
             "<th>Marcadores</th></tr>")
    for c in a['casos']:
        tags = ''.join(
            f"<span class='tag{' forte' if m == a['eixo'] else ''}'>"
            f"{_e(ROTULO.get(m, m))}</span>" for m in c['marcadores']
        ) or "<span class='fraco'>—</span>"
        P.append(f"<tr><td>{_e(c['data']) or '—'}</td><td>{_e(c['proc'])}</td>"
                 f"<td>{_e(str(c['classe'])[:44])}</td><td>{tags}</td></tr>")
    P.append("</table>")

    # metodologia
    P.append("<h2>Metodologia e limites</h2>")
    P.append("<div class='nota'>"
             "<b>Unidade:</b> o processo, não a publicação — um caso com quatro "
             "intimações contaria quatro vezes numa contagem por documento.<br>"
             "<b>Fonte:</b> publicações do Diário de Justiça Eletrônico Nacional "
             "indexadas no acervo, filtradas por menção verbatim ao nome do "
             "magistrado no tribunal indicado.<br>"
             "<b>O que o marcador significa:</b> que o <b>termo aparece</b> na "
             "publicação — não que o ato foi praticado naquele processo. Uma decisão "
             "pode citar &ldquo;prisão preventiva&rdquo; para negá-la. Distinguir "
             "exigiria o dispositivo, que a intimação não traz.<br>"
             "<b>O que falta para medir mérito:</b> o inteiro teor da sentença. "
             "Enquanto ele não estiver no acervo, taxa de deferimento, propensão a "
             "condenar e severidade de pena <b>não são mensuráveis</b> — e este "
             "documento se abstém de estimá-las.</div>")

    P.append(f"<div class='rodape'>Voyager · dossiê de jurimetria gerado em "
             f"{_e(a['gerado_em'])} a partir de {a['publicacoes']} publicações "
             f"e {a['n_processos']} processos. Números conferíveis no acervo.</div>")
    P.append("</body></html>")
    return ''.join(P)


def render_pdf(a: dict) -> bytes:
    """HTML → PDF via WeasyPrint. `RuntimeError` se a lib não está disponível."""
    try:
        from weasyprint import HTML
    except Exception as e:  # noqa: BLE001 — ImportError ou libs nativas
        raise RuntimeError(f'WeasyPrint indisponível: {e}') from e
    return HTML(string=render_html(a)).write_pdf()


def dossie(nome: str, tribunal: str, eixo: str = 'condenacao') -> dict:
    """Coleta + análise. `render_pdf(dossie(...))` fecha o caminho.

    Prefere o MODEL e cai para o texto quando ele ainda não foi populado — as
    tabelas nascem vazias de propósito (o backfill nacional projeta ~184-191 M
    de linhas e é decisão de disco). A ficha diz de qual caminho veio, porque
    os dois medem coisas diferentes: o model conta ASSINATURA, o texto conta
    MENÇÃO, e menção infla com citação de precedente.
    """
    bruto = coletar_do_model(nome, tribunal)
    if not bruto or not bruto['processos']:
        bruto = coletar(nome, tribunal)
        bruto['origem'] = 'texto'
        bruto['orgaos_do_magistrado'] = None
    return analisar(bruto, eixo=eixo)
