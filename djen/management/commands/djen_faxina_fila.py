"""Censo e conserto dos ids CASCA da fila — o job que existe na lista e não existe.

O QUE É UMA CASCA (medido em 24/08/2026). A fila do RQ é uma lista de **ids** no
Redis; o trabalho de verdade mora num hash separado (`rq:job:<id>`). Quando o
hash some e o id fica, a fila continua contando aquele item — e o worker que o
pega descarta em silêncio. Não é falha: é trabalho que evaporou sem deixar
`failed`, sem log e sem run.

Censo da `djen_backfill` no dia: **344 de 1.945 ids (17,7%) eram casca**, e a
distribuição diz de quem era o prejuízo:

    adiado  TJRS    120   ← 100% do trabalho pendente do TJRS
    bfd     TJAM    120
    bfd     TJDFT    73
    bfd     TJPR     26
    adiado  TJRJ      4
    adiado  TJSP      1

E o pior não é a perda: é o **bloqueio**. O `ressuscitar_dias_de_recuperacao`
considera "já está a caminho" todo dia cujo id está na fila, justamente pra não
duplicar. Com o id casca lá, aqueles 120 dias do TJRS ficaram permanentemente
invisíveis pro auto-heal — a tela dizia "falta 138" e a fila dizia "já vai",
enquanto nada acontecia. Um mês assim não deixaria rastro nenhum.

**Como o hash some.** Os ids são determinísticos (`bfd:<sigla>:<dia>`), então o
mesmo id vive várias vidas. Basta a sequência: job falha → entra no
`FailedJobRegistry`; o watchdog reenfileira o MESMO id → hash novo, id na fila;
`djen_censo_falhas --apagar` limpa o registry com `delete_job=True` → **apaga o
hash do job que está na fila agora**. O id sobrevive na lista, o trabalho não.
(Por isso o `djen_censo_falhas` ganhou, no mesmo commit, a trava que se recusa a
apagar hash de id que está na fila/agendado/em execução.)

Uso:

    manage.py djen_faxina_fila                      # só mede (dry-run)
    manage.py djen_faxina_fila --consertar          # remove a casca e refaz o job
    manage.py djen_faxina_fila --consertar --frente # ... e põe na frente da fila
"""
import collections
import json

import django_rq
from django.core.management.base import BaseCommand
from rq.job import Job

LOTE = 500

#: prefixo do id → (função a reenfileirar, timeout). Um id casca só pode ser
#: refeito se dá pra reconstruir os ARGUMENTOS a partir do próprio id — que é o
#: caso dos três formatos determinísticos do DJEN.
REFAZ = {
    'f2': ('djen.jobs.reprocessar_janela', 21600),
    'adiado': ('djen.jobs.reprocessar_janela', 21600),
    'bfd': ('djen.jobs.backfill_dia', 3600),
}


def censo_cascas(fila) -> tuple[list[str], collections.Counter, int]:
    """Devolve (ids casca, contador por prefixo/tribunal, total de ids)."""
    ids = fila.job_ids
    cascas, por_chave = [], collections.Counter()
    for i in range(0, len(ids), LOTE):
        pedaco = ids[i:i + LOTE]
        for jid, j in zip(pedaco, Job.fetch_many(pedaco, connection=fila.connection),
                          strict=False):
            if j is not None:
                continue
            partes = jid.split(':')
            por_chave[(partes[0], partes[1]) if len(partes) >= 3 else ('outro', '-')] += 1
            cascas.append(jid)
    return cascas, por_chave, len(ids)


class Command(BaseCommand):
    help = 'Mede (e conserta) os ids da fila cujo hash do job sumiu.'

    def add_arguments(self, parser):
        parser.add_argument('fila', nargs='?', default='djen_backfill')
        parser.add_argument('--consertar', action='store_true',
                            help='Remove a casca e reenfileira o dia de verdade.')
        parser.add_argument('--frente', action='store_true',
                            help='Com --consertar: entra na frente da fila.')
        parser.add_argument('--limite', type=int, default=0,
                            help='Teto de dias refeitos (0 = todos).')
        parser.add_argument('--json', dest='dump_json', default=None)

    def handle(self, *args, fila, consertar, frente, limite, dump_json, **opts):
        q = django_rq.get_queue(fila)
        cascas, por_chave, total = censo_cascas(q)
        pct = (100.0 * len(cascas) / total) if total else 0.0
        self.stdout.write(self.style.HTTP_INFO(
            f'fila `{fila}`: {total:,} ids · com payload {total - len(cascas):,} · '
            f'CASCA {len(cascas):,} ({pct:.1f}%)'))
        for (pref, trib), n in por_chave.most_common(20):
            self.stdout.write(f'   {pref:7s} {trib:7s} {n:5,}')
        if dump_json:
            with open(dump_json, 'w') as fh:
                json.dump(cascas, fh)

        if not cascas:
            return
        if not consertar:
            self.stdout.write(self.style.WARNING(
                '  dry-run — nada mexido. Use --consertar pra refazer os jobs.'))
            return

        refeitos = pulados = 0
        for jid in cascas:
            partes = jid.split(':')
            if len(partes) < 3 or partes[0] not in REFAZ:
                pulados += 1
                continue
            pref, sigla, dia = partes[0], partes[1], partes[2]
            func, timeout = REFAZ[pref]
            q.remove(jid)                      # tira a casca da lista
            # `adiado:` e `f2:` são o mesmo trabalho (reprocessar o dia); refazer
            # como `f2:` unifica o id que o watchdog conhece.
            novo_id = f'{"bfd" if pref == "bfd" else "f2"}:{sigla}:{dia}'
            if novo_id in set(q.job_ids):
                pulados += 1                   # o dia já voltou por outro caminho
                continue
            args = ((sigla, dia, dia) if func.endswith('reprocessar_janela')
                    else (sigla, dia))
            q.enqueue(func, *args, job_id=novo_id, job_timeout=timeout,
                      at_front=frente)
            refeitos += 1
            if limite and refeitos >= limite:
                break
        self.stdout.write(self.style.SUCCESS(
            f'  cascas removidas e refeitas: {refeitos:,} · puladas: {pulados:,} · '
            f'fila agora: {len(q.job_ids):,}'))
