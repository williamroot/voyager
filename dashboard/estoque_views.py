"""Estoque — quanto já marcamos × quanto já foi consumido.

Esta tela responde uma pergunta comercial: **por tribunal, quanto de lead nós
temos marcado, e quanto disso o Juriscope/Falcon já puxou.** Ela alterna entre
duas trilhas (Precatório e Direito Creditório), porque são produtos diferentes
com estoques de ordem de grandeza diferente.

A medição vive em `dashboard/estoque.py` (outro dono). Aqui só se **lê o
cache** — a agregação custa ~52 s e medição de rodapé sem teto de espera já
derrubou o site em julho (princípio nº 7 do CLAUDE.md).

## As quatro coisas que esta tela é PROIBIDA de fazer

Cada uma saiu de um erro medido nesta semana; não são preferências de estilo.

1. **"estoque − consumido" não é saldo.** Medido em 01/09/2026: o consumido
   (811.360 processos distintos) é **14,7×** o estoque de PRECATORIO (55.285).
   A subtração dá negativo, e negativo aqui não significa dívida — significa
   que os dois conjuntos não são comparáveis por subtração (consumo histórico
   × classificação de hoje). A tela mostra os DOIS lados e, para a relação
   entre eles, só o que o `cruzamento` apurar de verdade.

   E o corolário que a primeira versão desta tela violou no gráfico: **duas
   barras lado a lado são uma comparação**, e comparação exige a mesma régua.
   `estoque` é filtrado pelos rótulos da trilha; `consumido` não é filtrado por
   nada. Pôr os dois lado a lado fazia TRF1, TJSP e TJMG aparecerem com mais
   consumo do que estoque — o usuário perguntou "como?" e a resposta era o
   recorte, não o mundo. Medido no TRF1 em 01/09/2026: dos 486.074 consumidos,
   313.826 estão na trilha e 172.248 não (147.841 hoje são `NAO_LEAD`, 24.407
   são `DIREITO_CREDITORIO`). A barra de consumo passou a ser **`ambos`** — o
   consumido recortado pela MESMA trilha —, e os 172.248 continuam na tela como
   série e coluna próprias, `fora`. Somar por baixo do pano seria trocar um
   erro de comparação por um erro de contagem.

2. **Somar `falcon` + `juriscope` em silêncio é pior do que parecia.** Não é
   só que são clientes distintos: medido em 01/09/2026, os **405.740**
   processos do juriscope estão TODOS dentro do falcon (sobreposição total).
   Somar dá 1.217.100 e conta 405.740 duas vezes, contra a união real de
   **811.360**. A tela mostra cada cliente, a UNIÃO distinta e a sobreposição
   — e quando escreve "soma", escreve que é soma.

3. **Registro de consumo ≠ processo distinto.** 1.224.278 registros para
   811.360 processos: 51% de diferença, porque re-consumo cria registro novo
   (`LeadConsumption` não tem unique). Todo número diz qual dos dois é — e a
   unidade do bloco de resultado NÃO é adivinhada: vem declarada pelo medidor
   em `consumo_resultado_unidade`.

4. **Bloco sem medição SAI da tela dizendo que saiu.** Nunca aparece zerado —
   meia régua é pior que régua nenhuma.

## E a quinta, que é de implementação

O bloco é guardado pelo **NÚMERO**, nunca pelo texto formatado: `'0'` é uma
string verdadeira no `{% if %}` do Django, e um painel zerado que se anuncia
como medido produz exatamente a confiança falsa que o produto existe pra
evitar. Por isso `_num()` devolve `None` para tudo que não é número — e um
dicionário (sempre verdadeiro, mesmo com `n=0`) para o que é.
"""
import logging

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.shortcuts import render
from django.views.decorators.http import require_GET

logger = logging.getLogger('voyager.dashboard.estoque')

#: Chave de cache usada como FALLBACK. O dono da medição é
#: `dashboard/estoque.py`; se ele expuser `ler()`/`CHAVE`, é o que vale.
CHAVE_BASE = 'estoque:v1'

TRILHAS = [
    ('precatorio', 'Precatório'),
    ('direito_creditorio', 'Direito creditório'),
]
TRILHA_PADRAO = 'precatorio'
TRILHA_ROTULO = dict(TRILHAS)

#: Os clientes NÃO são uma lista fixa aqui — vêm do payload (`consumo_clientes`
#: / `por_cliente`). Fixar `['falcon', 'juriscope']` faria um cliente novo
#: sumir da tela em silêncio no dia em que fosse cadastrado.
#:
#: Cores validadas para daltonismo nos dois temas (deltaE >= 9,2 deutan/protan;
#: >= 19,8 normal; contraste >= 3:1 sobre #18181b e #ffffff). Cliente além da
#: terceira posição NÃO ganha cor gerada: fica só na tabela, e o gráfico diz
#: que ficou.
CORES_CLIENTE = ['#ea580c', '#0284c7', '#7c3aed']

#: Rótulo humano dos resultados. Fonte de verdade é o model; este dicionário só
#: existe pra não quebrar se aparecer valor fora das choices — nesse caso o
#: próprio valor cru vira o rótulo, em vez de sumir.
def _rotulos_resultado():
    try:
        from tribunals.models import LeadConsumption
        return dict(LeadConsumption.RESULTADO_CHOICES)
    except Exception:      # pragma: no cover - só se o model sumir
        logger.exception('estoque: não consegui ler RESULTADO_CHOICES')
        return {}


#: Os três que a tela destaca (mesmo contrato do medidor). Os demais entram
#: como "outros resultados" — sumir com eles quebraria a soma declarada.
RESULTADOS_DESTAQUE = ('validado', 'pendente', 'sem_expedicao')

#: Quantos tribunais entram em cada gráfico. A tabela abaixo tem todos.
TOPO_GRAFICO = 15


# --------------------------------------------------------------------------
# leitura (só cache)
# --------------------------------------------------------------------------

def _payload(trilha):
    """Payload da trilha, ou `None`. **Nunca** calcula.

    O módulo de medição é de outro dono e pode expor `ler(trilha)`, `ler()` ou
    só uma chave de cache. Tentamos nessa ordem e nos contentamos com o que
    houver — o que não pode acontecer é a tela quebrar porque o contrato mudou
    do outro lado.
    """
    modulo = None
    try:
        from . import estoque as modulo  # noqa: F401
    except ImportError:
        modulo = None
    except Exception:
        logger.exception('estoque: módulo de medição não pôde ser importado')
        modulo = None

    if modulo is not None:
        ler = getattr(modulo, 'ler', None)
        if callable(ler):
            for args in ((trilha,), ()):
                try:
                    p = ler(*args)
                except TypeError:
                    continue          # assinatura diferente: tenta a outra
                except Exception:
                    logger.exception('estoque: ler() falhou')
                    return None
                if _serve(p, trilha, pedimos=bool(args)):
                    return p

    base = getattr(modulo, 'CHAVE', CHAVE_BASE) if modulo is not None else CHAVE_BASE
    for chave, pedimos in ((f'{base}:{trilha}', True), (base, False)):
        p = cache.get(chave)
        if _serve(p, trilha, pedimos=pedimos):
            return p
    return None


def _serve(p, trilha, pedimos):
    """O payload é desta trilha?

    Quando **pedimos** a trilha (chave de cache por trilha, ou `ler(trilha)`),
    a ausência do carimbo `trilha` é aceitável — quem respondeu sabia o que
    perguntamos. Quando não pedimos (chave única, `ler()` sem argumento), o
    carimbo é OBRIGATÓRIO: servir um payload sem carimbo nas duas abas mostraria
    o mesmo número em Precatório e em Direito Creditório, que é exatamente o
    tipo de número redondo que este projeto existe pra não produzir.
    """
    if not isinstance(p, dict):
        return False
    carimbo = p.get('trilha')
    return carimbo == trilha or (pedimos and carimbo is None)


# --------------------------------------------------------------------------
# normalização
# --------------------------------------------------------------------------

def _num(valor):
    """`{'n': int, 'txt': '1.234'}` se for número; `None` se não for.

    É aqui que a regra do "guarde pelo NÚMERO" vira código. `_num('0')`,
    `_num(None)` e `_num('—')` devolvem `None` — o bloco some da tela e entra
    na lista do que não foi medido. `_num(0)` devolve um dicionário (verdadeiro
    no template) porque zero **medido** é um resultado legítimo.
    """
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return None
    n = int(valor)
    return {'n': n, 'txt': f'{n:,}'.replace(',', '.')}


def _int(valor, padrao=0):
    """Inteiro pra somatório interno. Não-número vira `padrao`."""
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return padrao
    return int(valor)


def _pct(parte, todo):
    """`parte/todo` em %, com 1 casa. `None` se o denominador não serve."""
    if not isinstance(todo, (int, float)) or todo <= 0:
        return None
    return round(100.0 * parte / todo, 1)


def _clientes(p):
    """Clientes SEPARADOS, com a sobreposição na cara.

    Aqui mora a armadilha nº 2, e ela é maior do que "são dois clientes": em
    01/09/2026 **todos** os 405.740 processos do juriscope estavam também no
    falcon. A soma (1.217.100) conta 405.740 duas vezes; a união real é
    811.360. Por isso a tela mostra os três números — cliente a cliente, união
    e soma — e nomeia a diferença entre os dois últimos.
    """
    bloco = p.get('consumo_clientes')
    if not isinstance(bloco, dict):
        return None
    itens_brutos = bloco.get('itens')
    if not isinstance(itens_brutos, (list, tuple)) or not itens_brutos:
        return None

    uniao = _num(bloco.get('uniao_distinta'))
    itens = []
    for i, it in enumerate(itens_brutos):
        if not isinstance(it, dict):
            continue
        proc = _num(it.get('processos'))
        if proc is None:
            continue
        itens.append({
            'cliente': str(it.get('cliente') or '—'),
            'processos': proc,
            'registros': _num(it.get('registros')),
            'cor': CORES_CLIENTE[i] if i < len(CORES_CLIENTE) else None,
            'pct_uniao': _pct(proc['n'], uniao['n']) if uniao else None,
        })
    if not itens:
        return None
    return {
        'itens': itens,
        'uniao': uniao,
        'soma': _num(bloco.get('soma_dos_clientes')),
        'sobreposicao': _num(bloco.get('sobreposicao')),
        'nota': bloco.get('nota') if isinstance(bloco.get('nota'), str) else None,
        'sem_cor': [i['cliente'] for i in itens if i['cor'] is None],
    }


def _resultados(p):
    """Resultado reportado, com a UNIDADE que o medidor declarou.

    A tela não adivinha se a base é registro ou processo distinto: lê
    `consumo_resultado_unidade`. Se o medidor não declarar, o bloco diz que a
    unidade é desconhecida — que é diferente de assumir uma.

    Os resultados fora dos três destacados (erro, pago, arquivado, cedido) não
    somem: viram uma linha "outros". Somem-los seria quebrar a soma que o
    próprio bloco publica como conferência.
    """
    bruto = p.get('consumo_por_resultado')
    if not isinstance(bruto, dict) or not bruto:
        return None
    rotulos = _rotulos_resultado()

    itens, outros, soma = [], 0, 0
    for k in RESULTADOS_DESTAQUE:
        v = _num(bruto.get(k))
        if v is None:
            continue
        soma += v['n']
        itens.append({'k': k, 'rotulo': rotulos.get(k, k), 'v': v})
    for k, valor in bruto.items():
        if k in RESULTADOS_DESTAQUE:
            continue
        v = _num(valor)
        if v is not None:
            outros += v['n']
            soma += v['n']
    if not itens and not outros:
        return None
    if outros:
        itens.append({'k': '_outros', 'rotulo': 'Outros resultados',
                      'v': _num(outros)})
    for it in itens:
        it['pct'] = _pct(it['v']['n'], soma)

    unidade = p.get('consumo_resultado_unidade')
    registros = _num(p.get('total_consumos'))
    return {
        'itens': itens,
        'soma': _num(soma),
        'unidade': unidade if isinstance(unidade, str) else None,
        # conferência visível: a soma tem que fechar com o total de registros
        'fecha': bool(registros and registros['n'] == soma),
        'registros': registros,
    }


def _consumo_hoje(p):
    """Onde está HOJE, na classificação, cada processo já consumido.

    É a explicação da fatia `so_consumo`: o processo foi puxado e desde então
    virou outra coisa. Sem este bloco, "consumido e fora do estoque" fica
    parecendo erro de contagem em vez de reclassificação.
    """
    bruto = p.get('consumo_por_classificacao_atual')
    if not isinstance(bruto, dict) or not bruto:
        return None
    total = sum(v for v in bruto.values() if isinstance(v, (int, float))
                and not isinstance(v, bool))
    itens = []
    for rotulo, valor in bruto.items():
        v = _num(valor)
        if v is not None:
            itens.append({'rotulo': str(rotulo), 'v': v,
                          'pct': _pct(v['n'], total)})
    itens.sort(key=lambda i: -i['v']['n'])
    return {'itens': itens, 'total': _num(total)} if itens else None


#: Sinônimos aceitos para as três fatias do cruzamento. O medidor publica
#: `ambos`/`so_estoque`/`so_consumo`; os apelidos ficam porque um contrato que
#: só aceita uma grafia esconde dado por motivo de digitação.
CRUZAMENTO_FATIAS = [
    ('ambos', ('ambos', 'interseccao', 'intersecao'),
     'no estoque E já consumido',
     'Marcamos e o cliente já levou. É a única fatia em que os dois lados '
     'falam do mesmo processo.'),
    ('so_estoque', ('so_estoque', 'somente_estoque', 'estoque_nao_consumido'),
     'no estoque, nunca consumido',
     'Marcado e ninguém puxou — o estoque que de fato resta.'),
    ('so_consumo', ('so_consumo', 'somente_consumo', 'consumido_fora_do_estoque'),
     'consumido, fora do estoque de hoje',
     'O cliente levou e hoje o processo não está nesta trilha: consumo '
     'histórico, ou classificação que mudou depois. É esta fatia que faz a '
     'subtração dar negativo.'),
]


def _cruzamento(bruto):
    """Normaliza o cruzamento em fatias + tiles.

    Devolve `None` se não veio nada aproveitável — o card some e o nome entra
    em `nao_medidos`. **Não deduz** fatia que faltou a partir dos totais:
    interseção de conjunto não se calcula com duas contagens.
    """
    if not isinstance(bruto, dict) or not bruto:
        return None

    usadas, fatias = set(), []
    for chave, apelidos, rotulo, nota in CRUZAMENTO_FATIAS:
        for apelido in apelidos:
            if apelido in bruto:
                usadas.add(apelido)
                v = _num(bruto[apelido])
                if v is not None:
                    fatias.append({'k': chave, 'rotulo': rotulo,
                                   'nota': nota, 'v': v})
                break

    # A barra de partição só é desenhada com o conjunto COMPLETO: barra com
    # uma fatia faltando desenha um todo que não é o todo.
    uniao = sum(f['v']['n'] for f in fatias)
    completa = len(fatias) == len(CRUZAMENTO_FATIAS) and uniao > 0
    if completa:
        for f in fatias:
            f['pct'] = _pct(f['v']['n'], uniao)

    extras = []
    for k, v in sorted(bruto.items()):
        if k in usadas or k in ('nota', 'explicacao', 'metodo'):
            continue
        n = _num(v)
        if n is not None:
            extras.append({'rotulo': k.replace('_', ' '), 'v': n})

    nota = bruto.get('nota') or bruto.get('explicacao') or bruto.get('metodo')
    if not (fatias or extras or nota):
        return None
    return {
        'fatias': fatias,
        'uniao': _num(uniao) if completa else None,
        'completa': completa,
        'extras': extras,
        'nota': nota if isinstance(nota, str) else None,
    }


def _saldo_por_rotulo(p, cruz, rotulos):
    """O `so_estoque` aberto por rótulo — o número mais acionável da tela.

    "395.570 em estoque, nunca consumido" passa a impressão de que há muito
    precatório disponível, e é falso: medido em 01/09/2026, **4.652** são
    `PRECATORIO` (91,6% desse rótulo JÁ foi puxado) e 390.918 são
    `PRE_PRECATORIO`. Quem lê o agregado decide errado.

    A conta é exata, não estimativa: para o rótulo R,
    `estoque_por_rotulo[R] − consumo_por_classificacao_atual[R]` é
    "classificado como R e nunca consumido", porque o segundo é o rótulo de
    HOJE dos processos já consumidos — o mesmo universo, a mesma foto.

    E ela vem com **controle**: a soma das parcelas tem que dar exatamente o
    `so_estoque` do cruzamento. Se não der, a decomposição NÃO é publicada e o
    nome dela entra em `nao_medidos`. Régua que não fecha com a própria fonte
    é régua torta, e número medido com régua torta é pior que número nenhum.
    """
    por_rotulo = p.get('estoque_por_rotulo')
    consumo_atual = p.get('consumo_por_classificacao_atual')
    if not (isinstance(por_rotulo, dict) and isinstance(consumo_atual, dict)
            and cruz and rotulos):
        return None
    alvo = next((f['v']['n'] for f in cruz['fatias'] if f['k'] == 'so_estoque'), None)
    if alvo is None:
        return None

    itens, soma = [], 0
    for r in rotulos:
        est = _num(por_rotulo.get(r))
        if est is None:
            return None                     # parcela faltando: não publica
        cons = _int(consumo_atual.get(r))
        sobra = est['n'] - cons
        soma += sobra
        itens.append({'rotulo': str(r), 'v': _num(sobra),
                      'estoque': est, 'consumido': _num(cons),
                      'pct_puxado': _pct(cons, est['n'])})
    if soma != alvo:
        logger.error('estoque: decomposição do so_estoque não fecha — '
                     '%s parcelas contra %s do cruzamento', soma, alvo)
        return None
    for i in itens:
        i['pct'] = _pct(i['v']['n'], alvo) if alvo else None
    return {'itens': itens, 'total': _num(alvo)}


def _normalizar(bruto):
    """Payload cru → contexto de template. Devolve `(ctx, nao_medidos)`.

    Toda guarda é pelo NÚMERO (`_num`). Bloco que não deu número não é
    renderizado e entra em `nao_medidos` PELO NOME — inclusive quando o
    medidor já explicou o motivo dele, porque o nome do bloco na tela e o nome
    da medição no job não são a mesma frase.
    """
    faltando = []
    p = bruto or {}

    origem = p.get('nao_medidos')
    if isinstance(origem, (list, tuple)):
        faltando.extend(str(x) for x in origem)

    estoque = _num(p.get('total_estoque'))
    distintos = _num(p.get('total_consumido_distinto'))
    registros = _num(p.get('total_consumos'))
    if estoque is None:
        faltando.append('Estoque marcado')
    if distintos is None:
        faltando.append('Processos distintos consumidos')
    if registros is None:
        faltando.append('Registros de consumo')

    # Re-consumo: quantos registros a mais do que processos. É a prova viva de
    # que os dois totais medem coisas diferentes.
    reconsumo = None
    if registros is not None and distintos is not None and distintos['n'] > 0:
        reconsumo = {'excedente': _num(registros['n'] - distintos['n']),
                     'razao': round(registros['n'] / distintos['n'], 2)}

    # RAZÃO, nunca diferença: o fato é "14,7×", e ele não vira saldo.
    razao_consumo = None
    if estoque is not None and distintos is not None and estoque['n'] > 0:
        razao_consumo = round(distintos['n'] / estoque['n'], 1)

    # Composição da trilha: `precatorio` soma PRECATORIO + PRE_PRECATORIO, e
    # isso tem que estar VISÍVEL. Recorte escondido é recorte que ninguém
    # confere.
    rotulos = p.get('rotulos') if isinstance(p.get('rotulos'), (list, tuple)) else []
    por_rotulo = p.get('estoque_por_rotulo')
    composicao = []
    if isinstance(por_rotulo, dict):
        for r in (rotulos or sorted(por_rotulo)):
            v = _num(por_rotulo.get(r))
            if v is not None:
                composicao.append({'rotulo': str(r), 'v': v,
                                   'pct': _pct(v['n'], estoque['n']) if estoque else None})

    # ---- quebra por tribunal --------------------------------------------
    clientes = _clientes(p)
    if clientes is None:
        faltando.append('Consumo por cliente')
    colunas = [i['cliente'] for i in clientes['itens']] if clientes else []

    linhas = []
    bruto_trib = p.get('por_tribunal')
    if isinstance(bruto_trib, (list, tuple)) and bruto_trib:
        for t in bruto_trib:
            if not isinstance(t, dict):
                continue
            pc = t.get('por_cliente') if isinstance(t.get('por_cliente'), dict) else {}
            rs = t.get('resultado') if isinstance(t.get('resultado'), dict) else {}
            for nome in pc:
                if nome not in colunas:
                    colunas.append(nome)
            consumido, ambos = _int(t.get('consumido')), _int(t.get('ambos'))
            linhas.append({
                't': str(t.get('t') or '—'),
                'estoque': _int(t.get('estoque')),
                'consumido': consumido,
                'consumos': _int(t.get('consumos')),
                'ambos': ambos,
                # O consumo que a barra de estoque NÃO cobre. Subtração crua,
                # sem `max(x, 0)`: ver o controle da partição logo abaixo.
                'fora': consumido - ambos,
                'cli': {k: _int(pc.get(k)) for k in colunas},
                'res': {k: _int(rs.get(k)) for k in RESULTADOS_DESTAQUE},
            })
        linhas.sort(key=lambda l: (-l['estoque'], -l['consumido'], l['t']))

        # CONTROLE da partição por tribunal: `ambos` é o `consumido` recortado
        # pelos rótulos da trilha, então `ambos ≤ consumido` em toda linha e
        # `ambos + fora` reconstrói o total. Violar isso significa que as duas
        # colunas do payload não vêm da mesma foto — e aí a barra "fora" desenha
        # um conjunto que não existe. O número quebrado FICA na tela (negativo
        # grita) e o bloco entra pelo nome no que não foi medido.
        quebrados = [l['t'] for l in linhas if l['fora'] < 0]
        if quebrados:
            logger.error('estoque: partição consumido = ambos + fora não fecha '
                         'em %s tribunais: %s', len(quebrados), quebrados[:10])
            faltando.append('Consumo fora da trilha, por tribunal '
                            f'(a partição não fecha em {len(quebrados)}: '
                            f'{", ".join(quebrados[:5])})')
    else:
        faltando.append('Quebra por tribunal')

    resultados = _resultados(p)
    if resultados is None:
        faltando.append('Resultado reportado pelo cliente')

    cruz = _cruzamento(p.get('cruzamento'))
    if cruz is None:
        faltando.append('Cruzamento estoque × consumo')

    hoje = _consumo_hoje(p)

    # O `so_estoque` aberto por rótulo. Publicado só se fechar com o
    # cruzamento — a conferência está em `_saldo_por_rotulo`.
    saldo = _saldo_por_rotulo(p, cruz, rotulos)
    if saldo is None and cruz is not None:
        faltando.append('O que sobra, por rótulo')

    # séries dos gráficos. Cliente sem cor validada FICA DE FORA do gráfico (e
    # a tela diz que ficou) — cor gerada na hora é a porta de entrada da
    # paleta padrão do ECharts, que já pintou um mapa inteiro de cinza aqui.
    topo = linhas[:TOPO_GRAFICO]
    com_cor = [i for i in (clientes['itens'] if clientes else []) if i['cor']]
    _pico = lambda l: max([l['cli'].get(i['cliente'], 0) for i in com_cor] or [0])
    topo_cliente = [l for l in sorted(linhas, key=lambda l: -_pico(l))
                    if _pico(l) > 0][:TOPO_GRAFICO]
    ctx = {
        'em': p.get('em') or '',
        'segundos': _num(p.get('segundos_varredura')),
        'estoque': estoque,
        'distintos': distintos,
        'registros': registros,
        'reconsumo': reconsumo,
        'razao_consumo': razao_consumo,
        'composicao': composicao,
        'rotulos_txt': ' + '.join(str(r) for r in rotulos),
        'nao_classificados': _num(p.get('estoque_nao_classificados')),
        'total_processos': _num(p.get('estoque_total_processos')),
        'linhas': linhas,
        'colunas_cliente': colunas,
        'clientes': clientes,
        'resultados': resultados,
        'cruzamento': cruz,
        'consumo_hoje': hoje,
        'saldo': saldo,
        'topo_grafico': TOPO_GRAFICO,
        'tem_mais_tribunais': len(linhas) > TOPO_GRAFICO,
        # `ambos` e `fora` são as séries desenhadas; `consumido` viaja junto
        # porque o tooltip mostra o total, mas ele NÃO é barra: barra ao lado do
        # estoque é comparação, e o total não passa pela régua da trilha.
        'g_tribunais': [{'t': l['t'], 'estoque': l['estoque'],
                         'ambos': l['ambos'], 'fora': l['fora'],
                         'consumido': l['consumido']}
                        for l in topo],
        # O gráfico de clientes recorta pelo CONSUMO, não pelo estoque. Medido
        # em prod: o consumo se concentra em TRF1/TRF3, e recortar pelos
        # maiores estoques deixava 11 das 15 linhas vazias — meia tela gasta
        # dizendo "zero" e as barras que importam espremidas no topo.
        'g_clientes': ([{'t': l['t'], **{i['cliente']: l['cli'].get(i['cliente'], 0)
                                         for i in com_cor}}
                        for l in topo_cliente]
                       if com_cor and topo_cliente else []),
        'series_clientes': [{'chave': i['cliente'], 'cor': i['cor'],
                             'rotulo': i['cliente']} for i in com_cor],
    }

    vistos, nao_medidos = set(), []
    for nome in faltando:
        if nome not in vistos:
            vistos.add(nome)
            nao_medidos.append(nome)
    return ctx, nao_medidos


# --------------------------------------------------------------------------
# view
# --------------------------------------------------------------------------

@login_required
@require_GET
def estoque(request):
    """GET /dashboard/estoque/?trilha=precatorio|direito_creditorio

    Só lê cache. Sem cache, a página CARREGA e diz que ainda não mediu — o que
    não pode acontecer é ela quebrar, nem inventar zero.
    """
    trilha = (request.GET.get('trilha') or '').strip()
    if trilha not in TRILHA_ROTULO:
        trilha = TRILHA_PADRAO

    bruto = _payload(trilha)
    dados, nao_medidos = _normalizar(bruto) if bruto else (None, [])

    return render(request, 'dashboard/estoque.html', {
        'trilha': trilha,
        'trilha_rotulo': TRILHA_ROTULO[trilha],
        'trilhas': TRILHAS,
        'dados': dados,
        'nao_medidos': nao_medidos,
        'resultados_meta': [(k, _rotulos_resultado().get(k, k))
                            for k in RESULTADOS_DESTAQUE],
    })
