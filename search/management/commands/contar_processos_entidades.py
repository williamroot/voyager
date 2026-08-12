"""Preenche `n_processos` no índice de entidades com a contagem REAL do ES.

    docker exec voyagerdev-web-1 python manage.py contar_processos_entidades \
        --indice entidades-teste --dry-run --limite 2000

POR QUE ESTE COMANDO EXISTE
===========================
O ranking do autocomplete usava `n_partes` (linhas na tabela `Parte`) como proxy
de prevalência, e ele é fraco. Medido no índice real:

    buscar "inss"
      1º  Gerente Executivo do INSS de São Paulo/Centro   109 partes
      2º  INSTITUTO NACIONAL DO SEGURO SOCIAL             764 partes  ← 4,4 MI

`n_partes` conta CADASTRO (quantas vezes um cartório redigitou o nome), não
litígio. Este comando troca o proxy pelo fato: dispara, por entidade, o OR de
`match_phrase` das `variantes_busca` contra `voyager-processos` e grava o total.

100% ES→ES — **nenhuma query no Postgres**. O DB de prod é contido e já derrubou
o site 2× nesta semana; aqui não há nada que ele precise responder.

DECISÕES DE OPERAÇÃO
====================
* **`_msearch`, não 1 request por entidade.** 182k round-trips seriam ~1h só de
  RTT. Em lote, o ES paraleliza as sub-buscas: medido 250 entidades/requisição
  em 0,24-1,40s (~350/s no miolo, ~2.000/s na cauda barata).
* **Lote adaptativo.** O ES serve a dashboard de produção. Cada `_msearch` é
  cronometrado; passou de `--alvo-lote-s`, o lote cai pela metade (piso 50) na
  hora e só volta a crescer quando fica rápido de novo. Mesmo instinto do freio
  do `construir_indice_entidades`, que protege o Postgres.
* **`update` parcial, nunca `index`.** O doc da entidade é o produto do build
  (variantes, documentos, consolidação). Reindexá-lo por causa de um número
  seria trocar o carro pelo retrovisor — e apagaria o build se o comando rodasse
  com um `_source` incompleto.
* **Idempotente.** `_id` determinístico + `update` parcial dos MESMOS 2 campos:
  rodar 2× reescreve o mesmo número (só o `n_processos_em` anda). Com
  `--somente-faltantes` a 2ª passada nem chega a consultar quem já foi contado,
  o que faz da retomada de uma corrida interrompida a operação mais barata.
* **Paginação por `search_after` no `entidade_id`.** Chave única e imutável, e o
  cursor só anda pra frente: `--desde` retoma exatamente de onde parou, e o fato
  de estarmos escrevendo nos docs já visitados não desloca o que vem depois
  (é o que quebraria com `from`/`size`).
* **Ausência ≠ zero.** Sub-busca que falhou ou total truncado viram `None` e o
  campo NÃO é gravado (ver `entidades.total_exato`). Zero gravado é sempre um
  zero MEDIDO.
"""
import json
import os
import statistics
import time
from datetime import datetime, timezone

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from search.entidades import (CONTAGEM_MIN_PARTES, INDICE,
                              MAX_CLAUSULAS_VARIANTES, doc_contagem,
                              escopo_contagem, query_contagem, total_exato)

#: piso do lote adaptativo — abaixo disso o overhead de RTT come o ganho
LOTE_MINIMO = 50

#: só o que a contagem precisa. `variantes`/`variantes_n` entram porque é de lá
#: que sai a frequência de cada grafia — sem ela `grafias_para_contagem` não
#: consegue distinguir o NOME ("INSTITUTO NACIONAL DO SEGURO SOCIAL", 610 linhas)
#: de um TRUNCAMENTO ("INSTITUTO NACIONAL", 1 linha) e o OR varre meio índice.
FONTE = ['entidade_id', 'nome_canonico', 'variantes_busca', 'variantes',
         'variantes_n', 'n_partes', 'eh_ente_publico', 'n_processos']


def _ocorrencias(fonte: dict) -> dict:
    """`{grafia: nº de linhas de Parte}` — `variantes` e `variantes_n` são
    listas PARALELAS no doc (é o contrato de `grupo_to_doc`)."""
    return dict(zip(fonte.get('variantes') or [],
                    fonte.get('variantes_n') or []))


class Command(BaseCommand):
    help = ('Conta processos reais (voyager-processos) por entidade via _msearch '
            'e grava n_processos/n_processos_em por _bulk update. Não toca no Postgres.')

    def add_arguments(self, parser):
        parser.add_argument('--indice', type=str, default=INDICE,
                            help=f'Sufixo do índice de entidades (default "{INDICE}"). '
                                 'Use "entidades-teste" pra validar sem tocar no final.')
        parser.add_argument('--indice-processos', type=str, default='processos',
                            help='Sufixo do índice contado (default "processos").')
        parser.add_argument('--min-partes', type=int, default=CONTAGEM_MIN_PARTES,
                            help=f'Escopo: n_partes >= N (default {CONTAGEM_MIN_PARTES}).')
        parser.add_argument('--sem-ente-publico', action='store_true',
                            help='NÃO incluir ente público fora do corte de n_partes.')
        parser.add_argument('--somente-faltantes', action='store_true',
                            help='Pula quem já tem n_processos (retomada barata).')
        parser.add_argument('--limite', type=int, default=0,
                            help='Máximo de entidades a contar (0 = todas do escopo).')
        parser.add_argument('--desde', type=str, default='',
                            help='Retomada: começa DEPOIS deste entidade_id (search_after).')
        parser.add_argument('--lote', type=int, default=250,
                            help='Entidades por _msearch (default 250). Encolhe sozinho.')
        parser.add_argument('--alvo-lote-s', type=float, default=3.0,
                            help='Duração alvo de um _msearch (s). Estourou, o lote cai pela metade.')
        parser.add_argument('--sleep', type=float, default=0.0,
                            help='Pausa (s) entre lotes — throttle explícito do ES.')
        parser.add_argument('--max-clausulas', type=int, default=0,
                            help='Teto de grafias no OR (0 = MAX_CLAUSULAS_VARIANTES).')
        parser.add_argument('--dry-run', action='store_true',
                            help='CONTA e relata (mede latência), mas não escreve no ES.')
        parser.add_argument('--checkpoint', type=str, default='',
                            help='Arquivo JSON com o cursor (retomada automática).')
        parser.add_argument('--top', type=int, default=0,
                            help='Imprime as N maiores por n_processos no fim (auditoria).')

    # ------------------------------------------------------------------ #
    def handle(self, *args, **opts):
        self.es_url = getattr(settings, 'ELASTICSEARCH_URL',
                              'http://elasticsearch:9200').rstrip('/')
        prefixo = getattr(settings, 'ELASTICSEARCH_INDEX_PREFIX', 'voyager')
        self.indice = f'{prefixo}-{opts["indice"]}'
        self.indice_proc = f'{prefixo}-{opts["indice_processos"]}'
        self.sessao = requests.Session()
        self.opts = opts

        escopo = escopo_contagem(min_partes=opts['min_partes'],
                                 incluir_ente_publico=not opts['sem_ente_publico'],
                                 somente_faltantes=opts['somente_faltantes'])
        total = self._contar_escopo(escopo)
        if not total:
            raise CommandError(f'Escopo vazio em {self.indice} — nada a contar.')

        alvo = min(total, opts['limite']) if opts['limite'] else total
        self.stdout.write(
            f'{self.indice} → contando em {self.indice_proc} · escopo {total:,} entidades '
            f'(n_partes>={opts["min_partes"]}'
            f'{" OU ente_público" if not opts["sem_ente_publico"] else ""}'
            f'{" · só faltantes" if opts["somente_faltantes"] else ""}) '
            f'· alvo {alvo:,} · lote={opts["lote"]}'
            f'{" [DRY-RUN]" if opts["dry_run"] else ""}')
        self.stdout.flush()

        if not opts['dry_run']:
            self._garantir_campos()

        r = self._rodar(escopo, alvo, opts)
        self._relatorio(r, alvo)
        if opts['top']:
            self._top(opts['top'])

    # ------------------------------------------------------------------ #
    # Índice
    # ------------------------------------------------------------------ #
    def _contar_escopo(self, escopo) -> int:
        r = self.sessao.post(f'{self.es_url}/{self.indice}/_count',
                             json={'query': escopo}, timeout=120)
        if r.status_code >= 300:
            raise CommandError(f'_count em {self.indice}: HTTP {r.status_code} '
                               f'{r.text[:300]}')
        return int(r.json().get('count', 0))

    def _garantir_campos(self):
        """PUT _mapping dos 2 campos novos — idempotente e não-destrutivo.

        Sem isso o ES criaria os campos por dynamic mapping na primeira escrita:
        `n_processos` viraria `long` (ok por sorte) e `n_processos_em` dependeria
        do date-detection. Tipo de campo no ES é pra sempre — melhor declarar.
        """
        corpo = {'properties': {'n_processos': {'type': 'long'},
                                'n_processos_em': {'type': 'date'}}}
        r = self.sessao.put(f'{self.es_url}/{self.indice}/_mapping',
                            json=corpo, timeout=60)
        if r.status_code >= 300:
            raise CommandError(f'PUT _mapping em {self.indice}: HTTP {r.status_code} '
                               f'{r.text[:300]}')

    # ------------------------------------------------------------------ #
    # Laço principal
    # ------------------------------------------------------------------ #
    def _pagina(self, escopo, tamanho, apos):
        """Página do índice de entidades — `search_after` no `entidade_id`."""
        corpo = {'size': tamanho, 'query': escopo, '_source': FONTE,
                 'sort': [{'entidade_id': 'asc'}], 'track_total_hits': False}
        if apos:
            corpo['search_after'] = [apos]
        r = self.sessao.post(f'{self.es_url}/{self.indice}/_search',
                             json=corpo, timeout=180)
        if r.status_code >= 300:
            raise CommandError(f'_search em {self.indice}: HTTP {r.status_code} '
                               f'{r.text[:300]}')
        return [h['_source'] for h in r.json()['hits']['hits']]

    def _msearch(self, entidades, max_clausulas):
        linhas = []
        for e in entidades:
            linhas.append(json.dumps({'index': self.indice_proc}))
            corpo = query_contagem(e.get('variantes_busca'),
                                   max_clausulas=max_clausulas,
                                   ocorrencias=_ocorrencias(e))
            linhas.append(json.dumps(corpo, ensure_ascii=False))
        dados = ('\n'.join(linhas) + '\n').encode('utf-8')
        t0 = time.monotonic()
        r = self.sessao.post(f'{self.es_url}/_msearch', data=dados,
                             headers={'Content-Type': 'application/x-ndjson'},
                             timeout=600)
        dt = time.monotonic() - t0
        if r.status_code >= 300:
            self.stderr.write(f'  _msearch HTTP {r.status_code}: {r.text[:300]}')
            return dt, []
        return dt, r.json().get('responses', [])

    def _bulk(self, atualizacoes):
        """`update` parcial — o resto do doc (o build) fica intacto."""
        linhas = []
        for _id, doc in atualizacoes:
            linhas.append(json.dumps({'update': {'_index': self.indice, '_id': _id}}))
            linhas.append(json.dumps({'doc': doc}, ensure_ascii=False))
        dados = ('\n'.join(linhas) + '\n').encode('utf-8')
        t0 = time.monotonic()
        try:
            r = self.sessao.post(f'{self.es_url}/_bulk', data=dados,
                                 headers={'Content-Type': 'application/x-ndjson'},
                                 timeout=300)
        except Exception as exc:            # noqa: BLE001 — ES hipou; é idempotente
            self.stderr.write(f'  _bulk ERRO: {str(exc)[:300]}')
            return time.monotonic() - t0, len(atualizacoes)
        dt = time.monotonic() - t0
        if r.status_code >= 300:
            self.stderr.write(f'  _bulk HTTP {r.status_code}: {r.text[:300]}')
            return dt, len(atualizacoes)
        resposta = r.json()
        if not resposta.get('errors'):
            return dt, 0
        falhas = [i for i in resposta.get('items', [])
                  if i.get('update', {}).get('error')]
        if falhas:
            self.stderr.write(f'  _bulk: {len(falhas)} item(ns) com erro — '
                              f'ex.: {json.dumps(falhas[0])[:250]}')
        return dt, len(falhas)

    def _rodar(self, escopo, alvo, opts):
        agora = datetime.now(timezone.utc).isoformat()
        max_cl = opts['max_clausulas'] or MAX_CLAUSULAS_VARIANTES
        lote_pedido = max(LOTE_MINIMO, opts['lote'])
        lote = lote_pedido
        cursor = opts['desde'] or ''

        vistas = contadas = zeros = sem_total = sem_variantes = erros_bulk = 0
        maior = ('', 0)
        t_msearch, t_bulk = [], []
        t0 = time.monotonic()
        n_lotes = 0

        while vistas < alvo:
            pegar = min(lote, alvo - vistas)
            entidades = self._pagina(escopo, pegar, cursor)
            if not entidades:
                break
            cursor = entidades[-1]['entidade_id']
            vistas += len(entidades)
            n_lotes += 1

            # entidade sem grafia de busca não tem OR possível: fica sem campo
            uteis = [e for e in entidades if (e.get('variantes_busca') or [])]
            sem_variantes += len(entidades) - len(uteis)

            respostas = []
            if uteis:
                dt, respostas = self._msearch(uteis, max_cl)
                t_msearch.append(dt)
                # _msearch inteiro falhou: as entidades do lote ficam SEM campo
                # (e o relatório precisa dizer isso — silêncio viraria "contamos")
                sem_total += max(0, len(uteis) - len(respostas))

            atualizacoes = []
            for e, resp in zip(uteis, respostas):
                n = total_exato(resp)
                if n is None:
                    sem_total += 1
                    continue
                contadas += 1
                zeros += (n == 0)
                if n > maior[1]:
                    maior = (e.get('nome_canonico') or e['entidade_id'], n)
                atualizacoes.append((e['entidade_id'], doc_contagem(n, agora)))

            if atualizacoes and not opts['dry_run']:
                dt_b, falhas = self._bulk(atualizacoes)
                t_bulk.append(dt_b)
                erros_bulk += falhas

            # --- freio: o ES serve a dashboard de produção -------------------
            if t_msearch and t_msearch[-1] > opts['alvo_lote_s'] and lote > LOTE_MINIMO:
                lote = max(LOTE_MINIMO, lote // 2)
                self.stdout.write(self.style.WARNING(
                    f'  _msearch levou {t_msearch[-1]:.1f}s (> {opts["alvo_lote_s"]}s) '
                    f'→ encolhendo o lote para {lote}'))
            elif (t_msearch and t_msearch[-1] < opts['alvo_lote_s'] / 3
                  and lote < lote_pedido):
                lote = min(lote_pedido, lote * 2)

            if n_lotes % 20 == 0 or vistas >= alvo:
                self._progresso(vistas, alvo, cursor, contadas, maior, t0,
                                t_msearch, lote)
            if opts['checkpoint'] and n_lotes % 20 == 0:
                self._checkpoint(opts['checkpoint'], cursor, vistas, contadas)
            if opts['sleep']:
                time.sleep(opts['sleep'])
            if len(entidades) < pegar:
                break

        if opts['checkpoint']:
            self._checkpoint(opts['checkpoint'], cursor, vistas, contadas, fim=True)
        if not opts['dry_run']:
            self.sessao.post(f'{self.es_url}/{self.indice}/_refresh', timeout=180)

        return {
            'vistas': vistas, 'contadas': contadas, 'zeros': zeros,
            'sem_total': sem_total, 'sem_variantes': sem_variantes,
            'erros_bulk': erros_bulk, 'lotes': n_lotes, 'cursor': cursor,
            'maior': maior, 'duracao_s': time.monotonic() - t0,
            't_msearch': t_msearch, 't_bulk': t_bulk, 'lote_final': lote,
            'quando': agora, 'max_clausulas': max_cl,
        }

    def _progresso(self, vistas, alvo, cursor, contadas, maior, t0, tempos, lote):
        el = time.monotonic() - t0
        taxa = vistas / el if el else 0
        eta = (alvo - vistas) / taxa / 60 if taxa and alvo > vistas else 0
        ult = tempos[-1] if tempos else 0.0
        self.stdout.write(
            f'  {vistas:,}/{alvo:,} ({100.0 * vistas / alvo:.1f}%) '
            f'· {taxa:,.0f} ent/s · lote {ult:.2f}s ({lote}) '
            f'· contadas {contadas:,} · maior {maior[1]:,} ({maior[0][:34]}) '
            f'· cursor {cursor[:26]} · ETA {eta:.0f}min')
        self.stdout.flush()

    def _checkpoint(self, caminho, cursor, vistas, contadas, fim=False):
        tmp = f'{caminho}.tmp'
        with open(tmp, 'w') as fh:
            json.dump({'desde': cursor, 'vistas': vistas, 'contadas': contadas,
                       'concluido': fim, 'quando': time.time()}, fh)
        os.replace(tmp, caminho)

    # ------------------------------------------------------------------ #
    # Relatório
    # ------------------------------------------------------------------ #
    def _relatorio(self, r, alvo):
        w = self.stdout.write
        seg = r['duracao_s']
        w('')
        w('── CONTAGEM ' + '─' * 54)
        w(f'  entidades visitadas .. {r["vistas"]:,} de {alvo:,} em {r["lotes"]} lotes')
        w(f'  contadas ............. {r["contadas"]:,} '
          f'(zeros medidos {r["zeros"]:,})')
        w(f'  SEM contagem ......... {r["sem_total"] + r["sem_variantes"]:,} '
          f'(sem variantes_busca {r["sem_variantes"]:,} · '
          f'resposta inutilizável {r["sem_total"]:,}) → campo NÃO gravado')
        w(f'  maior ................ {r["maior"][1]:,} processos '
          f'· {r["maior"][0][:60]}')
        w(f'  duração .............. {seg:.1f}s '
          f'({r["vistas"] / seg if seg else 0:,.0f} entidades/s)')
        w('── LATÊNCIA ' + '─' * 54)
        self._lat(w, '_msearch', r['t_msearch'], r['lote_final'])
        self._lat(w, '_bulk   ', r['t_bulk'], r['lote_final'])
        if r['erros_bulk']:
            w(self.style.WARNING(f'  itens com erro no _bulk: {r["erros_bulk"]:,}'))
        w(f'  cursor final ......... {r["cursor"]}   (use em --desde pra retomar)')
        w('')

    def _lat(self, w, rotulo, tempos, lote):
        if not tempos:
            w(f'  {rotulo} ........... (nenhum)')
            return
        ordenados = sorted(tempos)
        p95 = ordenados[min(len(ordenados) - 1, int(0.95 * len(ordenados)))]
        w(f'  {rotulo} ........... média {statistics.mean(tempos):.2f}s '
          f'· p95 {p95:.2f}s · PICO {max(tempos):.2f}s '
          f'({len(tempos)} lotes, lote final {lote})')

    def _top(self, n):
        corpo = {'size': n, 'query': {'exists': {'field': 'n_processos'}},
                 # `unmapped_type`: em --dry-run o campo pode nem existir ainda
                 # (o PUT _mapping só roda na escrita) e o sort daria HTTP 400
                 'sort': [{'n_processos': {'order': 'desc', 'unmapped_type': 'long'}}],
                 '_source': ['nome_canonico', 'n_processos', 'n_partes',
                             'chave', 'eh_ente_publico']}
        r = self.sessao.post(f'{self.es_url}/{self.indice}/_search',
                             json=corpo, timeout=120)
        if r.status_code >= 300:
            self.stderr.write(f'  top: HTTP {r.status_code}')
            return
        self.stdout.write(f'── TOP {n} POR n_processos ' + '─' * 44)
        for i, hit in enumerate(r.json()['hits']['hits'], 1):
            d = hit['_source']
            self.stdout.write(
                f'  {i:>3}. {d["n_processos"]:>10,} processos '
                f'· {d["n_partes"]:>6,} partes · {d["chave"]:<4} '
                f'· {"ente" if d["eh_ente_publico"] else "priv"} '
                f'· {d["nome_canonico"][:56]}')
        self.stdout.write('')
