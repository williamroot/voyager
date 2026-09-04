"""Leitura das APIs REST próprias (TJMT e TJPA). Puro: dict entra, dado sai.

São as duas melhores fontes da matriz — as únicas com total real e paginação de
verdade — e as duas com a mesma armadilha em espelho:

    o TJMT responde 200 com a BASE INTEIRA quando o parâmetro não é o que ele
    conhece (`documento=`, `cpfCnpj=` e `nomeCpfCnpj=` devolveram os mesmos
    11.672.774 registros do baseline sem filtro), e o TJPA responde 405 quando
    a rota não é a que ele conhece.

O 405 é barulhento e se descobre na primeira tentativa; o 200 com a base
inteira é silencioso e traria processos aleatórios como se fossem "os processos
deste CPF". Daí `parece_base_inteira` existir.
"""
from __future__ import annotations

import re

from .base import ItemEncontrado, PaginaResultado


def formatar_cnj(digitos: str) -> str:
    """20 dígitos -> NNNNNNN-DD.AAAA.J.TR.OOOO. Devolve o original se não der."""
    d = re.sub(r'\D', '', digitos or '')
    if len(d) != 20:
        return digitos or ''
    return f'{d[0:7]}-{d[7:9]}.{d[9:13]}.{d[13]}.{d[14:16]}.{d[16:20]}'


# ── TJMT ──────────────────────────────────────────────────────────────────────

def _nome_da_classe(valor) -> str:
    """A classe do TJMT vem como objeto; nas demais fontes, como texto."""
    if isinstance(valor, dict):
        return str(valor.get('nome') or '')
    return str(valor or '')


def parse_tjmt(corpo: dict, pagina: int = 1) -> PaginaResultado:
    """`{pagina, totalRegistros, itens:[...]}` do `ProcessosJudiciais/v2`.

    O CNJ vem em `numeroUnico` (20 dígitos crus). `partes` vem RICO nesta
    listagem — nome, tipo, filiação — e é justamente o que NÃO usamos: quem
    cria parte é o enricher, com a ficha do processo. Aqui só o nome dos polos
    sobe, para a tela ter o que mostrar antes do enriquecimento.
    """
    itens = []
    for bruto in (corpo.get('itens') or []):
        numero = formatar_cnj(str(bruto.get('numeroUnico') or ''))
        if not numero:
            continue
        polos = tuple(p for p in (bruto.get('nomePartePoloAtivo') or '',
                                  bruto.get('nomePartePoloPassivo') or '') if p)
        itens.append(ItemEncontrado(
            numero_cnj=numero,
            tribunal='TJMT',
            # `classe` aqui é OBJETO ({idClasse, codigo, nome, hierarquia}), e
            # não string como nas outras fontes. `str()` nele produziria a
            # repr do dict dentro do campo — o tipo de sujeira que só aparece
            # na tela, semanas depois.
            classe=_nome_da_classe(bruto.get('classe')),
            orgao=str(bruto.get('orgaoJulgador') or bruto.get('jurisdicao') or ''),
            distribuicao=str((bruto.get('ultimoEvento') or {}).get('descricao') or ''),
            url_fonte=str((bruto.get('links') or {}).get('detalhe') or ''),
            partes_na_lista=polos,
        ))
    total = corpo.get('totalRegistros')
    return PaginaResultado(
        itens=itens,
        pagina=pagina,
        total_declarado=int(total) if isinstance(total, int) else None,
        total_e_teto=False,
        tem_proxima=bool(total and pagina * max(len(itens), 1) < total and itens),
    )


def parece_base_inteira(total_com_filtro: int | None,
                        total_sem_filtro: int | None) -> bool:
    """O filtro foi ignorado?

    Teste de sanidade obrigatório do TJMT: uma busca COM filtro nunca pode
    devolver o mesmo total da busca SEM filtro. Se devolver, o parâmetro não
    foi entendido e o 200 é uma mentira educada.
    """
    return (total_com_filtro is not None and total_sem_filtro is not None
            and total_com_filtro == total_sem_filtro and total_sem_filtro > 0)


# ── TJPA ──────────────────────────────────────────────────────────────────────

def parse_tjpa(corpo: dict, pagina: int = 1, por_pagina: int = 20) -> PaginaResultado:
    """`{qtdRegistrosTotal, listaResultado:[{listaProcessos:[...]}]}`.

    A resposta é aninhada em dois níveis porque o `consilium-rest` agrupa por
    número: `listaResultado` traz o processo e `listaProcessos` as instâncias
    dele (1º e 2º grau vêm como entradas distintas). Achatar aqui é o certo —
    cada instância é um processo no nosso acervo.
    """
    itens = []
    for grupo in (corpo.get('listaResultado') or []):
        instancias = grupo.get('listaProcessos') or [grupo]
        for bruto in instancias:
            numero = (bruto.get('numeroFormatado')
                      or formatar_cnj(str(bruto.get('numero') or '')))
            if not numero:
                continue
            itens.append(ItemEncontrado(
                numero_cnj=numero,
                tribunal='TJPA',
                classe=str(bruto.get('classe') or ''),
                assunto=str(bruto.get('assunto') or '').replace('Não informado', ''),
                orgao=' - '.join(x for x in (bruto.get('comarca'), bruto.get('vara')) if x),
                distribuicao=str(bruto.get('dataDistribuicaoFormatada') or ''),
            ))
    total = corpo.get('qtdRegistrosTotal')
    total = int(total) if isinstance(total, int) else None
    return PaginaResultado(
        itens=itens,
        pagina=pagina,
        total_declarado=total,
        total_e_teto=False,
        tem_proxima=bool(total and pagina * por_pagina < total),
    )


def parse_tjpa_nomes(corpo: list) -> list[dict]:
    """`processobynomeparte` não devolve processo: devolve NOMES.

    `[{nome, quantidade, sistema}]` — as grafias reais que casam com o que foi
    digitado, cada uma com quantos processos tem. É um desambiguador, e é a
    resposta certa para "MARIA JOSE DOS SANTOS": em vez de escolher por conta
    própria entre "ESPOLIO DE MARIA JOSE DOS SANTOS MARTINS" e "MARIA JOSE DOS
    SANTOS SILVA", devolve as duas e deixa quem perguntou decidir.
    """
    nomes = []
    for bruto in (corpo or []):
        nome = str(bruto.get('nome') or '').strip()
        if not nome:
            continue
        try:
            quantidade = int(str(bruto.get('quantidade') or 0))
        except ValueError:
            quantidade = 0
        nomes.append({'nome': nome, 'quantidade': quantidade,
                      'sistema': str(bruto.get('sistema') or '')})
    return sorted(nomes, key=lambda n: -n['quantidade'])
