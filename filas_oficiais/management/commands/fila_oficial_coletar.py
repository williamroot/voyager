"""Coleta a fila oficial de precatórios e descobre os pendentes. DRY-RUN por padrão.

    # medir (não escreve nada: nem processo, nem registro, nem IngestionRun)
    manage.py fila_oficial_coletar --tribunal TJAL
    manage.py fila_oficial_coletar --tribunal TJAL --entidade 26 --saida /tmp/delmiro.jsonl

    # executar (backfill ou sob demanda) — o mesmo que o cron das 01:30 faz
    manage.py fila_oficial_coletar --tribunal TJAL --executar

    # retomar uma execução que morreu hoje (pula entidades já gravadas hoje)
    manage.py fila_oficial_coletar --tribunal TJAL --executar --retomar

A coleta inteira do TJAL leva ~2 h (SAPRE ~10 s por página, uma requisição por
vez, pausa ≥ 1 s); a do TJMT (SRP, `--tribunal TJMT`) ~11 min, 243 requisições.
`--entidade` (id do SAPRE/SRP ou nome) restringe, repetível; no TJMT, qualquer
ente do grupo pagador seleciona o grupo inteiro.
`--saida` grava uma linha JSON por item no dry-run (sem CPF e sem nome: a
fonte publica, o coletor descarta).
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from filas_oficiais.execucao import COLETORES, executar


class Command(BaseCommand):
    help = 'Fila oficial de precatórios → acervo (dry-run por padrão).'

    def add_arguments(self, parser):
        parser.add_argument('--tribunal', default='TJAL', choices=sorted(COLETORES))
        parser.add_argument('--executar', action='store_true',
                            help='sem isto, só mede')
        parser.add_argument('--retomar', action='store_true',
                            help='com --executar: pula entidades já gravadas hoje')
        parser.add_argument('--entidade', action='append', default=[],
                            help='id do SAPRE ou nome da entidade (repetível)')
        parser.add_argument('--sem-enricher', action='store_true',
                            help='não enfileira o enricher dos processos hidratados')
        parser.add_argument('--saida', default='',
                            help='dry-run: arquivo JSONL com uma linha por item')
        parser.add_argument('--json', action='store_true')

    def handle(self, *args, **o):
        arq = open(o['saida'], 'w', encoding='utf-8') if o['saida'] else None  # noqa: SIM115 — fechado no finally

        def saida(linha):
            arq.write(json.dumps(linha, ensure_ascii=False) + '\n')

        def progresso(res, placar):
            self.stdout.write(
                f"  {res.entidade}: {len(res.itens)} itens, completa={res.completa} "
                f"(acumulado: {placar.itens} itens, criados={dict(placar.criados)})")

        try:
            resumo = executar(o['tribunal'], escrever=o['executar'],
                              retomar=o['retomar'], entidades=o['entidade'] or None,
                              com_enricher=not o['sem_enricher'],
                              saida=saida if arq else None,
                              ao_concluir_entidade=None if o['json'] else progresso)
        finally:
            if arq:
                arq.close()
        if o['json']:
            self.stdout.write(json.dumps(resumo, ensure_ascii=False, default=str))
        else:
            for k, v in resumo.items():
                self.stdout.write(f'{k}: {v}')
        if not o['executar']:
            self.stdout.write(self.style.SUCCESS('DRY-RUN — nada alterado. Use --executar.'))
        elif resumo.get('entidades_incompletas') or resumo.get('falhas'):
            self.stdout.write(self.style.WARNING(
                f"ATENÇÃO: {len(resumo.get('entidades_incompletas') or [])} entidades "
                f"incompletas e {len(resumo.get('falhas') or [])} falhas — ver o "
                f"IngestionRun {resumo.get('run_id')}."))
