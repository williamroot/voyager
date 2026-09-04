"""Que tribunal aceita que critério — o catálogo, com o que foi MEDIDO.

Um mapa de sigla para classe seria metade da verdade. O que a API precisa
responder é "o TJPA não busca por nome de advogado" e "o PJe devolve no máximo
30 e não pagina", e isso são fatos por (tribunal, motor), medidos no recon de
04/09/2026 e guardados aqui junto com a data da medição.

`verificado_em = None` significa NÃO MEDIDO, e é o caso do TRF3: o host inteiro
recusa conexão a partir de IP residencial, então o recon dele tem de rodar do
container. O motor é o mesmo dos outros quatro PJe e por isso ele fica
habilitado — mas o catálogo diz que ninguém conferiu, em vez de fingir que sim.
"""
from __future__ import annotations

from dataclasses import dataclass

from .base import CRITERIOS, BuscaPorParte, CriterioIndisponivel

# Escopo: os nove tribunais que o JURISCOPE opera (`VOYAGER_TRIBUNAIS`), todos
# com enricher aqui. Tribunal sem enricher não entra — a busca reusa a malha
# de coleta do enricher, e sem ela não há por onde sair.
ESAJ = ('TJSP', 'TJAL')
PJE = ('TRF1', 'TRF3', 'TRF5', 'TJMG', 'TJMA')
REST = ('TJPA', 'TJMT')

TRIBUNAIS = ESAJ + PJE + REST


@dataclass(frozen=True)
class Fonte:
    tribunal: str
    motor: str
    criterios: frozenset[str]
    teto_da_fonte: int | None
    pagina: bool
    #: Data do recon que exercitou esta fonte. `None` = não medido.
    verificado_em: str | None
    #: Quais critérios foram de fato EXERCITADOS ao vivo neste tribunal. O
    #: motor oferece os quatro em toda instalação PJe/e-SAJ, mas oferecer não é
    #: ter medido: no TRF1 só documento e nome foram exercidos. A diferença
    #: aparece na resposta como aviso, em vez de virar confiança falsa.
    criterios_medidos: frozenset[str] = frozenset()
    #: O que quem consome precisa saber antes de interpretar o resultado.
    nota: str = ''


_MEDIDO = '2026-09-04'

CATALOGO: dict[str, Fonte] = {
    'TJSP': Fonte('TJSP', 'esaj', frozenset(CRITERIOS), 1000, True, _MEDIDO, frozenset(CRITERIOS),
                  'a fonte trava o contador em 1.000; acima disso não é '
                  'alcançável por este critério'),
    'TJAL': Fonte('TJAL', 'esaj', frozenset(CRITERIOS), 1000, True, _MEDIDO, frozenset(CRITERIOS),
                  'mesmo software do TJSP; o teto de 1.000 não foi exercido aqui'),
    'TRF1': Fonte('TRF1', 'pje', frozenset(CRITERIOS), 30, False, _MEDIDO, frozenset(CRITERIOS),
                  'a consulta pública devolve no máximo 30 e não pagina'),
    'TRF3': Fonte('TRF3', 'pje', frozenset(CRITERIOS), 30, False, None, frozenset(),
                  'não medido: o host recusa conexão fora da malha de proxies'),
    # O TRF5 CONTA certo e mostra UMA linha: seis buscas, rodapés 30/30/16/13/
    # zero/30, sempre uma linha na tabela. Não é o nosso cliente que trunca —
    # a resposta não contém as outras. Toda busca aqui sai `truncado`.
    'TRF5': Fonte('TRF5', 'pje', frozenset(CRITERIOS), 30, False, _MEDIDO, frozenset(CRITERIOS),
                  'a fonte conta certo mas renderiza só o primeiro resultado: '
                  'a busca por parte alcança 1 processo por consulta'),
    'TJMG': Fonte('TJMG', 'pje', frozenset(CRITERIOS), 30, False, _MEDIDO, frozenset(CRITERIOS),
                  'a consulta pública devolve no máximo 30 e não pagina'),
    'TJMA': Fonte('TJMA', 'pje', frozenset(CRITERIOS), 30, False, _MEDIDO, frozenset(CRITERIOS),
                  'a consulta pública devolve no máximo 30 e não pagina'),
    # `oab` fica FORA de `criterios_medidos`: a rota existe no bundle, mas a
    # única OAB que consegui testar devolveu 204 (sem conteúdo), e 204 não
    # distingue "advogado sem processo" de "rota que não funciona". O TJPA não
    # expõe OAB nas partes, então não há de onde colher uma real.
    'TJPA': Fonte('TJPA', 'rest', frozenset({'documento', 'nome', 'oab'}), None, True,
                  _MEDIDO, frozenset({'documento', 'nome'}),
                  'páginas contam a partir de 1 e a fonte não sinaliza fim: '
                  'paginamos até não vir processo novo. Busca por nome exige a '
                  'grafia exata — use a desambiguação de nomes antes'),
    'TJMT': Fonte('TJMT', 'rest', frozenset(CRITERIOS), None, True, _MEDIDO, frozenset(CRITERIOS),
                  'total real e paginação (Take até 60; 75 dá HTTP 422); filtro '
                  'desconhecido é ignorado pela API, então toda busca confere o '
                  'total contra o baseline'),
}


def _fabricar(sigla: str) -> BuscaPorParte:
    """Instancia o buscador do tribunal. Import tardio: `enrichers.jobs` importa
    os enrichers todos, e puxar isso no topo criaria ciclo com `enrichers/`."""
    from enrichers.esaj import TjalEnricher, TjspEnricher
    from enrichers.tjma import TjmaEnricher
    from enrichers.tjmg import TjmgEnricher
    from enrichers.tjmt import TjmtEnricher
    from enrichers.tjpa import TjpaEnricher
    from enrichers.trf1 import Trf1Enricher
    from enrichers.trf3 import Trf3Enricher
    from enrichers.trf5 import Trf5Enricher

    from .esaj import BuscaEsaj
    from .pje import BuscaPje
    from .rest import BuscaTjmt, BuscaTjpa

    enrichers = {
        'TJSP': TjspEnricher, 'TJAL': TjalEnricher,
        'TRF1': Trf1Enricher, 'TRF3': Trf3Enricher, 'TRF5': Trf5Enricher,
        'TJMG': TjmgEnricher, 'TJMA': TjmaEnricher,
        'TJPA': TjpaEnricher, 'TJMT': TjmtEnricher,
    }
    motores = {'esaj': BuscaEsaj, 'pje': BuscaPje}

    fonte = CATALOGO[sigla]
    classe = ((BuscaTjmt if sigla == 'TJMT' else BuscaTjpa)
              if fonte.motor == 'rest' else motores[fonte.motor])
    return classe(enrichers[sigla])


class TribunalSemBusca(Exception):
    """A busca por parte não existe neste tribunal (ou ele não está no escopo)."""


def buscador(sigla: str) -> BuscaPorParte:
    sigla = (sigla or '').upper()
    if sigla not in CATALOGO:
        raise TribunalSemBusca(
            f'{sigla or "(vazio)"} não tem busca por parte no Voyager. '
            f'Disponíveis: {", ".join(sorted(CATALOGO))}')
    return _fabricar(sigla)


def aceita(sigla: str, criterio: str) -> bool:
    fonte = CATALOGO.get((sigla or '').upper())
    return bool(fonte and criterio in fonte.criterios)


def exigir(sigla: str, criterio: str) -> None:
    if not aceita(sigla, criterio):
        raise CriterioIndisponivel(sigla, criterio)


def tribunais_com(criterio: str) -> list[str]:
    """Quem aceita este critério, em ordem estável.

    Quem chama usa isto para RECUSAR explicitamente os outros na resposta, em
    vez de buscar neles e devolver zero — que é a diferença entre "o TJPA não
    busca por nome de advogado" e "não achei nada no TJPA".
    """
    return sorted(s for s, f in CATALOGO.items() if criterio in f.criterios)


def foi_medido(sigla: str, criterio: str) -> bool:
    """Este critério já foi exercitado ao vivo NESTE tribunal?

    Serve ao aviso da resposta: uma busca por OAB no TRF1 usa o mesmo motor que
    funciona no TJMG, mas ninguém a rodou contra o TRF1 — e "0 resultados" numa
    fonte nunca exercitada não é um fato sobre a pessoa buscada.
    """
    fonte = CATALOGO.get((sigla or '').upper())
    return bool(fonte and criterio in fonte.criterios_medidos)


def catalogo_publico() -> list[dict]:
    """O catálogo como a API o expõe — inclusive o que não foi medido."""
    return [{
        'tribunal': f.tribunal,
        'motor': f.motor,
        'criterios': sorted(f.criterios),
        'teto_da_fonte': f.teto_da_fonte,
        'pagina': f.pagina,
        'verificado_em': f.verificado_em,
        'criterios_medidos': sorted(f.criterios_medidos),
        'nota': f.nota,
    } for f in sorted(CATALOGO.values(), key=lambda x: x.tribunal)]
