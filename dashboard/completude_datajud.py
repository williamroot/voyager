"""O lado da FONTE do Datajud, medido em rodadas — nunca contra número velho.

## O defeito que este arquivo existe para matar

Em 01/09/2026 o card do Datajud da tela de completude dizia:

    temos 344.630.543 · a fonte declara 343.235.554 · lacuna −1.394.989 · 100,4%

Uma lacuna NEGATIVA. E ela não vinha de nenhum achado sobre o acervo: vinha de
subtrair um número **vivo** (o nosso `_count` de hoje) de um número **congelado**
(o declarado, anotado em 14/08). Os dois lados cresceram desde então; só um deles
estava sendo relido. É a mesma família do defeito do gráfico de Estoque —
número do passado ao lado de número do presente, sem dizer qual é qual.

**A regra que fica: diferença só entre números medidos no MESMO instante.**
Um confronto é um par `(declarado, nosso)` do mesmo tribunal, colhido na mesma
rodada. O agregado é a soma dos pares, e a tela publica a JANELA de datas em que
os pares foram colhidos — não uma data única que fingiria simultaneidade.

## Por que rodada, e não uma varredura só

Medir o declarado custa 1 requisição por tribunal ao CNJ, e o relógio é da casa
do CNJ: medido em 01/09/2026, com a puxada nacional rodando em paralelo, foram
**~46 s por tribunal** (a cota `varredura` está disputada). Os 60 tribunais numa
tacada levariam ~45 min — mais que o `timeout` do job e muito mais que o
intervalo de 30 min do aquecimento.

Então cada passada gasta um ORÇAMENTO de tempo, mede quem está com a medição
mais VELHA e para. O estado vive no cache com validade longa, e o país inteiro
se renova em algumas horas. Cada tribunal carrega a data da própria medição.

## O denominador do CNJ tem linha vazia dentro (5,5 milhões delas)

Medido em 31/08/2026: 5.516.272 documentos declarados **não têm
`numeroProcesso`** — `classe: {codigo: "-1", nome: "Inválido"}`, `grau: null`,
`@timestamp` ausente. Sem CNJ eles não casam com nada e `doc_do_datajud` já os
descarta. Contá-los no denominador acusa buraco onde não há. Por isso a segunda
requisição (`must_not exists numeroProcesso`) — gasta-se ela **só** quando o
tribunal acusou diferença, que é quando ela muda alguma conclusão.

⚠️ `classe.codigo = -1` **não** serve como critério: é superconjunto (TJSP
5.408.140 contra 5.337.680) e engloba processo REAL com classe inválida.

## Sobra não é erro

Um tribunal pode ficar acima do declarado: o acervo se move enquanto se mede, e
o CNJ remove documento. O agregado publica `falta` e `sobra` **separadas**, nos
dois sentidos, em vez de uma subtração que se anula. Somar as duas daria zero e
esconderia as duas.
"""
import datetime
import logging
import time

from django.core.cache import cache
from django_rq import job

logger = logging.getLogger('voyager.completude')

#: Estado da régua: `{sigla: {declarado, invalidos, nosso, em, erro}}`.
CHAVE = 'completude:datajud:confronto:v1'

#: Trava de rodada. O aquecimento roda de 30 em 30 min e a rodada leva minutos;
#: sem trava, uma rodada lenta viraria fila de rodadas contra a API do CNJ.
CHAVE_LOCK = 'completude:datajud:rodando:v1'
LOCK_TTL = 60 * 25

#: 30 dias. O estado é a régua acumulada — perdê-lo joga fora rodadas já pagas
#: ao CNJ. (O Redis de prod ganhou AOF em 31/08; antes disso todo restart
#: apagava tudo — ver .ia/OPS.md.)
TTL_ESTADO = 60 * 60 * 24 * 30

#: Teto de tempo de UMA passada. Regra nº 7: nada sem teto de espera.
#:
#: ⚠️ É teto do LAÇO, não da requisição: o orçamento é conferido ANTES de cada
#: tribunal, e um `_post` sozinho pode passar disso (o `acquire_varredura`
#: espera até 120 s por token e o cliente ainda rotaciona proxy). Por isso esta
#: medição tem JOB PRÓPRIO — no mesmo job do aquecimento ela atrasaria a parte
#: barata, que é a que a tela lê.
ORCAMENTO_S = 180

#: Acima disso a medição do tribunal é considerada velha e entra na fila da
#: próxima rodada. 12 h mantém o país inteiro renovado em ~1 dia.
IDADE_MAX_H = 12

#: Teto de espera do ES nas contagens por tribunal.
ES_TIMEOUT = 30


def _agora() -> datetime.datetime:
    return datetime.datetime.now()


def _idade_h(em) -> float:
    if not isinstance(em, datetime.datetime):
        return float('inf')
    return (_agora() - em).total_seconds() / 3600.0


def _medir_tribunal(cli, es, acervo, sigla: str) -> dict:
    """Um par `(declarado, nosso)` do MESMO instante. Nunca levanta.

    `erro` preenchido = tribunal sem fonte (índice inexistente no CNJ, cota
    estourada, timeout). Ele fica de FORA da régua e aparece pelo nome na tela:
    régua que encolhe em silêncio é régua que mente.
    """
    linha = {'declarado': None, 'invalidos': None, 'nosso': None,
             'em': _agora(), 'erro': None}
    try:
        d = cli._post(sigla, {'size': 0, 'track_total_hits': True,
                              'query': {'match_all': {}}}, cota='varredura')
        linha['declarado'] = int(d['hits']['total']['value'])
    except Exception as exc:  # noqa: BLE001 — índice inexistente não é falha nossa
        linha['erro'] = str(exc)[:120]
        return linha

    try:
        linha['nosso'] = int(es.options(request_timeout=ES_TIMEOUT).count(
            index=acervo, query={'term': {'tribunal': sigla}})['count'])
    except Exception as exc:  # noqa: BLE001
        linha['erro'] = f'nosso: {str(exc)[:110]}'
        return linha

    # A requisição extra só se paga quando há diferença para explicar.
    if linha['declarado'] > linha['nosso']:
        try:
            d = cli._post(sigla, {'size': 0, 'track_total_hits': True, 'query': {
                'bool': {'must_not': [{'exists': {'field': 'numeroProcesso'}}]}}},
                cota='varredura')
            linha['invalidos'] = int(d['hits']['total']['value'])
        except Exception:  # noqa: BLE001
            # `None` de propósito: descontar um número que não se mediu seria
            # inventar completude. O agregado trata None como "não descontado".
            linha['invalidos'] = None
    else:
        linha['invalidos'] = 0
    return linha


def medir_rodada(orcamento_s: int = ORCAMENTO_S,
                 idade_max_h: float = IDADE_MAX_H) -> dict:
    """Mede os tribunais com a medição mais VELHA até o orçamento acabar.

    Devolve o estado inteiro (o acumulado, não só o desta rodada). Nunca
    levanta — o aquecimento não pode morrer por causa da API do CNJ.
    """
    estado = cache.get(CHAVE) or {}
    try:
        from datajud.client import DatajudClient
        from search.client import get_es, index_name
        from tribunals.models import Tribunal

        siglas = list(Tribunal.objects.order_by('sigla')
                      .values_list('sigla', flat=True))
        cli = DatajudClient(prefer_cortex=False)
        es = get_es()
        acervo = index_name('acervo')
    except Exception:  # noqa: BLE001
        logger.warning('completude/datajud: não deu pra montar a régua', exc_info=True)
        return estado

    # mais velho primeiro; nunca medido vem antes de tudo
    fila = sorted(siglas, key=lambda s: -_idade_h((estado.get(s) or {}).get('em')))
    t0 = time.monotonic()
    medidos = 0
    for sigla in fila:
        if time.monotonic() - t0 >= orcamento_s:
            break
        if _idade_h((estado.get(sigla) or {}).get('em')) < idade_max_h:
            break                      # a fila está ordenada: o resto é novo
        estado[sigla] = _medir_tribunal(cli, es, acervo, sigla)
        medidos += 1

    estado['_rodada'] = {'em': _agora(), 'medidos': medidos,
                         'orcamento_s': orcamento_s,
                         # quantos tribunais a régua PRECISA ter para fechar.
                         # Sem isto não dá pra distinguir "régua completa" de
                         # "régua que ainda só tem 10 dos 60" — e uma régua
                         # parcial publicada como total diria que a fonte
                         # declara 37 milhões.
                         'esperado': len(siglas),
                         'dt_s': round(time.monotonic() - t0, 1)}
    cache.set(CHAVE, estado, timeout=TTL_ESTADO)
    logger.info('completude/datajud: %d tribunais medidos em %.0fs',
                medidos, time.monotonic() - t0)
    return estado


def precisa_rodada(estado: dict | None = None) -> bool:
    """Há tribunal sem medição ou com medição velha? `True` = vale gastar rede.

    Sem esta pergunta, a rodada rodaria a cada aquecimento e bateria na API do
    CNJ 48 vezes por dia para reconfirmar número que não mudou.
    """
    estado = cache.get(CHAVE) if estado is None else estado
    if not estado or '_rodada' not in estado:
        return True
    esperado = (estado.get('_rodada') or {}).get('esperado') or 0
    pares = [v for k, v in estado.items() if k != '_rodada' and isinstance(v, dict)]
    if len(pares) < esperado or not pares:
        return True
    return max(_idade_h(v.get('em')) for v in pares) >= IDADE_MAX_H


def agendar_rodada() -> bool:
    """Enfileira a rodada como JOB próprio. Nunca mede aqui dentro.

    Chamado pelo aquecimento da tela, que roda a cada 30 min e **não pode**
    ficar minutos preso na API do CNJ — a parte barata da medição é a que a
    tela lê. A trava impede fila de rodadas; ela expira sozinha, então rodada
    que morrer no meio não trava o país para sempre.
    """
    try:
        if cache.get(CHAVE_LOCK) or not precisa_rodada():
            return False
        cache.set(CHAVE_LOCK, 1, timeout=LOCK_TTL)
        warm_completude_datajud.delay()
        return True
    except Exception:  # noqa: BLE001 — agendar é melhor-esforço
        logger.warning('completude/datajud: não deu pra agendar a rodada',
                       exc_info=True)
        return False


@job('default', timeout=1800)
def warm_completude_datajud() -> dict:
    """Uma rodada do confronto. Nunca propaga erro, nunca escreve na tela.

    Só atualiza o ESTADO (`CHAVE`). Quem publica é o `warm_completude`, que só
    agrega o que já está medido — sem tocar a rede.
    """
    try:
        est = medir_rodada()
        return {'ok': True, 'rodada': (est or {}).get('_rodada')}
    finally:
        # solta a trava mesmo se a rodada estourar: trava que só expira por TTL
        # transforma um erro de 1 rodada em 25 min de país sem remedição
        cache.delete(CHAVE_LOCK)


def agregar(estado: dict | None) -> dict | None:
    """Soma os pares. `None` quando não há par nenhum — abster > chutar.

    O agregado NÃO é um número com uma data: é uma soma de pares colhidos entre
    `de` e `ate`. A tela mostra a janela, e é ela que diz o quanto o confronto
    envelheceu.
    """
    estado = estado or {}
    esperado = (estado.get('_rodada') or {}).get('esperado')
    linhas = [(s, v) for s, v in estado.items()
              if s != '_rodada' and isinstance(v, dict)]
    validos = [(s, v) for s, v in linhas
               if v.get('erro') is None and isinstance(v.get('declarado'), int)
               and isinstance(v.get('nosso'), int)]
    if not validos:
        return None

    declarado = sum(v['declarado'] for _, v in validos)
    nosso = sum(v['nosso'] for _, v in validos)
    # `invalidos=None` significa "não medido", e não zero. Ele NÃO entra no
    # desconto, e o tribunal é nomeado — descontar o que não se mediu seria
    # inventar completude; calar seria pior.
    sem_invalidos = sorted(s for s, v in validos if v.get('invalidos') is None)
    invalidos = sum(v['invalidos'] or 0 for _, v in validos)
    util = declarado - invalidos

    # Falta e sobra POR TRIBUNAL e nos dois sentidos. O líquido nacional
    # esconderia as duas: sobra de um cobre falta de outro.
    falta = sum(max(0, (v['declarado'] - (v['invalidos'] or 0)) - v['nosso'])
                for _, v in validos)
    sobra = sum(max(0, v['nosso'] - (v['declarado'] - (v['invalidos'] or 0)))
                for _, v in validos)
    datas = [v['em'] for _, v in validos if isinstance(v.get('em'), datetime.datetime)]
    return {
        # PARCIAL enquanto a régua não cobre todos os tribunais. Publicar uma
        # régua de 10 tribunais como se fosse o país diria "a fonte declara 37
        # milhões" ao lado de "temos 344 milhões" — pior que não publicar.
        'parcial': not esperado or len(linhas) < esperado,
        'medidos': len(linhas),
        'esperado': esperado,
        'tribunais': len(validos),
        'tribunais_total': len(linhas),
        'sem_fonte': sorted(s for s, v in linhas if v.get('erro')),
        'sem_invalidos': sem_invalidos,
        'declarado': declarado,
        'invalidos': invalidos,
        'util': util,
        'nosso': nosso,
        'falta': falta,
        'sobra': sobra,
        'pct': (100.0 * nosso / util) if util else None,
        'de': min(datas) if datas else None,
        'ate': max(datas) if datas else None,
        'idade_h': round(_idade_h(min(datas)), 1) if datas else None,
    }
