"""Hidrata um recorte do `voyager-acervo` (Datajud) que não está no acervo rico.

O caso que motivou: precatórios do TJAL autuados NA COMARCA como classe 1298
"Processo Administrativo", numeração 050 (ex.: 0500008-51.2023.8.02.0043).
Nunca tiveram ato publicado no DJEN, então a ingestão não os criou; o Datajud
os tem. Medido em 23/10/2026: 3.526 documentos TJAL 1298 `050*` no índice,
3.503 CNJs distintos fora do foro 9003; 2.203 já no Postgres (1.910 PRECATORIO,
293 NAO_LEAD) e 1.300 faltando. As outras 8.168 PA do TJAL (sem o prefixo) são
ruído e ficam de fora.

    # medir (padrão: não escreve nada)
    manage.py hidratar_recorte_acervo --tribunal TJAL --classe 1298 \\
        --prefixo 050 --excluir-foro 9003

    # executar, em lotes
    manage.py hidratar_recorte_acervo --tribunal TJAL --classe 1298 \\
        --prefixo 050 --excluir-foro 9003 --executar --limite 200

Cada CNJ criado custa 1 requisição à API pública do Datajud (movimentos), que
tem uma chave compartilhada (~100 rpm): `--pausa` (padrão 1 s) segura o ritmo.
O enricher vai para a fila do TRIBUNAL (`--fila-enricher tribunal`), não para a
manual da tela.

Hidratar não promove a lead por si: o processo nasce sem publicação do DJEN e
o classificador decide depois (a regra do 1298 de comarca exige publicação do
órgão de precatórios). O ganho imediato é o processo, as partes e os movimentos
no acervo — base do gate de completude.

`--limite` é TETO: quando ele corta o recorte, o comando diz quantos ficaram
para trás (nunca corte mudo).
"""
from __future__ import annotations

import json
import time
from collections import Counter

from django.core.management.base import BaseCommand, CommandError

from datajud.hidratacao import FILAS_ENRICHER, hidratar_cnj, iterar_esqueletos
from tribunals.models import Process

LOTE_CONSULTA = 500


class Command(BaseCommand):
    help = 'Hidrata um recorte do voyager-acervo (dry-run por padrão).'

    def add_arguments(self, parser):
        parser.add_argument('--tribunal', required=True)
        parser.add_argument('--classe', required=True)
        parser.add_argument('--prefixo', default='',
                            help='prefixo do número CNJ (ex.: 050)')
        parser.add_argument('--excluir-foro', action='append', default=[],
                            help='4 últimos dígitos do CNJ a excluir (repetível)')
        parser.add_argument('--executar', action='store_true',
                            help='sem isto, só mede')
        parser.add_argument('--limite', type=int, default=0,
                            help='máximo de CNJs a hidratar nesta execução (0 = todos)')
        parser.add_argument('--pausa', type=float, default=1.0,
                            help='segundos entre hidratações (chave pública do Datajud)')
        parser.add_argument('--fila-enricher', choices=FILAS_ENRICHER + ('nenhuma',),
                            default='tribunal')
        parser.add_argument('--json', action='store_true')

    def handle(self, *args, **o):
        if o['limite'] < 0:
            raise CommandError('--limite não pode ser negativo')
        docs = 0
        cnjs: dict[str, dict] = {}
        for src in iterar_esqueletos(o['tribunal'], o['classe'], o['prefixo'],
                                     tuple(o['excluir_foro'])):
            docs += 1
            cnjs.setdefault(src['proc'], src)

        ja = {}
        todos = sorted(cnjs)
        for i in range(0, len(todos), LOTE_CONSULTA):
            ja.update(Process.objects
                      .filter(numero_cnj__in=todos[i:i + LOTE_CONSULTA])
                      .values_list('numero_cnj', 'classificacao'))
        faltam = [c for c in todos if c not in ja]

        def _assunto(c):
            nomes = cnjs[c].get('assunto_nomes') or ['(sem assunto)']
            return nomes[0]

        medida = {
            'tribunal': o['tribunal'].upper(), 'classe': str(o['classe']),
            'prefixo': o['prefixo'], 'excluir_foros': o['excluir_foro'],
            'documentos': docs, 'cnjs_distintos': len(todos),
            'ja_no_acervo': len(ja),
            'ja_no_acervo_por_classificacao': dict(Counter(
                v or '(sem)' for v in ja.values())),
            'faltam': len(faltam),
            'faltam_por_assunto': dict(Counter(map(_assunto, faltam)).most_common(10)),
            'faltam_por_orgao': dict(Counter(
                cnjs[c].get('orgao_nome') or '' for c in faltam).most_common(10)),
        }
        self._saida(medida, o['json'])
        if not o['executar']:
            self.stdout.write(self.style.SUCCESS('DRY-RUN — nada alterado. Use --executar.'))
            return

        alvo = faltam[:o['limite']] if o['limite'] else faltam
        com_enricher = o['fila_enricher'] != 'nenhuma'
        fila = o['fila_enricher'] if com_enricher else 'manual'
        estados = Counter()
        enfileirados = 0
        for n, cnj in enumerate(alvo, 1):
            try:
                out = hidratar_cnj(cnj, com_enricher=com_enricher, fila_enricher=fila)
            except Exception as e:  # noqa: BLE001 — um CNJ ruim não mata o lote
                estados['falha'] += 1
                self.stderr.write(f'falha {cnj}: {e}')
                continue
            estados[out['estado']] += 1
            enfileirados += 1 if out.get('enricher_enfileirado') else 0
            if n % 50 == 0:
                self.stdout.write(f'  {n}/{len(alvo)} {dict(estados)}')
            if o['pausa']:
                time.sleep(o['pausa'])

        resultado = {'hidratados': dict(estados), 'enricher_enfileirado': enfileirados,
                     'restam': len(faltam) - len(alvo)}
        self._saida(resultado, o['json'])
        if resultado['restam']:
            self.stdout.write(self.style.WARNING(
                f'TETO --limite={o["limite"]} atingido: {resultado["restam"]} CNJs '
                'do recorte ficaram para a próxima execução.'))

    def _saida(self, dados: dict, como_json: bool):
        if como_json:
            self.stdout.write(json.dumps(dados, ensure_ascii=False))
            return
        for k, v in dados.items():
            self.stdout.write(f'{k}: {v}')
