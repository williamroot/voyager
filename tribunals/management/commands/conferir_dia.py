"""O PORTÃO: um dia da ingestão só fecha quando os 59 tribunais fecham.

Por que este comando existe. Em 25/08/2026 a ingestão do dia morreu inteira —
10.410 `IngestionRun` em `failed`, zero `success` — e ninguém viu por 21 horas.
O que denunciou foi um KPI da tela, não um portão. E quando o dia voltou, voltou
com **1.180.554** publicações contra **1.529.530** do dia vizinho: o run fechou
`success`, o log ficou limpo e o número parecia plausível. Faltava um quarto do
dia.

Esse é o padrão mais caro deste projeto, e ele tem nome no `CLAUDE.md`:
**run verde, log limpo, número redondo**. Já custou 47.141 publicações uma vez.

## Os três critérios, e por que cada um sozinho não basta

Para CADA tribunal, no dia pedido:

1. **existe `success`** — pega o dia que nunca rodou;
2. **a contagem está dentro do desvio da mediana dos 5 dias úteis vizinhos DO
   MESMO TRIBUNAL** — pega o `success` que trouxe um terço. Comparar com o
   agregado nacional NÃO serve: um TJSP inteiro sumido (4,7% do fluxo) desaparece
   dentro do ruído dos outros 58;
3. **nenhum `failed` sem `success` posterior** — pega o dia que quebrou depois de
   ter fechado, que é o caso do watchdog não ter reenfileirado.

Um tribunal que passa nos três está fechado. Qualquer outro é ERRO com nome,
número e a razão — nunca uma linha de resumo dizendo "quase tudo ok".

## Contra o que este comando se defende

- **fim de semana e feriado**: um tribunal que publica 0 no sábado não está
  quebrado. A mediana usa dias ÚTEIS vizinhos, e dia cuja mediana é ~0 sai do
  portão com o rótulo `sem_expediente` em vez de virar alarme falso;
- **tribunal que simplesmente não publica todo dia**: mediana baixa ⇒ o piso
  absoluto (`--piso`) evita acusar quem sempre teve pouco;
- **tribunal novo ou desativado**: só entram tribunais `ativo=True` com
  `data_inicio_disponivel` anterior ao dia.

    python manage.py conferir_dia 2026-08-25
    python manage.py conferir_dia --ultimos 7          # varre a semana
    python manage.py conferir_dia 2026-08-25 --json    # para outro programa ler

Sai com **código 1** quando há tribunal fora do portão — dá para pendurar em
cron e em CI sem ninguém precisar ler a saída.
"""
import datetime
import json

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

#: dias úteis vizinhos usados na mediana (antes e depois do dia conferido).
VIZINHOS = 5

#: abaixo disso a mediana do tribunal é ruído — não dá para acusar ninguém de
#: incompleto quando o normal dele já é pouco.
PISO_MEDIANA = 200

#: fração da mediana abaixo da qual o dia é considerado INCOMPLETO. 0,60 = o
#: tribunal trouxe menos de 60% do que costuma trazer. Não é 1,0 porque volume
#: diário oscila de verdade (recesso, greve, pauta) — o que se caça aqui é o
#: buraco de um terço, não a variação de 10%.
FRACAO_MINIMA = 0.60

SQL_CONTAGEM = """
SELECT m.tribunal_id, m.data_disponibilizacao::date AS d, count(*)
  FROM tribunals_movimentacao m
 WHERE m.data_disponibilizacao >= %s AND m.data_disponibilizacao < %s
 GROUP BY 1, 2
"""

SQL_RUNS = """
SELECT r.tribunal_id, r.status, max(r.started_at)
  FROM tribunals_ingestionrun r
 WHERE r.janela_inicio = %s AND r.janela_fim = %s
 GROUP BY 1, 2
"""


def _mediana(valores):
    v = sorted(valores)
    if not v:
        return 0
    meio = len(v) // 2
    return v[meio] if len(v) % 2 else (v[meio - 1] + v[meio]) / 2


class Command(BaseCommand):
    help = 'Confere se um dia da ingestão fechou nos 59 tribunais (portão de completude).'

    def add_arguments(self, p):
        p.add_argument('dia', nargs='?', help='AAAA-MM-DD (default: ontem)')
        p.add_argument('--ultimos', type=int, default=0,
                       help='confere os N dias anteriores a hoje em vez de um só')
        p.add_argument('--fracao', type=float, default=FRACAO_MINIMA,
                       help=f'fração da mediana que caracteriza incompleto (default {FRACAO_MINIMA})')
        p.add_argument('--piso', type=int, default=PISO_MEDIANA)
        p.add_argument('--json', action='store_true')

    # ------------------------------------------------------------------ dados
    def _contagens(self, ini: datetime.date, fim: datetime.date) -> dict:
        """{(tribunal, dia): n} no intervalo [ini, fim)."""
        with transaction.atomic(), connection.cursor() as c:
            c.execute("SET LOCAL statement_timeout = '240s'")
            c.execute(SQL_CONTAGEM, [ini, fim])
            return {(t, d): n for t, d, n in c.fetchall()}

    def _runs(self, dia: datetime.date) -> dict:
        with transaction.atomic(), connection.cursor() as c:
            c.execute("SET LOCAL statement_timeout = '60s'")
            c.execute(SQL_RUNS, [dia, dia])
            fora = {}
            for trib, status, quando in c.fetchall():
                fora.setdefault(trib, {})[status] = quando
            return fora

    def _tribunais(self, dia: datetime.date) -> list:
        with transaction.atomic(), connection.cursor() as c:
            c.execute("SET LOCAL statement_timeout = '30s'")
            c.execute("""SELECT sigla FROM tribunals_tribunal
                          WHERE ativo = TRUE
                            AND (data_inicio_disponivel IS NULL OR data_inicio_disponivel <= %s)
                          ORDER BY sigla""", [dia])
            return [r[0] for r in c.fetchall()]

    # ----------------------------------------------------------------- portão
    def _conferir(self, dia, o) -> dict:
        janela_ini = dia - datetime.timedelta(days=VIZINHOS + 2)
        janela_fim = dia + datetime.timedelta(days=VIZINHOS + 3)
        cont = self._contagens(janela_ini, janela_fim)
        runs = self._runs(dia)
        tribunais = self._tribunais(dia)

        fechados, problemas = [], []
        for t in tribunais:
            n = cont.get((t, dia), 0)
            # mediana dos dias ÚTEIS vizinhos DO MESMO tribunal (o dia em si fora)
            vizinhos = []
            for k in range(-(VIZINHOS + 2), VIZINHOS + 3):
                d = dia + datetime.timedelta(days=k)
                if d == dia or d.weekday() >= 5:      # sábado/domingo fora
                    continue
                vizinhos.append(cont.get((t, d), 0))
            med = _mediana(vizinhos)

            st = runs.get(t, {})
            tem_ok = 'success' in st
            falhou_por_ultimo = ('failed' in st and
                                 (not tem_ok or st['failed'] > st['success']))

            if med < o['piso'] and n < o['piso']:
                # o normal deste tribunal já é pouco: não dá pra acusar.
                fechados.append({'t': t, 'n': n, 'med': med, 'nota': 'sem_expediente'})
                continue

            motivos = []
            if not tem_ok:
                motivos.append('sem run success')
            if falhou_por_ultimo:
                motivos.append('failed sem success posterior')
            if med >= o['piso'] and n < med * o['fracao']:
                motivos.append(f'{n:,} contra mediana {med:,.0f} '
                               f'({100.0 * n / med:.0f}% do normal)')
            if motivos:
                problemas.append({'t': t, 'n': n, 'med': med,
                                  'falta': max(int(med) - n, 0), 'motivos': motivos})
            else:
                fechados.append({'t': t, 'n': n, 'med': med, 'nota': 'ok'})

        return {'dia': dia.isoformat(), 'tribunais': len(tribunais),
                'fechados': len(fechados), 'problemas': problemas,
                'total_dia': sum(v for (t, d), v in cont.items() if d == dia),
                'falta_estimado': sum(p['falta'] for p in problemas)}

    # ----------------------------------------------------------------- handle
    def handle(self, *a, **o):
        if o['ultimos'] and o['dia']:
            raise CommandError('use `dia` OU `--ultimos`, não os dois')
        hoje = timezone.localdate()
        if o['ultimos']:
            dias = [hoje - datetime.timedelta(days=k)
                    for k in range(1, o['ultimos'] + 1)]
        elif o['dia']:
            try:
                dias = [datetime.date.fromisoformat(o['dia'])]
            except ValueError:
                raise CommandError('data inválida — use AAAA-MM-DD')
        else:
            dias = [hoje - datetime.timedelta(days=1)]

        relatorios = [self._conferir(d, o) for d in dias]

        if o['json']:
            self.stdout.write(json.dumps(relatorios, ensure_ascii=False, default=str))
        else:
            for r in relatorios:
                self._imprimir(r)

        # Código de saída 1 quando há buraco: o portão precisa servir a cron e a
        # CI sem depender de alguém LER a saída. Silêncio verde é o que a gente
        # está tentando matar.
        if any(r['problemas'] for r in relatorios):
            raise SystemExit(1)

    def _imprimir(self, r):
        cab = f"{r['dia']}  ·  {r['fechados']}/{r['tribunais']} tribunais fechados  ·  {r['total_dia']:,} publicações"
        self.stdout.write(cab)
        if not r['problemas']:
            self.stdout.write(self.style.SUCCESS('   PORTÃO FECHADO — nenhum tribunal fora.'))
            return
        self.stdout.write(self.style.ERROR(
            f"   {len(r['problemas'])} TRIBUNAIS FORA DO PORTÃO "
            f"· faltam ~{r['falta_estimado']:,} publicações"))
        for p in sorted(r['problemas'], key=lambda x: -x['falta']):
            self.stderr.write(
                f"   {p['t']:<8} {p['n']:>10,}  (mediana {p['med']:>10,.0f})  "
                f"{' · '.join(p['motivos'])}")
