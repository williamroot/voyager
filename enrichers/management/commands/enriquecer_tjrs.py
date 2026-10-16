"""Semeia a fila do TJRS com os leads PRIMEIRO — sem esperar o refill.

O refill (`reabastecer_filas_enriquecimento`, a cada 2 min) já serve
PRECATORIO → PRE_PRECATORIO na frente da fila em lotes de 500
(`ENRICH_PRIORIDADE_TRIBUNAIS`). Este comando é a mesma operação sob demanda,
para o dia do deploy ou depois de um respiro: conta o que está pendente por
nível e, com `--apply`, põe na FRENTE da fila `enrich_tjrs` até `--limite`
processos dos níveis pedidos, na ordem pedida.

Sem flood (incidente de 28/07/2026, 4,9 M jobs no Redis): o lote tem teto
duro (`TETO_LIMITE`), passa pela trava de voo (um pid não volta para a fila
antes de 1 h) e pela faixa fora-da-fonte. Dry-run é o padrão.

Uso:
  python manage.py enriquecer_tjrs                              # só conta
  python manage.py enriquecer_tjrs --nivel PRECATORIO --apply
  python manage.py enriquecer_tjrs --nivel PRECATORIO --nivel PRE_PRECATORIO --limite 5000 --apply
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from tribunals.models import Process

TRIBUNAL = 'TJRS'
NIVEIS_VALIDOS = ('PRECATORIO', 'PRE_PRECATORIO', 'DIREITO_CREDITORIO')
TETO_LIMITE = 5_000


class Command(BaseCommand):
    help = 'Enfileira os pendentes TJRS por nível de lead, na frente da fila (dry-run por padrão).'

    def add_arguments(self, parser):
        parser.add_argument('--nivel', action='append', choices=NIVEIS_VALIDOS,
                            help='repetível; a ordem dada é a ordem de prioridade '
                                 '(padrão: PRECATORIO, PRE_PRECATORIO)')
        parser.add_argument('--limite', type=int, default=2_000,
                            help=f'processos por execução (teto {TETO_LIMITE:,})')
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **opts):
        from enrichers.jobs import em_respiro, enfileirar_na_frente, enrich_pausado

        niveis = tuple(opts['nivel'] or ('PRECATORIO', 'PRE_PRECATORIO'))
        limite = opts['limite']
        if not 0 < limite <= TETO_LIMITE:
            raise CommandError(f'--limite entre 1 e {TETO_LIMITE:,}')

        for nivel in niveis:
            n = Process.objects.filter(
                tribunal_id=TRIBUNAL, classificacao=nivel,
                enriquecimento_status=Process.ENRIQ_PENDENTE).count()
            self.stdout.write(f'{TRIBUNAL} {nivel}: {n:,} pendentes')

        if not opts['apply']:
            self.stdout.write(self.style.SUCCESS('DRY-RUN — nada enfileirado.'))
            return
        if enrich_pausado(TRIBUNAL):
            motivo = 'respiro (a fonte recusou credenciais)' if em_respiro(TRIBUNAL) else 'pausa'
            raise CommandError(f'{TRIBUNAL} está em {motivo} — não enfileirei nada.')

        contagem = enfileirar_na_frente(TRIBUNAL, niveis=niveis, lote=limite)
        total = sum(contagem.values())
        self.stdout.write(self.style.SUCCESS(
            f'na frente da fila enrich_tjrs: {total:,} '
            f'({", ".join(f"{k} {v:,}" for k, v in contagem.items()) or "nada novo — tudo em voo"})'))
