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
    fingerprint_ato,
    id_bloco_impresso,
    registrar,
)

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


def _br(d: date) -> str:
    return d.strftime('%d/%m/%Y')


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
        for bloco in blocos(paginas, secoes):
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
