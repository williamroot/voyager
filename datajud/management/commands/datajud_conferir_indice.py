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
            raise CommandError(f'faixa vazia: {desde.isoformat()} → {ate.isoformat()}')
        reparar = not o['sem_reparo']
        passo = dt.timedelta(minutes=o['passo_min'])

        self.stdout.write(
            f'faixa de escrita: {desde.isoformat()} → {ate.isoformat()}  '
            f'(passo {o["passo_min"]} min, reparo {"LIGADO" if reparar else "DESLIGADO"})')
        self.stdout.write(
            f'{"janela (inserido_em)":34} {"movs_pg":>9} {"fora":>8} {"proc_pg":>9} '
            f'{"atrasados":>10} {"enfileirado":>12} {"s":>6}')

        tot = {'movs_pg': 0, 'movs_fora': 0, 'procs_pg': 0, 'procs_atrasados': 0,
               'enfileirado': 0, 'abstidos': 0, 'tetos': 0, 'passos': 0}
        cursor = desde
        while cursor < ate:
            prox = min(cursor + passo, ate)
            t0 = time.monotonic()
            m = ({'pg': None, 'faltando': None, 'enfileiradas': 0, 'teto_atingido': False,
                  'abstido': False} if o['so_processos']
                 else indice.conferir_movs(cursor, prox, reparar=reparar))
            p = ({'pg': None, 'atrasados': None, 'enfileirados': 0, 'teto_atingido': False,
                  'abstido': False} if o['so_movs']
                 else indice.conferir_processos(cursor, prox, reparar=reparar))
            dur = time.monotonic() - t0

            def _n(v):
                return '   —' if v is None else f'{v:,}'.replace(',', '.')

            enf = (m['enfileiradas'] or 0) + (p['enfileirados'] or 0)
            marca = ''
            if m['abstido'] or p['abstido']:
                marca = '  ABSTIDO'
                tot['abstidos'] += 1
            if m['teto_atingido'] or p['teto_atingido']:
                marca += '  TETO'
                tot['tetos'] += 1
            self.stdout.write(
                f'{cursor:%Y-%m-%d %H:%M} +{o["passo_min"]:>3}min{"":10} '
                f'{_n(m["pg"]):>9} {_n(m["faltando"]):>8} {_n(p["pg"]):>9} '
                f'{_n(p["atrasados"]):>10} {_n(enf):>12} {dur:>6.2f}{marca}')
            for chave, valor in (('movs_pg', m['pg']), ('movs_fora', m['faltando']),
                                 ('procs_pg', p['pg']), ('procs_atrasados', p['atrasados']),
                                 ('enfileirado', enf)):
                if valor:
                    tot[chave] += valor
            tot['passos'] += 1
            cursor = prox
            if o['sleep']:
                time.sleep(o['sleep'])

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'{tot["passos"]} passos · movimentações: {tot["movs_pg"]:,} conferidas, '
            f'{tot["movs_fora"]:,} fora do índice · processos: {tot["procs_pg"]:,} '
            f'conferidos, {tot["procs_atrasados"]:,} com doc anterior à escrita · '
            f'{tot["enfileirado"]:,} re-enfileirados'.replace(',', '.')))
        if tot['abstidos'] or tot['tetos']:
            # Abstenção e teto NÃO são detalhe de rodapé: o trecho continua em
            # dívida e alguém tem que rodar de novo. Regra nº 2 + nº 6.
            self.stdout.write(self.style.ERROR(
                f'{tot["abstidos"]} passo(s) ABSTIDOS e {tot["tetos"]} com TETO atingido — '
                'esses trechos NÃO foram conferidos. Rode-os de novo.'))
