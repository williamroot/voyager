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

    def handle(self, *a, **o):
        cli = DatajudClient(prefer_cortex=False)
        es = get_es()
        acervo = index_name('acervo')
        processos = index_name('processos')

        siglas = ([s.strip().upper() for s in o['tribunais'].split(',') if s.strip()]
                  or list(Tribunal.objects.order_by('sigla')
                          .values_list('sigla', flat=True)))
        estado_fila = self._estado_da_fila()

        linhas = []
        for sigla in siglas:
            linhas.append(self._medir(cli, es, acervo, processos, sigla,
                                      estado_fila, o))

        controle = self._controle(es, acervo)
        if o['como_json']:
            self.stdout.write(json.dumps(
                {'controle': controle, 'tribunais': linhas},
                ensure_ascii=False, default=str))
        else:
            self._tabela(linhas, controle, o)

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
        * uma amostra de 200 docs conferida no formato do CNJ — é o que pega o
          vazio e o truncado. Campo `text`/`keyword` só se mede por amostra.

        A amostra é `random_score` semeado **pelo `proc`**, e não pelo
        `_seq_no`. Com `_seq_no` o score é função da ordem de ESCRITA e o top-N
        cai numa janela estreita de escrita, não numa amostra do índice — foi
        essa armadilha que fez uma medição do TJSP errar por 50× (25/08/2026).
        """
        es_t = es.options(request_timeout=ES_TIMEOUT)
        total = es_t.count(index=acervo)['count']
        sem = es_t.count(
            index=acervo,
            query={'bool': {'must_not': [{'exists': {'field': 'proc'}}]}})['count']
        r = es_t.search(index=acervo, size=200, source=['proc'],
                        query={'function_score': {
                            'query': {'match_all': {}},
                            'random_score': {'seed': 20260831,
                                             'field': 'proc'}}})
        amostra = [h['_source'].get('proc') or '' for h in r['hits']['hits']]
        bons = sum(1 for p in amostra
                   if len(p) == 25 and p[7] == '-' and p[10] == '.')
        return {
            'docs': total, 'sem_proc': sem, 'amostra': len(amostra),
            'amostra_valida': bons,
            'ok': sem == 0 and bool(amostra) and bons == len(amostra),
        }

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

    def _tabela(self, linhas, controle, o):
        self.stdout.write(
            f'{"trib":8}{"declarado":>13}{"inválidos":>11}{"acervo":>13}'
            f'{"delta":>11}{"sobra":>8}{"cob":>8}{"reqs":>7}{"ETA":>7}'
            f'{"processos":>12}  estado')
        for x in sorted(linhas, key=lambda y: -(y['declarado'] or 0)):
            if x['declarado'] is None:
                self.stdout.write(self.style.ERROR(
                    f'{x["tribunal"]:8}{"—":>13}{"—":>11}{"—":>13}{"—":>11}'
                    f'{"—":>8}{"—":>8}{"—":>7}{"—":>7}{"—":>12}'
                    f'  {x["estado"]}: {x["erro"]}'))
                continue
            marca = '✔' if x['estado'] == 'OK' else ' '
            inval = x.get('invalidos')
            linha = (
                f'{x["tribunal"]:8}{x["declarado"]:>13,}'
                f'{("?" if inval is None else f"{inval:,}"):>11}'
                f'{x["acervo"]:>13,}{x["delta"]:>11,}{x["sobra"]:>8,}'
                f'{x["cobertura"]:>7.2%}{x["requisicoes"]:>7,}'
                f'{_dur(x["eta_s"]):>7}'
                f'{(x["processos"] if x["processos"] is not None else 0):>12,}'
                f'  {marca} {x["estado"]}')
            self.stdout.write(
                self.style.WARNING(linha) if x['estado'] == 'INCOMPLETO' else linha)

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
