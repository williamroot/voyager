"""Faz o `sync_es` enxergar o que já foi escrito em lote — sem reindex manual.

PROBLEMA (medido em 27/08/2026, em produção). O tique incremental
`sync_processos_atualizados` é keyset por `Process.atualizado_em`. Mas
`atualizado_em` é `auto_now`, e **`auto_now` só roda em `Model.save()`**: nem
`.update()`, nem SQL cru, nem `bulk_create` em tabela relacionada o disparam.

Consequência, conferida processo a processo:

    Process 57016866 ... 100 ProcessoParte no Postgres ... 0 no índice
    Process 57011804 ...  79                            ... 0
    Process 57013950 ...  78                            ... 0
    Process 57000736 ...  53                            ... 0
    Process 57003117 ...  51                            ... 0

Não era o teto do tique (esse já foi corrigido) nem o dreno: era a **campainha**.
Quem escreve em lote precisa avisar, e os escritores novos não avisavam.
`datajud/ingestion.py` já fazia certo desde sempre; `backfill_partes_djen` e
`backfill_grau` não faziam — agora fazem, e este comando cobre o retrospecto.

    python manage.py tocar_campainha --alvo partes
    python manage.py tocar_campainha --alvo grau --lote 20000 --max-segundos 1800

O que ele NÃO é: um reindex. Ele só marca `atualizado_em = now()` e deixa o
tique de 10 min fazer o trabalho — com o freio de fila do próprio tique. Assim o
ritmo continua sendo o do dreno, e não o de um comando solto empurrando.
"""
import time

from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

ALVOS = {
    # cada alvo é um predicado sobre uma FAIXA de pk (barato, usa a pkey)
    'partes': ("EXISTS (SELECT 1 FROM tribunals_processoparte pp "
               "WHERE pp.processo_id = p.id AND pp.fonte = 'djen')"),
    'grau': "p.grau <> ''",
    'assunto': "p.assunto_id IS NOT NULL",
    'tudo': 'TRUE',
}

#: acima disso o comando ESPERA em vez de empurrar. Mesmo princípio do tique:
#: quem manda no ritmo é o dreno; engordar a fila não aumenta vazão nenhuma.
FILA_ALTA = 150_000


class Command(BaseCommand):
    help = ('Marca `atualizado_em = now()` para o `sync_es` reindexar o que foi '
            'escrito em lote (partes, grau, assunto).')

    def add_arguments(self, p):
        p.add_argument('--alvo', choices=sorted(ALVOS), required=True)
        p.add_argument('--lote', type=int, default=20_000, help='faixa de pk por passada')
        p.add_argument('--sleep', type=float, default=0.3)
        p.add_argument('--min-id', type=int, default=None)
        p.add_argument('--max-id', type=int, default=None)
        p.add_argument('--max-segundos', type=int, default=0, help='0 = sem teto de tempo')
        p.add_argument('--reiniciar', action='store_true', help='ignora o checkpoint')
        p.add_argument('--dry-run', action='store_true')

    # ---------------------------------------------------------------- helpers
    def _fila(self) -> int:
        try:
            import django_rq
            return django_rq.get_queue('es_index').count
        except Exception:
            return -1        # não deu pra ler ⇒ não freia por um número inventado

    def _topo(self) -> int:
        with connection.cursor() as c:
            c.execute('SELECT max(id) FROM tribunals_process')
            return c.fetchone()[0] or 0

    # ------------------------------------------------------------------ handle
    def handle(self, *a, **o):
        alvo, lote = o['alvo'], o['lote']
        if lote < 1:
            raise CommandError('--lote tem que ser >= 1')
        chave = f'campainha:{alvo}:cursor'
        topo = o['max_id'] if o['max_id'] is not None else self._topo()
        inicio = o['min_id'] if o['min_id'] is not None else 0
        if not o['reiniciar']:
            salvo = cache.get(chave)
            if salvo is not None and salvo > inicio:
                inicio = salvo
                self.stdout.write(f'retomando do checkpoint pk={inicio:,}')

        pred = ALVOS[alvo]
        self.stdout.write(
            f'alvo={alvo} faixa=({inicio:,}, {topo:,}] lote={lote:,} '
            f'sleep={o["sleep"]}s' + ('  DRY-RUN' if o['dry_run'] else ''))
        self.stdout.flush()

        t0 = time.monotonic()
        tocados = passadas = esperas = 0
        lo = inicio
        motivo = 'fim da faixa'
        while lo < topo:
            hi = min(lo + lote, topo)

            fila = self._fila()
            if fila > FILA_ALTA:
                esperas += 1
                if esperas % 10 == 1:
                    self.stdout.write(f'  fila em {fila:,} (>{FILA_ALTA:,}) — esperando o dreno')
                    self.stdout.flush()
                time.sleep(15)
                continue

            sql = (f'UPDATE tribunals_process p SET atualizado_em = now() '
                   f'WHERE p.id > %s AND p.id <= %s AND ({pred})')
            if o['dry_run']:
                sql = (f'SELECT count(*) FROM tribunals_process p '
                       f'WHERE p.id > %s AND p.id <= %s AND ({pred})')
            with transaction.atomic(), connection.cursor() as c:
                c.execute("SET LOCAL lock_timeout = '5s'")
                c.execute("SET LOCAL statement_timeout = '120s'")
                c.execute(sql, [lo, hi])
                n = c.fetchone()[0] if o['dry_run'] else c.rowcount

            tocados += n
            passadas += 1
            lo = hi
            if not o['dry_run']:
                cache.set(chave, lo, None)

            if passadas % 20 == 0:
                dur = time.monotonic() - t0
                self.stdout.write(
                    f'  pk={lo:,}  tocados={tocados:,}  '
                    f'{tocados / max(dur, 1e-9):,.0f}/s  {dur / 60:.1f}min  fila={fila:,}')
                self.stdout.flush()

            if o['max_segundos'] and (time.monotonic() - t0) >= o['max_segundos']:
                motivo = 'teto de tempo'
                break
            time.sleep(o['sleep'])

        dur = time.monotonic() - t0
        self.stdout.write(
            f'FIM ({motivo}): {tocados:,} processos marcados em {dur / 60:.1f} min '
            f'({passadas:,} passadas, {esperas:,} esperas de fila). '
            f'checkpoint pk={lo:,} de {topo:,}.')
        if lo < topo:
            # Teto atingido é ERRO registrado com o número real — nunca `return`
            # discreto (regra nº 2). "Marquei 300 mil" sem dizer que faltam 40
            # milhões é o corte mudo de novo.
            self.stderr.write(
                f'ERRO: parou em pk={lo:,} e o topo é {topo:,} — faltam '
                f'{topo - lo:,} pks da faixa. Retome o comando (ele guarda o '
                f'checkpoint) ou o resto NÃO chega à busca.')
