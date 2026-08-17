"""Kill switch da coleta de diários — parar em segundos, sem deploy.

POR QUE ISTO EXISTE
===================
As quatro fontes desta porta são servidores de TERCEIRO que não nos defendem de
nós mesmos: nenhuma tem rate limit, WAF ou robots.txt, e uma delas (o DEJT) é um
JBoss 4.3.0.GA de 2010 do CSJT. A conduta é toda auto-imposta (rps por fonte,
janela horária, circuit-breaker). Quando ela não basta — porque o outro lado
começou a sofrer, porque o Postgres está afogado, porque alguém do outro lado
ligou pedindo — a resposta tem que ser em SEGUNDOS, e não um deploy.

É o mesmo mecanismo do `set_varredura_pausados` do `datajud/jobs.py`, que existe
pela mesma razão (a APIKey compartilhada do CNJ estrangula sem avisar): uma
chave no Redis, lida por `diarios.base.checar_pausa` ANTES de cada requisição.
Job que pega pausa devolve `{'skip': 'adiado'}` — não empilha no FailedRegistry
e não conta tentativa, então religar retoma de onde parou.

USO
---
    manage.py diarios_pausar --listar
    manage.py diarios_pausar dejt tjsp-dje      # pausa estas
    manage.py diarios_pausar --tudo             # pausa TODAS ('*')
    manage.py diarios_pausar --religar dejt     # tira só o dejt da lista
    manage.py diarios_pausar --religar --tudo   # limpa a lista inteira
"""

import json

from django.core.management.base import BaseCommand, CommandError

from diarios.base import listar, pausados, pausar

TODAS = '*'


class Command(BaseCommand):
    help = 'Pausa/religa a coleta de diários (kill switch por fonte, efeito imediato).'

    def add_arguments(self, parser):
        parser.add_argument('fontes', nargs='*', help='slugs (tjsp-dje, dejt, stf, ...)')
        parser.add_argument('--tudo', action='store_true', help=f"usa o coringa {TODAS!r}")
        parser.add_argument('--religar', action='store_true', help='remove em vez de acrescentar')
        parser.add_argument('--listar', action='store_true', help='só mostra o estado atual')

    def handle(self, *args, **o):
        atuais = pausados()
        if o['listar'] or (not o['fontes'] and not o['tudo']):
            self.stdout.write(json.dumps({
                'pausadas': sorted(atuais),
                'tudo_pausado': TODAS in atuais,
                'registradas': listar(),
            }, ensure_ascii=False, indent=2))
            return

        alvo = {TODAS} if o['tudo'] else set(o['fontes'])
        conhecidas = set(listar()) | {TODAS}
        # Só avisa: pausar um slug que ainda não existe é legítimo (a fonte pode
        # estar sendo implementada). Recusar seria pior — kill switch que discute
        # não é kill switch.
        for slug in sorted(alvo - conhecidas):
            self.stderr.write(self.style.WARNING(f'aviso: {slug!r} não está registrada'))

        if not o['religar'] and not alvo:
            raise CommandError('nada a pausar: passe slugs ou --tudo')
        # `--religar --tudo` limpa a lista inteira; `--religar <slug>` tira só ele.
        novas = (set() if o['tudo'] else atuais - alvo) if o['religar'] else atuais | alvo

        pausar(novas)
        self.stdout.write(json.dumps({
            'acao': 'religar' if o['religar'] else 'pausar',
            'alvo': sorted(alvo),
            'antes': sorted(atuais),
            'agora': sorted(novas),
            'tudo_pausado': TODAS in novas,
        }, ensure_ascii=False, indent=2))
