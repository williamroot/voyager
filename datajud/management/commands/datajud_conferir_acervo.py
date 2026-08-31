"""Dry-run e gate de completude da puxada: o CNJ declara × o que temos.

    # A MEDIÇÃO QUE EMBASA A DECISÃO (não escreve nada, 1 requisição/tribunal)
    datajud_conferir_acervo

    # a mesma coisa em JSON, pra colar em issue
    datajud_conferir_acervo --json

    # devolve pra fila quem está incompleto (ESCREVE: enfileira jobs)
    datajud_conferir_acervo --enfileirar

Por que existe: `status='fim'` diz que o LAÇO terminou, não que o acervo está
completo. Um job pode terminar cedo por erro tratado, por cota, por um
milissegundo que não coube no fatiamento — ou porque alguém apertou o kill
switch. A única resposta honesta é contar dos DOIS lados: o total declarado
pelo tribunal ao CNJ e o nosso `_count` no `voyager-acervo`.

O total declarado é lido AO VIVO (1 requisição por tribunal, `size: 0`, ~190
bytes de resposta): ele cresce todo dia, e comparar com um número anotado ontem
daria falso incompleto.

TRÊS COISAS QUE ESTA TABELA NÃO DIZ, e é importante que não digam:

1. **`voyager-acervo` conta DOCUMENTOS, não processos.** O mesmo CNJ é um doc
   em G1 e outro em G2; medido em 31/08/2026, a razão nacional é **1,155
   doc/CNJ** (344.603.487 docs para 298.271.660 CNJs distintos).
2. **O esqueleto não é o processo.** O `_source` do Datajud não tem **parte,
   advogado nem valor** — a coluna `processos` mostra quantos daqueles CNJs
   viraram processo de verdade no `voyager-processos`. Contar esqueleto como
   "temos" seria dado coletado pela metade, que vale menos que zero.
3. **Sobra não é erro.** O acervo se move enquanto varremos: um tribunal pode
   ficar alguns milhares ACIMA do declarado, e foi assim que o TRT20 fechou
   235.758 contra 235.754 — completude, não defeito. Por isso a divergência é
   mostrada nas duas direções.

O DENOMINADOR DO CNJ TEM LIXO DENTRO, e sem descontá-lo esta tabela acusa
buraco onde não há (medido em 31/08/2026):

    TJSP  declarado 74.686.714 · sem `numeroProcesso` 5.337.680 (7,15%)
    TJMG  declarado 36.698.417 · sem `numeroProcesso`    20.313
    TJRJ  declarado 23.152.022 · sem `numeroProcesso`    87.341

São documentos com `numeroProcesso: null`, `classe: {codigo: "-1", nome:
"Inválido"}`, `grau: null` — 200 de 200 numa amostra do TJSP. Sem CNJ eles não
casam com nada, e `doc_do_datajud` já os descarta. E há um detalhe que fecha o
argumento: o conjunto "sem `numeroProcesso`" é EXATAMENTE o conjunto "sem
`@timestamp`" (5.337.680 = 5.337.680 nos três tribunais conferidos) — sem chave
de ordenação, a varredura, que pagina por `range @timestamp`, **nunca poderia
alcançá-los**. Não é buraco nosso; é linha vazia do CNJ.

Por isso a coluna `inválidos` existe e o veredito usa o delta LÍQUIDO. Ela custa
1 requisição a mais, e só nos tribunais que acusaram diferença.

⚠️ `classe.codigo = -1` NÃO serve como critério: é um superconjunto (TJSP
5.408.140) que engloba processos REAIS com classe inválida. O critério é a
ausência de `numeroProcesso`.
"""
import json
import random
import time

import django_rq
from django.core.management.base import BaseCommand

from datajud.client import DatajudClient
from search.client import get_es, index_name
from tribunals.models import Tribunal

#: tolerância: abaixo disso o tribunal é considerado incompleto. Não é 100%
#: porque o acervo se move enquanto varremos — processo novo entra, e o nosso
#: número pode ficar acima OU abaixo do declarado por alguns milhares.
LIMIAR = 0.995

#: vazão SERIAL medida em produção (TJMG e TRT20, 14/08/2026): 1.232-1.458
#: docs/s. O ETA usa o pior dos dois — prometer o melhor caso num job de horas
#: é como não prometer nada.
DOCS_POR_S = 1232.0

#: `request_timeout` de toda leitura do ES. Nada no caminho sem teto de espera:
#: uma medição de rodapé sem timeout já derrubou o site (regra nº 7).
ES_TIMEOUT = 30


class Command(BaseCommand):
    help = 'Compara o acervo declarado ao CNJ com o que a varredura trouxe'

    def add_arguments(self, p):
        p.add_argument('--enfileirar', action='store_true',
                       help='devolve pra fila os tribunais incompletos (ESCREVE)')
        p.add_argument('--limiar', type=float, default=LIMIAR)
        p.add_argument('--docs-por-s', type=float, default=DOCS_POR_S,
                       help='vazão usada no ETA (default: a medida em produção)')
        p.add_argument('--conexoes', type=int, default=1,
                       help='conexões paralelas assumidas no ETA agregado')
        p.add_argument('--json', action='store_true', dest='como_json')
        p.add_argument('--tribunais', default='',
                       help='lista separada por vírgula (default: todos)')
        p.add_argument('--histograma', action='store_true',
                       help='compara o `@timestamp` mês a mês (fonte × nosso) e '
                            'diz QUAL janela varrer — 1 requisição por tribunal')

    def handle(self, *a, **o):
        cli = DatajudClient(prefer_cortex=False)
        es = get_es()
        acervo = index_name('acervo')
        processos = index_name('processos')

        siglas = ([s.strip().upper() for s in o['tribunais'].split(',') if s.strip()]
                  or list(Tribunal.objects.order_by('sigla')
                          .values_list('sigla', flat=True)))
        if o['histograma']:
            for sigla in siglas:
                self._histograma(cli, es, acervo, sigla)
            return
        estado_fila = self._estado_da_fila()

        # Imprime LINHA A LINHA, não no fim. Uma medição de 59 tribunais leva
        # ~25 min pelo pool de proxies, e a primeira versão deste comando perdeu
        # os 25 minutos inteiros quando a última consulta estourou o timeout:
        # run que morre não chega ao fim, e o que ele já sabia morreu junto.
        linhas = []
        if not o['como_json']:
            self._cabecalho()
        for sigla in siglas:
            linha = self._medir(cli, es, acervo, processos, sigla, estado_fila, o)
            linhas.append(linha)
            if not o['como_json']:
                self._linha(linha)

        controle = self._controle(es, acervo)
        if o['como_json']:
            self.stdout.write(json.dumps(
                {'controle': controle, 'tribunais': linhas},
                ensure_ascii=False, default=str))
        else:
            self._rodape(linhas, controle, o)

        if o['enfileirar']:
            self._enfileirar([x for x in linhas if x['estado'] == 'INCOMPLETO'])

    # -- medição ------------------------------------------------------------ #

    def _medir(self, cli, es, acervo, processos, sigla, estado_fila, o) -> dict:
        linha = {'tribunal': sigla, 'declarado': None, 'acervo': None,
                 'processos': None, 'delta': None, 'sobra': None,
                 'cobertura': None, 'eta_s': None, 'requisicoes': None,
                 'estado': None, 'erro': None}
        try:
            t0 = time.monotonic()
            d = cli._post(sigla, {'size': 0, 'track_total_hits': True,
                                  'query': {'match_all': {}}}, cota='varredura')
            linha['declarado'] = d['hits']['total']['value']
            linha['latencia_ms'] = int((time.monotonic() - t0) * 1000)
        except Exception as exc:      # noqa: BLE001 — índice inexistente não é falha nossa
            linha['erro'] = str(exc)[:120]
            linha['estado'] = 'SEM FONTE'
            return linha

        es_t = es.options(request_timeout=ES_TIMEOUT)
        linha['acervo'] = es_t.count(index=acervo,
                                     query={'term': {'tribunal': sigla}})['count']
        try:
            linha['processos'] = es_t.count(
                index=processos, query={'term': {'tribunal': sigla}})['count']
        except Exception:             # noqa: BLE001 — índice rico é opcional aqui
            linha['processos'] = None

        dec, nosso = linha['declarado'], linha['acervo']
        # divergência NOS DOIS SENTIDOS: falta é buraco, sobra é o acervo se
        # movendo durante a varredura (ou doc que o CNJ removeu depois)
        bruto = max(0, dec - nosso)
        linha['sobra'] = max(0, nosso - dec)
        # só gasta a requisição extra quando há diferença para explicar
        linha['invalidos'] = self._invalidos(cli, sigla) if bruto else 0
        linha['declarado_util'] = dec - (linha['invalidos'] or 0)
        linha['delta'] = max(0, bruto - (linha['invalidos'] or 0))
        linha['cobertura'] = ((nosso / linha['declarado_util'])
                              if linha['declarado_util'] else 0)
        linha['requisicoes'] = -(-linha['delta'] // 10_000)     # teto da página
        linha['eta_s'] = linha['delta'] / o['docs_por_s'] if linha['delta'] else 0
        linha['estado'] = (
            'rodando' if sigla in estado_fila['rodando']
            else 'na fila' if sigla in estado_fila['na_fila']
            else 'OK' if linha['cobertura'] >= o['limiar'] else 'INCOMPLETO')
        return linha

    def _invalidos(self, cli, sigla) -> int | None:
        """Linhas que o CNJ conta e que não são processo: sem `numeroProcesso`.

        `None` quando a contagem falhou — e aí o delta fica BRUTO, porque
        descontar um número que não se mediu seria inventar completude.
        """
        try:
            d = cli._post(sigla, {'size': 0, 'track_total_hits': True, 'query': {
                'bool': {'must_not': [{'exists': {'field': 'numeroProcesso'}}]}}},
                cota='varredura')
            return d['hits']['total']['value']
        except Exception:                                       # noqa: BLE001
            return None

    def _controle(self, es, acervo) -> dict:
        """CAMPO DE CONTROLE: `proc` tem que dar 100%, ou a régua é lixo.

        Duas medições, porque uma só mentiria:

        * `must_not exists proc` — pega o campo AUSENTE. Sozinho não basta:
          `exists` do ES conta string vazia como valor presente, então um
          `proc: ''` passaria por "presente" (regra nº 4);
        * uma amostra conferida no FORMATO do CNJ — é o que pega o vazio e o
          truncado. Campo `keyword` só se mede por amostra.

        A amostra sai de 4 FAIXAS ESTREITAS da chave, sorteadas — um prefixo de
        7 dígitos do número sequencial, que pega todos os anos e tribunais
        daquele número. Sem `sort`. Cada uma das três alternativas óbvias foi
        medida em produção, no índice de 344 M, e as três são piores:

        | como amostrar | custo | por quê não |
        |---|---|---|
        | `random_score` por `_seq_no` | barato | amostra a janela de ESCRITA, não o índice — a armadilha que fez uma medição do TJSP errar por 50× |
        | `random_score` por `proc` | **>30 s, derrubou a medição** | uniforme, mas custa um score por documento |
        | `range proc >= x` **com `sort: proc`** | **42 s** | ordenar 344 M por keyword frio lê doc_values do índice inteiro |
        | `range` de faixa estreita, **sem sort** | **147 ms** | ✔ |

        NUNCA levanta: um controle que derruba a régua não protege nada.
        """
        base = {'docs': None, 'sem_proc': None, 'amostra': 0,
                'amostra_valida': 0, 'ok': False, 'erro': None}
        es_t = es.options(request_timeout=ES_TIMEOUT)
        try:
            base['docs'] = es_t.count(index=acervo)['count']
            base['sem_proc'] = es_t.count(
                index=acervo,
                query={'bool': {'must_not': [{'exists': {'field': 'proc'}}]}})['count']
        except Exception as exc:                            # noqa: BLE001
            base['erro'] = f'contagem: {str(exc)[:120]}'
            return base

        # Faixa de 4 dígitos, não de 7: a faixa de 7 dígitos é um número
        # sequencial EXATO e volta vazia na maioria dos sorteios (medido: 3
        # documentos em 12 tentativas). A de 4 dígitos cobre 1.000 sequenciais
        # e entregou 50 de 50 em 5 sorteios de 5, entre 0,2 s e 8,5 s.
        rnd = random.Random(20260831)
        amostra = []
        for _ in range(8):
            if len(amostra) >= 200:
                break
            n = rnd.randrange(10_000)
            faixa = {'gte': f'{n:04d}000-', 'lt': f'{n:04d}999-'}
            try:
                r = es_t.search(index=acervo, size=50, source=['proc'],
                                query={'range': {'proc': faixa}})
                amostra += [h['_source'].get('proc') or '' for h in r['hits']['hits']]
            except Exception as exc:                        # noqa: BLE001
                # uma faixa lenta não pode zerar a amostra: anota e segue
                base['erro'] = f'amostra: {str(exc)[:120]}'
                continue

        base['amostra'] = len(amostra)
        base['amostra_valida'] = sum(
            1 for p in amostra if len(p) == 25 and p[7] == '-' and p[10] == '.')
        # amostra pequena demais não prova nada — 100% de 3 é 100% de nada
        base['ok'] = (base['sem_proc'] == 0 and len(amostra) >= 100
                      and base['amostra_valida'] == len(amostra))
        return base

    def _histograma(self, cli, es, acervo, sigla) -> None:
        """Mês a mês, `@timestamp` na fonte × `atualizado_em` no nosso índice.

        Existe porque o delta NÃO É ALCANÇÁVEL pelo watermark e um `--do-zero`
        custa o tribunal inteiro: no TJSP, 6.900 requisições e ~40 h para
        reencontrar 270.185 documentos. Comparando os buckets, o mesmo trabalho
        vira uma JANELA — 248 requisições e ~1,4 h para 69% do buraco.

        ⚠️ Só os meses RECENTES são comparáveis. O CNJ reescreve
        `dataHoraUltimaAtualizacao` em lote, e nos meses antigos os dois lados
        falam de documentos idênticos com carimbos diferentes (TJSP 2025-06:
        12.949.424 lá, 0 aqui — e não falta nada ali). Um mês em que os dois
        números batem prova que aquele trecho não foi reescrito; um mês em que
        a fonte tem MAIS, e os vizinhos batem, é buraco de verdade.
        """
        try:
            d = cli._post(sigla, {'size': 0, 'aggs': {'m': {'date_histogram': {
                'field': '@timestamp', 'calendar_interval': 'month'}}}},
                cota='varredura')
            fonte = {b['key_as_string'][:7]: b['doc_count']
                     for b in d['aggregations']['m']['buckets'] if b['doc_count']}
        except Exception as exc:                            # noqa: BLE001
            self.stdout.write(self.style.ERROR(f'{sigla}: {str(exc)[:120]}'))
            return
        r = es.options(request_timeout=120).search(
            index=acervo, size=0, query={'term': {'tribunal': sigla}},
            aggs={'m': {'date_histogram': {'field': 'atualizado_em',
                                           'calendar_interval': 'month'}}})
        nosso = {b['key_as_string'][:7]: b['doc_count']
                 for b in r['aggregations']['m']['buckets'] if b['doc_count']}

        meses = sorted(set(fonte) | set(nosso))
        # "batem" = os dois lados falam do mesmo trecho sem reescrita no meio
        batem = {m for m in meses
                 if fonte.get(m) and nosso.get(m)
                 and abs(fonte[m] - nosso[m]) / max(fonte[m], nosso[m]) < 0.01}

        def vizinhos_batem(mes):
            """Um bucket com a fonte à frente só é BURACO se os vizinhos batem.

            Sem esta regra, o TJSP acusaria 2025-11 (+13,1 M) como a janela mais
            valiosa — e ali não falta nada: é o carimbo de 14,2 M de documentos
            que nós temos em 2025-08 e que a fonte reescreveu. O sinal de
            reescrita é o vizinho: quando ele NÃO bate, os dois lados estão
            falando de documentos com carimbos diferentes, e a subtração não
            significa nada.
            """
            i = meses.index(mes)
            volta = [meses[j] for j in (i - 1, i + 1) if 0 <= j < len(meses)]
            return bool(volta) and all(m in batem for m in volta)

        self.stdout.write(self.style.MIGRATE_HEADING(f'\n▶ {sigla}'))
        self.stdout.write(f'{"mês":9}{"fonte":>13}{"nosso":>13}{"dif":>13}  veredito')
        alvos = []
        for mes in meses:
            f, n = fonte.get(mes, 0), nosso.get(mes, 0)
            dif = f - n
            if mes in batem:
                veredito = 'batem'
            elif dif > 0 and vizinhos_batem(mes):
                veredito = self.style.WARNING('BURACO → janela')
                alvos.append((mes, dif))
            elif dif > 0:
                veredito = 'carimbo reescrito (vizinho não bate) — ignorar'
            else:
                veredito = 'carimbo reescrito (nosso maior) — ignorar'
            self.stdout.write(f'{mes:9}{f:>13,}{n:>13,}{dif:>13,}  {veredito}')

        if not alvos:
            self.stdout.write(
                '\n  nenhum BURACO localizável: ou o tribunal está em dia, ou o '
                'que falta está espalhado por trechos que a fonte reescreveu — '
                'e aí só `--do-zero` alcança. Compare com o delta do gate antes '
                'de gastar a varredura completa.')
            return
        self.stdout.write('\n  janelas a varrer (não tocam o watermark):')
        for mes, dif in sorted(alvos, key=lambda x: -x[1]):
            ano, m = int(mes[:4]), int(mes[5:])
            prox = f'{ano + 1}-01-01' if m == 12 else f'{ano}-{m + 1:02d}-01'
            reqs = -(-fonte[mes] // 10_000)
            self.stdout.write(
                f'    manage.py datajud_varredura {sigla} --desde {mes}-01 '
                f'--ate {prox}    # +{dif:,} docs · ~{reqs:,} requisições')

    def _estado_da_fila(self) -> dict:
        from rq.registry import StartedJobRegistry
        try:
            fila = django_rq.get_queue('varredura')
            return {
                'rodando': {j.split(':')[-1]
                            for j in StartedJobRegistry(queue=fila).get_job_ids()},
                'na_fila': {j.split(':')[-1] for j in fila.get_job_ids()},
            }
        except Exception:             # noqa: BLE001 — sem Redis a tabela ainda vale
            return {'rodando': set(), 'na_fila': set()}

    # -- saída -------------------------------------------------------------- #

    def _cabecalho(self):
        self.stdout.write(
            f'{"trib":8}{"declarado":>13}{"inválidos":>11}{"acervo":>13}'
            f'{"delta":>11}{"sobra":>8}{"cob":>8}{"reqs":>7}{"ETA":>7}'
            f'{"processos":>12}  estado')

    def _linha(self, x):
        if x['declarado'] is None:
            self.stdout.write(self.style.ERROR(
                f'{x["tribunal"]:8}{"—":>13}{"—":>11}{"—":>13}{"—":>11}'
                f'{"—":>8}{"—":>8}{"—":>7}{"—":>7}{"—":>12}'
                f'  {x["estado"]}: {x["erro"]}'))
            return
        marca = '✔' if x['estado'] == 'OK' else ' '
        inval = x.get('invalidos')
        linha = (
            f'{x["tribunal"]:8}{x["declarado"]:>13,}'
            f'{("?" if inval is None else f"{inval:,}"):>11}'
            f'{x["acervo"]:>13,}{x["delta"]:>11,}{x["sobra"]:>8,}'
            f'{x["cobertura"]:>7.2%}{x["requisicoes"]:>7,}'
            f'{_dur(x["eta_s"]):>7}'
            f'{("—" if x["processos"] is None else format(x["processos"], ",")):>12}'
            f'  {marca} {x["estado"]}')
        self.stdout.write(
            self.style.WARNING(linha) if x['estado'] == 'INCOMPLETO' else linha)

    def _rodape(self, linhas, controle, o):
        dec = sum(x['declarado'] or 0 for x in linhas)
        inval = sum(x.get('invalidos') or 0 for x in linhas)
        util = dec - inval
        ace = sum(x['acervo'] or 0 for x in linhas)
        pro = sum(x['processos'] or 0 for x in linhas)
        delta = sum(x['delta'] or 0 for x in linhas)
        reqs = sum(x['requisicoes'] or 0 for x in linhas)
        self.stdout.write('─' * 110)
        self.stdout.write(
            f'{"TOTAL":8}{dec:>13,}{inval:>11,}{ace:>13,}{delta:>11,}{"":>8}'
            f'{(ace / util if util else 0):>7.2%}{reqs:>7,}'
            f'{_dur(delta / o["docs_por_s"] / max(1, o["conexoes"])):>7}{pro:>12,}')
        self.stdout.write(
            f'\nCUSTO DA PUXADA DO QUE FALTA: {delta:,} docs · {reqs:,} requisições '
            f'· ~{_dur(delta / o["docs_por_s"])} serial '
            f'· ~{_dur(delta / o["docs_por_s"] / max(1, o["conexoes"]))} '
            f'com {o["conexoes"]} conexões · ~{delta * 225 / 1e9:.1f} GB em disco')
        if inval:
            self.stdout.write(
                f'descontados {inval:,} documentos que o CNJ conta e que não são '
                f'processo (sem `numeroProcesso`) — sem esse desconto o delta '
                f'seria {delta + inval:,} e a tabela acusaria buraco onde não há')
        self.stdout.write(self.style.WARNING(
            '⚠ o watermark NÃO alcança este delta: a passada incremental pede '
            '`@timestamp >= cursor` e o cursor já está no máximo da fonte. '
            'Fechar o delta exige `--do-zero` (varredura completa do tribunal).'))

        c = controle
        if c.get('erro') or c['docs'] is None:
            self.stdout.write(self.style.ERROR(
                f'✘ CONTROLE `proc` NÃO MEDIDO ({c.get("erro")}) ⇒ a tabela acima '
                f'vale como leitura, não como prova'))
            return
        texto = (f'CONTROLE `proc`: {c["docs"]:,} docs · sem o campo {c["sem_proc"]:,} '
                 f'· amostra {c["amostra_valida"]}/{c["amostra"]} no formato do CNJ')
        self.stdout.write(self.style.SUCCESS('✔ ' + texto) if c['ok']
                          else self.style.ERROR('✘ ' + texto +
                                                '  ⇒ MEDIÇÃO INVÁLIDA, não publique'))
        self.stdout.write(
            'lembrete: `acervo` conta DOCUMENTOS (1,155 por CNJ) e é ESQUELETO — '
            'sem parte, advogado nem valor. A coluna `processos` é o acervo rico.')

    def _enfileirar(self, incompletos):
        from datajud.jobs import DATAJUD_RETRY, varredura_parada, varrer_acervo
        if varredura_parada():
            self.stdout.write(self.style.ERROR(
                'parada global LIGADA — não enfileiro nada. '
                '`datajud_varredura_status --retomar` primeiro.'))
            return
        fila = django_rq.get_queue('varredura')
        for x in incompletos:
            fila.enqueue(varrer_acervo, x['tribunal'],
                         job_id=f'varr:{x["tribunal"]}', retry=DATAJUD_RETRY)
        self.stdout.write(self.style.SUCCESS(
            f'\nenfileirados {len(incompletos)}: '
            f'{[x["tribunal"] for x in incompletos]}'))


def _dur(segundos) -> str:
    if segundos is None:
        return '—'
    s = int(segundos)
    if s < 60:
        return f'{s}s'
    if s < 3600:
        return f'{s // 60}m'
    return f'{s // 3600}h{(s % 3600) // 60:02d}'
