"""Cruza processos TRF1 classificados como PRECATÓRIO (não consumidos) com
leads do falcon (Juriscope) que já têm pelo menos 1 parte com valor_acao E
oficio_requisitorio preenchidos — indica que o Juriscope já processou o lead.

Marca os que cruzam como consumidos (resultado=validado) e reporta quantos
ainda não foram encontrados no falcon.

Critério falcon: processo TRF1 com pelo menos 1 processpart onde
  - is_lawyer = false
  - oficio_requisitorio IS NOT NULL AND oficio_requisitorio <> ''
  - valor_acao IS NOT NULL

Idempotência: já consumidos são pulados — re-run é seguro.

Uso:
  python manage.py sync_precatorios_juriscope --dry-run
  python manage.py sync_precatorios_juriscope --apply
"""
from __future__ import annotations

import logging
import time

import psycopg
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from tribunals.models import ApiClient, LeadConsumption, Process

logger = logging.getLogger('voyager.tribunals.commands')

FALCON_DSN = 'postgres://postgres:iVu4DrDCS372uCRyTJhaLjmEEdRajWq@10.10.0.51:5432/falcon'
CLIENT_NOME = 'juriscope'


class Command(BaseCommand):
    help = 'Sync precatórios TRF1 não consumidos com leads do Juriscope (falcon).'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Apenas reporta contagens, sem inserir nada.')
        parser.add_argument('--apply', action='store_true',
                            help='Executa o insert. Mutuamente exclusivo com --dry-run.')
        parser.add_argument('--tribunal', default='TRF1',
                            help='Sigla do tribunal (default TRF1).')

    def handle(self, *args, **opts):
        if opts['dry_run'] == opts['apply']:
            raise CommandError('Passe --dry-run OU --apply (exatamente um).')
        dry = opts['dry_run']
        tribunal_sigla = opts['tribunal'].upper()
        tribunal_lower = tribunal_sigla.lower()

        self.stdout.write(self.style.NOTICE(
            f'modo: {"DRY-RUN" if dry else "APPLY"} · tribunal={tribunal_sigla} · classificacao=PRECATORIO'
        ))

        # 1) Cliente juriscope
        if dry:
            cliente = ApiClient.objects.filter(nome=CLIENT_NOME).first()
            ja_consumidos: set[int] = set()
            if cliente:
                ja_consumidos = set(
                    LeadConsumption.objects.filter(cliente=cliente)
                    .values_list('processo_id', flat=True)
                )
        else:
            cliente, created = ApiClient.objects.get_or_create(
                nome=CLIENT_NOME,
                defaults={'api_key': f'{CLIENT_NOME}-imported-{int(time.time())}', 'ativo': True},
            )
            self.stdout.write(f'cliente: {cliente.nome} (id={cliente.id}, '
                              f'{"criado" if created else "existente"})')
            ja_consumidos = set(
                LeadConsumption.objects.filter(cliente=cliente)
                .values_list('processo_id', flat=True)
            )

        # 2) Todos os PRECATORIO do tribunal não consumidos
        prec_qs = Process.objects.filter(
            tribunal_id=tribunal_sigla, classificacao='PRECATORIO',
        ).exclude(id__in=ja_consumidos)
        total_prec = Process.objects.filter(tribunal_id=tribunal_sigla, classificacao='PRECATORIO').count()
        ja_consumidos_prec = total_prec - prec_qs.count()

        cnj_to_pid: dict[str, int] = dict(prec_qs.values_list('numero_cnj', 'id'))
        nao_consumidos_count = len(cnj_to_pid)

        self.stdout.write(
            f'PRECATORIO TRF1 total: {total_prec:,} | '
            f'já consumidos: {ja_consumidos_prec:,} | '
            f'pendentes (a cruzar): {nao_consumidos_count:,}'
        )

        # 3) Busca no falcon: CNJs TRF1 com ao menos 1 parte com valor+oficio
        self.stdout.write('conectando ao falcon...')
        t0 = time.time()
        sql = f"""
            SELECT DISTINCT p.numero_autos, p.created_at
            FROM datamodel_process p
            JOIN datamodel_processpart pp ON pp.process_id = p.id
            WHERE LOWER(p.tribunal) = '{tribunal_lower}'
              AND pp.is_lawyer = false
              AND pp.oficio_requisitorio IS NOT NULL
              AND pp.oficio_requisitorio <> ''
              AND pp.valor_acao IS NOT NULL
              AND p.numero_autos IS NOT NULL
              AND p.numero_autos <> ''
        """
        with psycopg.connect(FALCON_DSN, connect_timeout=15) as falcon, falcon.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        self.stdout.write(f'falcon: {len(rows):,} CNJs com leads (valor+oficio) em {time.time()-t0:.1f}s')

        falcon_cnjs: dict[str, object] = {r[0]: r[1] for r in rows}

        # 4) Cruzamento
        matches: list[tuple[int, object]] = []
        sem_match: list[str] = []
        for cnj, pid in cnj_to_pid.items():
            if cnj in falcon_cnjs:
                matches.append((pid, falcon_cnjs[cnj]))
            else:
                sem_match.append(cnj)

        self.stdout.write(
            f'\nCruzamento:\n'
            f'  match (a marcar como consumido): {len(matches):,}\n'
            f'  sem match no falcon (ainda não consumidos): {len(sem_match):,}\n'
        )

        if dry or not matches:
            self.stdout.write(self.style.SUCCESS(
                f'DRY-RUN concluído. {len(sem_match):,} precatórios ainda não consumidos pelo Juriscope.'
            ))
            return

        # 5) Marca consumidos em bulk
        instances = [
            LeadConsumption(
                processo_id=pid,
                cliente=cliente,
                resultado=LeadConsumption.RESULTADO_VALIDADO,
            )
            for pid, _ in matches
        ]
        with transaction.atomic():
            created_lcs = LeadConsumption.objects.bulk_create(instances, batch_size=2000)
            # Backdate consumido_em para created_at do falcon
            ts_by_pid = {pid: ts for pid, ts in matches}
            for lc in created_lcs:
                lc.consumido_em = ts_by_pid.get(lc.processo_id)
            LeadConsumption.objects.bulk_update(
                [lc for lc in created_lcs if lc.consumido_em],
                fields=['consumido_em'],
                batch_size=2000,
            )

        self.stdout.write(self.style.SUCCESS(
            f'\n=== resultado ===\n'
            f'  marcados como consumidos agora: {len(matches):,}\n'
            f'  precatórios ainda SEM match no falcon: {len(sem_match):,}\n'
        ))
