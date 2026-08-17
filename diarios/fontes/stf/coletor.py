"""Coletor do DJe do STF.

POR QUE ESTA FONTE EXISTE
=========================
O STF é o único tribunal do país que NÃO está no DJEN, e a adesão é facultativa
a ele. Prova de 16/08/2026: `siglaTribunal=STF` devolve o mesmo HTTP 500 que a
sigla inventada `ZZZ`, enquanto TRF1/STJ/TST devolvem 200. E o DJe em PDF do
portal legado morreu entre dez/2022 e jul/2023 — a URL responde 200 com 142
bytes de `alert("DJ Eletrônico solicitado inválido ou não disponível.")`.

O valor aqui não é volume (~590 publicações/dia útil, ~5% do STJ): é QUE
publicação é. É onde sai a tese de repercussão geral que decide precatório, com
**relator** identificado e **envolvidos** já estruturados com polo, categoria e
OAB — dois dados que hoje não existem em lugar nenhum do acervo.

DUAS DECISÕES SEMÂNTICAS QUE PRECISAM DE OLHO HUMANO ANTES DE LIGAR EM PROD
===========================================================================
1. **O CNJ que gravamos é o de ORIGEM na maioria dos recursos.** Medido em 40
   processos de um dia real: ARE/RE trazem o CNJ do TJ/TRF de origem
   (`0034988-34.2013.8.26.0053` = TJSP), enquanto HC/Pet/Rcl trazem o CNJ
   nativo do STF (`0181511-31.2026.1.00.0000`, J=1). Como
   `persistir_movimentacoes` cria `Process(tribunal=STF, numero_cnj=<CNJ do
   TJSP>)` e a unicidade de `Process` é (tribunal, numero_cnj), o MESMO processo
   passa a ter duas linhas: uma sob TJSP e outra sob STF. Isso é idêntico ao que
   o STJ vai fazer quando for ligado, e é exatamente o nó dos "incidentes CNJ
   vinculados" que já está na fila. Deixei o comportamento PADRÃO do contrato
   (não sobrescrevi `persistir`) e travei em teste, para a decisão ser
   consciente em vez de emergir de um `if` escondido aqui.
2. **Publicação sem CNJ não é gravada.** ~10% dos processos respondem "Sem
   número único" no portal. Abster > chutar: elas entram na contabilidade
   (`ColetorSTF.balanco`) e no log, nunca no banco com CNJ inventado.

CONDUTA DE REDE
===============
Duas origens, dois circuit-breakers, de propósito:
  · `stf` — a API JSON, rápida e estável (~1,1 s para 500 itens; ~25 requisições
    seguidas sem um 429 de IP de datacenter);
  · `stf-portal` — o IIS/ASP legado do resolvedor de CNJ, lento e frágil.
Se o portal cair, o breaker DELE abre e a API continua coletando (as
publicações do dia ficam pendentes até o portal voltar). Compartilhar breaker
faria uma queda do IIS calar o coletor inteiro.
"""

import logging
import re
from collections.abc import Iterator
from datetime import date, timedelta

from django.conf import settings

from diarios.base import (
    PROXY_DIRETO,
    ColetorDiario,
    ColetorError,
    ItemDiario,
    SessaoDiario,
    UnidadeColeta,
    UnidadeInexistente,
    external_id_de,
    fingerprint_ato,
    registrar,
)
from djen.parser import parse_dt, registrar_drift
from tribunals.models import SchemaDriftAlert

from .api import CA_BUNDLE, CHAVES_PUBLICACAO, USER_AGENT_NAVEGADOR, SessaoSTF
from .resolver_cnj import ResolvedorCNJ

logger = logging.getLogger('voyager.diarios.stf')

#: Primeira divulgação que a API tem. MEDIDO por totais anuais (16/08/2026):
#: 2015/2018/2019 = 0, 2020 = 2.235 (a mais antiga é 01/09/2020), 2021 = 38.154,
#: 2022 = 68.320, 2023 = 212.505. O acervo é completo de 2023 em diante e
#: parcial em 2021-2022 — justo o período em que o PDF do portal legado ainda
#: vive. Quem quiser série longa do STF vai ter que costurar as duas fontes.
PRIMEIRA_DIVULGACAO = date(2020, 9, 1)

#: Rótulo do veículo, para a origem ficar legível na UI sem coluna nova.
MEIO_COMPLETO = 'DJe/STF (digital.stf.jus.br)'

#: Polo do DJEN é 'A'/'P'; o STF fala 'ATIVO'/'PASSIVO'/'INTERESSADO'. Traduzir
#: mantém `destinatarios` legível pelo mesmo código que lê o DJEN.
POLO = {'ATIVO': 'A', 'PASSIVO': 'P', 'INTERESSADO': 'I'}

#: `categoria` do envolvido que identifica advogado (o resto é parte).
CATEGORIA_ADVOGADO = 'ADVOGADO(A/S)'


@registrar
class ColetorSTF(ColetorDiario):
    """Um dia de DIVULGAÇÃO do DJe do STF = uma unidade de coleta."""

    slug = 'stf'
    nome = 'DJe do STF (digital.stf.jus.br)'

    # O STF nunca entrou no DJEN ⇒ esta é a ÚNICA porta, em todo o período em
    # que a API tem dado. Sem `janela_fim`: ao contrário do DJE/TJSP e do DEJT,
    # esta fonte não é histórica, é o fluxo corrente.
    janela_inicio = PRIMEIRA_DIVULGACAO
    janela_fim = None

    # Nenhum 403/429 observado de IP de datacenter em ~25 requisições seguidas:
    # proxy aqui só acrescentaria o SPOF do pool sem resolver problema nenhum.
    modo_proxy = PROXY_DIRETO
    # Teto auto-imposto. Um dia útil inteiro são 2 requisições à API; quem manda
    # no ritmo real é o resolvedor de CNJ, que tem sessão e rps próprios.
    rps = 1.0
    # Não é `False`: é o caminho do bundle com o intermediário que o STF não
    # envia. O `requests` aceita path em `verify=` — ver api.CA_BUNDLE.
    verificar_tls = CA_BUNDLE

    def __init__(self):
        super().__init__()
        self.sessao.user_agent = USER_AGENT_NAVEGADOR
        # `or CA_BUNDLE`: a setting existe com default '' (env vazia = "use o
        # bundle embarcado"). Sem o `or`, um '' iria para `verify=` e derrubaria
        # a verificação TLS por acidente de configuração.
        self.sessao.verificar_tls = getattr(settings, 'DIARIOS_STF_CA_BUNDLE', '') or CA_BUNDLE
        self.api = SessaoSTF(self.sessao)
        # Sessão SEPARADA para o portal legado: breaker próprio (ver docstring
        # do módulo) e ritmo mais lento, porque é um IIS de outra era.
        self.sessao_portal = SessaoDiario(
            fonte='stf-portal', modo_proxy=PROXY_DIRETO,
            rps=float(getattr(settings, 'DIARIOS_RPS_STF_PORTAL', 0.8)),
            user_agent=USER_AGENT_NAVEGADOR,
            verificar_tls=self.sessao.verificar_tls,
        )
        self.resolver = ResolvedorCNJ(self.sessao_portal)
        #: chave da unidade → contabilidade da última coleta. É o que
        #: `esperado()` devolve e o que o log de fechamento imprime.
        self.balanco: dict[str, dict] = {}

    # -- catálogo -------------------------------------------------------------
    def catalogar(self, data_inicio: date, data_fim: date) -> Iterator[UnidadeColeta]:
        """Um dia de DIVULGAÇÃO por unidade, e SÓ dia já fechado.

        O TETO ESTAVA NO EIXO ERRADO (corrigido em 2026-08-16)
        ------------------------------------------------------
        A versão anterior usava o `/ultimo-dje` como teto. Ele é a data da última
        EDIÇÃO publicada — eixo `publicacao` —, enquanto a unidade aqui é o dia
        de DIVULGAÇÃO. Os dois não coincidem, e o erro é perda permanente:

          · medido em sáb 16/08/2026: `/ultimo-dje` = 2026-08-14, mas existem
            588 divulgações em 14/08 (16h36→20h32) e 39 em 15/08, ambas com
            `publicacao=None`. Ou seja, o teto é `max(publicacao)` e a edição de
            14/08 já estava no ar na MANHÃ de 14/08, quando as 588 divulgações
            daquele dia ainda não tinham acontecido;
          · com aquele teto, o dia CORRENTE entrava no catálogo. Coletado às 09h
            ele responde `total=0` (medido) → `UnidadeInexistente` →
            `EdicaoDiario.inexistente` (terminal, o tick não reenfileira) com
            `IngestionRun.status='success'`: as 588-815 publicações do dia somem
            para sempre e o run fica VERDE. Coletado às 18h é pior de forma:
            traz metade, o `esperado()` é medido na própria coleta (é
            auto-referente), o gate de 95% passa, a unidade fecha `ok` e o resto
            do dia se perde igual.

        Cura: só catalogar dia cuja divulgação já FECHOU — `hoje-1` em horário de
        Brasília — respeitando também o `/ultimo-dje` quando ele for menor. A
        fronteira do dia corrente entra amanhã, pelo `catalogar_fronteira`, que
        roda com janela de 7 dias e portanto nunca deixa buraco.

        Não perguntamos à API quais dias TÊM publicação: descobrir isso custaria
        uma requisição por dia — o mesmo preço da coleta. Dia sem divulgação
        (fim de semana, feriado forense) é resolvido em `coletar`.
        """
        ultimo = self.api.ultimo_dje()
        teto = min(ultimo, self._ultimo_dia_fechado())
        dia = max(data_inicio, self.janela_inicio)
        fim = min(data_fim, teto)
        if fim < dia:
            logger.info('STF: nada a catalogar em %s→%s (último DJe = %s, último dia de '
                        'divulgação fechado = %s)',
                        data_inicio, data_fim, ultimo, self._ultimo_dia_fechado())
            return
        while dia <= fim:
            yield UnidadeColeta(
                chave=f'stf-{dia.isoformat()}',
                data=dia,
                tribunal_sigla='STF',
                rotulo=f'DJe do STF — divulgação de {dia.strftime("%d/%m/%Y")}',
                meta={'ultimo_dje_no_catalogo': ultimo.isoformat()},
            )
            dia += timedelta(days=1)

    @staticmethod
    def _ultimo_dia_fechado() -> date:
        """Último dia cuja janela de divulgação já terminou (ontem, em BRT).

        Em `America/Sao_Paulo` de propósito: o servidor pode rodar em UTC, e às
        22h BRT (01h UTC do dia seguinte) o "ontem" calculado em UTC seria HOJE
        — que é exatamente o dia aberto que este método existe para excluir.
        """
        from django.utils import timezone as djtz
        return djtz.localdate() - timedelta(days=1)

    # -- coleta ---------------------------------------------------------------
    def coletar(self, unidade: UnidadeColeta) -> Iterator[ItemDiario]:
        tribunal = self.tribunal_de(unidade)
        vistos: set[int] = set()
        sem_cnj = 0
        sem_processo_id = 0
        sem_data = 0
        titulo_divergente = 0
        drift_conferido = False

        for pub in self.api.iter_dia(unidade.data):
            if not drift_conferido:
                self._checar_drift(pub, tribunal)
                drift_conferido = True
            id_pub = pub.get('id')
            if id_pub is None or id_pub in vistos:
                # A API não repetiu id em nenhuma medição (2.993 ids distintos
                # numa janela de 3 dias), mas paginação de terceiro sem
                # transação é sempre suspeita: contar duas vezes falsearia o
                # fechamento contra o `total`.
                continue
            vistos.add(id_pub)

            proc = self.resolver.resolver(pub.get('processoId'))
            if proc is None:
                sem_processo_id += 1
                continue
            if proc.cnj is None:
                sem_cnj += 1
                continue
            if not _confere_processo(pub, proc):
                # Guarda do JOIN: o CNJ vem de OUTRA origem (o portal legado) que
                # a publicação. Se `processoId` apontasse para o incidente errado,
                # a publicação do STF grudaria no processo errado — que é pior do
                # que perdê-la. Medido em 44 publicações de 11/08/2026: 42 títulos
                # batem exatamente, 0 divergem, 2 vêm sem título (e são justamente
                # os 2 'sem número único', já descartados acima).
                titulo_divergente += 1
                logger.warning('STF: publicação %s diz %r e o portal diz %r — descartada',
                               pub.get('id'), pub.get('processo'), proc.titulo)
                continue
            item = self._para_item(pub, proc)
            if item is None:
                sem_data += 1
                continue
            yield item

        total = self.api.ultimo_total or 0
        if total == 0 and not vistos:
            if unidade.data > self._ultimo_dia_fechado():
                # DIA AINDA ABERTO: zero não quer dizer "não houve", quer dizer
                # "ainda não houve". Marcar `inexistente` aqui é terminal (o tick
                # não reenfileira) e queimaria o dia inteiro — foi o bloqueio
                # achado na verificação de 16/08/2026. `catalogar` já não cria
                # unidade de dia aberto; esta é a rede para o caminho manual
                # (`--chave`, `--sobrepor`), e falha RETENTÁVEL de propósito.
                raise ColetorError(
                    f'STF: {unidade.data} ainda está em curso (divulgações saem até ~20h30 BRT) '
                    f'e a API devolveu total=0. Colete depois de {self._ultimo_dia_fechado()} — '
                    'zero num dia aberto não é ausência.'
                )
            # Dia FECHADO e sem divulgação: fim de semana, feriado forense,
            # recesso. NÃO é falha — a lição do `_dia_coberto` do DJEN é que
            # tratar ausência como lacuna faz o backfill retentar para sempre.
            raise UnidadeInexistente(f'STF: nenhuma divulgação em {unidade.data}')
        if len(vistos) < total:
            # O `total` da página 1 é o gabarito da própria fonte para a
            # paginação (validado: 742 declarados e 742 ids distintos em 2
            # páginas). Faltar item aqui é perda SILENCIOSA — melhor falhar e
            # deixar a unidade pendente.
            raise ColetorError(
                f'STF {unidade.data}: paginação trouxe {len(vistos)} de {total} declarados'
            )

        # `aproveitados` desconta TODA abstenção — inclusive a data ilegível,
        # que é rara mas existiria em silêncio: sem descontá-la, o gate de
        # cobertura do runner reprovaria uma coleta que fez a coisa certa.
        aproveitados = len(vistos) - sem_cnj - sem_processo_id - sem_data - titulo_divergente
        self.balanco[unidade.chave] = {
            'total_declarado': total,
            'lidos': len(vistos),
            'sem_numero_unico': sem_cnj,
            'sem_processo_id': sem_processo_id,
            'sem_data_legivel': sem_data,
            'titulo_divergente': titulo_divergente,
            'aproveitados': aproveitados,
        }
        logger.info(
            'STF %s: %d lidos, %d sem número único, %d sem processoId, %d sem data, '
            '%d com título divergente → %d graváveis (portal: %d consultas, %d do cache)',
            unidade.data, len(vistos), sem_cnj, sem_processo_id, sem_data,
            titulo_divergente, aproveitados,
            self.resolver.consultas, self.resolver.acertos_cache,
        )

    def esperado(self, unidade: UnidadeColeta) -> int | None:
        """Quantas publicações do dia eram GRAVÁVEIS, medido na própria coleta.

        Ressalva honesta, porque este número é mais fraco que o gabarito do
        DEJT: ele não vem de fora, vem da contagem feita durante a coleta. O que
        ele prova é que nada se perdeu ENTRE o parser e o banco. Quem prova que
        nada se perdeu na FONTE é o fechamento contra o `total` da página 1, que
        já roda dentro de `coletar` e falha alto.

        Não devolvemos o `total` cru aqui de propósito: os ~10% sem número único
        são abstenção deliberada, e reprová-los no gate de 95% faria a unidade
        ser retentada cinco vezes para dar o mesmo resultado correto.
        """
        b = self.balanco.get(unidade.chave)
        return b['aproveitados'] if b else None

    # -- parsing --------------------------------------------------------------
    def _para_item(self, pub: dict, proc) -> ItemDiario | None:
        """Publicação do STF → `ParsedItem` (a mesma forma que o DJEN produz).

        Mapeamento, com o motivo de cada escolha:
          · `data_disponibilizacao` = `divulgacao`. É o análogo exato: o DJe é
            divulgado no dia X e considerado publicado no dia útil seguinte,
            que é o que vem em `publicacao`. Usar `publicacao` deslocaria toda a
            série em um dia em relação ao DJEN.
          · `data_envio` fica VAZIO. No DJEN ele é quando o cartório liberou o
            ato — sempre ANTES da disponibilização. O `publicacao` do STF é
            DEPOIS. Encaixar um no outro seria mentir sobre a semântica da
            coluna, então abstemos.
          · `tipo_documento` = `tipo` ('Decisão Final', 'Despacho', 'Presidência
            Distribuição'); `tipo_comunicacao` = `tipoConteudo` ('Publicação
            Monocrática', 'Publicação Legada do DJe').
          · `nome_orgao` = `colegiado` quando há; senão o `relator`. Numa
            decisão monocrática o órgão prolator É o gabinete do relator, e o
            relator não tem coluna própria em `Movimentacao` — jogá-lo fora
            perderia o dado mais distintivo desta fonte.
          · `nome_classe` vem do portal ('RECURSO EXTRAORDINÁRIO COM AGRAVO') e
            `codigo_classe` fica VAZIO: o código TPU não é conhecido, e
            `persistir_movimentacoes` usa `codigo_classe` como PK de
            `ClasseJudicial` — preencher com 'ARE' poluiria o catálogo nacional
            com código que não é TPU.
          · `status` = `confidencialidade` ('Público' / 'Segredo de Justiça' /
            'Sigiloso'). Vocabulário diferente do 'P' do DJEN, e assumido: é a
            única coluna livre onde o sigilo cabe, e ele é filtrável por
            tribunal ('STF').
          · `link` fica VAZIO: o payload não traz URL da publicação. O que
            existe é o link do PROCESSO no portal, que é outra coisa — a UI diz
            'abrir publicação' e abriria a capa do processo.
          · `texto` VERBATIM, incluindo o XHTML do LibreOffice. É texto nativo,
            sem OCR.
        """
        quando = parse_dt(pub.get('divulgacao'))
        if quando is None:
            logger.warning('STF: publicação %s sem divulgacao parseável (%r)',
                           pub.get('id'), pub.get('divulgacao'))
            return None
        texto = str(pub.get('texto') or '')
        partes, advogados = _envolvidos(pub.get('envolvidos') or [])
        return ItemDiario(
            cnj=proc.cnj,
            external_id=external_id_de(self.slug, pub['id']),
            data_disponibilizacao=quando,
            data_envio=None,
            tipo_comunicacao=str(pub.get('tipoConteudo') or '')[:120],
            tipo_documento=str(pub.get('tipo') or '')[:120],
            nome_orgao=str(pub.get('colegiado') or pub.get('relator') or '')[:255],
            id_orgao=None,
            nome_classe=proc.classe[:255],
            codigo_classe='',
            link='',
            destinatarios=partes,
            destinatario_advogados=advogados,
            texto=texto,
            numero_comunicacao=str(pub.get('codigo') or '')[:120],
            # FINGERPRINT SOBRE O CORPO VISÍVEL, não sobre o documento inteiro.
            # `fingerprint_ato` hasheia os primeiros 4.000 chars normalizados, e
            # no XHTML do LibreOffice que o STF serve o `<body>` só começa por
            # volta do char 4.345: a janela inteira caía no `<head>` + CSS. Medido
            # em 16/08/2026 nas 205 publicações gravadas em dev: 18 (8,8%)
            # dividiam a MESMA janela com outra, e num grupo de 4 havia 3 corpos
            # visíveis distintos. Como a camada 3 da dedupe lê com
            # `DISTINCT ON (processo_id, hash)`, isso APAGARIA publicação legítima
            # na leitura — hash de folha de estilo não é impressão digital de ato.
            hash=fingerprint_ato(proc.cnj, quando, _corpo_visivel(texto)),
            meio='D',
            meio_completo=MEIO_COMPLETO,
            status=str(pub.get('confidencialidade') or '')[:40],
            ativo=True,
        )

    def _checar_drift(self, pub: dict, tribunal) -> None:
        """Uma amostra por unidade basta para pegar mudança de contrato.

        A API não é documentada nem versionada e foi achada por engenharia
        reversa: quando ela mudar, ninguém vai nos avisar. O alerta é o mesmo
        `SchemaDriftAlert` que o DJEN usa — reusar significa que a mudança
        aparece na tela de saúde que a equipe já olha.
        """
        if tribunal is None:
            return
        chaves = set(pub)
        extra = chaves - CHAVES_PUBLICACAO
        faltando = CHAVES_PUBLICACAO - chaves
        if extra:
            registrar_drift(tribunal, SchemaDriftAlert.TIPO_EXTRA, list(extra), pub, None)
        if faltando:
            registrar_drift(tribunal, SchemaDriftAlert.TIPO_MISSING, list(faltando), pub, None)


_RE_BODY = re.compile(r'<body\b[^>]*>(.*)</body\s*>', re.IGNORECASE | re.DOTALL)


def _corpo_visivel(texto: str) -> str:
    """Só o `<body>` do XHTML, para ALIMENTAR O FINGERPRINT — nunca para gravar.

    O que o STF serve é um documento XHTML exportado do LibreOffice, e o
    cabeçalho dele (dublincore + folha de estilo com dezenas de classes `.P1`)
    ocupa ~24% do arquivo. Medido: numa publicação real de 18.753 chars, o
    `<body>` começa no offset 4.502 — depois dos 4.000 chars que
    `fingerprint_ato` usa. Resultado: o fingerprint era o hash do CSS, igual
    para toda publicação do mesmo template.

    O `texto` gravado continua VERBATIM, com o documento inteiro: o que a fonte
    mandou é o que fica no banco. Este recorte existe só para a impressão
    digital do ATO ser do ato.

    Regex e não parser de HTML de propósito: é uma extração de uma marca só,
    roda por publicação num backfill de ~450 mil, e falhar é inofensivo — sem
    `<body>` devolvemos o texto inteiro, que é o comportamento antigo.
    """
    m = _RE_BODY.search(texto or '')
    return m.group(1) if m else (texto or '')


def _confere_processo(pub: dict, proc) -> bool:
    """O processo que o portal devolveu é o mesmo que a publicação diz ser?

    `processoId` é a única cola entre as duas origens, e ele não é verificável
    de outra forma. O título da página (`ARE 1617690`) tem que ser prefixo do
    `processo` da publicação (`ARE 1617690`, `ARE 1617690 Mérito`,
    `Rcl 96448 ED`). Título vazio não reprova: a página de processo antigo às
    vezes vem sem ele, e nesse caso a checagem simplesmente não opina.
    """
    titulo = (proc.titulo or '').strip()
    if not titulo:
        return True
    return str(pub.get('processo') or '').strip().startswith(titulo)


def _envolvidos(envolvidos: list) -> tuple[list, list]:
    """`envolvidos[]` do STF → (`destinatarios`, `destinatario_advogados`).

    O STF é MAIS estruturado que o DJEN aqui: manda polo, categoria e as OABs
    separadas, enquanto o DJEN manda uma string ('1. FULANO (AGRAVANTE)'). A
    tradução preserva a forma que o resto do sistema já lê ('nome'/'polo',
    'advogado.nome'/'numero_oab'/'uf_oab') e ACRESCENTA o que só o STF tem,
    em vez de descartar.
    """
    partes, advogados = [], []
    for e in envolvidos or []:
        nome = str(e.get('nome') or '').strip()
        if not nome:
            continue
        polo_origem = str(e.get('polo') or '')
        categoria = str(e.get('categoria') or '')
        if categoria == CATEGORIA_ADVOGADO:
            oabs = _oabs(e.get('identificacoes') or [])
            numero, uf = oabs[0] if oabs else ('', '')
            advogados.append({
                'advogado': {'nome': nome, 'numero_oab': numero, 'uf_oab': uf},
                # Um advogado do STF costuma ter dezenas de inscrições (vi 26
                # numa só). Emitir uma linha por OAB duplicaria a pessoa; guardar
                # a lista completa aqui não mente e não perde nada.
                'oabs': [{'numero_oab': n, 'uf_oab': u} for n, u in oabs],
                'polo': POLO.get(polo_origem, ''),
            })
        else:
            partes.append({'nome': nome, 'polo': POLO.get(polo_origem, ''),
                           'categoria': categoria, 'polo_origem': polo_origem})
    return partes, advogados


#: Prefixo literal da fonte: `'OAB 15181/PR'` ou `"OAB's (59957/SC, 68488/BA)"`.
RE_PREFIXO_OAB = re.compile(r"^\s*OAB(?:'s)?\s*\(?")
#: Um token já sem o prefixo: tudo até a última barra é NÚMERO, o resto é UF.
RE_OAB = re.compile(r'^(.+?)\s*/\s*([A-Z]{2})$')


def _oabs(identificacoes: list) -> list[tuple[str, str]]:
    """Extrai (número, UF) das duas formas que o STF usa de verdade:
    `'OAB 15181/PR'` e `"OAB's (59957/SC, 68488/BA, 1566 - A/RN, 30067/A/MT)"`.

    Repare nos números sujos: `'A2557'` (prefixo de suplementar), `'31218-A'`,
    `'10.581-A'`, `'30067/A'`, `'1566 - A'`. O sufixo/prefixo FAZ PARTE do
    número da inscrição — por isso o parser tira só o rótulo 'OAB' e a
    pontuação de lista, e devolve o resto verbatim. Normalizar aqui (tirar o
    'A', tirar o ponto) seria alterar o dado da fonte, que é o que a casa não
    faz. A UF é o único campo cobrado com rigor: duas maiúsculas no fim.
    """
    achados = []
    for ident in identificacoes:
        corpo = RE_PREFIXO_OAB.sub('', str(ident)).rstrip(') ')
        for token in corpo.split(','):
            m = RE_OAB.match(re.sub(r'\s+', ' ', token).strip())
            if m:
                achados.append((m.group(1).strip()[:40], m.group(2)))
    return achados
