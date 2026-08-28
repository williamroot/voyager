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
from django.utils import timezone

from tribunals import portao


class Command(BaseCommand):
    help = 'Confere se um dia da ingestão fechou nos 59 tribunais (portão de completude).'

    def add_arguments(self, p):
        p.add_argument('dia', nargs='?', help='AAAA-MM-DD (default: ontem)')
        p.add_argument('--ultimos', type=int, default=0,
                       help='confere os N dias anteriores a hoje em vez de um só')
        p.add_argument('--fracao', type=float, default=portao.FRACAO_MINIMA,
                       help=f'fração da mediana que caracteriza incompleto '
                            f'(default {portao.FRACAO_MINIMA})')
        p.add_argument('--piso', type=int, default=portao.PISO_MEDIANA)
        p.add_argument('--json', action='store_true')

    def handle(self, *a, **o):
        if o['ultimos'] and o['dia']:
            raise CommandError('use `dia` OU `--ultimos`, não os dois')
        hoje = timezone.localdate()
        if o['ultimos']:
            dias = [hoje - datetime.timedelta(days=k) for k in range(1, o['ultimos'] + 1)]
        elif o['dia']:
            try:
                dias = [datetime.date.fromisoformat(o['dia'])]
            except ValueError:
                raise CommandError('data inválida — use AAAA-MM-DD')
        else:
            dias = [hoje - datetime.timedelta(days=1)]

        relatorios = [portao.conferir(d, fracao=o['fracao'], piso=o['piso']) for d in dias]

        if o['json']:
            self.stdout.write(json.dumps(relatorios, ensure_ascii=False, default=str))
        else:
            for r in relatorios:
                self._imprimir(r)

        # Código 1 quando há buraco: o portão precisa servir a cron e a CI sem
        # depender de alguém LER a saída. Silêncio verde é o que estamos matando.
        if any(r['problemas'] for r in relatorios):
            raise SystemExit(1)

    def _imprimir(self, r):
        self.stdout.write(
            f"{r['dia']}  ·  {r['fechados']}/{r['tribunais']} tribunais fechados"
            f"  ·  {r['total_dia']:,} publicações")
        if r.get('sem_amostra'):
            self.stdout.write(
                f"   ABSTENÇÃO: {len(r['sem_amostra'])} sem amostra do mesmo dia da "
                f"semana ({', '.join(r['sem_amostra'][:10])}) — não avaliados por volume.")
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
