"""O que fazer com os números que a busca descobriu.

Regra nº 1 do Voyager: o produto é o acervo. Um processo que a busca achou e
que ninguém trouxe para dentro vale zero — e a mesma pergunta, amanhã, custaria
o mesmo scraping. Então tudo que a busca encontra é ingerido: `hidratar_cnj`
cria o `Process`, puxa os movimentos do Datajud e enfileira o enricher do
tribunal, que traz partes, valor e o resto. A segunda busca igual sai do índice,
de graça.

Duas cautelas, ambas medidas:

- **quem já está no acervo não é re-hidratado**: uma consulta de CPF que devolve
  250 processos conhecidos viraria 250 requisições ao Datajud para não mudar
  nada;
- **a hidratação vai para fila própria, nunca em linha**. `hidratar_cnj` faz uma
  requisição ao Datajud por processo, e o bucket de rate limit do CNJ é global
  e compartilhado com a varredura do acervo. Segurar isso dentro do job de
  busca faria a tela esperar pelo pacing da API do CNJ.
"""
from __future__ import annotations

import logging

logger = logging.getLogger('voyager.busca.ingestao')

#: Teto de processos trazidos para o acervo por busca. Não é economia de disco:
#: é o freio para uma consulta ampla (um advogado com 823 processos, um CNPJ com
#: o teto de 1.000) não virar mil jobs de hidratação de uma vez. Atingir o teto
#: é ERRO registrado no run, com o número real — nunca um corte mudo.
TETO_INGESTAO = 500


def separar_novos(numeros: list[str]) -> tuple[list[str], list[str]]:
    """`(ja_no_acervo, novos)` — uma consulta só, não uma por número."""
    from tribunals.models import Process

    numeros = [n for n in dict.fromkeys(numeros) if n]
    if not numeros:
        return [], []
    conhecidos = set(
        Process.objects.filter(numero_cnj__in=numeros)
        .values_list('numero_cnj', flat=True))
    return ([n for n in numeros if n in conhecidos],
            [n for n in numeros if n not in conhecidos])


def enfileirar(numeros: list[str], ja_ingeridos: int = 0) -> dict:
    """Manda os números novos para a fila de hidratação, respeitando o teto.

    Devolve o que aconteceu, para o run registrar: quantos já existiam, quantos
    foram enfileirados e — o que importa — se algum ficou de fora por teto.
    """
    import django_rq

    from .jobs import hidratar_achado

    ja_tem, novos = separar_novos(numeros)
    cabem = max(0, TETO_INGESTAO - ja_ingeridos)
    enfileirados, fora = novos[:cabem], novos[cabem:]

    fila = django_rq.get_queue('busca_hidratacao')
    for cnj in enfileirados:
        fila.enqueue(hidratar_achado, cnj, job_timeout=300)

    if fora:
        logger.error(
            'busca: teto de ingestão atingido — %d processos achados ficaram '
            'FORA do acervo (teto=%d)', len(fora), TETO_INGESTAO)

    return {
        'ja_no_acervo': len(ja_tem),
        'enfileirados': len(enfileirados),
        'fora_do_teto': len(fora),
    }
