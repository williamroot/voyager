"""Resolvedor de CNJ do STF — o elo que decide se a publicação vira dado ou lixo.

O PROBLEMA
==========
O payload da API do STF identifica o processo pelo número NATIVO do tribunal
(`"processo": "ARE 1617690"`), e não pelo CNJ. Sem CNJ não há `Process`, e sem
`Process` não há `Movimentacao` — a publicação entraria órfã. Medido em 200
itens reais: só 8,5% trazem algum CNJ solto dentro do `texto`, e esses são
AMBÍGUOS (um HC citava três CNJs diferentes: o de origem, o do TJ e o do
tribunal militar). Extrair CNJ do texto seria exatamente o "chutar" que a casa
proíbe — então não fazemos isso em lugar nenhum deste módulo.

A CURA, E POR QUE ELA É BARATA
==============================
O campo `processoId` do JSON **é** o `incidente` do portal legado: para
`ARE 1617690` o JSON traz `processoId: 7661810` e
`portal.stf.jus.br/processos/listarProcessos.asp?classe=ARE&numeroProcesso=1617690`
redireciona (302) para `detalhe.asp?incidente=7661810`. Logo dá para ir DIRETO
ao detalhe: um GET por processo, sem redirect e sem parsear classe/número.

A página traz `<div class="processo-rotulo">Número Único: 0000876-17.2013.8.16.0021</div>`
— e é o STF dizendo qual é o CNJ, não nós adivinhando.

MEDIDO (amostra de 40 processos de um dia real, 16/08/2026)
-----------------------------------------------------------
  · 36/40 (90%) devolvem CNJ;
  · 4/40 devolvem literalmente **"Sem número único"** — processos autuados no
    próprio STF antes da numeração unificada (Pet 11841, Rcl 90781). Para esses
    a resposta honesta é ABSTER: a publicação não é gravada.
  · incidente inexistente responde **HTTP 200 com 33.461 bytes** da casca do
    portal, sem `processo-titulo` e sem `processo-rotulo` — o clássico "200 que
    não é dado", que aqui vira `RespostaInvalida`.

DUAS ARMADILHAS QUE CUSTARAM TEMPO
----------------------------------
1. **O portal MENTE o charset**: manda `Content-Type: text/html` sem charset e o
   `requests` cai no ISO-8859-1 do RFC, mas os bytes são UTF-8 — 'Número Único'
   vira 'NÃºmero Ãnico' e qualquer regex com acento falha em silêncio (a
   primeira versão deste resolvedor mediu 0/40 por causa disso). Forçamos
   `encoding='utf-8'` e, por cima, as regexes NÃO dependem de acento: ancoram na
   classe CSS e procuram o CNJ dentro do bloco.
2. **User-Agent curto = 403**: o IIS/ASP recusa `Mozilla/5.0` cru.

CACHE
=====
`(incidente) → CNJ` é imutável na prática, então o acerto é cacheado **para
sempre**. A ausência ('Sem número único'), não: um processo pode receber número
único depois, então o negativo expira em 30 dias. Sem cache o custo seria ~590
GETs/dia num IIS legado e lento; com cache, some depois do primeiro mês.

Limitação honesta: o cache é o Redis do `django.core.cache`. Um flush obriga a
re-resolver. Não usei tabela própria porque `diarios/models.py` é arquivo
compartilhado (contrato do general) e este resolvedor não justifica migration.
"""

import logging
import re
from dataclasses import dataclass

from django.core.cache import cache

from diarios.base import RespostaInvalida, SessaoDiario

from .api import HEADERS_IDENTIFICACAO

logger = logging.getLogger('voyager.diarios.stf.cnj')

URL_DETALHE = 'https://portal.stf.jus.br/processos/detalhe.asp?incidente={incidente}'

#: Ancoradas em classe CSS, não em texto acentuado (ver docstring, armadilha 1).
RE_ROTULO = re.compile(r'class="processo-rotulo"[^>]*>(.*?)</div>', re.S)
RE_TITULO = re.compile(r'class="processo-titulo[^"]*"[^>]*>(.*?)(?:<div|</div>)', re.S)
RE_CLASSE = re.compile(r'class="processo-classe[^"]*"[^>]*>(.*?)</div>', re.S)
RE_CNJ = re.compile(r'\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}')

PREFIXO_CACHE = 'diarios:stf:incidente:'
#: Positivo é imutável (o número único de um processo não muda).
TTL_POSITIVO = None
#: Negativo NÃO é permanente: 'Sem número único' pode virar número depois da
#: autuação. 30 dias é o meio-termo entre re-perguntar à toa e ficar órfão.
TTL_NEGATIVO = 60 * 60 * 24 * 30


@dataclass(frozen=True)
class ProcessoSTF:
    """O que o portal sabe sobre o processo. `cnj=None` = o STF diz que não há."""
    incidente: int
    cnj: str | None
    classe: str = ''
    titulo: str = ''


class ResolvedorCNJ:
    """incidente → CNJ, com cache e abstenção explícita.

    Recebe a `SessaoDiario` de fora porque ela carrega o circuit-breaker: o
    portal legado é frágil e NÃO pode compartilhar breaker com a API (uma queda
    do IIS não pode calar o coletor inteiro). Ver `ColetorSTF.__init__`.
    """

    def __init__(self, sessao: SessaoDiario):
        self.sessao = sessao
        self.acertos_cache = 0
        self.consultas = 0
        self.sem_numero_unico = 0

    def resolver(self, incidente: int | None) -> ProcessoSTF | None:
        """Devolve o processo, ou None quando nem dá para perguntar.

        `processoId` vem `null` em uma fração pequena das publicações (1 em 200
        na amostra) — sem ele não há o que consultar, e inventar não é opção.
        """
        if not incidente:
            return None
        chave = f'{PREFIXO_CACHE}{incidente}'
        em_cache = cache.get(chave)
        if em_cache is not None:
            self.acertos_cache += 1
            proc = ProcessoSTF(incidente=incidente, **em_cache)
        else:
            proc = self._consultar(incidente)
            cache.set(
                chave,
                {'cnj': proc.cnj, 'classe': proc.classe, 'titulo': proc.titulo},
                timeout=TTL_POSITIVO if proc.cnj else TTL_NEGATIVO,
            )
        if proc.cnj is None:
            self.sem_numero_unico += 1
        return proc

    def _consultar(self, incidente: int) -> ProcessoSTF:
        self.consultas += 1
        resp = self.sessao.get(URL_DETALHE.format(incidente=incidente),
                               headers=HEADERS_IDENTIFICACAO)
        # O servidor declara ISO-8859-1 e serve UTF-8. Ver armadilha 1.
        resp.encoding = 'utf-8'
        return self.parsear(incidente, resp.text)

    @staticmethod
    def parsear(incidente: int, html: str) -> ProcessoSTF:
        """Separa três desfechos que HTTP 200 não distingue.

        · página de processo com número único  → CNJ;
        · página de processo sem número único  → `cnj=None` (abstenção legítima);
        · casca do portal (incidente que não existe) → `RespostaInvalida`.
        """
        rotulo = RE_ROTULO.search(html or '')
        titulo = RE_TITULO.search(html or '')
        if rotulo is None and titulo is None:
            raise RespostaInvalida(
                f'STF portal: incidente {incidente} devolveu a casca do site '
                f'({len(html or "")} bytes, sem processo-rotulo/processo-titulo)'
            )
        texto_rotulo = _limpar(rotulo.group(1) if rotulo else '')
        achado = RE_CNJ.search(texto_rotulo)
        m_classe = RE_CLASSE.search(html or '')
        classe = _limpar(m_classe.group(1)) if m_classe else ''
        return ProcessoSTF(
            incidente=incidente,
            cnj=achado.group(0) if achado else None,
            classe=classe[:255],
            titulo=_limpar(titulo.group(1) if titulo else '')[:120],
        )


def _limpar(fragmento: str) -> str:
    """Tira tags, entidades de espaço e quebra de linha do pedaço de HTML."""
    sem_tag = re.sub(r'<[^>]*>', ' ', fragmento or '')
    return re.sub(r'\s+', ' ', sem_tag.replace('&nbsp;', ' ')).strip()
