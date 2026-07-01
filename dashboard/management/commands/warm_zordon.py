"""Pré-aquece o cache de extração do Zordon (showcase sempre instantâneo).

A extração (RAG + LLM 20b) leva dezenas de segundos na 1ª vez. Este command
lista os processos do acervo (GET /api/processos) e roda a extração de cada um,
gravando no cache (`zordon_extract:<cnj>`, TTL longo). Rode off-peak / após mudar
o pipeline de extração.

    python manage.py warm_zordon                 # todos do acervo
    python manage.py warm_zordon --concurrency 3 # 3 em paralelo (Ollama serializa)
    python manage.py warm_zordon --cnj 0001234-56.2024.8.26.0100   # um só
"""
import time
from concurrent.futures import ThreadPoolExecutor

from django.core.cache import cache
from django.core.management.base import BaseCommand

from dashboard import zordon_client


class Command(BaseCommand):
    help = 'Pré-aquece o cache de extração do Zordon para os processos do acervo.'

    def add_arguments(self, parser):
        parser.add_argument('--concurrency', type=int, default=2,
                            help='Extrações em paralelo (Ollama serializa na GPU; 2-3 é o útil).')
        parser.add_argument('--cnj', action='append', default=[],
                            help='CNJ específico (repetível). Sem isso, usa todo o acervo.')

    def handle(self, *args, **opts):
        cnjs = opts['cnj']
        if not cnjs:
            resp = zordon_client.listar_processos()
            if resp.get('erro'):
                self.stderr.write(self.style.ERROR(f"Falha ao listar acervo: {resp['erro']}"))
                return
            cnjs = [p['numero_cnj'] for p in resp['processos'] if p.get('numero_cnj')]
        total = len(cnjs)
        self.stdout.write(f'Pré-aquecendo {total} processos (concurrency={opts["concurrency"]})…')

        stats = {'ok': 0, 'sem_contexto': 0, 'erro': 0}
        t0 = time.time()

        def _warm(cnj):
            r = zordon_client.extrair(cnj)
            erro = r.get('erro')
            if erro in (None, 'sem_contexto'):
                cache.set(zordon_client.extract_cache_key(cnj), r, zordon_client.EXTRACT_CACHE_TTL)
            return cnj, ('sem_contexto' if erro == 'sem_contexto' else ('erro' if erro else 'ok')), erro

        with ThreadPoolExecutor(max_workers=max(1, opts['concurrency'])) as ex:
            for i, (cnj, status, erro) in enumerate(ex.map(_warm, cnjs), 1):
                stats[status] += 1
                msg = f'[{i}/{total}] {cnj}: {status}'
                if erro and status == 'erro':
                    msg += f' ({str(erro)[:60]})'
                self.stdout.write(msg)

        dt = time.time() - t0
        self.stdout.write(self.style.SUCCESS(
            f'Concluído em {dt:.0f}s — ok={stats["ok"]} sem_contexto={stats["sem_contexto"]} erro={stats["erro"]}'
        ))
