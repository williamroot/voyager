"""Funde no índice de entidades JÁ CONSTRUÍDO as FACETAS do mesmo devedor
(decisão 14 de `search/entidades.py`). ES→ES, zero Postgres.

    docker exec voyagerdev-web-1 python manage.py consolidar_entidades \
        --indice entidades-teste --dry-run

O DEFEITO
=========
Contadas as 182.026 entidades do escopo, o top-20 de "quem mais deve no Brasil"
abria com OITO linhas do INSS e TRÊS da União:

     1  4.402.239  768l  cnpj  INSTITUTO NACIONAL DO SEGURO SOCIAL
     2  4.255.175   53l  cnpj  INSS
     3  4.174.336   22l  nome  INSTITUTO NACIONAL DO SEGURO SOCIAL INSS
     4  2.087.983    4l  nome  CEAB - INSS
     9  1.232.679    1l  cnpj  UNIÃO FEDERAL
    10  1.232.679    1l  cnpj  UNIÃO FEDERAL
    11  1.232.679  119l  nome  UNIAO FEDERAL
    13  1.180.434    6l  nome  CEAB-DJ INSS
    14  1.175.484    1l  nome  Procuradoria da CEAB-DJ INSS

Nenhuma linha está errada: `n_processos` mede uma FRASE e o CEAB é setor do
INSS. Mas uma tela de devedores precisa de uma linha por devedor.

O QUE ESTE COMANDO FAZ (duas passadas, a MESMA decisão do build)
================================================================
1. **Grafia idêntica cruzando as chaves** (`entidades.consolidacao_cnpj`, agora
   sem exigir CNPJ dos dois lados): as três "UNIÃO FEDERAL" viram uma. Medido: 6
   grupos no índice inteiro (UNIÃO FEDERAL, ESTADO DE ALAGOAS e os municípios de
   Juiz de Fora, Belo Horizonte, Poços de Caldas e Contagem) — todos entidade
   ÚNICA no país, nenhum homônimo. A dominância aqui é sobre a atestação
   PRÓPRIA (`n_partes` − `n_partes_absorvidas`): sem isso o cadastro que a
   passada 2 empresta viraria dominância emprestada na rodada seguinte.
2. **Facetas** (`entidades.plano_facetas`): quem não tem CNPJ próprio funde na
   entidade provada por CNPJ cujo nome inteiro está dentro do nome dela — ou de
   quem ela é a sigla atestada. Dona ambígua abstém. Medido: 10.584 fusões em
   164 donas contra 13.222 abstenções.

O QUE ELE NÃO É: idempotente no sentido forte. Fundir muda a paisagem — a
entidade "INSS" (sigla) sai do índice, e com ela sai uma dona possível, o que
pode desempatar na rodada seguinte um caso antes ambíguo. O comando CONVERGE (a
2ª passada propõe ~0,4% do que a 1ª propôs) e os freios (atestação própria,
recusa por encolhimento) o mantêm preso, mas a leitura correta é: **rode uma vez
depois de cada build**, e leia o `--dry-run` antes de repetir.

POR QUE UM COMANDO E NÃO UM REBUILD
===================================
Mesma razão do `corrigir_indice_entidades`: reconstruir custa ~17 min relendo
16,7M linhas do Postgres de PRODUÇÃO e **apaga `n_processos` de 182k entidades**.
Aqui se lê só o ES e se escreve cirurgicamente — o líder é reescrito por
`entidades.fundir_doc` (o mesmo do build) e as facetas são APAGADAS, com os ids
guardados em `entidades_absorvidas` e os documentos em `documentos_secundarios`:
auditável e reversível.

DEPOIS DE RODAR
===============
Os líderes cujo OR de busca mudou ficam SEM `n_processos` (contrato do
`fundir_doc`: número velho é pior que ausência). Recomponha com a retomada
barata do contador:

    manage.py contar_processos_entidades --indice entidades-teste --somente-faltantes

O número do líder NÃO encolhe: a prova 2 da decisão 14 garante que o OR da dona
já casava os processos da faceta, e o que a prova não cobre (o ponto cego da
poda) é recusado ANTES de escrever — ver `_recusar_quem_encolhe`. `--verificar`
reconta depois e confirma.

Medido na aplicação real (13/08, `voyager-entidades-teste`): 1.141.610 →
1.131.058 entidades, 164 líderes reescritos, 6 fusões recusadas por encolhimento,
ES com PICO de 1,19s por requisição. O INSS canônico saiu com os MESMOS
4.402.239 processos.
"""
import json
import time
from datetime import datetime, timezone

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from search.entidades import (INDICE, consolidacao_cnpj, fundir_doc,
                              normalizar_nome, plano_facetas, query_contagem,
                              total_exato)

#: o mínimo pra decidir a passada 1 (grafia idêntica) — 1,14M docs
FONTE_GRAFIA = ['entidade_id', 'chave', 'nome_canonico', 'nome_normalizado',
                'n_partes', 'n_partes_absorvidas', 'nome_suspeito']

#: a passada 2 (facetas) precisa das grafias, e só corre no escopo CONTADO —
#: quem não foi contado não disputa o ranking que a decisão 14 conserta
FONTE_FACETA = FONTE_GRAFIA + ['variantes', 'variantes_n', 'n_processos']

#: `_source` completo de quem vai fundir — `fundir_doc` reescreve o doc inteiro
FONTE_FUSAO = ['entidade_id', 'chave', 'raiz_cnpj', 'nome_canonico',
               'nome_normalizado', 'nome_suspeito', 'nome_suspeito_motivo',
               'variantes', 'variantes_n', 'n_variantes', 'variantes_busca',
               'variantes_truncadas', 'documentos', 'n_documentos',
               'documentos_secundarios', 'n_documentos_secundarios',
               'documentos_mascarados', 'tipo', 'grupos_absorvidos',
               'entidades_absorvidas', 'eh_ente_publico',
               'ente_publico_por_complemento', 'n_partes',
               'n_partes_absorvidas', 'parte_id_min',
               'n_processos', 'n_processos_em', 'atualizado_em']


class Command(BaseCommand):
    help = ('Funde as facetas do mesmo devedor no índice de entidades '
            '(decisão 14). ES→ES, sem Postgres.')

    def add_arguments(self, parser):
        parser.add_argument('--indice', type=str, default=INDICE,
                            help=f'Sufixo do índice (default "{INDICE}").')
        parser.add_argument('--dry-run', action='store_true',
                            help='Monta o plano e RELATA. Não escreve nada.')
        parser.add_argument('--so-grafia', action='store_true',
                            help='Só a passada 1 (grafia idêntica cruzando chaves).')
        parser.add_argument('--so-facetas', action='store_true',
                            help='Só a passada 2 (decisão 14).')
        parser.add_argument('--pagina', type=int, default=5000,
                            help='Docs por página da varredura (default 5000).')
        parser.add_argument('--bulk', type=int, default=500,
                            help='Operações por requisição _bulk (default 500).')
        parser.add_argument('--sleep', type=float, default=0.0,
                            help='Pausa (s) entre páginas — throttle do ES.')
        parser.add_argument('--amostra', type=int, default=20,
                            help='Quantos itens de cada plano imprimir.')
        parser.add_argument('--verificar', action='store_true',
                            help='Reconta os líderes no fim e compara com o antes.')

    # ------------------------------------------------------------------ #
    def handle(self, *args, **opts):
        self.es_url = getattr(settings, 'ELASTICSEARCH_URL',
                              'http://elasticsearch:9200').rstrip('/')
        prefixo = getattr(settings, 'ELASTICSEARCH_INDEX_PREFIX', 'voyager')
        self.indice = f'{prefixo}-{opts["indice"]}'
        self.sessao = requests.Session()
        self.tempos: list = []
        self.opts = opts
        self.rotulos: dict = {}
        self.antes: dict = {}
        self.indice_processos = f'{prefixo}-processos'

        if opts['so_grafia'] and opts['so_facetas']:
            raise CommandError('--so-grafia com --so-facetas se anulam.')

        total = self._total()
        self.stdout.write(f'{self.indice}: {total:,} entidades'
                          f'{" [DRY-RUN]" if opts["dry_run"] else ""}')
        self.stdout.flush()
        t0 = time.monotonic()

        plano: dict = {}
        if not opts['so_facetas']:
            plano.update(self._plano_grafia(opts))
        if not opts['so_grafia']:
            plano.update(self._plano_facetas(opts, ja_absorvidos=set(plano)))

        self._relatorio_plano(plano, opts['amostra'])
        if opts['dry_run']:
            self.stdout.write(self.style.WARNING(
                'DRY-RUN: nada foi escrito no Elasticsearch.'))
            return

        self._garantir_campos()
        escritos = self._aplicar(plano)
        self.sessao.post(f'{self.es_url}/{self.indice}/_refresh', timeout=180)
        self._relatorio_final(escritos, total, time.monotonic() - t0)
        if opts['verificar']:
            self._verificar()

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

    def _varrer(self, fonte, query, pagina, sleep=0.0):
        """`search_after` no `entidade_id`: cursor imutável que só anda pra
        frente, então escrever no que já passou não desloca o que vem."""
        docs, apos, n = [], None, 0
        while True:
            corpo = {'size': pagina, 'query': query, '_source': fonte,
                     'sort': [{'entidade_id': 'asc'}], 'track_total_hits': False}
            if apos:
                corpo['search_after'] = [apos]
            hits = self._pedir(f'/{self.indice}/_search', corpo)['hits']['hits']
            if not hits:
                break
            docs.extend(h['_source'] for h in hits)
            apos = hits[-1]['_source']['entidade_id']
            n += 1
            if n % 25 == 0:
                self.stdout.write(f'  {len(docs):,} lidos…')
                self.stdout.flush()
            if len(hits) < pagina:
                break
            if sleep:
                time.sleep(sleep)
        return docs

    def _mget(self, ids) -> dict:
        """Docs COMPLETOS de quem vai fundir. Forma `docs` (e não `ids`) porque
        o `_source` seletivo só existe por-documento."""
        fontes: dict = {}
        for i in range(0, len(ids), 1000):
            resp = self._pedir(f'/{self.indice}/_mget',
                               {'docs': [{'_id': x, '_source': FONTE_FUSAO}
                                         for x in ids[i:i + 1000]]})
            fontes.update({d['_id']: d['_source'] for d in resp.get('docs', [])
                           if d.get('found')})
        return fontes

    # ------------------------------------------------------------------ #
    # Planos (decididos ANTES de qualquer escrita)
    # ------------------------------------------------------------------ #
    def _plano_grafia(self, opts) -> dict:
        """Passada 1: grafia idêntica cruzando as chaves (`{absorvido: líder}`)."""
        t0 = time.monotonic()
        docs = self._varrer(FONTE_GRAFIA, {'match_all': {}},
                            opts['pagina'], opts['sleep'])
        self.stdout.write(f'varredura completa: {len(docs):,} docs em '
                          f'{time.monotonic() - t0:.1f}s')
        self.rotulos = {d['entidade_id']: d for d in docs}

        por_nome: dict = {}
        for d in docs:
            if d.get('nome_suspeito'):
                continue
            norm = d.get('nome_normalizado') or normalizar_nome(d.get('nome_canonico'))
            if norm:
                # dominância sobre a atestação PRÓPRIA: sem descontar o que a
                # entidade já absorveu, a passada vira bola de neve — medido, a
                # "SECRETARIA DE SAÚDE" (19 linhas próprias, 146 depois de
                # absorver facetas) engoliu 3 secretarias municipais de 1 linha
                # numa 2ª rodada, que são entidades DIFERENTES.
                por_nome.setdefault(norm, []).append(
                    (d['entidade_id'],
                     (d.get('n_partes') or 0) - (d.get('n_partes_absorvidas') or 0),
                     d.get('nome_canonico') or ''))

        plano, self.homonimos_grafia = {}, 0
        self.grupos_grafia = 0
        for candidatos in por_nome.values():
            if len(candidatos) < 2:
                continue
            if not any(c[0].startswith('nome:') for c in candidatos):
                continue                      # cnpj↔cnpj é o corrigir_indice
            decisao = consolidacao_cnpj(candidatos)
            if decisao is None:
                self.homonimos_grafia += 1
                continue
            lider, absorvidos = decisao
            self.grupos_grafia += 1
            for a in absorvidos:
                plano[a] = lider
        return plano

    def _plano_facetas(self, opts, ja_absorvidos) -> dict:
        """Passada 2: decisão 14, no escopo CONTADO (quem disputa o ranking)."""
        t0 = time.monotonic()
        docs = self._varrer(FONTE_FACETA, {'exists': {'field': 'n_processos'}},
                            max(1, opts['pagina'] // 2), opts['sleep'])
        self.stdout.write(f'varredura contadas: {len(docs):,} docs em '
                          f'{time.monotonic() - t0:.1f}s')
        for d in docs:
            self.rotulos.setdefault(d['entidade_id'], d)
            self.rotulos[d['entidade_id']].setdefault(
                'n_processos', d.get('n_processos'))
        docs = [d for d in docs if d['entidade_id'] not in ja_absorvidos]
        plano, self.stats_facetas = plano_facetas(docs)
        self.stdout.write(f'plano de facetas em {time.monotonic() - t0:.1f}s')
        return plano

    # ------------------------------------------------------------------ #
    # Escrita
    # ------------------------------------------------------------------ #
    def _garantir_campos(self):
        """PUT _mapping do campo novo — idempotente e não-destrutivo. Sem isso o
        dynamic mapping decidiria o tipo na 1ª escrita, e tipo no ES é pra
        sempre."""
        r = self.sessao.put(
            f'{self.es_url}/{self.indice}/_mapping',
            json={'properties': {'n_partes_absorvidas': {'type': 'integer'}}},
            timeout=120)
        if r.status_code >= 300:
            # o campo já existe com outro tipo numérico (índice que recebeu a
            # escrita dinâmica antes deste comando) — `long` serve igual, e tipo
            # no ES é pra sempre. Não é motivo pra abortar a consolidação.
            self.stderr.write(f'  _mapping: {r.text[:200]}')

    def _aplicar(self, plano):
        """Reescreve o líder e APAGA os absorvidos, nesta ordem: se o comando
        morrer no meio, o pior estado é uma entidade duplicada (falso-split,
        barato) — nunca evidência perdida."""
        agora = datetime.now(timezone.utc).isoformat()
        por_lider: dict = {}
        for absorvido, lider in plano.items():
            por_lider.setdefault(lider, []).append(absorvido)

        ids = sorted(set(por_lider) | set(plano))
        fontes = self._mget(ids)
        self.antes = {l: fontes.get(l, {}).get('n_processos') for l in por_lider}

        novos: dict = {}
        for lider, absorvidos in por_lider.items():
            doc_lider = fontes.get(lider)
            # a ORDEM importa: `fundir_doc` respeita MAX_VARIANTES, então as
            # grafias de quem mais aparece em processo entram primeiro
            docs_abs = sorted((fontes[a] for a in absorvidos if a in fontes),
                              key=lambda d: (-(d.get('n_processos') or 0),
                                             -(d.get('n_partes') or 0)))
            if doc_lider and docs_abs:
                novos[lider] = fundir_doc(doc_lider, docs_abs, agora)

        self.encolheriam = self._recusar_quem_encolhe(novos)

        operacoes, apagados, recontar, fundidos = [], 0, 0, 0
        for lider, novo in novos.items():
            recontar += ('n_processos' not in novo
                         and 'n_processos' in fontes[lider])
            fundidos += 1
            operacoes.append([
                json.dumps({'index': {'_index': self.indice, '_id': lider}}),
                json.dumps(novo, default=str, ensure_ascii=False)])
            for a in por_lider[lider]:
                operacoes.append([json.dumps(
                    {'delete': {'_index': self.indice, '_id': a}})])
                apagados += 1
        erros = self._bulk_em_lotes(operacoes, max(1, self.opts['bulk']))
        return fundidos, apagados, recontar, erros

    def _recusar_quem_encolhe(self, novos: dict) -> list:
        """Descarta a fusão que faria o número do líder ENCOLHER. Muda `novos`.

        A prova 2 da decisão 14 é sobre o CONJUNTO de processos (o da faceta está
        contido no da dona), mas o `n_processos` não é o conjunto: é o conjunto
        visto pelo OR **podado** (`grafias_para_contagem`) e cortado em
        `MAX_CLAUSULAS_VARIANTES`. E a poda tem um ponto cego aqui — ela derruba
        a grafia CURTA quando a longa que a engole é ao menos tão frequente, e
        toda grafia de faceta é, por construção, uma versão LONGA da grafia da
        dona. Numa dona de cadastro raso (todas as grafias com 1 linha) o empate
        1×1 tira o nome real do OR e a contagem desaba.

        Medido na 1ª aplicação: 6 dos 164 líderes encolheram — "Desenvolve Sp"
        2.622→72, "Uniesp S/A" 1.171→101, "Embraport" 48→6 e três por menos de
        10 processos. Não é o critério que está errado (as facetas são delas
        mesmo): é a MEDIDA que fica pior. Como a régua da casa é não regredir
        número medido, aqui se abstém — a faceta continua no índice como linha
        própria, que é o erro barato.
        """
        alvos = [l for l, d in novos.items() if self.antes.get(l)]
        if not alvos:
            return []
        linhas, ordem = [], []
        for lider in alvos:
            doc = novos[lider]
            variantes = doc.get('variantes') or []
            ocorrencias = dict(zip(variantes, doc.get('variantes_n') or []))
            linhas.append(json.dumps({'index': self.indice_processos}))
            linhas.append(json.dumps(query_contagem(variantes,
                                                    ocorrencias=ocorrencias)))
            ordem.append(lider)
        t0 = time.monotonic()
        r = self.sessao.post(f'{self.es_url}/_msearch',
                             data=('\n'.join(linhas) + '\n').encode('utf-8'),
                             headers={'Content-Type': 'application/x-ndjson'},
                             timeout=600)
        self.tempos.append(time.monotonic() - t0)
        recusados = []
        for lider, resp in zip(ordem, r.json().get('responses', [])):
            depois = total_exato(resp)
            if depois is not None and depois < self.antes[lider]:
                recusados.append((lider, self.antes[lider], depois))
                novos.pop(lider)
        return recusados

    def _bulk_em_lotes(self, operacoes, por_lote) -> int:
        """Corte por OPERAÇÃO (não por linha): `index` tem 2 linhas e `delete`
        tem 1 — fatiar por linha separaria a ação do documento."""
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

    # ------------------------------------------------------------------ #
    # Verificação — a fusão não pode ENCOLHER o número de ninguém
    # ------------------------------------------------------------------ #
    def _verificar(self):
        """Reconta os líderes e compara com o antes.

        A prova 2 da decisão 14 diz que o OR da dona já casava todos os processos
        da faceta; então o número do líder pode CRESCER (grafia que a poda dele
        tinha cortado voltou pelo lado da faceta) mas nunca encolher. Encolheu ⇒
        a regra está errada e o relatório grita.
        """
        recusados = {l for l, _a, _d in getattr(self, 'encolheriam', [])}
        alvos = [l for l, n in self.antes.items() if n and l not in recusados]
        if not alvos:
            return
        fontes = self._mget(alvos)
        linhas, ordem = [], []
        for lider in alvos:
            doc = fontes.get(lider)
            if not doc:
                continue
            variantes = doc.get('variantes') or []
            ocorrencias = dict(zip(variantes, doc.get('variantes_n') or []))
            linhas.append(json.dumps({'index': self.indice_processos}))
            linhas.append(json.dumps(query_contagem(variantes,
                                                    ocorrencias=ocorrencias)))
            ordem.append(lider)
        t0 = time.monotonic()
        r = self.sessao.post(f'{self.es_url}/_msearch',
                             data=('\n'.join(linhas) + '\n').encode('utf-8'),
                             headers={'Content-Type': 'application/x-ndjson'},
                             timeout=600)
        self.tempos.append(time.monotonic() - t0)
        respostas = r.json().get('responses', [])
        w = self.stdout.write
        w('── VERIFICAÇÃO (recontagem dos líderes) ' + '─' * 26)
        encolheram = []
        for lider, resp in zip(ordem, respostas):
            depois = total_exato(resp)
            antes = self.antes[lider]
            if depois is not None and depois < antes:
                encolheram.append((lider, antes, depois))
        for lider, antes, depois in sorted(encolheram,
                                           key=lambda x: x[2] - x[1])[:10]:
            w(self.style.ERROR(
                f'  ENCOLHEU {antes:,} → {depois:,}  {self._rot(lider)}'))
        if not encolheram:
            w(f'  {len(ordem):,} líderes recontados · nenhum encolheu ✔')
        else:
            w(self.style.ERROR(f'  {len(encolheram):,} de {len(ordem):,} '
                               'líderes ENCOLHERAM — revise a decisão 14'))
        w('')

    # ------------------------------------------------------------------ #
    # Relatório
    # ------------------------------------------------------------------ #
    def _rot(self, _id):
        d = self.rotulos.get(_id) or {}
        return f"{(d.get('nome_canonico') or _id)[:44]} ({d.get('n_partes', '?')}l)"

    def _relatorio_plano(self, plano, amostra):
        w = self.stdout.write
        por_lider: dict = {}
        for a, l in plano.items():
            por_lider.setdefault(l, []).append(a)
        w('')
        if hasattr(self, 'grupos_grafia'):
            w('── PASSADA 1: GRAFIA IDÊNTICA CRUZANDO AS CHAVES ' + '─' * 17)
            w(f'  grupos fundidos ...... {self.grupos_grafia:,}')
            w(f'  homônimos PRESERVADOS  {self.homonimos_grafia:,} '
              '(nome compartilhado sem perfil de typo — abstenção)')
        if hasattr(self, 'stats_facetas'):
            s = self.stats_facetas
            w('── PASSADA 2: FACETAS (decisão 14) ' + '─' * 31)
            w(f"  facetas fundidas ..... {s['fundidas']:,} em {s['donas']:,} donas")
            w(f"  ABSTENÇÕES ........... {s['ambiguo']:,} por dona ambígua · "
              f"{s['sem_dominancia']:,} por falta de dominância")
        w('── TOTAL ' + '─' * 57)
        w(f'  entidades absorvidas . {len(plano):,} em {len(por_lider):,} líderes')
        ordem = sorted(por_lider, key=lambda l: -len(por_lider[l]))
        for lider in ordem[:amostra]:
            w(f'    {self._rot(lider):<52} ← {len(por_lider[lider]):,} entidade(s)')
            for a in sorted(por_lider[lider],
                            key=lambda x: -((self.rotulos.get(x) or {})
                                            .get('n_processos') or 0))[:3]:
                n = (self.rotulos.get(a) or {}).get('n_processos')
                w(f'        {(n or 0):>10,}  {self._rot(a)}')
        w('')

    def _relatorio_final(self, escritos, total, dt):
        w = self.stdout.write
        fundidos, apagados, recontar, erros = escritos
        w('── APLICADO ' + '─' * 54)
        w(f'  líderes reescritos ... {fundidos:,} · '
          f'{apagados:,} entidades apagadas do índice')
        recusados = getattr(self, 'encolheriam', [])
        if recusados:
            w(f'  RECUSADAS ............ {len(recusados):,} fusões abandonadas '
              'porque a contagem do líder encolheria (ponto cego da poda):')
            for lider, antes, depois in sorted(recusados, key=lambda x: x[2] - x[1]):
                w(f'      {antes:>10,} → {depois:<10,} {self._rot(lider)}')
        if recontar:
            w(self.style.WARNING(
                f'  recontar ............. {recontar:,} líder(es) ficaram SEM '
                'n_processos (o OR de busca mudou) → rode '
                '`contar_processos_entidades --somente-faltantes`'))
        if erros:
            w(self.style.ERROR(f'  ERROS no _bulk: {erros}'))
        w(f'  índice ............... {total:,} → {self._total():,} entidades')
        if self.tempos:
            w(f'  ES ................... {len(self.tempos)} requisições · '
              f'PICO {max(self.tempos):.2f}s · total {sum(self.tempos):.1f}s')
        w(f'  duração .............. {dt:.1f}s')
        w('')
