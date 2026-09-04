"""Roda a busca por parte pelo CÓDIGO DE PRODUÇÃO, de dentro do container.

O `scripts/recon_busca_parte.py` mede a FONTE com requisições cruas; este
comando exercita o que vai rodar de verdade — registry, motor, parser e (com
`--ingerir`) a hidratação. É por aqui que se confere um tribunal antes de
ligá-lo, e é o único jeito de medir o TRF3, cujo host recusa conexão fora da
malha de proxies:

    docker exec -w /app voyager-worker_busca-1 \\
        python manage.py busca_parte TRF3 nome "MARIA JOSE DOS SANTOS"

Só leitura por padrão: não cria run, não escreve no banco, não enfileira nada.
"""
import json
import time
from dataclasses import asdict

from django.core.management.base import BaseCommand, CommandError

from enrichers.busca.base import CRITERIOS, BuscaError
from enrichers.busca.entrada import EntradaInvalida, validar
from enrichers.busca.registry import CATALOGO, buscador


class Command(BaseCommand):
    help = 'Busca processos por CPF/CNPJ, nome, OAB ou nome de advogado na consulta pública.'

    def add_arguments(self, parser):
        parser.add_argument('tribunal', help=f'Sigla: {", ".join(sorted(CATALOGO))}')
        parser.add_argument('criterio', choices=list(CRITERIOS))
        parser.add_argument('valor')
        parser.add_argument('--paginas', type=int, default=2,
                            help='Teto de páginas (default 2 — é um diagnóstico, '
                                 'não uma coleta).')
        parser.add_argument('--ingerir', action='store_true',
                            help='ESCREVE: enfileira o que for novo para entrar no acervo.')
        parser.add_argument('--json', action='store_true', dest='como_json')

    def handle(self, *args, tribunal, criterio, valor, paginas, ingerir,
               como_json, **opts):
        sigla = tribunal.upper()
        fonte = CATALOGO.get(sigla)
        if not fonte:
            raise CommandError(f'{sigla} não tem busca por parte. '
                               f'Disponíveis: {", ".join(sorted(CATALOGO))}')
        if criterio not in fonte.criterios:
            raise CommandError(f'{sigla} não oferece busca por {criterio} '
                               f'na consulta pública (aceita: '
                               f'{", ".join(sorted(fonte.criterios))})')
        if criterio not in fonte.criterios_medidos:
            self.stdout.write(self.style.WARNING(
                f'AVISO: busca por {criterio} nunca foi conferida ao vivo no '
                f'{sigla}. Um resultado vazio aqui não prova ausência.'))

        try:
            entrada = validar(criterio, valor)
        except EntradaInvalida as exc:
            raise CommandError(f'{exc.codigo}: {exc.mensagem}') from exc

        motor = buscador(sigla)
        inicio = time.monotonic()
        colhidos, saida = [], {'tribunal': sigla, 'criterio': criterio,
                               'valor': entrada['valor'], 'paginas': 0}
        try:
            for pagina in motor.paginar(criterio, entrada['valor'],
                                        teto_paginas=paginas):
                saida['paginas'] += 1
                colhidos.extend(pagina.itens)
                saida.update({
                    'total_declarado': pagina.total_declarado,
                    'total_e_teto': pagina.total_e_teto,
                    'aviso_fonte': pagina.aviso_fonte,
                })
                if not como_json:
                    self.stdout.write(
                        f'  página {pagina.pagina}: {len(pagina.itens)} itens '
                        f'(a fonte declara {pagina.total_declarado}'
                        f'{" — É O TETO DELA" if pagina.total_e_teto else ""})')
                    if pagina.aviso_fonte:
                        self.stdout.write(self.style.WARNING(
                            f'  fonte inconsistente: {pagina.aviso_fonte}'))
                if not pagina.tem_proxima:
                    break
        except BuscaError as exc:
            saida['erro'] = f'{type(exc).__name__}: {exc}'
            if not como_json:
                self.stdout.write(self.style.ERROR(f'  {saida["erro"]}'))

        saida['encontrados'] = len(colhidos)
        saida['levou_s'] = round(time.monotonic() - inicio, 1)

        if ingerir and colhidos:
            from enrichers.busca.ingestao import enfileirar
            saida['ingestao'] = enfileirar([i.numero_cnj for i in colhidos])

        if como_json:
            saida['itens'] = [asdict(i) for i in colhidos]
            self.stdout.write(json.dumps(saida, ensure_ascii=False, indent=2, default=str))
            return

        for item in colhidos[:20]:
            self.stdout.write(f'  {item.numero_cnj}  {item.classe[:40]:40} '
                              f'{item.orgao[:38]}')
        if len(colhidos) > 20:
            self.stdout.write(f'  ... e mais {len(colhidos) - 20}')
        self.stdout.write(self.style.SUCCESS(
            f'{saida["encontrados"]} processos em {saida["paginas"]} página(s), '
            f'{saida["levou_s"]}s'))
        if saida.get('ingestao'):
            self.stdout.write(
                f'ingestão: {saida["ingestao"]["ja_no_acervo"]} já no acervo, '
                f'{saida["ingestao"]["enfileirados"]} enfileirados, '
                f'{saida["ingestao"]["fora_do_teto"]} fora do teto')
