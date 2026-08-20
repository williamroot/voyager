"""O `ColetorDiario` do DJE/TJSP: catálogo + download + segmentação → `Movimentacao`.

Conduta de rede, e por que ela é auto-imposta: o `dje.tjsp.jus.br` não tem
rate limit, não tem WAF, não tem `robots.txt` (404) e aceita IP de datacenter e
`User-Agent: voyager-ops/1.0` respondendo 200 — 120 requisições com
paralelismo 20 não tomaram um único 403. Ou seja, o servidor NÃO vai nos
defender de nós mesmos, e o teto é nosso: `rps` baixo, paralelismo por caderno
(nunca por página), e o kill switch de `diarios.base.pausar()` para parar em
segundos sem deploy. Não usamos proxy: as sondas de 16/08/2026 mostraram que
não é preciso, e tirar o Cortex do caminho tira um SPOF.

A janela declarada (`2007-10-01 → 2025-03-13`) é medida, não escolhida: o DJEN
devolve `count=0` para o TJSP em toda data até 13/03/2025 e `count>0` a partir
de 14/03/2025. Fora dela o runner recusa a coleta, e é isso que impede que
esta porta e o DJEN gravem o mesmo ato duas vezes.
"""

import logging
from collections.abc import Iterator
from datetime import date, datetime, time

from django.conf import settings
from django.utils import timezone

from diarios.base import (
    PROXY_DIRETO,
    ColetorDiario,
    ColetorError,
    ItemDiario,
    UnidadeColeta,
    UnidadeInexistente,
    UnidadeSemDadoAproveitavel,
    achar_cnjs,
    exigir_pdf,
    fingerprint_ato,
    id_bloco_impresso,
    registrar,
)

from . import catalogo, pdf, segmentador

logger = logging.getLogger('voyager.diarios.tjsp_dje')

#: J=8 (Justiça Estadual), TR=26 (TJSP). O caderno cita processo de outros
#: tribunais (recurso ao STJ, precedente do TRF) e gravar isso como processo do
#: TJSP contaminaria o acervo — o número existe, mas não é DAQUI.
SUFIXO_CNJ_TJSP = '.8.26.'
#: piso de blocos com CNJ abaixo do qual não faz sentido cobrar cobertura: o
#: caderno administrativo e as edições pré-2010 (numeração '583.00.2010.119027')
#: legitimamente quase não têm CNJ.
MINIMO_PARA_AFERIR_COBERTURA = 200


@registrar
class ColetorDjeTjsp(ColetorDiario):
    slug = 'tjsp-dje'
    nome = 'DJE/TJSP (e-SAJ)'
    tribunal_sigla = 'TJSP'

    # Medido (ver docstring do módulo). O fim NÃO é 2025-07-22 (última edição
    # publicada) porque a partir de 14/03/2025 o DJEN cobre o TJSP: no período
    # sobreposto quem manda é o DJEN, que já está ingerido.
    janela_inicio = date(2007, 10, 1)
    janela_fim = date(2025, 3, 13)

    modo_proxy = PROXY_DIRETO
    #: 1 req/s. Um caderno inteiro vem em 0,2-1,2s, então isto não é gargalo de
    #: throughput — é o teto de educação contra um servidor que não se defende.
    rps = 1.0
    #: declarativo: o runner de `diarios/base.py` ainda não lê este campo. Fica
    #: registrado para quando ler — backfill de 283 GB é assunto de madrugada.
    janela_horaria = (0, 7)

    #: caderno 5 (Editais e Leilões). O layout dele NÃO é relação de processos:
    #: são editais em texto corrido, com o número aparecendo como
    #: 'Processo Físico nº: 0009999-35.2014.8.26.0309' repetido dentro do
    #: mesmo edital. Medido no caderno 14 de 15/07/2015: o segmentador desta
    #: fonte cobre 4,7% dos CNJs dele (contra 99,7% no caderno 12 do mesmo dia).
    #: Fica FORA do catálogo até existir segmentador próprio — coletar com 4,7%
    #: de cobertura seria gravar lacuna e chamar de acervo.
    CADERNOS_SEM_SEGMENTADOR = frozenset({14})

    def __init__(self):
        super().__init__()
        self._cadernos_cache: dict[int, str] | None = None

    # ── catálogo ──────────────────────────────────────────────────────────
    def _cadernos(self) -> dict[int, str]:
        """Tabela de cadernos, lida da home a cada execução (1 requisição).

        Ler em vez de fixar porque a lista é da fonte; o `CADERNOS_PADRAO` é
        rede de proteção para o dia em que a home mudar de layout — catálogo
        vazio num backfill é lacuna invisível.
        """
        if self._cadernos_cache is not None:
            return self._cadernos_cache
        tabela: dict[int, str] = {}
        try:
            tabela = catalogo.parse_cadernos(self.sessao.get(catalogo.URL_HOME).text)
        except Exception as exc:
            logger.warning('%s: não consegui ler a tabela de cadernos da home (%s); '
                           'usando a medida em 16/08/2026', self.slug, exc)
        if not tabela:
            tabela = dict(catalogo.CADERNOS_PADRAO)
        self._cadernos_cache = {
            cd: rot for cd, rot in tabela.items() if cd not in self.CADERNOS_SEM_SEGMENTADOR
        }
        return self._cadernos_cache

    def catalogar(self, data_inicio: date, data_fim: date) -> Iterator[UnidadeColeta]:
        """Enumera edição x caderno do período. Duas requisições, sem PDF.

        O índice inteiro (4.162 edições, 2007-10-01 → 2025-07-22) vem numa só
        resposta, então o recorte por período é feito aqui e não na fonte.

        QUAIS cadernos existem em cada data é coisa que só o download responde
        (em 2015 são 7, em 2025 são 9) — por isso todas as combinações entram
        no catálogo e o caderno ausente vira `UnidadeInexistente` na coleta,
        status que nunca mais é retentado. O custo é um GET que devolve 851
        bytes.
        """
        html = self.sessao.get(catalogo.URL_INDICE).text
        edicoes = catalogo.parse_indice(html)
        sem_diario = catalogo.parse_datas_sem_diario(html)
        cadernos = self._cadernos()
        logger.info('%s: catálogo com %d edições (%s → %s), %d cadernos, %d datas sem diário',
                    self.slug, len(edicoes), edicoes[-1].data, edicoes[0].data,
                    len(cadernos), len(sem_diario))

        for edicao in edicoes:
            if not (data_inicio <= edicao.data <= data_fim):
                continue
            if edicao.data in sem_diario:
                # A própria fonte declara que não houve edição nesse dia.
                # Gabarito de graça: economiza 9 downloads de 851 bytes.
                continue
            for cd_caderno, rotulo in sorted(cadernos.items()):
                yield UnidadeColeta(
                    chave=catalogo.chave_unidade(edicao.nu_diario, cd_caderno),
                    data=edicao.data,
                    tribunal_sigla=self.tribunal_sigla,
                    rotulo=f'Edição {edicao.nu_diario} — {rotulo}',
                    meta={
                        'nu_diario': edicao.nu_diario,
                        'cd_volume': edicao.cd_volume,
                        'cd_caderno': cd_caderno,
                        'caderno': rotulo,
                    },
                )

    # ── coleta ────────────────────────────────────────────────────────────
    def _baixar(self, unidade: UnidadeColeta) -> bytes:
        cd_caderno = int(unidade.meta['cd_caderno'])
        resp = self.sessao.get(catalogo.url_download_caderno(unidade.data, cd_caderno))
        corpo = resp.content or b''
        # "200 que não é dado", variante e-SAJ: 851 bytes de HTML dizendo que o
        # caderno não existe naquela data. Isto é AUSÊNCIA, não falha — e é a
        # resposta esperada para os cadernos que só passaram a existir depois.
        parece_pdf = corpo.lstrip()[:5].startswith(b'%PDF')
        if not parece_pdf and catalogo.MARCA_CADERNO_INEXISTENTE in \
                corpo[:4096].decode('latin-1', 'replace'):
            raise UnidadeInexistente(
                f'e-SAJ não tem o caderno {cd_caderno} em {unidade.data:%d/%m/%Y}')
        exigir_pdf(corpo, min_bytes=2048, contexto=f'{self.slug}/{unidade.chave}')
        return corpo

    def coletar(self, unidade: UnidadeColeta) -> Iterator[ItemDiario]:
        corpo = self._baixar(unidade)
        meta = unidade.meta
        nu_diario, cd_volume = int(meta['nu_diario']), int(meta.get('cd_volume') or 0)
        cd_caderno = int(meta['cd_caderno'])
        rotulo_caderno = str(meta.get('caderno') or f'caderno {cd_caderno}')
        quando = timezone.make_aware(datetime.combine(unidade.data, time.min))
        meio_completo = f'DJE/TJSP (e-SAJ) — {rotulo_caderno}'[:120]

        # O `Document` do MuPDF é memória NATIVA (fora do heap do Python): o GC
        # não a devolve na hora. Aqui ele serve só para medir a moda do corpo nas
        # 12 primeiras páginas — `paginas()` abre o seu próprio, então este tem
        # que ser fechado ANTES, senão o caderno inteiro fica mapeado duas vezes
        # durante toda a segmentação.
        leitor = pdf.abrir(corpo)
        try:
            tamanho_corpo = pdf.tamanho_do_corpo(leitor)
        finally:
            pdf.fechar(leitor)
        cnjs_no_texto: set[str] = set()
        cnjs_em_bloco: set[str] = set()
        contagem = {'blocos': 0, 'itens': 0, 'sem_cnj': 0, 'outro_tribunal': 0}
        primeira = True

        def _paginas():
            """Passa as páginas para o segmentador e, de quebra, anota TODOS os
            CNJs impressos — é contra esse total que a cobertura é aferida."""
            nonlocal primeira
            for pagina in pdf.paginas(corpo):
                if primeira:
                    pdf.conferir_data(pagina, unidade.data)
                    primeira = False
                cnjs_no_texto.update(achar_cnjs(pagina.texto))
                yield pagina

        for bloco in segmentador.segmentar(_paginas(), tamanho_corpo):
            contagem['blocos'] += 1
            cnjs_em_bloco.update(achar_cnjs(bloco.texto_corrido))
            pub = segmentador.interpretar(bloco)
            if not pub.cnj:
                # Numeração pré-CNJ ('583.00.2012.157046', 'Nº ORDEM:01.38.2012/001131').
                # Sem de-para não há como casar com `Process.numero_cnj`, e
                # inventar o casamento seria pior que perder o bloco.
                contagem['sem_cnj'] += 1
                continue
            if SUFIXO_CNJ_TJSP not in pub.cnj:
                contagem['outro_tribunal'] += 1
                continue
            contagem['itens'] += 1
            yield ItemDiario(
                cnj=pub.cnj,
                external_id=id_bloco_impresso(
                    self.slug, nu_diario, cd_caderno, pub.pagina, texto=pub.texto),
                data_disponibilizacao=quando,
                tipo_comunicacao=pub.tipo_comunicacao[:120],
                nome_orgao=pub.nome_orgao[:255],
                nome_classe=pub.nome_classe[:255],
                link=catalogo.url_pagina_humana(cd_volume, nu_diario, cd_caderno, pub.pagina)[:500],
                destinatarios=pub.destinatarios,
                destinatario_advogados=pub.destinatario_advogados,
                texto=pub.texto,
                numero_comunicacao=pub.numero_impresso[:120],
                hash=fingerprint_ato(pub.cnj, unidade.data, pub.texto),
                meio='D',
                meio_completo=meio_completo,
            )

        self._aferir_cobertura(unidade, cnjs_no_texto, cnjs_em_bloco, contagem)

    def _aferir_cobertura(self, unidade: UnidadeColeta, cnjs_no_texto: set[str],
                          cnjs_em_bloco: set[str], contagem: dict) -> None:
        """Falha alto quando a segmentação deixou processo de fora.

        O e-SAJ não declara quantas publicações tem no caderno (por isso
        `esperado()` devolve None), mas o PRÓPRIO TEXTO declara quais processos
        estão ali: todo CNJ impresso tem que cair dentro de algum bloco. É o
        gabarito mecânico possível nesta fonte — e é ele que separa "o
        segmentador parece bom" de "o segmentador cobriu 99,1% do caderno".

        Falhar aqui é melhor que gravar meia edição em silêncio: a unidade fica
        pendente e é retentada, e a gravação já feita é idempotente
        (`external_id` determinístico), então retentar não duplica.
        """
        total = len(cnjs_no_texto)
        dentro = len(cnjs_no_texto & cnjs_em_bloco)
        cobertura = (dentro / total) if total else None
        logger.info(
            '%s/%s: %d blocos → %d itens (sem_cnj=%d, outro_tribunal=%d); '
            'cobertura de CNJ %d/%d = %s',
            self.slug, unidade.chave, contagem['blocos'], contagem['itens'],
            contagem['sem_cnj'], contagem['outro_tribunal'], dentro, total,
            # NUNCA imprimir "100,0%" com denominador zero: era o que fazia um
            # caderno pré-CNJ inteiramente descartado parecer coleta perfeita.
            f'{100 * cobertura:.1f}%' if cobertura is not None else 'n/a (nenhum CNJ impresso)')

        # SEGUNDO EIXO — bloco aproveitado, não só CNJ achado.
        # O eixo de CNJ é cego exatamente onde a perda é total: num caderno
        # pré-CNJ, `total` é 0 (ou 1), a divisão não acontece e o
        # `MINIMO_PARA_AFERIR_COBERTURA` desliga o gate. Medido em 15/06/2009
        # (16.952 blocos → 0 itens) e 15/06/2010 (11.429 → 0): a unidade fechava
        # como `vazia`, terminal, sem erro. Aqui ela passa a fechar como
        # `sem_aproveit`, com o motivo escrito.
        if contagem['blocos'] >= MINIMO_PARA_AFERIR_COBERTURA and contagem['itens'] == 0:
            raise UnidadeSemDadoAproveitavel(
                f'{self.slug}/{unidade.chave}: {contagem["blocos"]} blocos de publicação REAL e '
                f'ZERO aproveitável (sem_cnj={contagem["sem_cnj"]}, '
                f'outro_tribunal={contagem["outro_tribunal"]}). Caderno da era pré-CNJ '
                "(numeração '583.00.2009.161101') — falta o de-para com Process.numero_cnj. "
                'Isto NÃO é edição vazia: é acervo que existe e que ainda não sabemos ler.'
            )

        piso = float(getattr(settings, 'DIARIOS_COBERTURA_MINIMA', 0.95))
        if total >= MINIMO_PARA_AFERIR_COBERTURA and cobertura is not None and cobertura < piso:
            raise ColetorError(
                f'{self.slug}/{unidade.chave}: só {dentro} dos {total} CNJs impressos '
                f'caíram dentro de um bloco ({cobertura:.1%} < {piso:.0%}) — '
                'segmentação suspeita, unidade não vai ser dada como coletada'
            )
