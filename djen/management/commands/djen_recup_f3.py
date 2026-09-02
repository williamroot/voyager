"""Operar a recuperação da Fase 3: ver, parar, religar, incluir o TJPR.

O cron faz o trabalho sozinho (`tick_recuperacao_fase3`, a cada 5 min). Este
comando existe para as três perguntas que aparecem às 3h da manhã:

    manage.py djen_recup_f3                 # como está? (não muda nada)
    manage.py djen_recup_f3 --parar         # kill switch, vale em segundos
    manage.py djen_recup_f3 --religar
    manage.py djen_recup_f3 --agora         # força um tique agora, síncrono

O kill switch mora no Redis (`djen:recup_f3:off`), e não em variável de
ambiente, porque parar não pode custar um deploy. O Redis de prod tem AOF desde
31/08/2026 — a chave sobrevive a restart. Sem AOF isso seria um kill switch que
se desliga sozinho no próximo reboot, que é pior que não ter.
"""
from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.utils import timezone

from djen import recuperacao as R


class Command(BaseCommand):
    help = 'Estado e controle da recuperação da Fase 3 (19 tribunais fora da Fase 2).'

    def add_arguments(self, p):
        p.add_argument('--parar', action='store_true',
                       help='liga o kill switch — o tique para de enfileirar')
        p.add_argument('--religar', action='store_true', help='desliga o kill switch')
        p.add_argument('--tjpr-on', action='store_true',
                       help='inclui o TJPR (1.152 dias, 38,8M) — DECISÃO COMERCIAL')
        p.add_argument('--tjpr-off', action='store_true', help='tira o TJPR')
        p.add_argument('--agora', action='store_true',
                       help='roda um tique AGORA, síncrono, e imprime o resultado')
        p.add_argument('--medir', action='store_true',
                       help='recalcula pendentes/vazão do banco em vez de ler o cache')

    def handle(self, *a, **o):
        if o['parar']:
            cache.set(R.CHAVE_OFF, 1, None)
            self.stdout.write(self.style.WARNING(
                'PARADO. O tique segue rodando e MEDINDO (o card continua vivo), '
                'mas não enfileira. Religar: --religar'))
        if o['religar']:
            cache.delete(R.CHAVE_OFF)
            cache.set(R.CHAVE_LIGADO_EM, timezone.now(), None)
            self.stdout.write(self.style.SUCCESS('RELIGADO.'))
        if o['tjpr_on']:
            cache.set(R.CHAVE_TJPR, 1, None)
            self.stdout.write(self.style.WARNING(
                'TJPR INCLUÍDO — 1.152 dias e ~38,8M de publicações estimadas, '
                '43% do que resta. Isto é decisão do dono do produto.'))
        if o['tjpr_off']:
            cache.delete(R.CHAVE_TJPR)
            self.stdout.write('TJPR fora.')

        if o['agora']:
            self.stdout.write('tique síncrono...')
            r = R.tick_recuperacao_fase3()
            self._imprimir(r)
            return

        if o['medir']:
            siglas = R.siglas_alvo()
            pend = R.dias_pendentes(siglas)
            por = {}
            for s, _d, _n in pend:
                por[s] = por.get(s, 0) + 1
            self._imprimir({
                'pendentes': len(pend), 'vazao_24h': R.vazao(siglas),
                'por_tribunal': sorted(por.items(), key=lambda kv: -kv[1]),
                'tjpr_ligado': R.TJPR in siglas, 'medido_em': 'agora (--medir)',
            })
            return

        r = R.estado()
        if r is None:
            self.stdout.write(self.style.ERROR(
                'SEM RETRATO no cache. Ou o tique nunca rodou (scheduler sem o '
                'job agendado?), ou o retrato venceu o TTL de 6 h — que já é, '
                'por si, o alarme de que ele parou.'))
            return
        self._imprimir(r)

    def _imprimir(self, r):
        pausado = bool(cache.get(R.CHAVE_OFF))
        self.stdout.write('')
        self.stdout.write(f'  medido em ......... {r.get("medido_em")}')
        self.stdout.write(f'  kill switch ....... {"PARADO" if pausado else "ligado"}')
        self.stdout.write(f'  TJPR .............. {"DENTRO" if r.get("tjpr_ligado") else "fora"}')
        self.stdout.write(f'  pendentes ......... {r.get("pendentes")}')
        self.stdout.write(f'  em voo ............ {r.get("em_voo")}')
        self.stdout.write(f'  enfileirados ...... {r.get("enfileirados")}')
        self.stdout.write(f'  vazão 24h (dias) .. {r.get("vazao_24h")}')
        self.stdout.write(f'  teimosos .......... {r.get("teimosos")}')
        parada = r.get('motivo_parada')
        if parada:
            self.stdout.write(self.style.ERROR(f'  PAROU POR ......... {parada}'))
        self.stdout.write('')
        for sig, n in (r.get('por_tribunal') or [])[:25]:
            self.stdout.write(f'    {sig:<7}{n:>6}')
