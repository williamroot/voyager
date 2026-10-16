"""Recalcula `tem_ente_publico_passivo` nos docs de processo do ES com o gate
novo (`tribunals.estagio.RE_ENTE_PUBLICO`, 16/10/2026). Dry-run por padrão.

Por que um comando e não reindex: o flag depende só de `participacoes[].nome`
e `participacoes[].polo`, que JÁ estão no `_source` do próprio doc. Ler do ES e
escrever um `_update` parcial só onde o valor muda é ES-only — zero leitura de
`tribunals_processoparte` (centenas de milhões de linhas) e nenhum doc
reconstruído. Docs novos ou reindexados já saem certos pelo builder
(`search/documents.py`).

Varre por `search_after` em `id` (sem acumular: página lida, página decidida,
página escrita — regra nº 1). Só olha docs que TÊM o campo (os da era nova do
builder); doc legado sem o campo continua sem ele, e quem precisa dele é o
reindex. `--limite` é teto de leitura: atingi-lo sai em ERROR, nunca em silêncio.

Uso:
  python manage.py recalcular_ente_publico_es                       # mede, não escreve
  python manage.py recalcular_ente_publico_es --tribunal TJRS --apply
  python manage.py recalcular_ente_publico_es --apply --sleep 0.2
"""
from __future__ import annotations

import collections
import json
import time

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from search.client import index_name
from tribunals.estagio import RE_ENTE_PUBLICO


def flag_novo(participacoes) -> bool:
    """A mesma regra de `search.documents._serialize_partes`."""
    return any(p.get('polo') == 'passivo' and RE_ENTE_PUBLICO.search(p.get('nome') or '')
               for p in participacoes or [])


class Command(BaseCommand):
    help = 'Recalcula tem_ente_publico_passivo no ES com o gate novo (dry-run por padrão).'

    def add_arguments(self, p):
        p.add_argument('--tribunal', action='append', help='repetível; padrão: todos')
        p.add_argument('--apply', action='store_true')
        p.add_argument('--pagina', type=int, default=2000)
        p.add_argument('--limite', type=int, default=0, help='teto de docs lidos (0 = sem teto)')
        p.add_argument('--sleep', type=float, default=0.0, help='pausa (s) entre páginas')

    def _pedir(self, caminho, corpo=None, dados=None):
        r = self.sessao.post(
            f'{self.es}{caminho}', timeout=300,
            **({'json': corpo} if corpo is not None else {
                'data': dados, 'headers': {'Content-Type': 'application/x-ndjson'}}))
        if r.status_code >= 300:
            raise CommandError(f'{caminho}: HTTP {r.status_code} {r.text[:300]}')
        return r.json()

    def handle(self, *a, **o):  # noqa: PLR0912 — laço de varredura com teto e relatório
        self.es = getattr(settings, 'ELASTICSEARCH_URL', 'http://elasticsearch:9200').rstrip('/')
        self.sessao = requests.Session()
        indice = index_name('processos')
        filtro = [{'exists': {'field': 'tem_ente_publico_passivo'}}]
        if o['tribunal']:
            filtro.append({'terms': {'tribunal': [t.upper() for t in o['tribunal']]}})

        lidos = 0
        conflitos = 0
        mudou = collections.Counter()           # (tribunal, 'F->T'|'T->F')
        apos = None
        while True:
            corpo = {'size': o['pagina'], 'query': {'bool': {'filter': filtro}},
                     '_source': ['id', 'tribunal', 'tem_ente_publico_passivo',
                                 'participacoes.nome', 'participacoes.polo'],
                     'sort': [{'id': 'asc'}], 'track_total_hits': False,
                     # controle otimista: o `_update` só vale se o doc ainda é
                     # o que lemos — senão o builder o reescreveu (com o gate
                     # novo) entre a leitura e a escrita, e quem ganha é ele.
                     'seq_no_primary_term': True}
            if apos is not None:
                corpo['search_after'] = [apos]
            hits = self._pedir(f'/{indice}/_search', corpo)['hits']['hits']
            if not hits:
                break
            linhas = []
            for h in hits:
                s = h['_source']
                atual = bool(s.get('tem_ente_publico_passivo'))
                novo = flag_novo(s.get('participacoes'))
                if novo != atual:
                    mudou[(s.get('tribunal') or '?', 'F->T' if novo else 'T->F')] += 1
                    linhas.append(json.dumps({'update': {
                        '_index': h['_index'], '_id': h['_id'],
                        'if_seq_no': h['_seq_no'], 'if_primary_term': h['_primary_term']}}))
                    linhas.append(json.dumps({'doc': {'tem_ente_publico_passivo': novo}}))
            if o['apply'] and linhas:
                conflitos += self._escrever(linhas)
            lidos += len(hits)
            apos = hits[-1]['_source']['id']
            if lidos % (o['pagina'] * 50) == 0:
                self.stdout.write(f'  {lidos:,} lidos, {sum(mudou.values()):,} mudam…')
                self.stdout.flush()
            if o['limite'] and lidos >= o['limite']:
                self.stderr.write(self.style.ERROR(
                    f'TETO atingido: parei em {lidos:,} docs (--limite). O resto NÃO foi visto.'))
                break
            if len(hits) < o['pagina']:
                break
            if o['sleep']:
                time.sleep(o['sleep'])

        por_trib = collections.defaultdict(dict)
        for (t, d), n in mudou.items():
            por_trib[t][d] = n
        for t in sorted(por_trib):
            self.stdout.write(f'{t:6} F->T {por_trib[t].get("F->T", 0):>9,}  '
                              f'T->F {por_trib[t].get("T->F", 0):>9,}')
        modo = 'APLICADO' if o['apply'] else 'DRY-RUN — nada escrito'
        self.stdout.write(self.style.SUCCESS(
            f'{modo}: {lidos:,} docs lidos, {sum(mudou.values()):,} com o flag diferente'
            + (f', {conflitos:,} reescritos pelo builder no meio (não tocados)' if conflitos else '')
            + '.'))

    def _escrever(self, linhas) -> int:
        """Bulk `_update` condicional. Devolve quantos deram 409 (versão nova do
        doc — o builder já gravou com o gate novo). Qualquer OUTRO erro para."""
        resp = self._pedir('/_bulk', dados=('\n'.join(linhas) + '\n').encode())
        if not resp.get('errors'):
            return 0
        conflitos = 0
        for item in resp.get('items') or []:
            r = item.get('update') or {}
            if r.get('status', 200) < 300:
                continue
            if r.get('status') == 409:
                conflitos += 1
                continue
            raise CommandError(f'bulk com erro que não é conflito: {str(r)[:300]}')
        return conflitos
