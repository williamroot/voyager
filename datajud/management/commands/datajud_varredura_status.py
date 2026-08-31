"""Telemetria ao vivo da varredura do Datajud — e o kill switch, no mesmo lugar.

    # a tela: atualiza sozinha a cada 5s, Ctrl-C para sair
    datajud_varredura_status --watch 5

    # uma foto (para log, para colar em issue)
    datajud_varredura_status --json

    # PARE TUDO agora (vale em segundos, sem deploy) e depois religue
    datajud_varredura_status --parar
    datajud_varredura_status --retomar

    # pausa só um tribunal
    datajud_varredura_status --pausar TJSP
    datajud_varredura_status --despausar TJSP

Por que a tela e o switch moram juntos: quem olha o painel é quem decide
abortar, e na hora do aperto ninguém quer procurar em que arquivo estava o
`set_varredura_pausados`. Ver `.ia/ACERVO_CNJ.md`, seção "Runbook".
"""
import json
import time

from django.core.management.base import BaseCommand

from datajud import telemetria
from datajud.jobs import (set_varredura_parada, set_varredura_pausados,
                          varredura_parada, varredura_pausados)
from tribunals.models import Tribunal


def _dur(segundos) -> str:
    """Segundos → `2h13m`. ETA em segundos é ilegível num job de 20 h."""
    if segundos is None:
        return '—'
    s = int(segundos)
    if s < 60:
        return f'{s}s'
    if s < 3600:
        return f'{s // 60}m{s % 60:02d}'
    return f'{s // 3600}h{(s % 3600) // 60:02d}m'


def _mb(b) -> str:
    return '—' if not b else f'{b / 1048576:.0f}MB'


class Command(BaseCommand):
    help = 'Mostra a telemetria ao vivo da varredura e opera o kill switch'

    def add_arguments(self, p):
        p.add_argument('--watch', type=int, default=0,
                       help='repete a cada N segundos (0 = uma vez)')
        p.add_argument('--json', action='store_true', dest='como_json')
        p.add_argument('--todos', action='store_true',
                       help='mostra também os tribunais sem telemetria')
        p.add_argument('--parar', action='store_true',
                       help='PARADA GLOBAL: a varredura em curso sai na próxima página')
        p.add_argument('--retomar', action='store_true',
                       help='desliga a parada global')
        p.add_argument('--pausar', default=None, help='pausa um tribunal')
        p.add_argument('--despausar', default=None, help='despausa um tribunal')

    def handle(self, *a, **o):
        if self._mexeu_no_switch(o):
            return
        if not o['watch']:
            self._mostrar(o)
            return
        try:
            while True:
                self.stdout.write('\033[2J\033[H', ending='')   # limpa a tela
                self._mostrar(o)
                time.sleep(o['watch'])
        except KeyboardInterrupt:
            self.stdout.write('')

    # -- kill switch -------------------------------------------------------- #

    def _mexeu_no_switch(self, o) -> bool:
        mexeu = False
        if o['parar']:
            set_varredura_parada(True)
            self.stdout.write(self.style.WARNING(
                '⏹ PARADA GLOBAL ligada. A varredura em curso sai na próxima '
                'página (~10s) SALVANDO o cursor; a retomada continua dali.'))
            mexeu = True
        if o['retomar']:
            set_varredura_parada(False)
            self.stdout.write(self.style.SUCCESS('▶ parada global desligada'))
            mexeu = True
        if o['pausar']:
            set_varredura_pausados(varredura_pausados() | {o['pausar'].upper()})
            self.stdout.write(self.style.WARNING(f"⏸ {o['pausar'].upper()} pausado"))
            mexeu = True
        if o['despausar']:
            set_varredura_pausados(varredura_pausados() - {o['despausar'].upper()})
            self.stdout.write(self.style.SUCCESS(f"▶ {o['despausar'].upper()} despausado"))
            mexeu = True
        return mexeu

    # -- tela --------------------------------------------------------------- #

    def _mostrar(self, o):
        siglas = list(Tribunal.objects.order_by('sigla').values_list('sigla', flat=True))
        estados = telemetria.snapshot(siglas)
        pausados = varredura_pausados()
        parada = varredura_parada()

        if o['como_json']:
            self.stdout.write(json.dumps(
                {'parada_global': parada, 'pausados': sorted(pausados),
                 'tribunais': estados}, ensure_ascii=False, default=str))
            return

        if parada:
            self.stdout.write(self.style.ERROR(
                '⏹ PARADA GLOBAL LIGADA — nenhuma varredura nova roda, e as em '
                'curso saem na próxima página'))
        if pausados:
            self.stdout.write(self.style.WARNING(
                f'⏸ pausados: {", ".join(sorted(pausados))}'))
        if not estados:
            self.stdout.write('nenhuma varredura com telemetria nos últimos 7 dias')
            return

        cab = (f'{"trib":8}{"estado":11}{"reqs":>8}{"lidos":>12}{"gravados":>12}'
               f'{"docs/s":>9}{"B/doc":>8}{"pág":>7}{"restam":>13}{"ETA":>9}  erros')
        self.stdout.write(cab)
        self.stdout.write('─' * len(cab))
        rodando = [e for e in estados if e.get('estado') == 'rodando']
        outros = [e for e in estados if e.get('estado') != 'rodando']
        for e in rodando + sorted(outros, key=lambda x: x.get('atualizado_em') or '',
                                  reverse=True):
            self._linha(e)
        self._rodape(rodando, estados)

    def _linha(self, e):
        erros = e.get('erros') or {}
        txt_erros = ' '.join(f'{k}×{v}' for k, v in sorted(erros.items())) or '—'
        estado = e.get('estado') or '—'
        restam = e.get('restante')
        linha = (
            f"{e.get('tribunal', '?'):8}{estado[:10]:11}"
            f"{e.get('requisicoes', 0):>8,}{e.get('lidos', 0):>12,}"
            f"{e.get('gravados', 0):>12,}{e.get('docs_por_s', 0):>9,.0f}"
            f"{(e.get('bytes_por_doc') or 0):>8,.0f}"
            f"{(e.get('pagina_atual') or 0):>7,}"
            f"{(f'{restam:,}' if restam is not None else '—'):>13}"
            f"{_dur(e.get('eta_s')):>9}  {txt_erros}"
        )
        estilo = (self.style.ERROR if erros or estado.startswith(('erro', 'max_pag'))
                  else self.style.SUCCESS if estado == 'rodando' else lambda x: x)
        self.stdout.write(estilo(linha))
        if e.get('cursor_iso'):
            self.stdout.write(f"{'':8}cursor {e['cursor_iso']}  ·  lido {_mb(e.get('bytes'))}")

    def _rodape(self, rodando, estados):
        lidos = sum(e.get('lidos', 0) for e in estados)
        reqs = sum(e.get('requisicoes', 0) for e in estados)
        ritmo = sum(e.get('docs_por_s', 0) for e in rodando)
        restam = [e.get('restante') for e in estados if e.get('restante') is not None]
        self.stdout.write('─' * 40)
        self.stdout.write(
            f'{len(rodando)} rodando · {reqs:,} requisições · {lidos:,} docs lidos '
            f'· {ritmo:,.0f} docs/s agregado')
        if restam:
            total = sum(restam)
            self.stdout.write(
                f'faltam {total:,} docs medidos dos dois lados · ETA agregado '
                f'{_dur(total / ritmo if ritmo else None)}')
        else:
            # ETA agregado só existe se TODO mundo mediu o alvo; senão é chute
            self.stdout.write('ETA agregado: — (nenhum tribunal com alvo medido)')
