"""Constrói o índice canônico `voyager-entidades` a partir de `tribunals_parte`.

    docker exec voyagerdev-web-1 python manage.py construir_indice_entidades \
        --dry-run --limite 200000

LEITURA DO POSTGRES — REGRAS DA CASA (o DB de prod é CONTIDO e já derrubou o
site 2× nesta semana):
  * **keyset** `WHERE id > :ultimo ORDER BY id LIMIT :lote` — index scan puro na
    PK, número de linhas EXATO por query. Nada de OFFSET (relê o prefixo), nada
    de janela por faixa de id (os ids são esparsos: 16,68M linhas espalhadas em
    626M de ids — uma faixa fixa devolveria de 0 a 70k linhas);
  * **nenhum `COUNT(*)`** — o total é `reltuples` do `pg_class` (estimativa do
    autovacuum, custo O(1)). O progresso diz "estimado" porque é estimativa;
  * **`.values_list` de 6 colunas** — não instancia model, não puxa colunas que
    não usamos, corta o tráfego;
  * **cada lote é cronometrado**. Passou de `--alvo-lote-s`, o lote ENCOLHE
    (metade, piso 500) no ato; voltou a ficar rápido, cresce de novo até o
    tamanho pedido. É o mesmo instinto do `--sleep` do `reindexar_processos`,
    só que automático — o DB reclama e a gente recua sem operador no teclado.

RETOMADA / MODELO DE ESCRITA (a parte não-óbvia):
  a agregação é GLOBAL em memória e o ES só é escrito no FIM. Não dá pra fechar
  uma entidade antes do fim da leitura — a última linha lida pode ser mais uma
  grafia do INSS. Consequências, explícitas:
    - o build canônico é UMA passada completa (~16,7M linhas). `--checkpoint`
      grava o progresso da LEITURA (pra saber onde parou e retomar), mas
      retomar do meio produz um índice que cobre só a janela lida;
    - `--desde-id N` + `--merge-es` é o modo INCREMENTAL: lê só as partes novas
      e MESCLA com o que já está no índice (mget → une variantes/documentos/
      soma n_partes) antes de gravar. É assim que se faz o "top-up" diário sem
      reler 16,7M linhas.
"""
import json
import os
import time

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from search.entidades import INDICE, Agregador, nome_canonico
from search.mappings import ENTIDADE_MAPPING
from tribunals.models import Parte

#: colunas lidas — o mínimo que `classificar()` precisa
COLUNAS = ('id', 'nome', 'documento', 'tipo_documento', 'tipo', 'oab')

LOTE_MINIMO = 500


class Command(BaseCommand):
    help = ('Constrói/atualiza o índice ES de entidades canônicas '
            '(PJ + entes públicos) a partir de tribunals_parte.')

    def add_arguments(self, parser):
        parser.add_argument('--limite', type=int, default=0,
                            help='Máximo de linhas de Parte a LER (0 = todas).')
        parser.add_argument('--desde-id', type=int, default=0,
                            help='Keyset: lê a partir de id > N (retomada / build incremental).')
        parser.add_argument('--lote', type=int, default=20000,
                            help='Linhas por query (default 20000). Encolhe sozinho se o DB penar.')
        parser.add_argument('--alvo-lote-s', type=float, default=3.0,
                            help='Duração alvo de um lote (s). Estourou, o lote é cortado pela metade.')
        parser.add_argument('--sleep', type=float, default=0.0,
                            help='Pausa (s) entre lotes — throttle explícito do DB.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Lê, agrega e RELATA. Não escreve nada no ES.')
        parser.add_argument('--indice', type=str, default=INDICE,
                            help=f'Sufixo do índice (default "{INDICE}"). '
                                 'Use "entidades-teste" pra validar sem tocar no final.')
        parser.add_argument('--recriar', action='store_true',
                            help='DELETE + CREATE do índice antes de gravar (destrutivo).')
        parser.add_argument('--merge-es', action='store_true',
                            help='Mescla com o doc já existente no índice (build incremental).')
        parser.add_argument('--bulk', type=int, default=1000,
                            help='Docs por requisição _bulk no ES.')
        parser.add_argument('--checkpoint', type=str, default='',
                            help='Arquivo JSON com o progresso da leitura (retomada).')
        parser.add_argument('--amostra', type=int, default=0,
                            help='Imprime as N maiores entidades no fim (auditoria).')

    # ------------------------------------------------------------------ #
    def handle(self, *args, **opts):
        self.es_url = getattr(settings, 'ELASTICSEARCH_URL',
                              'http://elasticsearch:9200').rstrip('/')
        prefixo = getattr(settings, 'ELASTICSEARCH_INDEX_PREFIX', 'voyager')
        self.indice = f'{prefixo}-{opts["indice"]}'
        self.sessao = requests.Session()

        if opts['recriar'] and opts['dry_run']:
            raise CommandError('--recriar com --dry-run não faz sentido.')
        if opts['merge_es'] and opts['recriar']:
            raise CommandError('--merge-es com --recriar se anulam (o índice some antes).')

        estimado = self._reltuples('tribunals_parte')
        self.stdout.write(
            f'Lendo tribunals_parte (~{estimado:,} linhas estimadas via reltuples) '
            f'· keyset id > {opts["desde_id"]:,} · lote={opts["lote"]:,} '
            f'· limite={opts["limite"] or "sem"} → {self.indice}'
            f'{" [DRY-RUN]" if opts["dry_run"] else ""}')
        self.stdout.flush()

        agg, leitura = self._ler(opts, estimado)
        # nome→cnpj: a "Defensoria Pública da União" não pode sair 2× no
        # autocomplete só porque metade das linhas veio sem documento.
        # Pulado no build incremental: sem a base toda em memória, "nome
        # aponta pra EXATAMENTE um CNPJ" não é verificável.
        if opts['merge_es']:
            self.stdout.write(self.style.WARNING(
                'Build incremental (--merge-es): consolidação nome→cnpj PULADA '
                '(exige a base inteira em memória pra provar unicidade).'))
        else:
            agg.consolidar()
        resumo = agg.resumo()
        self._relatorio(resumo, leitura)

        if opts['amostra']:
            self._amostra(agg, opts['amostra'])

        if opts['dry_run']:
            self.stdout.write(self.style.WARNING(
                'DRY-RUN: nada foi escrito no Elasticsearch.'))
            return

        self._garantir_indice(recriar=opts['recriar'])
        gravados, erros, dt = self._gravar(agg, opts)
        self.stdout.write(self.style.SUCCESS(
            f'{gravados:,} entidades gravadas em {self.indice} '
            f'em {dt:.1f}s ({erros} erro(s) de bulk).'))

    # ------------------------------------------------------------------ #
    # Leitura (keyset, cronometrada, adaptativa)
    # ------------------------------------------------------------------ #
    def _reltuples(self, tabela: str) -> int:
        """Estimativa de linhas — O(1). NUNCA COUNT(*) numa tabela de 16,7M."""
        with connection.cursor() as cur:
            cur.execute('SELECT reltuples::bigint FROM pg_class WHERE relname = %s',
                        [tabela])
            linha = cur.fetchone()
        return int(linha[0]) if linha and linha[0] and linha[0] > 0 else 0

    def _ler(self, opts, estimado):
        agg = Agregador()
        ultimo = opts['desde_id']
        lote = max(LOTE_MINIMO, opts['lote'])
        lote_pedido = lote
        limite = opts['limite']
        alvo = opts['alvo_lote_s']

        lidas = 0
        n_lotes = 0
        pior_lote = 0.0
        tempo_db = 0.0
        t0 = time.monotonic()

        while True:
            if limite and lidas >= limite:
                break
            pegar = min(lote, limite - lidas) if limite else lote

            t_lote = time.monotonic()
            linhas = list(Parte.objects.filter(id__gt=ultimo)
                          .order_by('id')
                          .values_list(*COLUNAS)[:pegar])
            dt = time.monotonic() - t_lote
            tempo_db += dt
            pior_lote = max(pior_lote, dt)

            if not linhas:
                break

            for pid, nome, doc, tipo_doc, tipo, oab in linhas:
                agg.add(pid, nome, doc, tipo_doc, tipo, oab)
            lidas += len(linhas)
            ultimo = linhas[-1][0]
            n_lotes += 1

            # --- freio automático: o DB é contido, ele manda no ritmo -------
            if dt > alvo and lote > LOTE_MINIMO:
                lote = max(LOTE_MINIMO, lote // 2)
                self.stdout.write(self.style.WARNING(
                    f'  lote levou {dt:.1f}s (> {alvo}s) → encolhendo para {lote:,}'))
            elif dt < alvo / 3 and lote < lote_pedido:
                lote = min(lote_pedido, lote * 2)

            if n_lotes % 10 == 0 or len(linhas) < pegar:
                self._progresso(lidas, estimado, ultimo, agg, t0, dt, lote)
            if opts['checkpoint'] and n_lotes % 10 == 0:
                self._salvar_checkpoint(opts['checkpoint'], ultimo, lidas)

            if len(linhas) < pegar:      # acabou a tabela
                break
            if opts['sleep']:
                time.sleep(opts['sleep'])

        if opts['checkpoint']:
            self._salvar_checkpoint(opts['checkpoint'], ultimo, lidas, fim=True)

        return agg, {
            'lidas': lidas, 'lotes': n_lotes, 'ultimo_id': ultimo,
            'duracao_s': round(time.monotonic() - t0, 1),
            'tempo_db_s': round(tempo_db, 1),
            'pior_lote_s': round(pior_lote, 2),
            'lote_final': lote,
        }

    def _progresso(self, lidas, estimado, ultimo, agg, t0, dt, lote):
        el = time.monotonic() - t0
        taxa = lidas / el if el else 0
        pct = 100.0 * lidas / estimado if estimado else 0
        eta = (estimado - lidas) / taxa / 60 if taxa and estimado > lidas else 0
        self.stdout.write(
            f'  {lidas:,}/~{estimado:,} ({pct:.1f}%) · id={ultimo:,} '
            f'· {taxa:,.0f} linhas/s · lote {dt:.2f}s ({lote:,}) '
            f'· escopo {agg.stats["dentro"]:,} · entidades {len(agg.grupos):,} '
            f'· ETA {eta:.0f}min')
        self.stdout.flush()

    def _salvar_checkpoint(self, caminho, ultimo, lidas, fim=False):
        tmp = f'{caminho}.tmp'
        with open(tmp, 'w') as fh:
            json.dump({'ultimo_id': ultimo, 'lidas': lidas,
                       'concluido': fim, 'quando': time.time()}, fh)
        os.replace(tmp, caminho)

    # ------------------------------------------------------------------ #
    # Relatório
    # ------------------------------------------------------------------ #
    def _relatorio(self, r, leitura):
        w = self.stdout.write
        w('')
        w('── LEITURA ' + '─' * 55)
        w(f'  linhas lidas ......... {leitura["lidas"]:,} em {leitura["lotes"]} lotes')
        w(f'  duração .............. {leitura["duracao_s"]}s '
          f'(DB {leitura["tempo_db_s"]}s · pior lote {leitura["pior_lote_s"]}s '
          f'· lote final {leitura["lote_final"]:,})')
        w(f'  último id ............ {leitura["ultimo_id"]:,}')
        w('── ESCOPO ' + '─' * 56)
        w(f'  dentro ............... {r["dentro"]:,} '
          f'(cnpj {r["dentro_cnpj"]:,} · tipo_pj {r["dentro_tipo_pj"]:,} '
          f'· ente_público {r["dentro_ente_publico"]:,})')
        w(f'  fora ................. {r["fora"]:,} '
          f'(advogado {r["fora_advogado"]:,} · pessoa_física {r["fora_pessoa_fisica"]:,} '
          f'· nem_pj_nem_ente {r["fora_nao_pj_nem_ente"]:,} '
          f'· sem_nome {r["fora_sem_nome"]:,})')
        w(f'  CNPJ mascarado ....... {r["documentos_mascarados"]:,} linhas no escopo '
          '(NÃO fundidas por raiz — caem no nome)')
        w('── ENTIDADES ' + '─' * 53)
        w(f'  entidades ............ {r["entidades"]:,} '
          f'(chave cnpj {r["entidades_por_chave"]["cnpj"]:,} · '
          f'chave nome {r["entidades_por_chave"]["nome"]:,})')
        w(f'  taxa de fusão ........ {r["taxa_fusao_pct"]}% '
          f'({r["linhas_por_entidade"]} linhas de Parte por entidade)')
        if 'consolidados_nome_em_cnpj' in r:
            w(f'  consolidação nome→cnpj {r["consolidados_nome_em_cnpj"]:,} grupos '
              f'({r["consolidacao_linhas"]:,} linhas) · '
              f'{r["consolidacao_ambiguos"]:,} homônimos NÃO fundidos (abstenção)')
        w('')

    def _amostra(self, agg, n):
        maiores = sorted(agg.grupos.values(), key=lambda g: -g.n_partes)[:n]
        self.stdout.write(f'── TOP {n} POR n_partes ' + '─' * 45)
        for g in maiores:
            self.stdout.write(
                f'  {g.n_partes:>7,} partes · {g.chave}:{g.valor[:40]:<40} '
                f'· {len(g.variantes)} grafias · {len(g.documentos)} docs '
                f'· {nome_canonico(g.variantes)[:60]}')
        self.stdout.write('')

    # ------------------------------------------------------------------ #
    # Escrita no ES
    # ------------------------------------------------------------------ #
    def _garantir_indice(self, recriar=False):
        if recriar:
            self.sessao.delete(f'{self.es_url}/{self.indice}', timeout=60)
            self.stdout.write(self.style.WARNING(f'Índice {self.indice} apagado.'))
        r = self.sessao.get(f'{self.es_url}/{self.indice}', timeout=30)
        if r.status_code == 200:
            return
        r = self.sessao.put(f'{self.es_url}/{self.indice}',
                            json=ENTIDADE_MAPPING, timeout=60)
        if r.status_code >= 300:
            raise CommandError(f'Criando {self.indice}: HTTP {r.status_code} {r.text[:300]}')
        self.stdout.write(f'Índice {self.indice} criado (ENTIDADE_MAPPING).')

    def _mget(self, ids):
        """Docs já existentes (build incremental) — 1 round-trip por lote."""
        r = self.sessao.post(f'{self.es_url}/{self.indice}/_mget',
                             json={'ids': ids}, timeout=120)
        if r.status_code >= 300:
            return {}
        return {d['_id']: d['_source'] for d in r.json().get('docs', [])
                if d.get('found')}

    def _mesclar(self, doc, antigo):
        """União com o doc já indexado — `variantes` é o produto, não pode encolher."""
        if not antigo:
            return doc
        variantes = list(dict.fromkeys(list(doc['variantes'])
                                       + list(antigo.get('variantes') or [])))
        documentos = sorted(set(doc['documentos']) | set(antigo.get('documentos') or []))
        doc = dict(doc)
        doc['variantes'] = variantes
        doc['n_variantes'] = len(variantes)
        doc['documentos'] = documentos
        doc['n_documentos'] = len(documentos)
        doc['n_partes'] = doc['n_partes'] + int(antigo.get('n_partes') or 0)
        doc['documentos_mascarados'] = (doc['documentos_mascarados']
                                        + int(antigo.get('documentos_mascarados') or 0))
        doc['eh_ente_publico'] = bool(doc['eh_ente_publico']
                                      or antigo.get('eh_ente_publico'))
        return doc

    def _gravar(self, agg, opts):
        t0 = time.monotonic()
        tamanho = opts['bulk']
        pendentes, gravados, erros = [], 0, 0

        def flush(itens):
            nonlocal gravados, erros
            if not itens:
                return
            if opts['merge_es']:
                antigos = self._mget([i for i, _ in itens])
                itens = [(i, self._mesclar(d, antigos.get(i))) for i, d in itens]
            linhas = []
            for _id, doc in itens:
                linhas.append(json.dumps({'index': {'_index': self.indice, '_id': _id}}))
                linhas.append(json.dumps(doc, default=str, ensure_ascii=False))
            corpo = ('\n'.join(linhas) + '\n').encode('utf-8')
            try:
                r = self.sessao.post(f'{self.es_url}/_bulk', data=corpo,
                                     headers={'Content-Type': 'application/x-ndjson'},
                                     timeout=180)
                if r.status_code >= 300:
                    erros += 1
                    self.stderr.write(f'  bulk HTTP {r.status_code}: {r.text[:300]}')
                    return
                resp = r.json()
                if resp.get('errors'):
                    erros += 1
                    falha = next((i for i in resp['items']
                                  if i.get('index', {}).get('error')), None)
                    self.stderr.write(f'  bulk com erro: {json.dumps(falha)[:300]}')
            except Exception as exc:            # noqa: BLE001 — ES hipou; idempotente
                erros += 1
                self.stderr.write(f'  bulk ERRO: {str(exc)[:300]}')
                return
            gravados += len(itens)

        for _id, doc in agg.docs():
            pendentes.append((_id, doc))
            if len(pendentes) >= tamanho:
                flush(pendentes)
                pendentes = []
                if gravados % (tamanho * 20) == 0:
                    self.stdout.write(f'  {gravados:,} entidades gravadas…')
                    self.stdout.flush()
        flush(pendentes)
        self.sessao.post(f'{self.es_url}/{self.indice}/_refresh', timeout=120)
        return gravados, erros, time.monotonic() - t0
