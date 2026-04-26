"""Funde Partes mascaradas (TRF3) em Partes com doc real (TRF1) quando
nome bate e o real casa com a máscara posição-a-posição.

Para cada Parte com doc mascarado:
  1. Busca Partes com mesmo nome e doc REAL.
  2. Se uma delas casa com a máscara → move ProcessoParte refs e deleta a
     mascarada.
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

from enrichers.parsers import real_casa_com_mascara
from tribunals.models import Parte, ProcessoParte


class Command(BaseCommand):
    help = 'Funde Partes com doc mascarado em Partes com doc real (mesmo nome).'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', dest='dry_run')
        parser.add_argument('--limit', type=int, default=0)

    def handle(self, *args, dry_run, limit, **opts):
        mascaradas = (
            Parte.objects.exclude(documento='')
            .filter(Q(documento__contains='X') | Q(documento__contains='x') | Q(documento__contains='*'))
            .order_by('pk')
        )
        if limit:
            mascaradas = mascaradas[:limit]

        total = 0
        fundidas = 0
        sem_match = 0
        for masc in mascaradas.iterator(chunk_size=500):
            total += 1
            candidatos = (
                Parte.objects.filter(nome=masc.nome).exclude(pk=masc.pk).exclude(documento='')
                .exclude(Q(documento__contains='X') | Q(documento__contains='x') | Q(documento__contains='*'))
            )
            real = next((c for c in candidatos if real_casa_com_mascara(c.documento, masc.documento)), None)
            if not real:
                sem_match += 1
                continue
            if dry_run:
                self.stdout.write(f'  [dry] funde mascarada pk={masc.pk} ({masc.documento}) → real pk={real.pk} ({real.documento})')
                fundidas += 1
                continue
            with transaction.atomic():
                # Move ProcessoParte (na fk `parte`) e na fk `representa`
                # (ProcessoParte que representa esta parte).
                ProcessoParte.objects.filter(parte=masc).update(parte=real)
                ProcessoParte.objects.filter(representa__parte=masc).select_related('representa').update(
                    # representa aponta pra um ProcessoParte que continua válido
                    # (já moveu o `.parte` na linha anterior); não mexe aqui.
                )
                masc.delete()
            fundidas += 1
            if fundidas % 100 == 0:
                self.stdout.write(f'  fundidas={fundidas} sem_match={sem_match} total_visto={total}')

        self.stdout.write(self.style.SUCCESS(
            f'Total mascaradas vistas: {total} | fundidas: {fundidas} | sem match: {sem_match}'
        ))
