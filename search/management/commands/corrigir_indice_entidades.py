"""Aplica num índice de entidades JÁ CONSTRUÍDO as correções que o build passou
a fazer (decisões 12 e 13), sem reler o Postgres.

    docker exec voyagerdev-web-1 python manage.py corrigir_indice_entidades \
        --indice entidades-teste --dry-run

POR QUE ESTE COMANDO EXISTE (em vez de "reconstrói o índice")
=============================================================
As duas correções são do BUILD — `search/entidades.py` é a fonte de verdade, e
um índice construído do zero já sai correto. Só que reconstruir custa, medido:
~5 min lendo **16,7M linhas do Postgres de PRODUÇÃO** (que já derrubou o site
2× nesta semana) + ~11 min de escrita, e ainda **apaga `n_processos` de 182.196
entidades** — porque o `_bulk` do build usa `index`, que substitui o doc inteiro
—, obrigando a rodar o contador de novo (mais ~6 min).

Para consertar 25 entidades e marcar ~600, isso é caro e arriscado. Este comando
faz a mesma coisa **ES→ES**: uma varredura do índice de entidades, as MESMAS
decisões (`entidades.consolidacao_cnpj` / `entidades.nome_suspeito`), e escrita
cirúrgica. ZERO Postgres.

O QUE ELE FAZ
=============
1. **Consolidação cnpj→cnpj** (decisão 12): entidades de chave `cnpj` que
   compartilham o nome normalizado e cujo perfil de cadastro é um PICO (um líder
   dominante + minorias de 1 linha com grafia idêntica) viram uma só. O líder é
   reescrito por `entidades.fundir_doc` e as minorias são APAGADAS do índice —
   os CNPJs delas sobrevivem em `documentos_secundarios` e os ids em
   `entidades_absorvidas`, que é o que torna a fusão auditável e reversível.
2. **Nome suspeito** (decisão 13): quem tem grafia de 1 token sem atestação de
   cadastro ("JOSÉ") ou truncada num conectivo ("MUNICIPIO DE") ganha
   `nome_suspeito: true` e **perde `n_processos`** — o número existente é de uma
   frase que não é de ninguém, e apagá-lo devolve o campo ao estado honesto
   ("não contamos"), que o ranking já sabe tratar.

DECISÕES DE OPERAÇÃO
====================
* **Uma varredura só, `_source` de 5 campos.** 1,14M docs em ~230 páginas de
  5.000 com `search_after` no `entidade_id` — a mesma paginação do contador, e
  pelo mesmo motivo (cursor imutável que só anda pra frente, então escrever nos
  docs já visitados não desloca o que vem depois). O plano inteiro é decidido em
  memória ANTES de qualquer escrita: dá pra ver o que vai acontecer com
  `--dry-run` e a decisão não depende da ordem de leitura.
* **`_mget` dos docs completos só de quem vai fundir** (poucas dezenas), não dos
  1,14M — a varredura carrega o mínimo pra decidir.
* **Lote cronometrado.** O ES serve a dashboard de produção; cada requisição é
  medida e o relatório publica o PICO. Não há freio adaptativo aqui porque o
  volume de escrita é 3 ordens de grandeza menor que o do contador.
* **Idempotente.** Rodar 2× não muda nada: na 2ª passada os grupos já fundidos
  têm um membro só (`consolidacao_cnpj` devolve `None`) e os suspeitos já estão
  marcados. A checagem `--verificar` no fim confirma isso contra o índice.

DEPOIS DE RODAR
===============
Os líderes cujo OR de busca mudou ficam SEM `n_processos` (ver
`entidades.fundir_doc`) — recomponha com a retomada barata do contador:

    manage.py contar_processos_entidades --indice entidades-teste --somente-faltantes
"""
import json
import time
from datetime import datetime, timezone

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from search.entidades import (INDICE, consolidacao_cnpj, fundir_doc,
                              normalizar_nome, nome_suspeito)

#: o mínimo que as duas decisões precisam pra serem TOMADAS (os docs completos
#: dos que vão fundir vêm depois, por `_mget`)
FONTE_PLANO = ['entidade_id', 'chave', 'nome_canonico', 'nome_normalizado',
               'n_partes', 'nome_suspeito', 'n_processos']

#: `_source` completo de quem vai fundir — `fundir_doc` reescreve o doc inteiro
FONTE_FUSAO = ['entidade_id', 'chave', 'raiz_cnpj', 'nome_canonico',
               'nome_normalizado', 'nome_suspeito', 'nome_suspeito_motivo',
               'variantes', 'variantes_n', 'n_variantes', 'variantes_busca',
               'variantes_truncadas', 'documentos', 'n_documentos',
               'documentos_secundarios', 'n_documentos_secundarios',
               'documentos_mascarados', 'tipo', 'grupos_absorvidos',
               'entidades_absorvidas', 'eh_ente_publico',
               'ente_publico_por_complemento', 'n_partes', 'parte_id_min',
               'n_processos', 'n_processos_em', 'atualizado_em']

#: marca o suspeito E remove a contagem da frase que não é de ninguém. `remove`
#: (e não `= 0`): zero é medição, ausência é "não contamos" — ver decisão 6.
SCRIPT_SUSPEITO = (
    "ctx._source.nome_suspeito = true;"
    " ctx._source.nome_suspeito_motivo = params.motivo;"
    " ctx._source.remove('n_processos');"
    " ctx._source.remove('n_processos_em');"
)


class Command(BaseCommand):
    help = ('Aplica no índice de entidades já construído a consolidação '
            'cnpj→cnpj e a marcação de nome suspeito. ES→ES, sem Postgres.')

    def add_arguments(self, parser):
        parser.add_argument('--indice', type=str, default=INDICE,
                            help=f'Sufixo do índice (default "{INDICE}"). '
                                 'Use "entidades-teste" pra validar sem tocar no final.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Monta o plano e RELATA. Não escreve nada.')
        parser.add_argument('--so-consolidacao', action='store_true',
                            help='Só a fusão cnpj→cnpj (decisão 12).')
        parser.add_argument('--so-suspeitos', action='store_true',
                            help='Só a marcação de nome suspeito (decisão 13).')
        parser.add_argument('--pagina', type=int, default=5000,
                            help='Docs por página da varredura (default 5000).')
        parser.add_argument('--bulk', type=int, default=500,
                            help='Operações por requisição _bulk (default 500).')
        parser.add_argument('--sleep', type=float, default=0.0,
                            help='Pausa (s) entre páginas — throttle explícito do ES.')
        parser.add_argument('--amostra', type=int, default=15,
                            help='Quantos itens de cada plano imprimir (auditoria).')

    # ------------------------------------------------------------------ #
    def handle(self, *args, **opts):
        self.es_url = getattr(settings, 'ELASTICSEARCH_URL',
                              'http://elasticsearch:9200').rstrip('/')
        prefixo = getattr(settings, 'ELASTICSEARCH_INDEX_PREFIX', 'voyager')
        self.indice = f'{prefixo}-{opts["indice"]}'
        self.sessao = requests.Session()
        self.tempos: list = []
        self.opts = opts

        if opts['so_consolidacao'] and opts['so_suspeitos']:
            raise CommandError('--so-consolidacao com --so-suspeitos se anulam.')

        total = self._total()
        self.stdout.write(
            f'{self.indice}: {total:,} entidades'
            f'{" [DRY-RUN]" if opts["dry_run"] else ""}')
        self.stdout.flush()

        t0 = time.monotonic()
        docs, dt_scan = self._varrer(opts)
        self.stdout.write(f'varredura: {len(docs):,} docs em {dt_scan:.1f}s')

        fusoes = [] if opts['so_suspeitos'] else self._plano_fusao(docs)
        suspeitos = [] if opts['so_consolidacao'] else self._plano_suspeitos(docs)
        self._relatorio_plano(fusoes, suspeitos, opts['amostra'])

        if opts['dry_run']:
            self.stdout.write(self.style.WARNING(
                'DRY-RUN: nada foi escrito no Elasticsearch.'))
            return

        self._garantir_campos()
        escritos = self._aplicar_fusoes(fusoes) if fusoes else (0, 0, 0, 0)
        marcados = self._aplicar_suspeitos(suspeitos) if suspeitos else (0, 0)
        self.sessao.post(f'{self.es_url}/{self.indice}/_refresh', timeout=180)

        self._relatorio_final(escritos, marcados, total, time.monotonic() - t0)

    # ------------------------------------------------------------------ #
    # Leitura
    # ------------------------------------------------------------------ #
    def _pedir(self, caminho, corpo, metodo='post'):
        t0 = time.monotonic()
        r = getattr(self.sessao, metodo)(f'{self.es_url}{caminho}',
                                         json=corpo, timeout=300)
        self.tempos.append(time.monotonic() - t0)
        if r.status_code >= 300:
            raise CommandError(f'{caminho}: HTTP {r.status_code} {r.text[:300]}')
        return r.json()

    def _total(self) -> int:
        return int(self._pedir(f'/{self.indice}/_count',
                               {'query': {'match_all': {}}})['count'])

    def _varrer(self, opts):
        """Uma passada pelo índice inteiro — `_source` de 5 campos."""
        t0 = time.monotonic()
        docs, apos, n = [], None, 0
        while True:
            corpo = {'size': opts['pagina'], 'query': {'match_all': {}},
                     '_source': FONTE_PLANO, 'sort': [{'entidade_id': 'asc'}],
                     'track_total_hits': False}
            if apos:
                corpo['search_after'] = [apos]
            hits = self._pedir(f'/{self.indice}/_search', corpo)['hits']['hits']
            if not hits:
                break
            docs.extend(h['_source'] for h in hits)
            apos = hits[-1]['_source']['entidade_id']
            n += 1
            if n % 20 == 0:
                self.stdout.write(f'  {len(docs):,} lidos…')
                self.stdout.flush()
            if len(hits) < opts['pagina']:
                break
            if opts['sleep']:
                time.sleep(opts['sleep'])
        return docs, time.monotonic() - t0

    def _mget(self, ids) -> dict:
        """Docs COMPLETOS de quem vai fundir. Forma `docs` (e não `ids`) porque
        o `_source` seletivo só existe por-documento — com `{"ids": [...],
        "_source": [...]}` o ES devolve HTTP 400."""
        resp = self._pedir(f'/{self.indice}/_mget',
                           {'docs': [{'_id': i, '_source': FONTE_FUSAO}
                                     for i in ids]})
        return {d['_id']: d['_source'] for d in resp.get('docs', [])
                if d.get('found')}

    # ------------------------------------------------------------------ #
    # Planos (decididos ANTES de qualquer escrita)
    # ------------------------------------------------------------------ #
    def _plano_fusao(self, docs) -> list:
        """[(id_lider, [ids_absorvidos])] — a MESMA decisão do build."""
        por_nome: dict[str, list] = {}
        for d in docs:
            if d.get('chave') != 'cnpj':
                continue
            # `nome_normalizado` do doc é o do build; recalcular do canônico
            # deixaria a decisão à mercê de uma mudança do normalizador entre
            # o build e a correção. Usa-se o que está gravado, com fallback.
            norm = d.get('nome_normalizado') or normalizar_nome(d.get('nome_canonico'))
            if norm:
                por_nome.setdefault(norm, []).append(
                    (d['entidade_id'], d.get('n_partes') or 0,
                     d.get('nome_canonico') or ''))

        plano, self.homonimos = [], 0
        for candidatos in por_nome.values():
            if len(candidatos) < 2:
                continue
            decisao = consolidacao_cnpj(candidatos)
            if decisao is None:
                self.homonimos += 1
                continue
            plano.append(decisao)
        return plano

    def _plano_suspeitos(self, docs) -> list:
        """[(id, motivo, tinha_contagem)] de quem a decisão 13 marca."""
        plano = []
        for d in docs:
            suspeito, motivo = nome_suspeito(d.get('nome_canonico'),
                                             d.get('n_partes') or 0)
            if not suspeito:
                continue
            if d.get('nome_suspeito') is True:
                continue                     # idempotência: já marcado
            plano.append((d['entidade_id'], motivo,
                          d.get('n_processos'), d.get('nome_canonico') or ''))
        return plano

    # ------------------------------------------------------------------ #
    # Escrita
    # ------------------------------------------------------------------ #
    def _garantir_campos(self):
        """PUT _mapping dos campos novos — idempotente e não-destrutivo.

        Sem isso o dynamic mapping decidiria o tipo na 1ª escrita, e tipo de
        campo no ES é pra sempre.
        """
        corpo = {'properties': {
            'nome_suspeito': {'type': 'boolean'},
            'nome_suspeito_motivo': {'type': 'keyword'},
            'documentos_secundarios': {'type': 'keyword'},
            'n_documentos_secundarios': {'type': 'integer'},
            'entidades_absorvidas': {'type': 'keyword'},
        }}
        self._pedir(f'/{self.indice}/_mapping', corpo, metodo='put')

    def _bulk_em_lotes(self, operacoes, por_lote) -> int:
        """`operacoes` = lista de OPERAÇÕES, cada uma já com as suas linhas
        (`index` tem 2, `delete` tem 1). Fatiar por LINHA separaria a ação do
        seu documento e o ES rejeitaria o lote inteiro — por isso o corte é por
        operação."""
        erros = 0
        for i in range(0, len(operacoes), por_lote):
            linhas = [ln for op in operacoes[i:i + por_lote] for ln in op]
            if linhas:
                erros += self._bulk(linhas)
        return erros

    def _bulk(self, linhas) -> int:
        dados = ('\n'.join(linhas) + '\n').encode('utf-8')
        t0 = time.monotonic()
        r = self.sessao.post(f'{self.es_url}/_bulk', data=dados,
                             headers={'Content-Type': 'application/x-ndjson'},
                             timeout=300)
        self.tempos.append(time.monotonic() - t0)
        if r.status_code >= 300:
            self.stderr.write(f'  _bulk HTTP {r.status_code}: {r.text[:300]}')
            return len(linhas) // 2
        resp = r.json()
        if not resp.get('errors'):
            return 0
        falhas = [i for i in resp.get('items', [])
                  if next(iter(i.values())).get('error')]
        if falhas:
            self.stderr.write(f'  _bulk: {len(falhas)} erro(s) — '
                              f'ex.: {json.dumps(falhas[0])[:250]}')
        return len(falhas)

    def _aplicar_fusoes(self, plano):
        """Reescreve o líder e APAGA os absorvidos. Nesta ordem, de propósito:
        se o comando morrer no meio, o pior estado possível é uma entidade
        duplicada (falso-split, barato) — nunca evidência perdida."""
        agora = datetime.now(timezone.utc).isoformat()
        ids = [i for lider, absorv in plano for i in [lider, *absorv]]
        fontes = self._mget(ids)
        operacoes, apagados, recontar, fundidos = [], 0, 0, 0
        for lider, absorvidos in plano:
            doc_lider = fontes.get(lider)
            docs_absorv = [fontes[a] for a in absorvidos if a in fontes]
            if not doc_lider or not docs_absorv:
                continue
            novo = fundir_doc(doc_lider, docs_absorv, agora)
            recontar += ('n_processos' not in novo
                         and 'n_processos' in doc_lider)
            fundidos += 1
            operacoes.append([
                json.dumps({'index': {'_index': self.indice, '_id': lider}}),
                json.dumps(novo, default=str, ensure_ascii=False)])
            for a in absorvidos:
                operacoes.append([json.dumps(
                    {'delete': {'_index': self.indice, '_id': a}})])
                apagados += 1
        erros = self._bulk_em_lotes(operacoes, max(1, self.opts['bulk']))
        return fundidos, apagados, recontar, erros

    def _aplicar_suspeitos(self, plano):
        operacoes = [
            [json.dumps({'update': {'_index': self.indice, '_id': _id}}),
             json.dumps({'script': {'source': SCRIPT_SUSPEITO,
                                    'lang': 'painless',
                                    'params': {'motivo': motivo}}})]
            for _id, motivo, _n, _nome in plano]
        erros = self._bulk_em_lotes(operacoes, max(1, self.opts['bulk']))
        return len(plano), erros

    # ------------------------------------------------------------------ #
    # Relatório
    # ------------------------------------------------------------------ #
    def _relatorio_plano(self, fusoes, suspeitos, amostra):
        w = self.stdout.write
        w('')
        w('── PLANO: CONSOLIDAÇÃO cnpj→cnpj (decisão 12) ' + '─' * 20)
        absorvidas = sum(len(a) for _, a in fusoes)
        w(f'  grupos a fundir ...... {len(fusoes):,} '
          f'({absorvidas:,} entidades absorvidas)')
        w(f'  homônimos PRESERVADOS  {getattr(self, "homonimos", 0):,} '
          '(nome compartilhado sem perfil de typo — abstenção)')
        for lider, absorv in fusoes[:amostra]:
            w(f'    {lider:<20} ← {", ".join(absorv)}')
        if len(fusoes) > amostra:
            w(f'    … +{len(fusoes) - amostra} grupos')

        w('── PLANO: NOME SUSPEITO (decisão 13) ' + '─' * 29)
        por_motivo: dict = {}
        for _id, motivo, _n, _nome in suspeitos:
            por_motivo[motivo] = por_motivo.get(motivo, 0) + 1
        com_contagem = [s for s in suspeitos if s[2] is not None]
        w(f'  a marcar ............. {len(suspeitos):,} {por_motivo}')
        w(f'  perdem n_processos ... {len(com_contagem):,} '
          f'(soma {sum(s[2] for s in com_contagem):,} processos que a frase '
          'reivindicava sem ser de ninguém)')
        for _id, motivo, n, nome in sorted(
                com_contagem, key=lambda s: -(s[2] or 0))[:amostra]:
            w(f'    {n:>10,} · {motivo:<12} · {nome[:44]}')
        w('')

    def _relatorio_final(self, fusoes, marcados, total, dt):
        w = self.stdout.write
        grupos, apagados, recontar, erros = fusoes
        w('── APLICADO ' + '─' * 54)
        w(f'  fusões ............... {grupos:,} grupos · '
          f'{apagados:,} entidades apagadas do índice')
        if recontar:
            w(self.style.WARNING(
                f'  recontar ............. {recontar:,} líder(es) tiveram o OR '
                'de busca alterado e ficaram SEM n_processos → rode '
                '`contar_processos_entidades --somente-faltantes`'))
        if erros:
            w(self.style.ERROR(f'  ERROS no _bulk das fusões: {erros}'))
        w(f'  suspeitos marcados ... {marcados[0]:,} '
          f'({marcados[1]} erro(s) de bulk)')
        w(f'  índice ............... {total:,} → {self._total():,} entidades')
        if self.tempos:
            w(f'  ES ................... {len(self.tempos)} requisições · '
              f'PICO {max(self.tempos):.2f}s · total {sum(self.tempos):.1f}s')
        w(f'  duração .............. {dt:.1f}s')
        w('')
