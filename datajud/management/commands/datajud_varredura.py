"""Varre o acervo declarado ao CNJ (Datajud) pro índice `voyager-acervo`.

    # uma passada curta pra ver o comportamento: NÃO escreve no índice e NÃO
    # mexe no watermark — mede a fonte e devolve vazão, bytes/doc e o que sobrou
    datajud_varredura TJMG --max-paginas 3 --dry-run

    # varredura completa de um tribunal, retomando de onde parou
    datajud_varredura TJMG

    # só o nicho (Cumprimento de Sentença contra a Fazenda Pública)
    datajud_varredura TJSP --classe 12078

    # frota inteira, do maior acervo pro menor
    datajud_varredura --todos

Ver `datajud/varredura.py` pra por que a paginação é por `range` e não por
`search_after`, e `.ia/INGESTION.md` pro contexto da lacuna que isso fecha.
"""
import json

from django.core.management.base import BaseCommand, CommandError

from datajud.varredura import (Varredura, deve_parar, marcar_no_acervo,
                               varrer_tribunal)
from tribunals.models import Tribunal


class Command(BaseCommand):
    help = 'Varre o acervo do Datajud (esqueleto nacional) pro índice voyager-acervo'

    def add_arguments(self, p):
        p.add_argument('sigla', nargs='?', help='sigla do tribunal (ex: TJMG)')
        p.add_argument('--todos', action='store_true',
                       help='varre todos os tribunais ativos')
        p.add_argument('--max-paginas', type=int, default=None,
                       help='para depois de N páginas (cada uma = 10k docs)')
        p.add_argument('--do-zero', action='store_true',
                       help='ignora o watermark e recomeça do início')
        p.add_argument('--classe', type=int, default=None,
                       help='restringe a uma classe TPU (ex: 12078)')
        p.add_argument('--dry-run', action='store_true',
                       help='mede sem escrever: não toca no índice nem no watermark')
        p.add_argument('--marcar-acervo', action='store_true',
                       help='só remarca no_acervo comparando com voyager-processos')

    def handle(self, *a, **o):
        siglas = self._siglas(o)
        filtro = {'term': {'classe.codigo': o['classe']}} if o['classe'] else None

        for sigla in siglas:
            if o['marcar_acervo']:
                self.stdout.write(json.dumps(marcar_no_acervo(sigla), ensure_ascii=False))
                continue

            self.stdout.write(self.style.MIGRATE_HEADING(f'\n▶ {sigla}'))
            if o['dry_run']:
                # dry-run de VERDADE: não escreve no índice e não salva
                # watermark. Antes disto ele gravava — e "medir sem escrever"
                # que escreve é a mesma armadilha do run verde: o operador
                # acredita que só mediu.
                v = Varredura(sigla, escrever=False,
                              parar=lambda: deve_parar(sigla))
                resumo = v.rodar(cursor=None if o['do_zero'] else self._cursor(sigla),
                                 max_paginas=o['max_paginas'], filtro=filtro)
            else:
                resumo = varrer_tribunal(sigla, retomar=not o['do_zero'],
                                         max_paginas=o['max_paginas'], filtro=filtro)

            verbo = 'caberiam' if o['dry_run'] else 'gravados'
            self.stdout.write(
                f"  lidos {resumo['lidos']:,} · {verbo} {resumo['gravados']:,} · "
                f"{resumo['docs_por_s']:,}/s · {resumo['segundos']}s · "
                f"{resumo['requisicoes']:,} req · "
                f"{(resumo.get('bytes_por_doc') or 0):.0f} B/doc · "
                f"página final {resumo.get('pagina_final') or '—'} · "
                f"parou por: {resumo['parou_por']}")
            if resumo['perdidos']:
                self.stdout.write(self.style.ERROR(
                    f"  ⚠ {resumo['perdidos']:,} docs não couberam no fatiamento "
                    f"de milissegundo — a varredura NÃO está completa"))
            if resumo.get('restante_declarado'):
                # teto é ERRO com número, nunca `return` discreto (regra nº 2)
                self.stdout.write(self.style.ERROR(
                    f"  ⛔ parou por {resumo['parou_por']} com "
                    f"{resumo['restante_declarado']:,} docs AINDA NA FONTE depois "
                    f"do cursor {resumo['cursor']}"))
            elif resumo['parou_por'] in ('max_paginas', 'pausado', 'sem_sort'):
                self.stdout.write(self.style.ERROR(
                    f"  ⛔ parou por {resumo['parou_por']} e NÃO foi possível medir "
                    f"o que ficou de fora — trate como incompleto"))
            if resumo.get('erros'):
                self.stdout.write(self.style.WARNING(
                    '  erros: ' + ' '.join(f'{k}×{v}'
                                           for k, v in sorted(resumo['erros'].items()))))

    def _siglas(self, o):
        if o['todos']:
            return list(Tribunal.objects.filter(ativo=True)
                        .order_by('sigla').values_list('sigla', flat=True))
        if not o['sigla']:
            raise CommandError('informe a sigla ou use --todos')
        return [o['sigla'].upper()]

    def _cursor(self, sigla):
        t = Tribunal.objects.filter(sigla=sigla).first()
        return t.datajud_varredura_cursor if t else None
