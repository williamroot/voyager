"""Migra `voyager-movimentacoes` → `-v2`: 16 shards + entidades do texto.

    es_movs_v2 --criar        # cria o v2 (mapping novo, 16 shards, modo carga)
    es_movs_v2 --copiar       # cópia server-side por faixas de id (assíncrona)
    es_movs_v2 --progresso    # acompanha as tarefas de cópia
    es_movs_v2 --entidades    # extrai OAB/CPF/CNPJ/CNJ nos docs que têm texto
    es_movs_v2 --conferir     # gate: contagem + amostra campo a campo
    es_movs_v2 --finalizar    # refresh normal, forcemerge leve
    es_movs_v2 --cutover      # apaga o v1 e aponta o alias   (IRREVERSÍVEL)

POR QUE MIGRAR
--------------
1. **Shard único de 685 GB com 1,16 bilhão de docs.** O limite RÍGIDO do Lucene
   é 2,147 bilhões por shard: estamos em **55%**. Quando estourar não há
   migração barata — o índice simplesmente para de aceitar escrita. 16 shards de
   ~43 GB resolvem isso e ainda paralelizam a busca nos 16 vCPU do nó.
2. **Entidades do texto.** A OAB estava escrita no corpo das publicações e a
   busca alcançava 0,26% da base. Ver `search/entidades_texto.py`.

POR QUE EM DUAS PASSADAS (cópia + entidades)
--------------------------------------------
Extrair no caminho exigiria trazer os 685 GB pra fora do ES, processar em Python
e devolver — ~1,4 TB de rede. A cópia nativa (`_reindex`) é **server-side**: o
dado não sai da máquina. Depois, a passada de entidades lê só os documentos que
o índice invertido diz que citam OAB/CPF/CNPJ/CNJ/R$ — medido, **13,1%** do
total. Duas passadas baratas ganham de uma passada cara.

SEGURANÇA
---------
- O v1 NÃO é tocado até o `--cutover`, que é um passo separado e explícito.
- Durante a migração, `ES_INDICE_ESPELHO` faz a ingestão escrever nos dois
  índices (ver `search/client.py::indices_espelho`). Sem isso, as horas de
  publicação e os updates de enriquecimento da janela se perderiam.
- A cópia é idempotente: `_reindex` usa o mesmo `_id`, então repetir uma faixa
  sobrescreve em vez de duplicar.
"""
import json
import time

from django.core.management.base import BaseCommand, CommandError

from search.client import get_es, index_name
from search.entidades_texto import extrair
from search.mappings import ANALYZER_SETTINGS, MOV_MAPPING

ORIGEM = 'movimentacoes'
DESTINO = 'movimentacoes-v2'

#: 16 shards ⇒ ~43 GB cada (recomendação do ES é 30-50 GB) e um shard por vCPU
#: do nó, que é o que faz a busca paralelizar de verdade.
SHARDS = 16

#: faixas de id pra paralelizar a cópia. O `id` é a PK do Postgres: único,
#: monotônico e já indexado — cursor melhor que `publish_date`, que neste índice
#: tem lixo (mínimo 0021-10-13, máximo 2400-01-01).
FAIXAS = 32

#: só entra na passada de entidades o doc que o índice invertido já diz que
#: menciona algo extraível. Evita ler 1,16B docs pra achar 153M.
FILTRO_ENTIDADES = {
    'bool': {'should': [
        {'match_phrase': {'body': 'OAB'}},
        {'match_phrase': {'body': 'CPF'}},
        {'match_phrase': {'body': 'CNPJ'}},
        {'match': {'body': 'advogado'}},
        {'match_phrase': {'body': 'R$'}},
    ], 'minimum_should_match': 1},
}


class Command(BaseCommand):
    help = 'Migra o índice de movimentações pro v2 (16 shards + entidades)'

    def add_arguments(self, p):
        p.add_argument('--criar', action='store_true')
        p.add_argument('--copiar', action='store_true')
        p.add_argument('--progresso', action='store_true')
        p.add_argument('--entidades', action='store_true')
        p.add_argument('--conferir', action='store_true')
        p.add_argument('--finalizar', action='store_true')
        p.add_argument('--cutover', action='store_true')
        p.add_argument('--faixas', type=int, default=FAIXAS)
        p.add_argument('--lote', type=int, default=2000)
        p.add_argument('--max-docs', type=int, default=None,
                       help='teto de docs na passada de entidades (teste)')
        p.add_argument('--faixa', type=int, default=None,
                       help='qual faixa de id processar (0-based) — paraleliza '
                            'a passada de entidades em N processos')

    def handle(self, *a, **o):
        self.es = get_es()
        self.v1 = index_name(ORIGEM)
        self.v2 = index_name(DESTINO)
        feito = False
        for etapa in ('criar', 'copiar', 'progresso', 'entidades', 'conferir',
                      'finalizar', 'cutover'):
            if o[etapa]:
                getattr(self, f'_{etapa}')(o)
                feito = True
        if not feito:
            raise CommandError('escolha uma etapa (--criar, --copiar, ...)')

    # -- etapas ------------------------------------------------------------ #

    def _criar(self, o):
        if self.es.indices.exists(index=self.v2):
            self.stdout.write(f'{self.v2} já existe — nada a fazer')
            return
        corpo = {
            'settings': {
                **ANALYZER_SETTINGS,
                'index': {
                    'number_of_shards': SHARDS,
                    # modo CARGA: sem réplica e sem refresh. Ambos voltam no
                    # --finalizar. Refresh a cada 1s durante uma cópia de 1,16B
                    # docs geraria milhões de segmentos e merge sem fim.
                    'number_of_replicas': 0,
                    'refresh_interval': '-1',
                    'translog': {'durability': 'async', 'sync_interval': '60s'},
                },
            },
            'mappings': MOV_MAPPING['mappings'],
        }
        self.es.indices.create(index=self.v2, body=corpo)
        self.stdout.write(self.style.SUCCESS(
            f'{self.v2} criado — {SHARDS} shards, modo carga (sem refresh/réplica)'))

    def _faixas_de_id(self, n):
        a = self.es.search(index=self.v1, size=0, aggs={
            'min': {'min': {'field': 'id'}}, 'max': {'max': {'field': 'id'}}})
        lo = int(a['aggregations']['min']['value'])
        hi = int(a['aggregations']['max']['value']) + 1
        passo = (hi - lo) // n + 1
        return [(lo + i * passo, min(lo + (i + 1) * passo, hi)) for i in range(n)]

    def _copiar(self, o):
        tarefas = []
        for i, (ini, fim) in enumerate(self._faixas_de_id(o['faixas'])):
            r = self.es.reindex(
                body={
                    'source': {'index': self.v1, 'size': o['lote'],
                               'query': {'range': {'id': {'gte': ini, 'lt': fim}}}},
                    'dest': {'index': self.v2, 'op_type': 'index'},
                    'conflicts': 'proceed',
                },
                wait_for_completion=False, request_timeout=120)
            tarefas.append((i, ini, fim, r['task']))
            self.stdout.write(f'  faixa {i:>2} id[{ini:>12,} … {fim:>12,})  task={r["task"]}')
        self.stdout.write(self.style.SUCCESS(
            f'\n{len(tarefas)} tarefas de cópia disparadas (server-side, assíncronas).'
            f'\nAcompanhe: manage.py es_movs_v2 --progresso'))

    def _progresso(self, o):
        r = self.es.tasks.list(actions='*reindex', detailed=True)
        total = criados = 0
        vivas = 0
        for no in (r.get('nodes') or {}).values():
            for t in (no.get('tasks') or {}).values():
                st = t.get('status') or {}
                total += st.get('total', 0)
                criados += st.get('created', 0) + st.get('updated', 0)
                vivas += 1
        pct = (100.0 * criados / total) if total else 0
        self.stdout.write(f'tarefas de cópia vivas: {vivas}')
        self.stdout.write(f'progresso: {criados:,} de {total:,} ({pct:.1f}%)')
        try:
            self.es.indices.refresh(index=self.v2)
        except Exception:  # noqa: BLE001
            pass
        c1 = self.es.count(index=self.v1)['count']
        c2 = self.es.count(index=self.v2)['count']
        self.stdout.write(f'docs: v1 {c1:,} · v2 {c2:,} ({100.0*c2/c1:.1f}%)')

    def _entidades(self, o):
        """Segunda passada: extrai as entidades do `body` no v2.

        Roda DEPOIS da cópia porque o `_reindex` nativo não sabe fazer regex —
        e ensinar Painless a fazer isso exigiria ligar `script.painless.regex`
        (restart do cluster) e reescrever em Painless a validação de dígito
        verificador, que é justamente a parte que impede lixo virar entidade.
        """
        from elasticsearch.helpers import bulk
        lidos = atualizados = 0
        cursor = None
        t0 = time.time()

        # Uma passada só leria ~250M docs em série (35h medidas). Fatiar por
        # faixa de id deixa rodar N processos ao mesmo tempo, cada um dono da
        # sua faixa — sem coordenação e sem risco de dois escreverem o mesmo doc.
        query = FILTRO_ENTIDADES
        rotulo = ''
        if o['faixa'] is not None:
            ini, fim = self._faixas_de_id(o['faixas'])[o['faixa']]
            query = {'bool': {'must': [FILTRO_ENTIDADES],
                              'filter': [{'range': {'id': {'gte': ini, 'lt': fim}}}]}}
            rotulo = f'[faixa {o["faixa"]}/{o["faixas"]}] '
            self.stdout.write(f'{rotulo}id[{ini:,} … {fim:,})')

        while True:
            corpo = {'size': o['lote'], '_source': ['body', 'proc'],
                     'sort': [{'id': 'asc'}], 'query': query}
            if cursor:
                corpo['search_after'] = cursor
            r = self.es.search(index=self.v2, body=corpo, request_timeout=300)
            hits = r['hits']['hits']
            if not hits:
                break
            cursor = hits[-1]['sort']
            lidos += len(hits)
            acoes = []
            for h in hits:
                src = h['_source']
                ent = extrair(src.get('body') or '')
                citados = [c for c in ent.get('cnjs_citados', [])
                           if c != src.get('proc')]
                if citados:
                    ent['cnjs_citados'] = citados
                else:
                    ent.pop('cnjs_citados', None)
                if ent:
                    acoes.append({'_op_type': 'update', '_index': self.v2,
                                  '_id': h['_id'], 'doc': ent})
            if acoes:
                ok, _erros = bulk(self.es, acoes, raise_on_error=False,
                                  request_timeout=180)
                atualizados += ok
            if lidos % 100_000 < o['lote']:
                dt = time.time() - t0
                self.stdout.write(f'  {rotulo}{lidos:,} lidos · {atualizados:,} '
                                  f'com entidade · {lidos/dt:,.0f} docs/s', ending='\n')
                self.stdout.flush()
            if o['max_docs'] and lidos >= o['max_docs']:
                break
        self.stdout.write(self.style.SUCCESS(
            f'{rotulo}entidades: {lidos:,} lidos, {atualizados:,} atualizados '
            f'em {(time.time()-t0)/3600:.1f}h'))

    def _conferir(self, o):
        """Gate: contagem dos dois lados + amostra campo a campo.

        Contagem igual não prova cópia fiel (poderia copiar docs errados), por
        isso a amostra compara o `_source` inteiro de documentos sorteados.
        """
        self.es.indices.refresh(index=self.v2)
        c1 = self.es.count(index=self.v1, request_timeout=180)['count']
        c2 = self.es.count(index=self.v2, request_timeout=180)['count']
        self.stdout.write(f'contagem: v1 {c1:,} · v2 {c2:,} · diferença {c1-c2:+,}')

        divergentes = iguais = ausentes = 0
        exemplo = None
        for seed in (1, 2, 3, 4):
            # Sorteia do V2 e confere no V1 — não o contrário. O v1 é um shard
            # único de 685 GB; `function_score`+`random_score` nele custa
            # minutos sob carga e estourou o timeout do cliente duas vezes. O v2
            # tem 16 shards e responde o mesmo sorteio em segundos. A direção não
            # muda o que o gate prova (contagem já é idêntica; aqui se afere
            # FIDELIDADE campo a campo).
            r = self.es.search(index=self.v2, size=125, request_timeout=180, query={
                'function_score': {'query': {'match_all': {}},
                                   'random_score': {'seed': seed, 'field': '_seq_no'}}})
            hits = r['hits']['hits']
            if not hits:
                continue
            # `_mget` e não 500 `get`: com o nó em merge pós-cópia, uma chamada
            # por doc estoura o timeout do cliente antes de terminar a amostra.
            got = self.es.mget(index=self.v1, request_timeout=180,
                               body={'ids': [h['_id'] for h in hits]})
            no_v1 = {d['_id']: d.get('_source') for d in got['docs'] if d.get('found')}
            ENTIDADES = {'oabs', 'advogados', 'documentos', 'cnjs_citados',
                         'valores_citados'}
            for h in hits:
                d1 = no_v1.get(h['_id'])
                if d1 is None:
                    ausentes += 1
                    continue
                # o v2 tem campos A MAIS (as entidades extraídas): compara só o
                # que existe nos dois
                dif = [k for k, v in h['_source'].items()
                       if k not in ENTIDADES and d1.get(k) != v]
                if dif:
                    divergentes += 1
                    if exemplo is None:
                        exemplo = (h['_id'], dif[:4])
                else:
                    iguais += 1
        self.stdout.write(f'amostra de 500: {iguais} idênticos · '
                          f'{divergentes} divergentes · {ausentes} ausentes no v2')
        if exemplo:
            self.stdout.write(f'  1º divergente: {exemplo[0]} campos={exemplo[1]}')
        ent = self.es.count(index=self.v2, request_timeout=180,
                            query={'exists': {'field': 'oabs'}})['count']
        self.stdout.write(f'docs com OAB extraída no v2: {ent:,}')
        veredito = (c2 >= c1 * 0.999 and divergentes == 0 and ausentes == 0)
        self.stdout.write(self.style.SUCCESS('GATE OK — pode finalizar')
                          if veredito else
                          self.style.ERROR('GATE REPROVADO — NÃO fazer cutover'))

    def _finalizar(self, o):
        self.es.indices.put_settings(index=self.v2, body={'index': {
            'refresh_interval': '1s',
            'translog': {'durability': 'request'},
        }})
        self.stdout.write('refresh e translog normalizados')
        self.stdout.write('forcemerge em andamento (pode levar horas; é assíncrono)…')
        self.es.indices.forcemerge(index=self.v2, max_num_segments=5,
                                   wait_for_completion=False)

    def _cutover(self, o):
        """IRREVERSÍVEL: apaga o v1 e cria o alias apontando pro v2.

        Só depois do `--conferir` verde. O alias existe pra que o código
        (`index_name('movimentacoes')`) siga funcionando sem deploy.
        """
        c1 = self.es.count(index=self.v1)['count']
        c2 = self.es.count(index=self.v2)['count']
        if c2 < c1 * 0.999:
            raise CommandError(f'v2 tem menos docs que o v1 ({c2:,} < {c1:,}) — abortado')
        self.es.indices.delete(index=self.v1)
        self.es.indices.put_alias(index=self.v2, name=self.v1)
        self.stdout.write(self.style.SUCCESS(
            f'cutover feito: {self.v1} agora é alias de {self.v2}. '
            f'Desligue o ES_INDICE_ESPELHO e recicle os workers.'))
