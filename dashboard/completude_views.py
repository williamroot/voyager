"""Completude do acervo — a única tela que compara os DOIS lados.

Todas as outras telas medem contagem PRÓPRIA: quantos runs, quantas
movimentações, quantos processos. Isso responde "quanto trabalhamos", não
"quanto do acervo temos" — e a diferença entre as duas perguntas custou a este
projeto três perdas medidas (ver a tabela no CLAUDE.md).

Aqui cada linha tem o nosso número ao lado do número da FONTE, com a data em que
a fonte foi medida. Onde não há gabarito externo, a tela **diz que não há** em
vez de inventar um denominador — abster > chutar.

HOT PATH, ZERO QUERY PESADA. Lê do cache preenchido por `warm_completude`; no
miss mostra o estado "medindo" em vez de segurar a requisição. Uma medição de
rodapé sem `request_timeout` já derrubou o site (worker morto pelo gunicorn em
loop) — ver .ia/OPS.md.

## As duas regras de honestidade que esta view faz cumprir (01/09/2026)

**1. Número congelado nunca entra numa conta com número vivo.** A tela dizia
`temos 344.630.543 · a fonte declara 343.235.554 · lacuna −1.394.989 · 100,4%`:
lacuna NEGATIVA, porque o nosso lado era relido a cada 30 min e o lado da fonte
estava parado em 14/08. Agora a diferença só é publicada dentro de um
CONFRONTO — o par `(declarado, nosso)` colhido no mesmo instante
(`dashboard/completude_datajud.py`) —, e o retrato histórico, quando é ele que
está na tela, aparece rotulado com a data.

**2. Bloco sem medição sai pelo NOME.** `_num()` devolve `None` para tudo que
não é número e um dicionário (verdadeiro mesmo com `n=0`) para o que é. `'0'` é
string VERDADEIRA no `{% if %}` do Django — guardar pelo texto formatado põe
painel zerado na tela anunciando-se como medido.
"""
import datetime

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.shortcuts import render
from django.views.decorators.http import require_GET

from . import completude_medicoes as M

CACHE_KEY = 'completude:acervo:v1'


def _idade(quando: datetime.date) -> int:
    return (datetime.date.today() - quando).days


def _num(valor):
    """`{'n': int, 'txt': '1.234'}` se for número; `None` se não for.

    Mesma régua da tela de Estoque: zero MEDIDO aparece (o dicionário é
    verdadeiro), zero inventado some e o bloco é denunciado pelo nome.
    """
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return None
    n = int(valor)
    return {'n': n, 'txt': f'{n:,}'.replace(',', '.')}


def _int(valor, padrao=0):
    """Inteiro para somatório interno. Não-número vira `padrao`."""
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return padrao
    return int(valor)


def _dias_no_piso(serie: list, piso: int) -> int | None:
    """Há quantos dias seguidos a vazão está no PISO (= só a coleta do dia).

    O piso é o número de tribunais ativos: 59 runs/dia é a coleta diária e mais
    nada. Uma vazão que desabou ao piso e fica lá é o mutirão desligado — e é
    exatamente o tipo de coisa que não dispara alarme nenhum, porque tudo
    continua verde. `None` quando não dá pra dizer.
    """
    if not serie or not piso:
        return None
    n = 0
    for ponto in reversed(serie):
        if _int(ponto.get('n')) > piso:
            break
        n += 1
    return n


def _resumo_fase3(r: dict) -> dict:
    """Acrescenta o peso do que está FORA DO ALVO — calculado, nunca digitado.

    O tooltip do TJPR dizia "43% do estimado que resta". Número digitado em
    texto envelhece igual a número congelado em constante: some do radar de
    quem edita e passa a mentir na primeira vez que o mundo muda.
    """
    if not r:
        return {}
    total_est = _int(r.get('recuperavel')) + _int(r.get('fora_estimado'))
    return {**r, 'pct_fora_estimado':
            (100.0 * _int(r.get('fora_estimado')) / total_est) if total_est else None}


def _confronto(dados: dict) -> dict:
    """O par `(declarado, nosso)` do Datajud, medido no MESMO instante.

    Prefere a rodada viva; sem ela, cai no retrato histórico — e o campo
    `origem` diz qual dos dois está na tela, porque um retrato de semana
    passada apresentado como medição de hoje é exatamente o defeito que este
    bloco existe para não repetir.
    """
    vivo = dados.get('datajud') or {}
    completo = (not vivo.get('parcial')
                and _num(vivo.get('util')) and _num(vivo.get('nosso')))
    if completo:
        c = dict(vivo)
        c['origem'] = 'medido'
        c['quando'] = c.get('ate')
        return c
    c = dict(M.CONFRONTO_DATAJUD)
    c['origem'] = 'historico'
    c['quando'] = c.get('medido_em')
    c['idade_dias'] = _idade(c['medido_em'])
    # Régua meio construída NÃO vira confronto — mas o progresso aparece, senão
    # a tela pareceria parada enquanto a rodada anda.
    if vivo.get('esperado'):
        c['progresso'] = {'medidos': vivo.get('medidos'),
                          'esperado': vivo.get('esperado')}
    return c


@login_required
@require_GET
def completude(request):
    """GET /dashboard/completude/ — quanto do acervo nacional nós temos."""
    dados = cache.get(CACHE_KEY) or {}
    pendente = not dados

    portas = []
    for p in M.PORTAS:
        vivo = (dados.get('portas') or {}).get(p['slug'], {})
        temos = vivo.get('temos')
        portas.append({
            **p,
            'temos': _num(temos),
            # NÃO existe `pct`/`lacuna` de porta aqui, e é de propósito: seria
            # o nosso número de agora dividido por um declarado congelado. A
            # única diferença publicada é a do CONFRONTO, par do mesmo instante.
            'idade_medicao': _idade(p['medido_em']),
            'recuperavel': _num(p.get('recuperavel')),
            'idade_recuperavel': (_idade(p['recuperavel_em'])
                                  if p.get('recuperavel_em') else None),
        })

    nac = dados.get('recup_nacional') or {}
    recup_nac = {
        'recuperado': _num(nac.get('recuperado')),
        'estimado': _num(nac.get('estimado')),
        'dias': _num(nac.get('dias')),
        'alvo': _num(nac.get('alvo')),
        'nunca_refeito': _num(nac.get('nunca_refeito')),
        'alvo_da_casa': _num(nac.get('alvo_da_casa')),
        'nunca_da_casa': _num(nac.get('nunca_da_casa')),
        'refeitos_da_casa': _num(nac.get('refeitos_da_casa')),
        'pct': nac.get('pct'),
        'pct_honesto': nac.get('pct_honesto'),
        'estimado_em': nac.get('estimado_em') or M.RECUPERAVEL_MEDIDO_EM,
    }
    vaz = dados.get('vazao') or {}
    serie = vaz.get('serie') or []
    pico = _int(vaz.get('pico'))
    vazao = {
        'serie': [{**p, 'pct': (100.0 * _int(p.get('n')) / pico) if pico else 0,
                   'no_piso': _int(p.get('n')) <= _int(vaz.get('piso'))}
                  for p in serie],
        'de': serie[0].get('dia'),
        'ate': serie[-1].get('dia'),
        'piso': _num(vaz.get('piso')),
        'pico': _num(vaz.get('pico')),
        'ultimo': _num(vaz.get('ultimo')),
        # dias seguidos no piso = "a máquina parou e ninguém viu"
        'parada_ha': _dias_no_piso(serie, _int(vaz.get('piso'))),
    } if serie else {}
    # Bloco sem medição sai pelo NOME, nunca zerado. O confronto do Datajud não
    # entra aqui porque ele nunca some da tela: quando a rodada viva falta, o
    # card publica o retrato histórico com a etiqueta `histórico` colada no
    # número, que é aviso mais forte que uma lista no rodapé.
    nao_medidos = [nome for nome, bloco in (
        ('recuperação nacional (publicações que voltaram)', recup_nac['recuperado']),
        ('vazão da recuperação (runs/dia desde o corte)', _num(vaz.get('pico'))),
    ) if bloco is None]

    return render(request, 'dashboard/completude.html', {
        'portas': portas,
        'recuperacao': dados.get('recuperacao') or [],
        'resumo_recup': dados.get('resumo_recup') or {},
        'recup_nacional': recup_nac,
        'fase3': dados.get('fase3') or [],
        'resumo_fase3': _resumo_fase3(dados.get('resumo_fase3') or {}),
        'vazao': vazao,
        'confronto': _confronto(dados),
        'recuperavel_em': M.RECUPERAVEL_MEDIDO_EM,
        'nao_medidos': nao_medidos,
        'diarios': dados.get('diarios') or [],
        'medido_em': dados.get('medido_em'),
        'pendente': pendente,
        'fase2': M.FASE_2,
    })
