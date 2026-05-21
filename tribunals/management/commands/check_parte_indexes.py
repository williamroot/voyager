"""Verifica que os índices únicos de tribunals_parte/processoparte estão
VÁLIDOS. Exit 1 se algum inválido/faltando — pra runbook e monitoramento.
Índice único inválido não enforça nada e deixa o upsert do drainer
duplicar Partes silenciosamente.
"""
from django.core.management.base import BaseCommand
from django.db import connection

ESPERADOS = [
    'uniq_parte_oab', 'uniq_parte_documento_real',
    'uniq_parte_documento_mascarado', 'uniq_parte_sem_doc_nem_oab',
    'uniq_processo_parte_polo_papel_principal',
]


class Command(BaseCommand):
    help = 'Verifica indisvalid dos índices únicos de Parte/ProcessoParte.'

    def handle(self, *args, **opts):
        with connection.cursor() as cur:
            cur.execute(
                "SELECT i.relname, idx.indisvalid FROM pg_index idx "
                "JOIN pg_class i ON i.oid = idx.indexrelid "
                "WHERE i.relname = ANY(%s)", [ESPERADOS])
            estado = dict(cur.fetchall())
        problemas = []
        for nome in ESPERADOS:
            if nome not in estado:
                problemas.append(f'FALTANDO: {nome}')
            elif not estado[nome]:
                problemas.append(f'INVÁLIDO: {nome}')
        for p in problemas:
            self.stderr.write(self.style.ERROR(p))
        if problemas:
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS(
            f'OK — {len(ESPERADOS)} índices únicos válidos.'))
