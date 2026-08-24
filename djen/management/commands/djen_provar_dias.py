"""PROVA DE INTEGRIDADE de dias fechados: o que a FONTE declara vs o que temos.

`status='success'` não é prova de nada. Um dia pode fechar verde com metade do
conteúdo — foi exatamente assim que `for pagina in range(1, 11)` comeu 43,6% do
TJSP por 17 meses com run verde e log limpo. A regra nº 5 do CLAUDE.md ("meça a
completude dos DOIS lados") só é cumprida quando alguém pagina a fonte na força
bruta e confronta item a item.

Este comando faz isso, e faz de propósito **por fora do `iter_pages`**: usa só o
transporte (`DJENClient._fetch`, que dá proxy/retry/circuito) e implementa a
própria paginação. Se a calibração de página do coletor voltar a cortar mudo, a
prova NÃO repete o erro junto — ela o denuncia.

Quatro números por dia, e o que cada um significa:

    fonte_paginada  ids DISTINTOS que a API entrega paginando até a página vir
                    incompleta. É a régua. Cara: baixa o dia inteiro.
    fonte_count     o `count` da API com `itensPorPagina=1` (1 requisição).
                    Barato, mas TETO DE 10.000 — em dia grande é PISO
                    disfarçado, nunca total (regra nº 3).
    run             `movimentacoes_novas + movimentacoes_duplicadas` do
                    IngestionRun que fechou o dia. Diz o que o coletor LEU.
    banco           quantos dos ids da fonte existem hoje em `Movimentacao`.
                    Diz o que sobrou GRAVADO — é a completude de verdade.

`run` alto com `banco` baixo é escrita perdida; `run` baixo com `fonte` alta é
coleta cortada. Os dois casos morrem em silêncio sem esta conferência.

Uso:

    # um dia específico (o molde: TJDFT 2026-08-21 deu 14.651 dos dois lados)
    manage.py djen_provar_dias --tribunal TJDFT --dia 2026-08-21

    # amostra ALEATÓRIA de tamanho declarado entre os dias fechados há 24h
    manage.py djen_provar_dias --n 6 --seed 20260824 --horas 24

    # só os dias que estavam devendo (falharam antes e fecharam depois)
    manage.py djen_provar_dias --n 6 --seed 20260824 --horas 24 --so-recuperados

O `--seed` é obrigatório na amostra e sai no cabeçalho: amostra sem semente
declarada não é amostra, é escolha.
"""
import json
import random
import time
from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import F
from django.utils import timezone

from djen.client import DJENClient, DjenClientError, DjenPaginaGrandeError, DjenServerError
from tribunals.models import IngestionRun, Movimentacao, Tribunal

#: teto interno do `count` da DJEN. Chegar nele significa "≥ 10k", não "10k".
DJEN_HARD_CAP = 10_000

#: itens por página inicial da prova. Baixo de propósito: a publicação do TJDFT
#: pesa 56 KB (medido 24/08/2026), então 250 itens já são 14 MB numa resposta.
#: Quem paga o preço da prova é a memória de quem prova, não a do coletor.
ITENS_INICIAL = 250

#: piso do encolhimento por teto de bytes. Abaixo disso a prova custa mais
#: requisição do que informa.
ITENS_MINIMO = 25

#: pausa entre requisições. A prova é SERIAL (1 conexão), mas o teto de
#: concorrência contra o CNJ é `réplicas x DJEN_PAGINAS_PARALELAS <= 64` e a
#: frota já usa 42 (14 x 3). A prova entra como +1 e ainda respira entre
#: páginas — martelar a API pra provar completude seria trocar um pecado por
#: outro.
PAUSA_S = 0.3

#: tamanho do lote de `external_id` por SELECT no confronto com o banco. O
#: índice único (tribunal, external_id) atende, mas o Postgres é disk-I/O-bound:
#: lote grande demais vira scan.
LOTE_IDS = 1000


def paginar_forca_bruta(client: DJENClient, sigla_djen: str, dia: date,
                        itens: int = ITENS_INICIAL, verbose=None) -> dict:
    """Pagina um dia até esgotar e devolve os ids DISTINTOS que a fonte entrega.

    Paginação própria (não é `iter_pages`) — ver docstring do módulo. Guarda só
    o `id` de cada item: o dia inteiro do TJDFT são 822,6 MB de texto e 14.651
    ids, então o conjunto cabe em memória e o texto não.

    Regra nº 2: página incompleta seguida de página com dado é ERRO, não
    `return` discreto — é a assinatura exata do corte mudo.
    """
    ids: set[str] = set()
    pagina = 1
    lidos = 0            # offset de ITEM já visto (a paginação re-ancora por item)
    requisicoes = 0
    brutos = 0           # itens entregues somando repetição de sobreposição
    encolhimentos = 0
    fim_em = None
    while True:
        try:
            payload = client._fetch(sigla_djen, dia, dia, pagina=pagina,
                                    itens_por_pagina=itens)
        except DjenPaginaGrandeError:
            if itens <= ITENS_MINIMO:
                raise
            itens = max(ITENS_MINIMO, itens // 2)
            pagina = lidos // itens + 1      # RELÊ o mesmo offset, não pula item
            encolhimentos += 1
            continue
        except DjenServerError:
            if itens <= ITENS_MINIMO:
                raise
            itens = max(ITENS_MINIMO, itens // 5)
            pagina = lidos // itens + 1
            encolhimentos += 1
            continue
        requisicoes += 1
        items = payload.get('items') or []
        n = len(items)
        if fim_em is not None and n:
            raise DjenClientError(
                f'{sigla_djen} {dia}: página {fim_em} veio incompleta mas a '
                f'{pagina} trouxe {n} itens — paginação inconsistente, a prova '
                f'não pode declarar este dia'
            )
        brutos += n
        ids.update(str(i.get('id')) for i in items if i.get('id') is not None)
        del items
        lidos = max(lidos, (pagina - 1) * itens + n)
        if n < itens:
            fim_em = pagina
            break
        pagina += 1
        if verbose and requisicoes % 20 == 0:
            verbose(f'      ... {requisicoes} req, {len(ids):,} ids')
        time.sleep(PAUSA_S)
    return {
        'ids': ids, 'requisicoes': requisicoes, 'itens_brutos': brutos,
        'itens_por_pagina_final': itens, 'encolhimentos': encolhimentos,
    }


class Command(BaseCommand):
    help = 'Prova de integridade: pagina a DJEN na força bruta e confronta com o banco.'

    def add_arguments(self, parser):
        parser.add_argument('--tribunal', default=None, help='Sigla (com --dia).')
        parser.add_argument('--dia', default=None, help='YYYY-MM-DD (com --tribunal).')
        parser.add_argument('--n', type=int, default=0,
                            help='Tamanho da amostra aleatória de dias fechados.')
        parser.add_argument('--seed', type=int, default=None,
                            help='Semente da amostra — OBRIGATÓRIA com --n.')
        parser.add_argument('--horas', type=int, default=24,
                            help='Janela de dias fechados a amostrar (default 24h).')
        parser.add_argument('--so-recuperados', action='store_true',
                            dest='so_recuperados',
                            help='Só dias que tiveram `failed` antes do `success`.')
        parser.add_argument('--itens', type=int, default=ITENS_INICIAL)
        parser.add_argument('--json', dest='dump_json', default=None,
                            help='Arquivo pra despejar o resultado.')

    def handle(self, *args, tribunal, dia, n, seed, horas, so_recuperados, itens,
               dump_json, **opts):
        alvos = self._escolher(tribunal, dia, n, seed, horas, so_recuperados)
        client = DJENClient()
        saida = []
        for sigla, d in alvos:
            saida.append(self._provar_um(client, sigla, d, itens))
        self._rodape(saida)
        if dump_json:
            with open(dump_json, 'w') as fh:
                json.dump(saida, fh, ensure_ascii=False, indent=1, default=str)
            self.stdout.write(f'\njson em {dump_json}')

    def _escolher(self, tribunal, dia, n, seed, horas, so_recuperados):
        if tribunal and dia:
            return [(tribunal, date.fromisoformat(dia))]
        if not n:
            raise CommandError('use --tribunal/--dia ou --n (com --seed)')
        if seed is None:
            raise CommandError('amostra sem --seed declarada não é amostra, é escolha')
        desde = timezone.now() - timedelta(hours=horas)
        fechados = set(
            IngestionRun.objects
            .filter(fonte='djen', status=IngestionRun.STATUS_SUCCESS,
                    finished_at__gte=desde)
            .filter(janela_inicio=F('janela_fim'))
            .values_list('tribunal__sigla', 'janela_inicio')
        )
        if so_recuperados:
            falhos = set(
                IngestionRun.objects
                .filter(fonte='djen', status=IngestionRun.STATUS_FAILED,
                        started_at__gte=timezone.now() - timedelta(days=7))
                .filter(janela_inicio=F('janela_fim'))
                .values_list('tribunal__sigla', 'janela_inicio')
            )
            fechados &= falhos
        universo = sorted(fechados)
        if not universo:
            raise CommandError('nenhum dia fechado na janela — nada a provar')
        rnd = random.Random(seed)
        amostra = rnd.sample(universo, min(n, len(universo)))
        self.stdout.write(self.style.HTTP_INFO(
            f'universo: {len(universo)} dias fechados em {horas}h'
            f'{" (só recuperados)" if so_recuperados else ""} · '
            f'amostra ALEATÓRIA n={len(amostra)} seed={seed}'
        ))
        return amostra

    def _provar_um(self, client, sigla, d, itens):
        t = Tribunal.objects.get(sigla=sigla)
        self.stdout.write(self.style.HTTP_INFO(f'\n=== {sigla} {d} ==='))
        linha = {'tribunal': sigla, 'dia': d.isoformat()}

        t0 = time.monotonic()
        try:
            linha['fonte_count'] = client.count_window(t.sigla_djen, d, d)
        except Exception as exc:  # a prova não morre por causa de um probe
            linha['fonte_count'] = None
            linha['erro_count'] = str(exc)[:200]
        linha['seg_count'] = round(time.monotonic() - t0, 2)

        t0 = time.monotonic()
        try:
            bruto = paginar_forca_bruta(client, t.sigla_djen, d, itens,
                                        verbose=self.stdout.write)
        except Exception as exc:
            self.stdout.write(self.style.ERROR(
                f'  NÃO PROVADO — paginação falhou: {str(exc)[:200]}'))
            linha['erro'] = str(exc)[:300]
            return linha
        ids = bruto.pop('ids')
        linha.update(bruto)
        linha['fonte_paginada'] = len(ids)
        linha['seg_paginacao'] = round(time.monotonic() - t0, 1)

        run = (IngestionRun.objects
               .filter(fonte='djen', tribunal=t, janela_inicio=d, janela_fim=d,
                       status=IngestionRun.STATUS_SUCCESS)
               .order_by('-finished_at').first())
        if run:
            linha['run_id'] = run.pk
            linha['run_lidas'] = run.movimentacoes_novas + run.movimentacoes_duplicadas
            linha['run_novas'] = run.movimentacoes_novas
            linha['run_paginas'] = run.paginas_lidas
            linha['run_fim'] = run.finished_at

        t0 = time.monotonic()
        presentes = 0
        ordenados = sorted(ids)
        for i in range(0, len(ordenados), LOTE_IDS):
            presentes += Movimentacao.objects.filter(
                tribunal=t, external_id__in=ordenados[i:i + LOTE_IDS]).count()
        linha['banco_dos_ids'] = presentes
        linha['seg_banco'] = round(time.monotonic() - t0, 1)
        linha['faltando'] = len(ids) - presentes
        linha['cobertura_pct'] = round(presentes / len(ids) * 100, 3) if ids else 100.0

        self._imprimir(linha)
        return linha

    def _imprimir(self, l):  # noqa: E741
        fonte = l['fonte_paginada']
        cap = ' ⚠ TETO de 10k (piso, não total)' if (l.get('fonte_count') or 0) >= DJEN_HARD_CAP else ''
        self.stdout.write(
            f'  fonte paginada ..... {fonte:>10,} ids distintos '
            f'({l["requisicoes"]} req, {l["itens_brutos"]:,} itens brutos, '
            f'{l["seg_paginacao"]}s)')
        self.stdout.write(
            f'  fonte count (1 req)  {(l.get("fonte_count") or -1):>10,}{cap} '
            f'({l.get("seg_count")}s)')
        if 'run_lidas' in l:
            d = l['run_lidas'] - fonte
            estilo = self.style.SUCCESS if d == 0 else self.style.ERROR
            self.stdout.write(estilo(
                f'  run (novas+dup) ....  {l["run_lidas"]:>10,}  Δ {d:+,} '
                f'(run={l["run_id"]}, pgs={l["run_paginas"]})'))
        else:
            self.stdout.write(self.style.WARNING('  run .................  sem success de 1 dia'))
        estilo = self.style.SUCCESS if l['faltando'] == 0 else self.style.ERROR
        self.stdout.write(estilo(
            f'  banco (ids da fonte)  {l["banco_dos_ids"]:>10,}  Δ {-l["faltando"]:+,} '
            f'→ {l["cobertura_pct"]}% ({l["seg_banco"]}s)'))

    def _rodape(self, saida):
        provados = [x for x in saida if 'cobertura_pct' in x]
        if not provados:
            return
        integros = [x for x in provados if x['faltando'] == 0]
        self.stdout.write(self.style.HTTP_INFO(
            f'\n{"─" * 62}\n'
            f'provados {len(provados)}/{len(saida)} · íntegros ao item: '
            f'{len(integros)}/{len(provados)} · '
            f'itens da fonte: {sum(x["fonte_paginada"] for x in provados):,} · '
            f'faltando no banco: {sum(x["faltando"] for x in provados):,}'
        ))
        # O count barato só serve de gate onde ele NÃO bate o teto de 10k.
        uteis = [x for x in provados
                 if x.get('fonte_count') is not None and x['fonte_count'] < DJEN_HARD_CAP]
        if uteis:
            iguais = [x for x in uteis if x['fonte_count'] == x['fonte_paginada']]
            self.stdout.write(
                f'count de 1 requisição bateu a paginação em {len(iguais)}/{len(uteis)} '
                f'dias abaixo do teto de 10k '
                f'(custo medido: {sum(x["seg_count"] for x in uteis) / len(uteis):.2f}s/dia '
                f'contra {sum(x["seg_paginacao"] for x in uteis) / len(uteis):.0f}s da paginação)'
            )
