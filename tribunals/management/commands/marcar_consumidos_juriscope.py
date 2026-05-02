"""Marca processos TRF1 como já consumidos pelo Juriscope, baseado em
dados do banco falcon (Juriscope).

Critério: `tribunal IN (TRF1,trf1) AND files_downloaded=true` no falcon —
sinal forte de que o Juriscope baixou autos. `oficio_requisitorio`
preenchido sozinho é metadado importado, não implica ação humana.

Idempotência: set de Process.id já tendo LeadConsumption(cliente=juriscope)
é carregado uma vez; processos nesse set são pulados. Re-run não duplica.

Backdate: `consumido_em` é sobrescrito via UPDATE pós-bulk_create com
`created_at` do falcon — preserva histórico real de quando Juriscope
consumiu cada processo.

Uso:
  python manage.py marcar_consumidos_juriscope --dry-run
  python manage.py marcar_consumidos_juriscope --apply
  python manage.py marcar_consumidos_juriscope --apply --batch-size 5000

Conexão com falcon via DATABASE_FALCON_URL no .env (ou hardcoded fallback
no command — falcon é DB externo do Juriscope, não está em settings).
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import datetime

import psycopg
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from tribunals.models import ApiClient, LeadConsumption, Process

logger = logging.getLogger('voyager.tribunals.commands')

FALCON_DSN = 'postgres://postgres:iVu4DrDCS372uCRyTJhaLjmEEdRajWq@10.10.0.51:5432/falcon'

CLIENT_NOME = 'juriscope'
RESULTADO = LeadConsumption.RESULTADO_VALIDADO


class Command(BaseCommand):
    help = 'Marca processos como já consumidos pelo Juriscope (LeadConsumption).'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Apenas reporta contagens, sem inserir nada.')
        parser.add_argument('--apply', action='store_true',
                            help='Executa o insert. Mutuamente exclusivo com --dry-run.')
        parser.add_argument('--tribunal', default='TRF1',
                            help='Sigla do tribunal (default TRF1).')
        parser.add_argument('--batch-size', type=int, default=5000,
                            help='Linhas por chunk (default 5000).')
        parser.add_argument('--unmatched-log', default='/tmp/juriscope_unmatched.txt',
                            help='Path do arquivo de CNJs sem match no Voyager.')
        parser.add_argument('--limit', type=int, default=0,
                            help='Limita N CNJs do falcon — útil pra teste.')

    def handle(self, *args, **opts):
        if opts['dry_run'] == opts['apply']:
            raise CommandError('Passe --dry-run OU --apply (exatamente um).')
        dry = opts['dry_run']
        batch = opts['batch_size']
        limit = opts['limit']
        unmatched_path = opts['unmatched_log']
        tribunal_sigla = opts['tribunal'].upper()
        tribunal_lower = tribunal_sigla.lower()

        self.stdout.write(self.style.NOTICE(
            f'modo: {"DRY-RUN" if dry else "APPLY"} · tribunal={tribunal_sigla} · '
            f'batch={batch} · limit={limit or "todos"}'
        ))

        # 1) Cliente Juriscope
        if dry:
            cliente = ApiClient.objects.filter(nome=CLIENT_NOME).first()
            if not cliente:
                self.stdout.write(self.style.WARNING(
                    f'ApiClient "{CLIENT_NOME}" não existe — em --apply será criado.'
                ))
                # Cliente fake pra contagem
                ja_consumidos: set[int] = set()
            else:
                ja_consumidos = set(
                    LeadConsumption.objects.filter(cliente=cliente)
                    .values_list('processo_id', flat=True)
                )
        else:
            cliente, created = ApiClient.objects.get_or_create(
                nome=CLIENT_NOME,
                defaults={
                    'api_key': f'{CLIENT_NOME}-imported-{int(time.time())}',
                    'ativo': True,
                    'notas': 'Cliente do Juriscope. Marcado em massa via marcar_consumidos_juriscope.',
                },
            )
            self.stdout.write(f'cliente: {cliente.nome} (id={cliente.id}, '
                              f'{"criado" if created else "existente"})')
            ja_consumidos = set(
                LeadConsumption.objects.filter(cliente=cliente)
                .values_list('processo_id', flat=True)
            )

        self.stdout.write(f'já consumidos pelo cliente: {len(ja_consumidos):,}')

        # 2) Conecta no falcon e itera CNJs
        # DISTINCT ON garante 1 row por numero_autos (CNJ); ORDER BY pega o
        # created_at mais antigo (1ª vez que Juriscope tocou o processo) —
        # melhor do que `MIN(created_at) GROUP BY` em performance e clareza.
        sql = f"""
            SELECT DISTINCT ON (numero_autos) numero_autos, created_at
            FROM datamodel_process
            WHERE LOWER(tribunal) = '{tribunal_lower}'
              AND files_downloaded = true
              AND numero_autos IS NOT NULL
              AND numero_autos <> ''
            ORDER BY numero_autos, created_at ASC
        """
        if limit:
            sql += f' LIMIT {int(limit)}'

        self.stdout.write('conectando ao falcon...')
        t0 = time.time()
        with psycopg.connect(FALCON_DSN) as falcon, falcon.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        self.stdout.write(f'falcon: {len(rows):,} CNJs em {time.time() - t0:.1f}s')

        # 3) Processa em chunks
        unmatched: list[str] = []
        total_inseridos = 0
        total_skipped_existente = 0
        total_unmatched = 0
        total_dup_dentro_chunk = 0

        for i in range(0, len(rows), batch):
            chunk = rows[i:i + batch]
            cnjs = [r[0] for r in chunk]
            created_at_by_cnj: dict[str, datetime] = {r[0]: r[1] for r in chunk}

            cnj_to_pid: dict[str, int] = dict(
                Process.objects.filter(
                    numero_cnj__in=cnjs, tribunal_id=tribunal_sigla,
                ).values_list('numero_cnj', 'id')
            )

            # CNJs sem match no Voyager
            chunk_unmatched = [c for c in cnjs if c not in cnj_to_pid]
            unmatched.extend(chunk_unmatched)
            total_unmatched += len(chunk_unmatched)

            # Filtra fora os já consumidos
            novos_pares: list[tuple[int, datetime]] = []  # (process_id, consumido_em)
            seen_pids: set[int] = set()
            sem_created_at = 0
            for cnj, pid in cnj_to_pid.items():
                if pid in ja_consumidos:
                    total_skipped_existente += 1
                    continue
                if pid in seen_pids:
                    total_dup_dentro_chunk += 1
                    continue
                seen_pids.add(pid)
                ts = created_at_by_cnj.get(cnj)
                if ts is None:
                    sem_created_at += 1
                novos_pares.append((pid, ts or datetime.now()))
            self.stdout.write(
                f'  debug chunk: cnjs={len(cnjs)} match={len(cnj_to_pid)} '
                f'novos={len(novos_pares)} ja_consumidos_skipped={total_skipped_existente} '
                f'dup_pid={total_dup_dentro_chunk} sem_created_at={sem_created_at}'
            )

            if not novos_pares:
                continue

            if dry:
                total_inseridos += len(novos_pares)
                continue

            # bulk_create + UPDATE consumido_em
            instances = [
                LeadConsumption(processo_id=pid, cliente=cliente, resultado=RESULTADO)
                for pid, _ in novos_pares
            ]
            with transaction.atomic():
                created = LeadConsumption.objects.bulk_create(instances, batch_size=2000)
                # Backdate consumido_em — bulk_create não retorna pks em todas as DBs,
                # mas Postgres sim. Reuse mesma ordem.
                ts_by_pid = {pid: ts for pid, ts in novos_pares}
                # UPDATE em massa via CASE WHEN seria N parâmetros — mais simples
                # iterar e dar um UPDATE por entrada nova (rápido em Postgres com índice).
                lc_ids = [lc.id for lc in created if lc.id]
                # Se algum bulk_create veio sem pk, refaz lookup
                if len(lc_ids) != len(created):
                    raise CommandError('bulk_create não retornou pks (Postgres deveria).')
                # Update individual por pk — bulk_update aceita só campos do model
                for lc in created:
                    lc.consumido_em = ts_by_pid[lc.processo_id]
                LeadConsumption.objects.bulk_update(
                    created, fields=['consumido_em'], batch_size=2000,
                )

            total_inseridos += len(novos_pares)
            ja_consumidos.update(pid for pid, _ in novos_pares)

            self.stdout.write(
                f'  chunk {i // batch + 1}/{(len(rows) - 1) // batch + 1}: '
                f'+{len(novos_pares)} ({total_inseridos:,} acumulado)'
            )

        # 4) Persistir unmatched
        if unmatched and not dry:
            with open(unmatched_path, 'w') as f:
                f.write('\n'.join(unmatched))

        self.stdout.write(self.style.SUCCESS(
            f'\n=== resumo ===\n'
            f'  CNJs falcon (TRF1, files_downloaded=true): {len(rows):,}\n'
            f'  match Process Voyager: {len(rows) - total_unmatched:,}\n'
            f'  sem match (skip + log): {total_unmatched:,}\n'
            f'  já consumidos antes: {total_skipped_existente:,}\n'
            f'  duplicados dentro do chunk: {total_dup_dentro_chunk:,}\n'
            f'  {"a inserir" if dry else "inseridos"}: {total_inseridos:,}\n'
        ))
        if unmatched and not dry:
            self.stdout.write(f'  unmatched salvos em: {unmatched_path}')
