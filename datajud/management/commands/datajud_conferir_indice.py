"""Backfill do gate de índice da porta do Datajud — uma faixa de tempo à mão.

O cron (`datajud.jobs.conferir_indice_datajud`) cobre a fronteira: do watermark
até `agora - 20 min`. Quem cobre o PASSADO é este comando, e ele existe por dois
motivos concretos:

  · o gate nasceu em 24/08/2026, e a porta escreve desde 2026-06 — tudo que ela
    gravou antes disso nunca foi conferido por ninguém;
  · se a chave do watermark sumir do cache, o cron re-ancora 6 h atrás e GRITA
    (`datajud/indice.py::RE_ANCORA_HORAS`). O trecho descoberto se recupera
    aqui, por faixa explícita.

Ele NÃO toca o watermark do cron — mesmo acordo do `--marcar-acervo` da
varredura: quem viu só um recorte do tempo não pode mexer no relógio de quem
percorre a linha inteira.

    # medir sem consertar (leitura pura dos dois lados)
    manage.py datajud_conferir_indice --desde 2026-08-01 --ate 2026-08-05 --sem-reparo

    # conferir e reparar um dia, com folga entre passos
    manage.py datajud_conferir_indice --desde 2026-08-20 --ate 2026-08-21 --sleep 1

    # só o lado dos processos, passo maior
    manage.py datajud_conferir_indice --desde 2026-07-01 --ate 2026-08-01 \\
        --so-processos --passo-min 60
"""
import datetime as dt
import time

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from datajud import indice


def _instante(bruto: str) -> dt.datetime:
    """Aceita `YYYY-MM-DD` e `YYYY-MM-DDTHH:MM`. Naive vira hora local.

    Hora local e não UTC de propósito: quem digita uma data está pensando no
    relógio dele. O `inserido_em` é comparado como instante ABSOLUTO nos dois
    lados, então o fuso entra uma vez só, aqui.
    """
    try:
        quando = dt.datetime.fromisoformat(bruto)
    except ValueError as exc:
        raise CommandError(f'data inválida: {bruto!r} ({exc})') from exc
    if timezone.is_naive(quando):
        quando = timezone.make_aware(quando)
    return quando


class Command(BaseCommand):
    help = 'Confere (e repara) o índice do que a porta do Datajud gravou numa faixa de tempo.'

    def add_arguments(self, parser):
        parser.add_argument('--desde', required=True,
                            help='início da faixa de ESCRITA (inserido_em), inclusivo')
        parser.add_argument('--ate', default=None,
                            help='fim da faixa, exclusivo (default: agora - carência)')
        parser.add_argument('--passo-min', type=int, default=indice.PASSO_MIN,
                            help=f'tamanho do recorte em minutos (default {indice.PASSO_MIN}, '
                                 'medido: 2,33 s por passo no Postgres)')
        parser.add_argument('--sem-reparo', action='store_true',
                            help='só mede os dois lados; não enfileira nada')
        parser.add_argument('--so-movs', action='store_true')
        parser.add_argument('--so-processos', action='store_true')
        parser.add_argument('--sleep', type=float, default=0.0,
                            help='pausa entre passos — o Postgres é disk-I/O-bound')

    def handle(self, *args, **o):
        if o['so_movs'] and o['so_processos']:
            raise CommandError('--so-movs e --so-processos se excluem')
        desde = _instante(o['desde'])
        ate = (_instante(o['ate']) if o['ate']
               else timezone.now() - dt.timedelta(minutes=indice.CARENCIA_MIN))
        if ate <= desde:
            raise CommandError(f'faixa vazia: {desde.isoformat()} -> {ate.isoformat()}')
        reparar = not o['sem_reparo']
        lados = ('movs',) if o['so_movs'] else ('processos',) if o['so_processos'] else \
                ('movs', 'processos')

        self.stdout.write(
            f'faixa de escrita: {desde.isoformat()} -> {ate.isoformat()}  '
            f'(passo {o["passo_min"]} min, reparo {"LIGADO" if reparar else "DESLIGADO"})')
        self.stdout.write(
            f'{"janela (inserido_em)":36} {"movs_pg":>9} {"fora":>8} {"proc_pg":>9} '
            f'{"atrasados":>10} {"enfileirado":>12} {"s":>6}')

        def _n(v):
            return '       -' if v is None else f'{v:,}'.replace(',', '.')

        def relatar(a, b, m, p, dur):
            """Uma linha por recorte MEDIDO — inclusive os que o teto dividiu.

            Reusar `conferir_janela` (em vez de o comando ter o próprio laço) é
            o que faz o backfill se comportar EXATAMENTE como o cron: mesma
            divisão ao meio no teto, mesma abstenção, mesmo reparo. A primeira
            versão tinha laço próprio, não dividia, e num recorte de pico
            imprimiu `TETO` e mandou o operador "rodar de novo" — para bater no
            mesmo teto de novo.
            """
            enf = (m['enfileiradas'] or 0) + (p['enfileirados'] or 0)
            marca = ''
            if m['abstido'] or p['abstido']:
                marca += '  ABSTIDO'
            if m['teto_atingido'] or p['teto_atingido']:
                marca += '  TETO (dividindo)'
            mins = (b - a).total_seconds() / 60
            self.stdout.write(
                f'{a:%Y-%m-%d %H:%M} +{mins:>6.1f}min{"":8} '
                f'{_n(m["pg"]):>9} {_n(m["faltando"]):>8} {_n(p["pg"]):>9} '
                f'{_n(p["atrasados"]):>10} {_n(enf):>12} {dur:>6.2f}{marca}')
            if o['sleep']:
                time.sleep(o['sleep'])

        tot = indice.conferir_janela(desde, ate, reparar=reparar,
                                     passo_min=o['passo_min'], relatar=relatar,
                                     lados=lados)

        def _pt(n):
            # ponto como separador de milhar. Formatar o NÚMERO, nunca a frase
            # inteira: um `.replace(',', '.')` no fim comia também as vírgulas
            # do texto e o resumo saía com pontos no lugar delas.
            return f'{n:,}'.replace(',', '.')

        enfileirado = tot['movs_enfileiradas'] + tot['procs_enfileirados']
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'{tot["passos"]} recortes · movimentações: {_pt(tot["movs_pg"])} conferidas, '
            f'{_pt(tot["movs_fora"])} fora do índice · processos: '
            f'{_pt(tot["procs_pg"])} conferidos, {_pt(tot["procs_atrasados"])} com doc '
            f'anterior à escrita · {_pt(enfileirado)} re-enfileirados'))
        fechou = dt.datetime.fromisoformat(tot['ate'])
        if tot['abstidos'] or tot['teto'] or fechou < ate:
            # Abstenção e teto NÃO são detalhe de rodapé: a faixa continua em
            # dívida a partir de `ate` e alguém tem que rodar de novo. Regra
            # nº 2 + nº 6.
            self.stdout.write(self.style.ERROR(
                f'FECHOU só até {tot["ate"]} ({tot["abstidos"]} abstidos, '
                f'teto={tot["teto"]}) — o resto da faixa NÃO foi conferido. '
                f'Rode de novo a partir daí.'))
