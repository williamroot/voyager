"""Drainer service: consome stream de resultados de enrichment e aplica em bulk.

Streams sharded por hash(process_id) % N — cada partição tem seu próprio
drainer (sem cruzamento de DELETE+INSERT no mesmo proc → sem deadlock).

Modo de uso:
  - `--partition I` (0..N-1): processa shard I.
  - `--partition legacy`: processa stream legado (sem suffix), em uso até
    drenar entries publicadas antes do deploy do shard. Pode ser
    descontinuado quando XLEN do legado zerar.
  - sem flag: assume `legacy` (back-compat com docker-compose antigo).

Workers só publicam (em `voyager:enrichment:results:<partition>`) — só o
drainer escreve no Postgres. Elimina contenção de LWLock que tínhamos
com ~500 enrichers concorrentes.
"""
from django.core.management.base import BaseCommand, CommandError

from enrichers import drainer
from enrichers.stream import STREAM_PARTITIONS


class Command(BaseCommand):
    help = 'Drena o stream de resultados de enrichment e aplica em batch.'

    def add_arguments(self, parser):
        parser.add_argument('--batch-size', type=int, default=200,
                            help='Tamanho máximo do batch por iteração.')
        parser.add_argument('--block-ms', type=int, default=2000,
                            help='Timeout de XREADGROUP em ms (poll interval).')
        parser.add_argument('--idle-ms', type=int, default=60_000,
                            help='XAUTOCLAIM: pega entries idle há X ms (consumer travou).')
        parser.add_argument('--no-trim', action='store_true', dest='no_trim',
                            help='Não chama XDEL após ack — mantém histórico no stream.')
        parser.add_argument('--partition', default='legacy',
                            help=f'Shard a processar: int 0..{STREAM_PARTITIONS - 1}, '
                                 f'"legacy" (stream antigo sem suffix), ou "all" '
                                 f'(modo ROLLBACK — round-robin entre legado + todos shards '
                                 f'num único drainer; reintroduz potencial de deadlock). '
                                 f'Default: legacy.')

    def handle(self, *args, **opts):
        partition_arg = opts['partition']
        partition: int | str | None
        if partition_arg == 'legacy':
            partition = None
        elif partition_arg == 'all':
            partition = 'all'
        else:
            try:
                partition = int(partition_arg)
            except (TypeError, ValueError):
                raise CommandError(
                    f'--partition deve ser "legacy", "all" ou int em [0, {STREAM_PARTITIONS}); recebi: {partition_arg!r}'
                )
            if not 0 <= partition < STREAM_PARTITIONS:
                raise CommandError(
                    f'--partition fora do range — esperado [0, {STREAM_PARTITIONS}), recebi {partition}'
                )

        drainer.run(
            batch_size=opts['batch_size'],
            block_ms=opts['block_ms'],
            idle_ms=opts['idle_ms'],
            trim_after_ack=not opts['no_trim'],
            partition=partition,
        )
