"""Estoque × consumo — o que marcamos, e o que o cliente já levou.

Duas contagens que parecem a mesma pergunta e não são:

  estoque    `tribunals_process.classificacao` — o rótulo de HOJE, do
             classificador que roda continuamente sobre o acervo inteiro
  consumo    `tribunals_leadconsumption` — o registro HISTÓRICO de que um
             cliente puxou aquele processo pela API de leads

Este módulo mede as duas e mede o **cruzamento** entre elas. Ele não faz a
subtração, e a razão está abaixo.

## Por que `estoque − consumido` não é saldo

Medido em 01/09/2026: o consumo distinto (811.360 processos) é **14,7×** o
estoque de `PRECATORIO` (55.285). A subtração dá −756.075, e esse número não
significa dívida nenhuma — significa que os conjuntos não se subtraem: o
consumo é histórico e cumulativo, a classificação é o rótulo de agora, e um
processo consumido em 2025 pode ter sido reclassificado desde então (o
`F30_extinto_neg_ANTI`, por exemplo, rebaixa lead a `NAO_LEAD` quando lê
desfecho terminal negativo).

O que substitui a subtração é a partição medida, em `cruzamento`:

    ambos       está no estoque da trilha E foi consumido
    so_estoque  marcado e ninguém puxou     (a oferta que sobra)
    so_consumo  puxado e hoje não está nesta trilha

`so_consumo` é a fatia que faria a subtração dar negativo. Ela é publicada com
nome, número e explicação — não escondida atrás de um `max(x, 0)`.

E a partição responde o que a subtração jamais responderia. Trilha
`precatorio` em 01/09/2026:

    ambos ....... 541.185   já marcado e já consumido
    so_estoque .. 395.570   marcado e nunca consumido  ← o estoque que resta
    so_consumo .. 270.175   consumido e hoje fora da trilha

O `−756.075` da subtração não é saldo nenhum; o saldo real é **395.570** — e
dele só **4.652** são `PRECATORIO` (91,6% desse rótulo já foi puxado), contra
390.918 de `PRE_PRECATORIO`.

## Três armadilhas de contagem

1. **`LeadConsumption` não tem unique constraint** — re-consumo cria registro
   novo (está na docstring do model). 1.224.278 registros para 811.360
   processos distintos. Todo campo do payload diz qual dos dois é: `consumos`
   é registro, `consumido_distinto` é processo.

2. **Os dois clientes NÃO se somam.** Medido em 01/09/2026: `juriscope` tem
   405.740 processos distintos e **todos os 405.740 também estão no `falcon`**
   (interseção 405.740; exclusivos do juriscope: **0**). São fases do mesmo
   consumidor — o juriscope parou em 2026-05-03, o falcon começou em
   2026-05-17. Somar os dois dá 1.217.100 e conta 405.740 processos duas
   vezes. Por isso o payload traz os dois separados, a UNIÃO distinta
   explícita, e a sobreposição medida em `consumo_clientes`.

3. **Resultado é por REGISTRO, não por processo.** `validado + pendente +
   sem_expedicao + …` fecha em 1.224.278, o total de registros — não em
   811.360. O payload diz isso em `consumo_resultado_unidade`.

## Campos de controle

Cada bloco carrega um número que TEM que dar 100%. Se não der, o bloco sai da
tela com o motivo — meia régua é pior que régua nenhuma.

  catálogo   todo valor de `classificacao` não-nulo cai num dos quatro rótulos
             declarados no model. `classificacao` é `CharField` sem CHECK no
             banco, e a casa já perdeu contagem para casing legado (982
             `LeadConsumption.resultado='VALIDADO'` corrigidos em 2026-05-18).
             Um rótulo desconhecido sumiria de todas as trilhas em silêncio.

  join       a varredura conta o consumo pela junção com `tribunals_process`;
             `_consumo()` conta o mesmo direto na tabela, SEM join. São duas
             fontes independentes do mesmo número, nas duas unidades
             (processos distintos e registros). Se o join perder linha — FK
             órfã, filtro escondido, tribunal fora do catálogo —, o controle
             acusa e o cruzamento não é publicado.

## Custo

Tudo que depende do acervo sai de **uma varredura só**: `tribunals_process`
por (tribunal, classificação), com o consumo agregado por processo entrando
como lado de hash. O plano medido em 01/09/2026 é
`Parallel Seq Scan (104 M) ⋈ Hash (811 k)` — sem I/O aleatório e sem spill,
desde que o `work_mem` local seja levantado.

Os caminhos que PARECEM mais baratos e não são, medidos na mesma máquina:

  `GROUP BY tribunal_id, classificacao` (só estoque) ............  58 s
  o mesmo recorte pelo índice de `classificacao` ................ 135 s
  join a partir de `LeadConsumption` (811 k lookups no PK) ...... >400 s, abortado

O banco é I/O-bound: mandar o planner para o índice troca leitura sequencial
por leitura aleatória de heap e sai mais caro. Nada disso entra no caminho da
requisição (regra nº 7 do CLAUDE.md) — `aquecer()` roda no scheduler, `ler()`
só lê cache.
"""
import logging
import time

from django.core.cache import cache
from django.db import connection, transaction
from django.utils import timezone

logger = logging.getLogger('voyager.dashboard.estoque')

#: Base da chave. A chave real é `f'{CHAVE}:{trilha}'` — um payload por
#: trilha, como o contrato pede, mas UMA varredura para as duas.
CHAVE = 'estoque:v1'
TTL = 60 * 60 * 30          # 30 h: o warm roda de 6 em 6 h; o TTL só evita eternizar

#: Os rótulos que o model declara (`Process.CLASSIF_CHOICES`). Repetidos aqui
#: como CONTROLE, não como fonte: a varredura compara o que veio do banco com
#: esta lista e derruba o bloco se aparecer coisa fora dela.
ROTULOS = ('PRECATORIO', 'PRE_PRECATORIO', 'DIREITO_CREDITORIO', 'NAO_LEAD')

#: Composição de cada trilha.
#:
#: `precatorio` inclui `PRE_PRECATORIO` **de propósito**, e a justificativa é
#: medida, não estética:
#:
#: 1. A hierarquia do classificador é `PRECATORIO > PRE_PRECATORIO >
#:    DIREITO_CREDITORIO > NAO_LEAD` (`.ia/CLASSIFICACAO.md`), e os dois
#:    primeiros são o MESMO produto em estágios diferentes: N1 é "fila
#:    imediata pra baixar autos", N2 é "re-checar mensalmente". Quem compra
#:    crédito de precatório compra os dois.
#: 2. É o recorte que o próprio consumo confirma, medido em 01/09/2026. Dos
#:    811.360 processos já puxados, a classificação de hoje é:
#:
#:        PRE_PRECATORIO ....... 490.552   (de 881.470 no estoque — 55,7%)
#:        NAO_LEAD ............. 195.683
#:        DIREITO_CREDITORIO ...  74.492   (de 1.786.248 — 4,2%)
#:        PRECATORIO ...........  50.633   (de 55.285 — **91,6%**)
#:        não classificado .....       0
#:
#:    Com o N2, 541.185 (66,7%) do consumo cai dentro da trilha. Sem ele,
#:    50.633 (6,2%) — e os outros 93,8% virariam "consumido fora do estoque",
#:    fazendo a tela falar do artefato do recorte em vez do estoque.
#: 3. O payload publica `estoque_por_rotulo` com os dois números SEPARADOS e
#:    `rotulos` diz exatamente o que foi somado. Quem discordar da escolha
#:    refaz a conta sem refazer a medição — que é a condição para um recorte
#:    ser honesto.
TRILHAS = {
    'precatorio': ('PRECATORIO', 'PRE_PRECATORIO'),
    'direito_creditorio': ('DIREITO_CREDITORIO',),
}

#: Resultados que a tela mostra em destaque (o contrato do payload). Os demais
#: continuam em `consumo_por_resultado` — sumir com eles quebraria o controle,
#: que exige que a soma feche com o total de registros.
RESULTADOS_DESTAQUE = ('validado', 'pendente', 'sem_expedicao')

#: Tetos de espera. Generosos porque isto é job de aquecimento — mas EXISTEM, e
#: estourar um deles é ERRO registrado com o nome do bloco, não corte mudo
#: (regra nº 2).
TIMEOUT_VARREDURA = '900s'
TIMEOUT_CONSUMO = '120s'

#: `work_mem` local da varredura. Com os 32 MB do servidor o hash de 811 k
#: processos vai a disco (`DataFileWrite` na espera) e a consulta que deveria
#: durar ~1 min passa de 5. É `SET LOCAL`: vale só nesta transação.
WORK_MEM = '256MB'


# --------------------------------------------------------------------------
# blocos medidos
# --------------------------------------------------------------------------

def _consumo() -> dict:
    """Totais do consumo, medidos SEM join — a fonte independente do controle.

    Um `GROUPING SETS` numa passada da tabela pequena (1,22 M linhas): por
    cliente, por resultado, e o total geral. O total geral é a UNIÃO distinta,
    e é ele — não a soma dos clientes — que responde "quantos processos já
    foram consumidos".
    """
    with transaction.atomic(), connection.cursor() as c:
        c.execute('SET LOCAL statement_timeout = %s', [TIMEOUT_CONSUMO])
        c.execute("""
            SELECT cli.nome, cli.id, lc.resultado,
                   count(*) AS registros,
                   count(DISTINCT lc.processo_id) AS processos
              FROM tribunals_leadconsumption lc
              JOIN tribunals_apiclient cli ON cli.id = lc.cliente_id
             GROUP BY GROUPING SETS ((cli.nome, cli.id), (lc.resultado), ())
        """)
        linhas = c.fetchall()

    clientes: dict[str, int] = {}
    por_cliente: dict[str, dict[str, int]] = {}
    por_resultado: dict[str, int] = {}
    total_registros = total_distinto = 0
    for nome, cliente_id, resultado, registros, processos in linhas:
        if nome is not None:
            clientes[nome] = cliente_id
            por_cliente[nome] = {'registros': registros, 'processos': processos}
        elif resultado is not None:
            por_resultado[resultado] = registros
        else:
            total_registros, total_distinto = registros, processos

    return {
        'total_consumos': total_registros,
        'total_consumido_distinto': total_distinto,
        'clientes': clientes,
        'por_cliente': por_cliente,
        'por_resultado': por_resultado,
    }


def _varredura(clientes: dict[str, int], resultados: list[str]) -> dict:
    """UMA passada: estoque por (tribunal, classificação) **com** o consumo.

    O consumo entra agregado por processo (811 k linhas) como lado de hash de
    um `LEFT JOIN`; o lado que varre é `tribunals_process`. Assim o mesmo scan
    responde "quanto marcamos", "quanto disso foi consumido" e "o que os
    consumidos são hoje" — que é o cruzamento.

    Nomes de cliente e valores de resultado entram por PARÂMETRO. Os apelidos
    de coluna são fixos (`cli_0`, `res_0`, …) e o nome real volta pelo mapa em
    Python: nada de identificador vindo do banco interpolado em SQL.
    """
    nomes = list(clientes)
    externo = ['p.tribunal_id', 'p.classificacao', 'count(*) AS n',
               'count(d.processo_id) AS consumidos',
               'coalesce(sum(d.regs), 0) AS registros']
    interno = ['processo_id', 'count(*) AS regs']
    params: list = []
    for i, nome in enumerate(nomes):
        interno.append(f'count(*) FILTER (WHERE cliente_id = %s) AS cli_{i}')
        params.append(clientes[nome])
        externo.append(f'count(*) FILTER (WHERE d.cli_{i} > 0) AS o_cli_{i}')
    for i, valor in enumerate(resultados):
        interno.append(f'count(*) FILTER (WHERE resultado = %s) AS res_{i}')
        params.append(valor)
        externo.append(f'coalesce(sum(d.res_{i}), 0) AS o_res_{i}')

    sql = f"""
        SELECT {', '.join(externo)}
          FROM tribunals_process p
          LEFT JOIN (SELECT {', '.join(interno)}
                       FROM tribunals_leadconsumption
                      GROUP BY processo_id) d
            ON d.processo_id = p.id
         GROUP BY 1, 2
    """
    t0 = time.monotonic()
    with transaction.atomic(), connection.cursor() as c:
        c.execute('SET LOCAL statement_timeout = %s', [TIMEOUT_VARREDURA])
        c.execute('SET LOCAL work_mem = %s', [WORK_MEM])
        c.execute(sql, params)
        linhas = c.fetchall()
    segundos = round(time.monotonic() - t0, 1)

    n_cli, n_res = len(nomes), len(resultados)
    por_tribunal: dict[str, dict] = {}
    estoque_por_rotulo: dict[str, int] = {}
    consumido_por_rotulo: dict[str | None, int] = {}
    fora_do_catalogo: dict[str, int] = {}
    total_processos = classificados = 0
    consumido_distinto = consumos = 0

    for linha in linhas:
        tribunal, rotulo, n, consumidos, registros = linha[:5]
        cli = linha[5:5 + n_cli]
        res = linha[5 + n_cli:5 + n_cli + n_res]

        total_processos += n
        consumido_distinto += consumidos
        consumos += int(registros or 0)
        if rotulo is not None:
            classificados += n
            estoque_por_rotulo[rotulo] = estoque_por_rotulo.get(rotulo, 0) + n
            if rotulo not in ROTULOS:
                fora_do_catalogo[rotulo] = fora_do_catalogo.get(rotulo, 0) + n
        if consumidos:
            consumido_por_rotulo[rotulo] = consumido_por_rotulo.get(rotulo, 0) + consumidos

        t = por_tribunal.setdefault(tribunal, {
            'estoque_por_rotulo': {}, 'consumido_por_rotulo': {},
            'consumido': 0, 'consumos': 0,
            'por_cliente': dict.fromkeys(nomes, 0),
            'por_resultado': dict.fromkeys(resultados, 0),
        })
        t['estoque_por_rotulo'][rotulo] = t['estoque_por_rotulo'].get(rotulo, 0) + n
        t['consumido'] += consumidos
        t['consumos'] += int(registros or 0)
        if consumidos:
            t['consumido_por_rotulo'][rotulo] = (
                t['consumido_por_rotulo'].get(rotulo, 0) + consumidos)
        for i, nome in enumerate(nomes):
            t['por_cliente'][nome] += int(cli[i] or 0)
        for i, valor in enumerate(resultados):
            t['por_resultado'][valor] += int(res[i] or 0)

    # CONTROLE do catálogo: 100% do que está classificado usa um rótulo do
    # model. Um valor fora dele não apareceria em trilha nenhuma — sumiria
    # calado, que é exatamente o modo de falhar que esta tela existe pra evitar.
    if fora_do_catalogo:
        logger.error('estoque: rótulos fora do catálogo do model: %s', fora_do_catalogo)
        raise ValueError(
            f'campo de controle do catálogo: {sum(fora_do_catalogo.values())} '
            f'processos com classificação fora de {list(ROTULOS)} '
            f'({sorted(fora_do_catalogo)}) — a régua não cobre o catálogo inteiro')

    return {
        'por_tribunal': por_tribunal,
        'estoque_por_rotulo': estoque_por_rotulo,
        'consumido_por_rotulo': consumido_por_rotulo,
        'total_processos': total_processos,
        'nao_classificados': total_processos - classificados,
        'consumido_distinto': consumido_distinto,
        'consumos': consumos,
        'clientes': nomes,
        'resultados': list(resultados),
        'segundos': segundos,
    }


def _conferir(varredura: dict, consumo: dict) -> str | None:
    """CONTROLE do join. Devolve o motivo da reprovação, ou `None` se passou.

    Dois números medidos por caminhos independentes: a varredura conta o
    consumo DEPOIS de juntar com `tribunals_process`; `_consumo()` conta antes,
    direto na tabela. Têm que bater nas duas unidades.
    """
    problemas = []
    for rotulo, na_varredura, na_tabela in (
        ('processos distintos', varredura['consumido_distinto'],
         consumo['total_consumido_distinto']),
        ('registros', varredura['consumos'], consumo['total_consumos']),
    ):
        if na_varredura != na_tabela:
            problemas.append(f'{rotulo}: o join somou {na_varredura} e a tabela '
                             f'diz {na_tabela} (diferença {na_varredura - na_tabela})')
    if not problemas:
        return None
    return ('campo de controle do join falhou — ' + '; '.join(problemas) +
            '. A régua perdeu linha e o bloco não é publicável.')


# --------------------------------------------------------------------------
# montagem do payload
# --------------------------------------------------------------------------

def _mil(n) -> str:
    """`811360` → `811.360`. Aqui e não no template: o projeto não instala
    `django.contrib.humanize` e ligar um app por um separador é desproporcional.
    """
    try:
        return f'{int(n):,}'.replace(',', '.')
    except (TypeError, ValueError):
        return str(n)


def _nota_cruzamento(trilha, rotulos, ambos, so_estoque, so_consumo,
                     consumido_por_rotulo) -> str:
    """A explicação do achado, escrita a partir dos NÚMEROS medidos agora.

    Texto fixo envelhece e vira folclore; este se recalcula a cada aquecimento,
    então nunca descreve um mundo que já mudou.
    """
    total_consumo = ambos + so_consumo
    pct = (100.0 * ambos / total_consumo) if total_consumo else 0.0
    fora = sorted(((r or '(não classificado)', n)
                   for r, n in consumido_por_rotulo.items() if r not in rotulos),
                  key=lambda kv: -kv[1])[:3]
    detalhe = ' · '.join(f'{r} {_mil(n)}' for r, n in fora) or 'nenhum'
    return (
        f'Dos {_mil(total_consumo)} processos distintos já consumidos, '
        f'{_mil(ambos)} ({pct:.1f}%) estão hoje na trilha {trilha} '
        f'({"+".join(rotulos)}) e {_mil(so_consumo)} NÃO estão — hoje eles são '
        f'{detalhe}. Outros {_mil(so_estoque)} estão marcados e nunca foram '
        f'consumidos. Consumo é histórico e cumulativo; classificação é o '
        f'rótulo de agora. Por isso "estoque − consumido" não é saldo: a '
        f'resposta são estas três fatias, que somam '
        f'{_mil(ambos + so_estoque + so_consumo)}.'
    )


def _clientes(consumo: dict, nomes: list[str]) -> dict:
    """Os clientes SEPARADOS, com a sobreposição medida — nunca uma soma muda.

    `falcon` e `juriscope` não são consumidores paralelos: em 01/09/2026 os
    405.740 processos do juriscope estavam TODOS no falcon. A soma dá 1.217.100
    e conta 405.740 duas vezes. Aqui a soma aparece rotulada como soma, ao lado
    da união distinta; a diferença entre elas é o tamanho da sobreposição, que
    é justamente a informação que uma soma silenciosa apagaria.
    """
    itens = [{'cliente': nome,
              'registros': (consumo['por_cliente'].get(nome) or {}).get('registros', 0),
              'processos': (consumo['por_cliente'].get(nome) or {}).get('processos', 0)}
             for nome in nomes]
    soma = sum(i['processos'] for i in itens)
    uniao = consumo['total_consumido_distinto']
    return {
        'itens': itens,
        'soma_dos_clientes': soma,
        'uniao_distinta': uniao,
        'sobreposicao': soma - uniao,
        'nota': (f'{_mil(soma)} é a SOMA por cliente e {_mil(uniao)} é a UNIÃO '
                 f'distinta: {_mil(soma - uniao)} processos foram consumidos '
                 f'por mais de um cliente e apareceriam duas vezes na soma.'),
    }


def _montar(trilha: str, varredura: dict | None, consumo: dict | None,
            falhas: list[str]) -> dict:
    rotulos = TRILHAS[trilha]
    payload = {
        'em': timezone.now().isoformat(),
        'nao_medidos': list(falhas),
        'trilha': trilha,
        'rotulos': list(rotulos),
    }

    if consumo:
        payload['total_consumos'] = consumo['total_consumos']
        payload['total_consumido_distinto'] = consumo['total_consumido_distinto']
        payload['consumo_por_resultado'] = dict(consumo['por_resultado'])
        payload['consumo_resultado_unidade'] = 'registros'
        payload['consumo_clientes'] = _clientes(consumo, sorted(consumo['clientes']))

    if not varredura:
        return payload

    por_rotulo = {r: varredura['estoque_por_rotulo'].get(r, 0) for r in rotulos}
    payload['total_estoque'] = sum(por_rotulo.values())
    payload['estoque_por_rotulo'] = por_rotulo
    payload['estoque_nao_classificados'] = varredura['nao_classificados']
    payload['estoque_total_processos'] = varredura['total_processos']
    payload['segundos_varredura'] = varredura['segundos']

    linhas = []
    for sigla, t in varredura['por_tribunal'].items():
        linhas.append({
            't': sigla,
            'estoque': sum(t['estoque_por_rotulo'].get(r, 0) for r in rotulos),
            'consumido': t['consumido'],
            'consumos': t['consumos'],
            'ambos': sum(t['consumido_por_rotulo'].get(r, 0) for r in rotulos),
            'por_cliente': dict(t['por_cliente']),
            'resultado': {k: t['por_resultado'].get(k, 0) for k in RESULTADOS_DESTAQUE},
        })
    # Tribunal com consumo e sem estoque na trilha CONTINUA na lista: é ele que
    # denuncia a distorção que a subtração esconderia.
    linhas.sort(key=lambda l: (-l['estoque'], -l['consumido'], l['t']))
    payload['por_tribunal'] = linhas

    ambos = sum(varredura['consumido_por_rotulo'].get(r, 0) for r in rotulos)
    so_estoque = payload['total_estoque'] - ambos
    so_consumo = varredura['consumido_distinto'] - ambos
    payload['cruzamento'] = {
        'ambos': ambos,
        'so_estoque': so_estoque,
        'so_consumo': so_consumo,
        'nota': _nota_cruzamento(trilha, rotulos, ambos, so_estoque, so_consumo,
                                 varredura['consumido_por_rotulo']),
    }
    payload['consumo_por_classificacao_atual'] = {
        (r or '(não classificado)'): n
        for r, n in sorted(varredura['consumido_por_rotulo'].items(),
                           key=lambda kv: -kv[1])
    }
    return payload


def calcular() -> dict[str, dict]:
    """Mede uma vez e devolve `{trilha: payload}`.

    Bloco que falhou sai da lista COM O NOME e o motivo — o payload continua
    sem ele, e `nao_medidos` diz o quê e por quê. Meia régua é pior que régua
    nenhuma, mas régua parcial ANUNCIADA é honesta.
    """
    from tribunals.models import LeadConsumption

    falhas: list[str] = []

    try:
        consumo = _consumo()
    except Exception as exc:
        logger.error('estoque: não consegui medir o consumo', exc_info=True)
        falhas.append(f'consumo (totais e clientes): {exc}')
        consumo = None

    varredura = None
    if consumo:
        try:
            varredura = _varredura(
                consumo['clientes'], [v for v, _ in LeadConsumption.RESULTADO_CHOICES])
        except Exception as exc:
            logger.error('estoque: não consegui medir a varredura', exc_info=True)
            falhas.append(f'estoque classificado e cruzamento: {exc}')
    else:
        # Sem os clientes não há como montar a varredura: ela precisa dos ids.
        falhas.append('estoque classificado e cruzamento: depende do bloco de '
                      'consumo, que não foi medido')

    if varredura and consumo:
        motivo = _conferir(varredura, consumo)
        if motivo:
            logger.error('estoque: %s', motivo)
            falhas.append(f'estoque classificado e cruzamento: {motivo}')
            varredura = None

    return {t: _montar(t, varredura, consumo, falhas) for t in TRILHAS}


def aquecer() -> dict[str, dict] | None:
    """Job de aquecimento — chamado pelo scheduler. Nunca levanta."""
    try:
        payloads = calcular()
    except Exception:
        logger.error('estoque: aquecimento falhou', exc_info=True)
        return None
    for trilha, p in payloads.items():
        cache.set(f'{CHAVE}:{trilha}', p, TTL)
    ref = payloads.get('precatorio') or {}
    logger.info('estoque: precatório %s marcados · %s consumidos distintos em '
                '%s registros · varredura %ss · não medidos: %s',
                ref.get('total_estoque'), ref.get('total_consumido_distinto'),
                ref.get('total_consumos'), ref.get('segundos_varredura'),
                ref.get('nao_medidos') or 'nenhum')
    return payloads


def ler(trilha: str = 'precatorio'):
    """O que a TELA usa. Só cache — a varredura custa minutos (regra nº 7)."""
    if trilha not in TRILHAS:
        return None
    return cache.get(f'{CHAVE}:{trilha}')
