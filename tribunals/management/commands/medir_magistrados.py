"""Mede o extrator de magistrado em amostra ESTRATIFICADA por tribunal.

POR QUE UM COMANDO, E NÃO UM SCRIPT DE UMA VEZ
----------------------------------------------
Porque a taxa envelhece. O formato que a fonte imprime muda sem avisar — foi
assim que o segmentador do DJE ficou cego para dois formatos por 22 edições
seguidas (`.ia/DIARIOS.md` §15) — e uma taxa medida uma vez, em agosto, vira
documentação falsa em setembro. Este comando é a régua, e ela roda de novo.

O QUE ELE MEDE, E POR QUE CADA NÚMERO EXISTE
--------------------------------------------
    publicacoes            o denominador do estrato
    com_marcador           quantas a FONTE marcou (perna A: contado sobre o
                           texto, fora do extrator)
    com_atribuicao         quantas viraram nome
    abstencoes             `com_marcador − com_atribuicao` — marcador impresso
                           que NÃO virou nome. É o número que diz se presta;
                           sem ele, "extraí 100% do que extraí" é verdade e é
                           inútil
    erros{}                por que abstive, discriminado
    verbatim_falhou        gabarito mecânico reprovado — tem que ser ZERO
    verbatim_no_cru        o nome existe tal e qual no `texto` ORIGINAL?
                           Menos que 100% é ESPERADO (o TJGO publica o corpo
                           HTML-escapado e `RENATO C&Eacute;SAR` só vira
                           `RENATO CÉSAR` depois de `limpar`) — o número sai
                           publicado em vez de virar surpresa

O DESENHO DA AMOSTRA — e o viés que ele JÁ produziu, medido
-----------------------------------------------------------
Estratifica por (tribunal × dia), com dias fixos espalhados por ~2 anos.
`ORDER BY random()` está fora de questão: `tribunals_movimentacao` tem 1,4
bilhão de linhas e nenhum índice que sirva — seria varredura, e esta casa já
derrubou o site com medição sem teto (regra nº 7).

⚠️ **Ler as N PRIMEIRAS de cada dia não é amostra — é o primeiro lote de
ingestão daquele dia.** Medido em 03/09/2026, no MESMO tribunal e no mesmo
período:

    TJSP, cabeça do dia (1.200 publicações) .......  1,1% com marcador
    TJSP, sorteio de verdade no ES (600) .......... 14,8% com marcador

Treze vezes. O DJEN devolve a página agrupada por órgão, então a cabeça do dia
é um órgão só, e o formato do texto é propriedade do órgão. Por isso o comando
salta um `--salto` pseudoaleatório (determinístico por `(tribunal, dia)`, para
a régua continuar comparável entre execuções) antes de ler. O custo do salto
foi medido em produção: 0,05 s a 12,8 s por consulta, contra 0,04-0,36 s sem
salto — é caro, e é o preço de não medir o lote errado com três casas decimais.

    manage.py medir_magistrados --por-dia 40 --json > /tmp/magistrados.json
    manage.py medir_magistrados --tribunais TJSP,TJCE --salto 0   # cabeça do dia
"""
from __future__ import annotations

import datetime as dt
import json
import zlib

from django.core.management.base import BaseCommand
from django.utils import timezone

from tribunals.models import Movimentacao, Tribunal
from tribunals.services import magistrados as mag

#: Dias fixos, espalhados. Fixos porque a régua tem que ser COMPARÁVEL entre
#: execuções: mudar a amostra a cada rodada transforma "a taxa caiu" em "a
#: amostra mudou", e não há como separar as duas coisas depois.
DIAS_PADRAO = [
    dt.date(2024, 10, 9), dt.date(2024, 12, 4),
    dt.date(2025, 2, 12), dt.date(2025, 4, 16), dt.date(2025, 6, 11),
    dt.date(2025, 8, 13), dt.date(2025, 10, 15), dt.date(2025, 12, 3),
    dt.date(2026, 2, 11), dt.date(2026, 4, 8), dt.date(2026, 6, 10),
    dt.date(2026, 8, 12),
]


class Command(BaseCommand):
    help = 'Mede taxa de extração e de ABSTENÇÃO do extrator de magistrado.'

    def add_arguments(self, parser):
        parser.add_argument('--tribunais', default='',
                            help='siglas separadas por vírgula (default: ativos)')
        parser.add_argument('--por-dia', type=int, default=40)
        parser.add_argument('--salto', type=int, default=4000,
                            help='teto do salto pseudoaleatório dentro do dia. '
                                 '0 = ler a cabeça do dia (mede o lote, não o dia)')
        parser.add_argument('--exemplos', type=int, default=6,
                            help='nomes extraídos a mostrar por tribunal')
        parser.add_argument('--json', action='store_true')

    def handle(self, *args, **opts):
        siglas = [s.strip().upper() for s in opts['tribunais'].split(',') if s.strip()]
        if not siglas:
            siglas = list(Tribunal.objects.filter(ativo=True)
                          .order_by('sigla').values_list('sigla', flat=True))
        por_dia = opts['por_dia']
        relatorio = {
            'medido_em': timezone.now().isoformat(),
            'dias': [d.isoformat() for d in DIAS_PADRAO],
            'por_dia': por_dia,
            'salto_max': opts['salto'],
            'tribunais': {},
        }

        for sigla in siglas:
            est = self._medir(sigla, por_dia, opts['exemplos'], opts['salto'])
            relatorio['tribunais'][sigla] = est
            if not opts['json']:
                self._imprimir(sigla, est)

        relatorio['pais'] = self._totalizar(relatorio['tribunais'])
        if opts['json']:
            self.stdout.write(json.dumps(relatorio, ensure_ascii=False, indent=1))
        else:
            self._imprimir('PAÍS', relatorio['pais'])

    # ------------------------------------------------------------------ #
    def _medir(self, sigla: str, por_dia: int, n_exemplos: int,
               salto_max: int = 0) -> dict:
        pub = com_marc = com_atr = verb_falhou = verb_cru = atrib = 0
        erros: dict[str, int] = {}
        marcadores: dict[str, int] = {}
        formatos: dict[str, int] = {}
        exemplos: list[dict] = []

        for dia in DIAS_PADRAO:
            qs = (Movimentacao.objects
                  .filter(tribunal_id=sigla,
                          data_disponibilizacao__gte=dia,
                          data_disponibilizacao__lt=dia + dt.timedelta(days=1))
                  .values_list('texto', 'nome_orgao'))
            salto = _salto(sigla, dia, salto_max)
            lidos = list(qs[salto:salto + por_dia])
            if not lidos and salto:
                lidos = list(qs[:por_dia])   # dia menor que o salto: lê o que tem
            for texto, orgao in lidos:
                pub += 1
                leitura = mag.ler(texto)
                limpo = mag.limpar(texto)
                for k, v in leitura.marcadores_vistos.items():
                    marcadores[k] = marcadores.get(k, 0) + v
                for k, v in leitura.erros.items():
                    erros[k] = erros.get(k, 0) + v
                if leitura.marcadores_vistos:
                    com_marc += 1
                if leitura.atribuicoes:
                    com_atr += 1
                    atrib += len(leitura.atribuicoes)
                for a in leitura.atribuicoes:
                    formatos[a.formato] = formatos.get(a.formato, 0) + 1
                    if not a.verbatim_ok(limpo):
                        verb_falhou += 1
                    if a.nome in (texto or ''):
                        verb_cru += 1
                    if len(exemplos) < n_exemplos:
                        exemplos.append({'nome': a.nome, 'formato': a.formato,
                                         'cargo': a.cargo, 'orgao': orgao})

        return {
            'publicacoes': pub,
            'com_marcador': com_marc,
            'com_atribuicao': com_atr,
            # marcador impresso que NÃO virou nome — a taxa que importa
            'abstencoes': max(0, com_marc - com_atr),
            'atribuicoes': atrib,
            'pct_do_estrato': _pct(com_atr, pub),
            'pct_do_marcado': _pct(com_atr, com_marc),
            'pct_abstencao': _pct(max(0, com_marc - com_atr), com_marc),
            'verbatim_falhou': verb_falhou,
            'verbatim_no_cru': verb_cru,
            'pct_verbatim_no_cru': _pct(verb_cru, atrib),
            'marcadores_impressos': marcadores,
            'formatos': formatos,
            'erros': erros,
            'exemplos': exemplos,
        }

    def _totalizar(self, por_trib: dict) -> dict:
        soma = {'publicacoes': 0, 'com_marcador': 0, 'com_atribuicao': 0,
                'atribuicoes': 0, 'verbatim_falhou': 0, 'verbatim_no_cru': 0}
        erros: dict[str, int] = {}
        formatos: dict[str, int] = {}
        for est in por_trib.values():
            for k in soma:
                soma[k] += est.get(k, 0)
            for k, v in est['erros'].items():
                erros[k] = erros.get(k, 0) + v
            for k, v in est['formatos'].items():
                formatos[k] = formatos.get(k, 0) + v
        soma['abstencoes'] = max(0, soma['com_marcador'] - soma['com_atribuicao'])
        soma['pct_do_estrato'] = _pct(soma['com_atribuicao'], soma['publicacoes'])
        soma['pct_do_marcado'] = _pct(soma['com_atribuicao'], soma['com_marcador'])
        soma['pct_abstencao'] = _pct(soma['abstencoes'], soma['com_marcador'])
        soma['pct_verbatim_no_cru'] = _pct(soma['verbatim_no_cru'], soma['atribuicoes'])
        soma['erros'] = erros
        soma['formatos'] = formatos
        soma['exemplos'] = []
        soma['marcadores_impressos'] = {}
        return soma

    def _imprimir(self, sigla: str, e: dict) -> None:
        self.stdout.write(
            f"{sigla:8s} pub={e['publicacoes']:>6,} marcador={e['com_marcador']:>6,} "
            f"nome={e['com_atribuicao']:>6,} ({e['pct_do_marcado']:>5.1f}% do marcado) "
            f"abstencao={e['abstencoes']:>5,} ({e['pct_abstencao']:>5.1f}%) "
            f"verbatim_falhou={e['verbatim_falhou']}")
        if e.get('erros'):
            top = sorted(e['erros'].items(), key=lambda kv: -kv[1])[:6]
            self.stdout.write('           erros: ' + ', '.join(f'{k}={v}' for k, v in top))
        for ex in e.get('exemplos', [])[:4]:
            self.stdout.write(f"           · {ex['nome']}  [{ex['formato']}] {ex['orgao'][:48]}")


def _salto(sigla: str, dia: dt.date, teto: int) -> int:
    """Salto DETERMINÍSTICO dentro do dia — sorteio reprodutível.

    Determinístico de propósito: régua que muda de amostra a cada execução
    transforma "a taxa caiu" em "a amostra mudou", e não há como separar as
    duas coisas depois.
    """
    if teto <= 0:
        return 0
    semente = zlib.crc32(f'{sigla}|{dia.isoformat()}'.encode())
    return semente % teto


def _pct(parte: int, todo: int) -> float:
    return round(100.0 * parte / todo, 2) if todo else 0.0
