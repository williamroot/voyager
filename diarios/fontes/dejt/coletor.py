"""Coletor do DEJT no contrato de `diarios/base.py`.

A UNIDADE DE COLETA É O CADERNO-TRIBUNAL-DIA, não o dia. Um dia do DEJT são 25
cadernos independentes (TST + 24 TRTs), um deles com 100 MB. Tratar "o dia"
como unidade obrigaria a re-baixar 519 MB porque um caderno falhou, e mataria o
paralelismo natural e educado — por tribunal, nunca por página.

A JANELA (2008-06-09 → 2024-07-31) NÃO É PALPITE. Em 01/08/2024 a Justiça do
Trabalho migrou os cadernos judiciários para o DJEN. Medido byte a byte no
TRT3: 31/07/2024 = 18,46 MB (ed. 4026); 01/08/2024 = 1,48 MB (ed. 4027). Em
matérias: 183.567/dia no país antes, 211/dia depois. Fora da janela o runner
recusa a coleta, porque o DJEN já traz aquilo (≥10 mil comunicações/dia só no
TRT3 em 13/08/2026, contra 18 no DEJT no MESMO dia).

Sobrando fora da janela há uma coisa real e pequena: as ATAS e PAUTAS de sessão,
que o DEJT continua publicando e que o DJEN explicitamente não carrega (o aviso
está na home do próprio DEJT). Isso é sinal de calendário — julgamento marcado —
e se coleta com `--sobrepor`, conscientemente, não por acidente.
"""

import contextlib
import logging
import re
import unicodedata
from collections import Counter
from collections.abc import Iterator
from datetime import date, datetime, time

from django.conf import settings
from django.utils import timezone

from diarios.base import (
    PROXY_DIRETO,
    ColetorDiario,
    ColetorError,
    ItemDiario,
    RespostaInvalida,
    UnidadeColeta,
    UnidadeInexistente,
    UnidadeSemDadoAproveitavel,
    achar_cnjs,
    fingerprint_ato,
    id_bloco_impresso,
    registrar,
)
from diarios.inventario import Inventario, MarcadorRegistro

from . import segmentador
from .catalogo import (
    CADERNO_JUDICIARIO,
    SEM_TRIBUNAL_NO_VOYAGER,
    achar_source_por_titulo,
    indice_do_tribunal,
    linhas_de_cadernos,
    total_de_materias,
)
from .segmentador import blocos, conferir_capa, ler_caderno
from .sessao_jsf import SessaoJSF

logger = logging.getLogger('voyager.diarios.dejt')

MEIO = 'D'
MEIO_COMPLETO = 'Diário Eletrônico da Justiça do Trabalho (DEJT)'

#: SEGUNDO EIXO DO GATE (`diarios/inventario.py`) — as linhas que ABREM um
#: registro no DEJT, e o formato de bloco que cada uma tem que virar. Contadas
#: no TEXTO EXTRAÍDO, nunca na saída do segmentador.
#:
#: São exatamente as duas âncoras do caderno, e cada uma tem balde EXCLUSIVO
#: (ver `segmentador.FORMATO_*`) — sem isso a perna A não morde, porque as ~900
#: matérias `Processo Nº` cobririam sozinhas a conta da Distribuição inteira.
#:
#: MEDIDO em 7 cadernos reais antes de declarar (TRT3, TRT16 e TRT22, edições de
#: 2018, 2020, 2022 e 2024), pelos três caminhos que têm que concordar — regex no
#: texto colado, contagem linha a linha, e blocos produzidos:
#:
#:   TRT3  10/07/2024 16.954 impressos · 16.940 blocos | 1.828 distrib. · 1.828
#:   TRT22 10/07/2024    890 impressos ·    890 blocos |   109 distrib. ·   109
#:   TRT16 10/07/2024  1.102 impressos ·  1.102 blocos |   154 distrib. ·   154
#:   TRT16 10/03/2022  1.395 impressos ·  1.392 blocos |     0 · 0
#:   TRT16 11/03/2020  2.190 impressos ·  2.187 blocos |     0 · 0
#:   TRT22 15/03/2018    750 impressos ·    744 blocos |     0 · 0
#:   TRT16 15/03/2018  1.237 impressos ·  1.181 blocos |     0 · 0
#:
#: As diferenças da coluna `Processo Nº` são **82 de 82 (100%)** de numeração
#: pré-CNJ — dívida conhecida, contada e nomeada em `_aferir_cobertura`.
#:
#: E a coluna da Distribuição só bate porque o marcador é aplicado dentro da
#: seção que o outline declara Distribuição: no TRT3 há **1.042 linhas com a
#: MESMA forma fora dela**, que são citação e não registro. Ver `_ver_linha` —
#: foi o 7º caderno que revelou isso, e os 6 primeiros não teriam revelado.
NOME_MARCADOR_MATERIA = 'matéria (Processo Nº)'
NOME_MARCADOR_DISTRIBUICAO = 'linha de Distribuição'

MARCADORES_DEJT = (
    MarcadorRegistro(
        nome=NOME_MARCADOR_MATERIA,
        padrao=re.compile(r'^Processo\s+N[º°o]\b'),
        formato=segmentador.FORMATO_PROCESSO),
    MarcadorRegistro(
        nome=NOME_MARCADOR_DISTRIBUICAO,
        padrao=re.compile(r'^[A-Za-zÇç]{2,10}[ \t]+\d{7}-\d{2}\.\d{4}\.5\.\d{2}\.\d{4}$'),
        formato=segmentador.FORMATO_DISTRIBUICAO),
)

#: Piso de blocos abaixo do qual não faz sentido cobrar cobertura de CNJ: um
#: caderno de recesso tem meia dúzia de atos e a divisão vira ruído. Mesmo
#: espírito do `MINIMO_PARA_AFERIR_COBERTURA` do TJSP.
MINIMO_PARA_AFERIR_COBERTURA = 50


def _br(d: date) -> str:
    return d.strftime('%d/%m/%Y')


def _paginas_de_distribuicao(total: int, secoes) -> set[int]:
    """Índices de página que a seção de Distribuição TOCA, segundo o outline.

    A informação é da FONTE (o índice que o próprio PDF carrega), não do
    segmentador — é isso que mantém a perna A independente (DIARIOS.md §18.3).

    O `+ 1` no fim não é folga arbitrária: **seção troca no meio da página.** O
    outline diz em que página cada seção COMEÇA, então a última página de uma
    seção é também a primeira da seguinte. Cortando em `fim` (exclusivo), as
    linhas de Distribuição impressas depois do meio dessa página ficavam de
    fora da contagem — medido: TRT22 10/07/2024 caía de 109 para **107** e
    TRT16 de 154 para **144**. Não dava alarme falso (a comparação é `>=`), mas
    subcontar o impresso é enfraquecer o gate em silêncio, que é pior.

    O custo do `+ 1` é admitir UMA página de fronteira por seção; no TRT3, o
    caderno onde a citação `SIGLA CNJ` é frequente, isso não muda a conta.
    """
    paginas: set[int] = set()
    marcos = sorted((int(p), t) for p, _u, t in (secoes or []))
    for i, (inicio, tipo) in enumerate(marcos):
        fim = marcos[i + 1][0] if i + 1 < len(marcos) else total - 1
        if segmentador.TIPO_DISTRIBUICAO in _sem_acento(tipo).lower():
            paginas.update(range(max(0, inicio), max(0, fim) + 1))
    return paginas


def _sem_acento(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '')
                   if unicodedata.category(c) != 'Mn')


def _ver_linha(inventario: Inventario, linha: str, cnjs, *, em_distribuicao: bool) -> None:
    """`ver_linha` com a ÚNICA correção que esta fonte exige.

    A linha `SIGLA CNJ` só é REGISTRO dentro da seção que o outline declara
    como Distribuição. Fora dela, a mesma FORMA é citação no corpo de outra
    matéria — e contá-la como registro infla o `impresso` e produz alarme
    falso, que em gate é pior que gate ausente (ensina a ignorar).

    Isto NÃO é teoria: foi medido no 7º caderno de validação, o TRT3 de
    10/07/2024 (13.853 páginas), e só nele. Das **2.870** linhas com a forma
    `SIGLA CNJ`, apenas **1.828** estão em Distribuição; as outras **1.042**
    estão dentro de seções `Notificação` — inclusive
    `AIRR 0004300-04.2002.5.03.0009` repetida 4 vezes seguidas, que é citação e
    não ato. Sem esta correção a perna A acusaria `2.870 impressos x 1.828
    blocos` e reprovaria a edição de referência do DEJT por perda que não
    existe. Nos 6 cadernos menores o erro era invisível: em TRT16/TRT22 as
    duas contagens batiam exatamente (109x109, 154x154).

    A restauração do contador é feita AQUI, e não em `diarios/inventario.py`,
    porque o mecanismo do §18 é compartilhado com o `tjsp-dje`: marcador
    sensível a seção é contrato novo, e contrato novo se discute antes de
    escrever.
    """
    antes = inventario.impresso[NOME_MARCADOR_DISTRIBUICAO]
    inventario.ver_linha(linha, cnjs)
    if not em_distribuicao:
        inventario.impresso[NOME_MARCADOR_DISTRIBUICAO] = antes


@registrar
class ColetorDEJT(ColetorDiario):
    slug = 'dejt'
    nome = 'DEJT — Diário Eletrônico da Justiça do Trabalho'

    # Medidas, não chutadas — ver docstring do módulo.
    janela_inicio = date(2008, 6, 9)
    janela_fim = date(2024, 7, 31)

    # O DEJT não tem rate limit, não tem WAF e não tem robots.txt (404). Ou
    # seja: o servidor não vai nos defender de nós mesmos, e 765 GB puxados no
    # talo de um JBoss de 2010 do CSJT é negação de serviço acidental. 0,5 req/s
    # é teto de conduta, não limitação técnica (a sonda fez 80 requisições em 8 s
    # sem um erro). Ajustável por `DIARIOS_RPS_DEJT` no settings.
    rps = 0.5
    #: advisória: o backfill pesado deve rodar de madrugada. O runner de
    #: `diarios/base.py` ainda não a aplica — quem agenda respeita.
    janela_horaria = (20, 6)

    caderno = CADERNO_JUDICIARIO

    #: segundo eixo do gate (`diarios/inventario.py`)
    MARCADORES_DE_REGISTRO = MARCADORES_DEJT

    # ── época em que o segmentador foi VALIDADO ──────────────────────────────
    # A janela acima é sobre DEDUPE (até quando o DEJT é a única porta). Esta
    # data é sobre QUALIDADE, e é outra coisa. Medida rodando o coletor de
    # verdade contra o TRT22, comparando com o gabarito do próprio DEJT:
    #
    #   2010-03-10     0 de   418   0%   caderno assinado (PKCS#7), prosa corrida
    #   2013-03-11     0 de   354   0%
    #   2014-03-11    68 de   316  22%
    #   2016-03-11   162 de   226  72%
    #   2017-03-15   517 de   622  83%
    #   2017-09-13   819 de   804 102%
    #   2018-03-13 1.166 de   834 140%
    #   2020-03-11   885 de   747 118%
    #   2022-03-10 1.064 de 1.067 100%
    #   2024-03-13 1.131 de 1.080 105%
    #
    # A subida não é um "deploy": é a migração para o PJe. A matéria vinda do
    # PJe usa o template `Processo Nº <sigla>-<CNJ>` que este segmentador lê; a
    # matéria antiga é PROSA CORRIDA numerada ('32. PROCESSO
    # TRT-22ª/2ª TURMA/RO/0017700-10.2009.5.22.0107. RECORRENTE: X (Dr. Y).'),
    # que exige outro parser — com o seu próprio gabarito e a sua própria
    # fixture, porque atribuir advogado errado em prosa é fácil.
    #
    # De 2018 em diante a cobertura passa o piso da casa em toda amostra. Antes
    # disso o coletor ABSTÉM: falha rápido, ANTES de baixar o PDF, com a
    # mensagem dizendo o que falta. Deixar rodar só trocaria a abstenção por
    # meia edição gravada em silêncio — e ainda queimaria banda do CSJT.
    segmentavel_desde = date(2018, 1, 1)

    def __init__(self):
        # Hoje o DEJT responde 200 direto de IP de datacenter (AS28666), então
        # o default é sair sem proxy — menos SPOF. Se um dia precisar de proxy,
        # tem que ser PROXY_PRESO: a sessão é sticky no ALB e a conversa Seam
        # morre se o IP mudar no meio dos 3 passos.
        self.modo_proxy = getattr(settings, 'DIARIOS_DEJT_MODO_PROXY', PROXY_DIRETO)
        # Quem escrever o parser da era pré-PJe baixa esta data por settings,
        # sem mexer em código nem em migration.
        limite = getattr(settings, 'DIARIOS_DEJT_SEGMENTAVEL_DESDE', None)
        if limite:
            self.segmentavel_desde = date.fromisoformat(str(limite))
        super().__init__()

    # ── catálogo ────────────────────────────────────────────────────────────
    def catalogar(self, data_inicio: date, data_fim: date) -> Iterator[UnidadeColeta]:
        """Uma requisição devolve o período inteiro — inclusive 18 anos.

        A tela de cadernos não pagina e não tem cap: `01/01/2008 a 16/08/2026`
        com [Todos] devolveu 95.679 linhas numa resposta de 50,8 MB em 37 s.
        Nada parecido com o cap de 10k do DJEN. É por isso que o contrato separa
        catálogo de coleta: dá para MEDIR o acervo antes de baixar 765 GB.
        """
        jsf = SessaoJSF(self.sessao)
        html_ = jsf.buscar(_br(data_inicio), _br(data_fim), '', self.caderno)
        linhas = linhas_de_cadernos(html_)
        fora = 0
        pre_pje = 0
        for linha in linhas:
            if self.segmentavel_desde and linha.data < self.segmentavel_desde:
                # NÃO matricular o que sabemos que vamos recusar. Medido no
                # inventário de 18 anos (95.679 linhas): 46.845 edições são
                # pré-2018 = 49% do catálogo. Sem este corte, cada uma vira uma
                # `EdicaoDiario` que o tick reenfileira até `MAX_TENTATIVAS` e
                # depois fica de dívida vermelha na dashboard — 47 mil linhas de
                # ruído operacional para dizer o que já sabemos (falta o parser
                # da era pré-PJe). Quando ele existir, baixar
                # `DIARIOS_DEJT_SEGMENTAVEL_DESDE` recataloga tudo de uma vez.
                pre_pje += 1
                continue
            if linha.sigla is None or linha.sigla in SEM_TRIBUNAL_NO_VOYAGER.values():
                # CSJT e ENAMAT publicam no DEJT mas não existem como Tribunal
                # no Voyager. Catalogá-los criaria EdicaoDiario com FK quebrada
                # e falha eterna no runner. Fica contado no log — dívida visível.
                fora += 1
                continue
            yield UnidadeColeta(
                chave=f'{self.caderno}-{linha.sigla}-{linha.data:%Y-%m-%d}-{linha.edicao}',
                data=linha.data,
                tribunal_sigla=linha.sigla,
                rotulo=linha.titulo,
                meta={
                    'titulo': linha.titulo,
                    'edicao': linha.edicao,
                    'ano_edicao': linha.ano_edicao,
                    'caderno': self.caderno,
                    'tribunal_idx': indice_do_tribunal(linha.sigla),
                    'data_br': _br(linha.data),
                    # `source` do link NÃO vai para o meta de propósito: ele
                    # carrega um `j_id` gerado pelo JSF, que muda a cada deploy
                    # do DEJT. É relido na hora do download.
                },
            )
        if fora or pre_pje:
            logger.info('catálogo dejt %s→%s: %d edições ignoradas (CSJT/ENAMAT sem Tribunal), '
                        '%d ignoradas por serem anteriores a %s (era pré-PJe, sem parser)',
                        data_inicio, data_fim, fora, pre_pje, self.segmentavel_desde)

    # ── coleta ──────────────────────────────────────────────────────────────
    def coletar(self, unidade: UnidadeColeta) -> Iterator[ItemDiario]:
        # Abstenção ANTES do download: caderno da era pré-PJe não é segmentável
        # por este parser (ver `segmentavel_desde`). Falhar aqui custa zero
        # requisição em vez de 1 MB a 100 MB puxados do CSJT para depois o gate
        # reprovar — e a mensagem diz exatamente o que falta construir.
        if self.segmentavel_desde and unidade.data < self.segmentavel_desde:
            raise ColetorError(
                f'{unidade.chave}: caderno de {unidade.data} é da era pré-PJe, cujo formato '
                f'(prosa corrida numerada) este segmentador não lê — cobertura medida: 0% em '
                f'2013, 22% em 2014, 72% em 2016. Validado a partir de '
                f'{self.segmentavel_desde}. Falta o parser da era antiga; até lá, abster.'
            )

        meta = unidade.meta or {}
        caderno = meta.get('caderno') or self.caderno
        data_br = meta.get('data_br') or _br(unidade.data)
        tribunal_idx = meta.get('tribunal_idx')
        if tribunal_idx is None:
            tribunal_idx = indice_do_tribunal(unidade.tribunal_sigla or '')

        jsf = SessaoJSF(self.sessao)
        html_ = jsf.buscar(data_br, data_br, tribunal_idx, caderno)
        linhas = linhas_de_cadernos(html_)
        titulo = meta.get('titulo') or unidade.rotulo
        if not linhas:
            # AUSÊNCIA contra DRIFT — a distinção que a verificação adversarial pegou
            # em 2026-08-16, e que decide se 86 mil edições viram acervo ou
            # viram "feriado forense" para sempre.
            #
            # Zero linhas com o eco CONFERIDO PODE ser feriado forense/recesso
            # (medido em 14/08/2023, 12/03/2022 e 03/03/2025, com dias vizinhos
            # cheios) — e aí ausência ≠ falha, o watermark fecha `inexistente` e
            # nunca mais retenta (lição do `_dia_coberto` do djen/jobs.py).
            #
            # MAS: no caminho de produção (catalogar → EdicaoDiario → coletar) a
            # unidade só existe porque o CATÁLOGO a viu, com título e número de
            # edição. Se ela sumiu da tabela agora, o que mudou foi o layout do
            # DEJT (basta o CSJT renomear uma classe CSS: provado renomeando
            # `link-download`→`btn-download` no HTML real da sonda, e o resultado
            # era `inexistente` + IngestionRun `success` + tick que nunca mais
            # reenfileira). Chamar isso de feriado é gravar um fato FALSO no
            # watermark e reportar sucesso — a lacuna invisível que este projeto
            # inteiro existe para não repetir.
            #
            # Regra: unidade CATALOGADA (tem título/edição no meta) que some da
            # tabela é DRIFT (alerta + falha retentável). Unidade sem catálogo
            # (sonda, --chave manual de um dia nunca catalogado) é ausência.
            if meta.get('titulo') or meta.get('edicao'):
                self._alertar_drift(unidade, linhas, titulo)
                raise RespostaInvalida(
                    f'{unidade.chave}: o catálogo tinha {titulo!r} (edição '
                    f'{meta.get("edicao")}) em {data_br}, mas a busca voltou com ZERO linhas. '
                    'Isso é layout do DEJT mudado ou edição removida — não é feriado forense. '
                    'Confira o parser antes de retentar em massa.'
                )
            raise UnidadeInexistente(
                f'{unidade.chave}: o DEJT não lista caderno em {data_br} para o tribunal '
                f'{unidade.tribunal_sigla} (feriado forense/recesso)'
            )

        source = achar_source_por_titulo(html_, titulo)
        if not source:
            self._alertar_drift(unidade, linhas, titulo)
            raise RespostaInvalida(
                f'{unidade.chave}: a tabela tem {len(linhas)} linha(s) mas nenhuma casa com '
                f'{titulo!r} — layout do DEJT mudou ou a edição sumiu'
            )

        corpo = jsf.baixar_caderno(source, data_ini=data_br, data_fim=data_br,
                                   tribunal_idx=tribunal_idx, caderno=caderno)
        paginas, secoes = ler_caderno(corpo)
        conferir_capa(paginas, edicao=meta.get('edicao'))

        quando = timezone.make_aware(datetime.combine(unidade.data, time.min))
        numero_comunicacao = f'{meta.get("edicao", "")}/{meta.get("ano_edicao", "")}'.strip('/')
        vistos = 0
        # Dedupe DENTRO da unidade: o caderno às vezes imprime o mesmo ato duas
        # vezes na mesma página, e aí os dois blocos geram o mesmo external_id.
        # O banco já ignora o conflito, mas a CONTAGEM ficaria mentindo — a
        # segunda coleta da mesma edição reportaria `novas=1` para sempre, e o
        # critério de idempotência do runner é justamente `novas=0`.
        ids_vistos: set[str] = set()
        repetidos = 0

        # ── SEGUNDO EIXO: alimentado do TEXTO EXTRAÍDO, no mesmo passeio das
        # páginas. `Inventario.ver_bloco` recebe só o NOME do formato, nunca o
        # texto — é essa assinatura que impede o eixo de virar circular.
        inventario = Inventario(marcadores=tuple(self.MARCADORES_DE_REGISTRO))
        cnjs_no_texto: set[str] = set()
        cnjs_em_bloco: set[str] = set()
        distribuicao_na_pagina = _paginas_de_distribuicao(len(paginas), secoes)
        for numero, pagina in enumerate(paginas):
            em_distribuicao = numero in distribuicao_na_pagina
            for linha in pagina.split('\n'):
                limpa = linha.strip()
                achados = achar_cnjs(limpa)
                cnjs_no_texto.update(achados)
                _ver_linha(inventario, limpa, achados, em_distribuicao=em_distribuicao)
        descartes: Counter = Counter()

        for bloco in blocos(paginas, secoes, descartes):
            inventario.ver_bloco(bloco.formato)
            cnjs_em_bloco.update(achar_cnjs(bloco.texto))
            external_id = id_bloco_impresso(
                # Coordenada física + hash do conteúdo. O ordinal do bloco na
                # página NÃO serve: qualquer ajuste no recorte (e vai haver, o
                # layout mudou ao longo de 16 anos) deslocaria todos os ordinais
                # e a re-coleta duplicaria a edição inteira.
                self.slug, meta.get('edicao', '0'), bloco.pagina, texto=bloco.texto)
            if external_id in ids_vistos:
                repetidos += 1
                continue
            ids_vistos.add(external_id)
            vistos += 1
            yield ItemDiario(
                cnj=bloco.cnj,
                external_id=external_id,
                data_disponibilizacao=quando,
                tipo_comunicacao=bloco.tipo[:120],
                tipo_documento=bloco.subtipo[:120],
                nome_orgao=bloco.unidade[:255],
                # nome_classe/codigo_classe: ABSTENÇÃO. O caderno traz a sigla
                # ('ATOrd', 'ROT'), não o código da tabela de classes do CNJ, e
                # `preencher_classe_via_djen` propaga `nome_classe` para
                # `Process.classe_nome`. A sigla fica verbatim no texto.
                destinatarios=bloco.partes,
                destinatario_advogados=bloco.advogados,
                texto=bloco.texto,
                numero_comunicacao=numero_comunicacao[:120],
                hash=fingerprint_ato(bloco.cnj, unidade.data, bloco.texto),
                meio=MEIO,
                meio_completo=MEIO_COMPLETO,
                # link: ABSTENÇÃO. O DEJT não expõe URL estável do ato — só o
                # filename do attachment. Sintetizar um link que não abre é
                # pior que campo vazio.
            )
        logger.info('dejt %s: %d páginas → %d matérias (%d blocos repetidos na edição)',
                    unidade.chave, len(paginas), vistos, repetidos)
        self._aferir_cobertura(unidade, cnjs_no_texto, cnjs_em_bloco,
                               inventario=inventario, descartes=descartes, vistos=vistos)

    # ── gate ────────────────────────────────────────────────────────────────
    def _aferir_cobertura(self, unidade: UnidadeColeta, cnjs_no_texto: set[str],
                          cnjs_em_bloco: set[str], *, inventario: Inventario,
                          descartes: Counter, vistos: int) -> None:
        """Os DOIS eixos, nesta ordem: proporção primeiro, inventário depois.

        Até 03/09/2026 esta fonte não tinha eixo nenhum de segmentação: o único
        gate era o `esperado()`, o gabarito da pesquisa avançada do DEJT — que
        devolve `None` em qualquer erro e, com a fonte fora do ar desde
        18/08/2026, devolve `None` SEMPRE. Ou seja, coletar hoje seria coletar
        com zero régua. Estes dois eixos vivem no PDF e não dependem do host.

        O que a medição de 6 cadernos ensinou e que está codificado aqui: a
        diferença entre marcador impresso e bloco produzido no DEJT tem uma
        causa conhecida e UMA só — matéria com numeração trabalhista pré-CNJ,
        que o segmentador descarta de propósito por não haver de-para. Foram
        **68 de 68** descartes (100%) em TRT16/TRT22 de 2018 a 2024. Por isso o
        gate não usa tolerância percentual (que seria só um segundo número
        percentual ao lado do primeiro, DIARIOS.md §18.5): ele exige que a
        diferença esteja INTEIRAMENTE explicada pelo balde `pre_cnj`. Um único
        descarte `desconhecido` reprova.
        """
        total = len(cnjs_no_texto)
        dentro = len(cnjs_no_texto & cnjs_em_bloco)
        cobertura = (dentro / total) if total else None
        logger.info(
            'dejt/%s: cobertura de CNJ %d/%d = %s; descartes %s',
            unidade.chave, dentro, total,
            # NUNCA "100,0%" com denominador zero — era assim que um caderno
            # inteiramente descartado parecia coleta perfeita (DIARIOS.md §4).
            f'{100 * cobertura:.1f}%' if cobertura is not None else 'n/a (nenhum CNJ impresso)',
            dict(descartes) or '{}')

        # Havia matéria e NADA é aproveitável: terminal, com o motivo escrito.
        # É o `sem_aproveit` do §4 — a diferença entre "dia vazio" e "acervo que
        # existe e que ainda não sabemos ler".
        impressos = inventario.total_impresso()
        if impressos >= MINIMO_PARA_AFERIR_COBERTURA and vistos == 0:
            raise UnidadeSemDadoAproveitavel(
                f'dejt/{unidade.chave}: a fonte imprimiu {impressos} registros e ZERO virou '
                f'matéria aproveitável (descartes: {dict(descartes)}). Isto NÃO é edição '
                'vazia: é acervo que existe e que este parser não sabe ler.'
            )

        divergencias = []
        if inventario.mede:
            orfaos = cnjs_no_texto - cnjs_em_bloco
            divergencias = inventario.conferir(orfaos)
            logger.info('dejt/%s: inventário da fonte %s → blocos %s%s',
                        unidade.chave, dict(inventario.impresso), dict(inventario.segmentado),
                        ' (assinaturas TRUNCADAS)' if inventario.assinaturas_truncadas else '')
        else:
            # Abstenção EXPLÍCITA — "não medido" e "medido e ok" não podem ter a
            # mesma cara no log.
            logger.info('dejt/%s: inventário por marcador NÃO MEDIDO '
                        '(a fonte não declara marcador)', unidade.chave)

        piso = float(getattr(settings, 'DIARIOS_COBERTURA_MINIMA', 0.95))
        if total >= MINIMO_PARA_AFERIR_COBERTURA and cobertura is not None and cobertura < piso:
            raise ColetorError(
                f'dejt/{unidade.chave}: só {dentro} dos {total} CNJs impressos caíram dentro '
                f'de um bloco ({cobertura:.1%} < {piso:.0%}) — segmentação suspeita, unidade '
                'não vai ser dada como coletada'
                # TODAS as divergências, não só a primeira: quando o eixo de
                # proporção reprova, é justamente ali que a perna B costuma ter
                # o NOME do formato desconhecido — e perdê-lo por causa de um
                # `[0]` deixaria a causa raiz de fora da mensagem. Medido no
                # TRT22 de 15/03/2018: 92,6% de cobertura E 45 CNJs órfãos na
                # forma `Processo : #-#.#.#.#.#`, que o segmentador não lê.
                + (' | inventário também acusa: '
                   + '; '.join(str(d) for d in divergencias) if divergencias else '')
            )

        # Perna B (formato DESCONHECIDO) reprova sempre: ela só fala quando ≥30
        # CNJs órfãos compartilham a MESMA forma de linha, e isso não tem
        # explicação conhecida nesta fonte — é o suspeito nomeado.
        assinaturas = [d for d in divergencias if d.tipo == 'assinatura']
        if assinaturas:
            raise ColetorError(
                f'dejt/{unidade.chave}: inventário divergente — {assinaturas[0]}. '
                'A cobertura de CNJ estava ACIMA do piso — quem pegou foi o segundo eixo.'
            )

        # Perna A: a diferença precisa estar EXPLICADA pela conta, não por um
        # adjetivo. `impresso - segmentado` tem que caber dentro do balde
        # `pre_cnj`, E não pode haver descarte de causa desconhecida.
        #
        # A conta é indispensável, e isto foi pago: a primeira versão desta
        # função só olhava `desconhecidos`, e num caso em que a seção de
        # Distribuição inteira não virou bloco (20 impressos x 0 blocos, ZERO
        # descarte, porque as âncoras nem foram reconhecidas) ela rebaixava a
        # perda a um WARNING que dizia, sem ironia, "diferença EXPLICADA por 0
        # matérias". Perda total anunciada como explicada por nada — a
        # assinatura exata da doença que este eixo trata.
        marcadores = [d for d in divergencias if d.tipo == 'marcador']
        desconhecidos = descartes.get('desconhecido', 0) + descartes.get('vazio', 0)
        pre_cnj = descartes.get('pre_cnj', 0)
        faltando = sum(d.impresso - (d.segmentado or 0) for d in marcadores)
        if marcadores and (desconhecidos or faltando > pre_cnj):
            raise ColetorError(
                f'dejt/{unidade.chave}: inventário divergente — '
                + '; '.join(str(d) for d in marcadores)
                + f'. Faltam {faltando} bloco(s) e a numeração pré-CNJ explica só {pre_cnj}'
                + (f'; {desconhecidos} descarte(s) SEM numeração reconhecível de nenhuma era'
                   if desconhecidos else '')
                + '. A cobertura de CNJ estava ACIMA do piso — quem pegou foi o segundo eixo.'
            )
        if marcadores:
            logger.warning(
                'dejt/%s: %s — diferença EXPLICADA por %d matéria(s) de numeração pré-CNJ '
                '(dívida conhecida: falta o de-para com Process.numero_cnj)',
                unidade.chave, marcadores[0], pre_cnj)

    # ── gabarito da própria fonte ───────────────────────────────────────────
    def esperado(self, unidade: UnidadeColeta) -> int | None:
        """Quantas matérias o DEJT declara para este tribunal-dia.

        A pesquisa avançada informa "1 até 20 de 16.717" no rodapé. É gabarito
        de graça e MECÂNICO: transforma "o segmentador parece bom" em "achou
        16.956 das 16.717 que a fonte declara". Custa 4 requisições, então vai
        para o cache — e devolve None em qualquer erro, porque reprovar uma
        coleta boa por falha do gabarito seria pior que não ter gabarito.
        """
        from django.core.cache import cache

        meta = unidade.meta or {}
        data_br = meta.get('data_br') or _br(unidade.data)
        caderno = meta.get('caderno') or self.caderno
        idx = meta.get('tribunal_idx')
        if idx is None:
            try:
                idx = indice_do_tribunal(unidade.tribunal_sigla or '')
            except ValueError:
                return None
        chave_cache = f'dejt:esperado:{caderno}:{idx}:{data_br}'
        try:
            em_cache = cache.get(chave_cache)
        except Exception:
            em_cache = None
        if em_cache is not None:
            return em_cache or None

        try:
            jsf = SessaoJSF(self.sessao)
            form = jsf.abrir_pesquisa_avancada(data_br, data_br, caderno)
            html_ = jsf.pesquisar_materias(form, data_ini=data_br, data_fim=data_br,
                                           tribunal_idx=idx, caderno=caderno)
            total = total_de_materias(html_)
        except Exception as exc:
            logger.warning('dejt: gabarito indisponível para %s (%s) — seguindo sem gate',
                           unidade.chave, exc)
            return None
        with contextlib.suppress(Exception):  # cache indisponível não reprova coleta
            cache.set(chave_cache, total or 0, timeout=7 * 24 * 3600)
        return total

    # ── drift ───────────────────────────────────────────────────────────────
    def _alertar_drift(self, unidade: UnidadeColeta, linhas: list, titulo: str) -> None:
        """A tabela respondeu, mas não do jeito que conhecemos.

        O alvo aqui não é chave de JSON (como no DJEN) e sim o HTML gerado pelo
        JSF. Registrar em `SchemaDriftAlert` é o que faz o layout novo aparecer
        na dashboard em vez de virar 86 mil falhas silenciosas.
        """
        try:
            from djen.parser import registrar_drift
            from tribunals.models import SchemaDriftAlert, Tribunal

            tribunal = Tribunal.objects.filter(sigla=unidade.tribunal_sigla).first()
            if tribunal is None:
                return
            registrar_drift(
                tribunal, SchemaDriftAlert.TIPO_MISSING,
                ['dejt:linha-de-caderno-nao-casa'],
                {'chave': unidade.chave, 'titulo_esperado': titulo,
                 'titulos_recebidos': [linha.titulo for linha in linhas[:5]]},
                None,
            )
        except Exception:
            logger.exception('dejt: falhou ao registrar drift de %s', unidade.chave)
