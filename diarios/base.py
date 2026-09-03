"""Contrato dos coletores de diário oficial — a base que TODA fonte herda.

POR QUE ESTE ARQUIVO EXISTE
===========================
A ingestão do Voyager sempre foi DJEN-only, e o DJEN é veículo de COMUNICAÇÃO
(Res. CNJ 455/2022): só entra processo que teve intimação publicada no diário
NACIONAL. Medido por amostra aleatória de 300 CNJs por tribunal (15-16/08/2026),
falta de 81% (TRF1) a 96% (TJSP) do acervo declarado ao CNJ.

O recon das fontes candidatas (16/08/2026) mostrou que o buraco é sobretudo
TEMPORAL, não geográfico — e é isso que define a arquitetura aqui:

  · DJE/TJSP (e-SAJ) — o TJSP só publica no DJEN desde 14/03/2025 (medido:
    count=0 em TODA data anterior, count>0 a partir dela). São 4.077 edições
    (2007-10-01 → 2025-03-13) que o DJEN estruturalmente NÃO tem. Na janela
    recente a fonte é redundante: 28 de 30 CNJs sorteados de um caderno de
    jul/2025 já estavam no DJEN.
  · DEJT (Justiça do Trabalho) — migrou para o DJEN em 01/08/2024. O caderno do
    TRT3 caiu de 69 MB para 1,5 MB de um dia para o outro; as matérias
    nacionais, de 183.567/dia para 211/dia. A jazida é 2008→31/07/2024.
  · STJ — aderiu ao DJEN em 29/11/2024 e o payload bate 100% com
    `djen/parser.py` (200 itens conferidos chave a chave, zero drift). NÃO é
    fonte nova e NÃO usa este contrato: é ligar um `Tribunal` que já existe.
  · STF — nunca entrou no DJEN; tem API JSON própria. Fonte nova, pequena.
  · DOEs de entes devedores — diário do EXECUTIVO. É sinal de desfecho
    (o ente pagou / convocou para acordo), não porta de acervo: 0 de 30
    publicações aleatórias do DOE-SP contêm CNJ. Não escreve em `Movimentacao`.

Quatro implementações em paralelo só não viram quatro coletores diferentes se
houver contrato. Isto é o contrato. Ele existe para que:

  1. cada fonte seja DONA dos seus arquivos (zero conflito de merge);
  2. ninguém reinvente cliente HTTP, backoff, circuit-breaker, watermark de
     backfill, dedupe ou métrica — tudo isso já foi pago em incidente no DJEN;
  3. a quinta fonte custe um diretório, não uma refatoração.

O QUE ESTE ARQUIVO **NÃO** FAZ
------------------------------
Não baixa PDF, não segmenta caderno, não conhece JSF nem Seam nem e-SAJ. Toda
particularidade de fonte mora em `diarios/fontes/<slug>/`. Se você precisou
mudar este arquivo para implementar a SUA fonte, provavelmente o contrato está
errado — abra a discussão em vez de acrescentar um `if slug == ...`.
"""

import hashlib
import logging
import random
import re
import time
import unicodedata
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime

import requests
from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

# Reuso deliberado: o `ParsedItem` do DJEN JÁ é a forma exata de uma
# `Movimentacao`. Criar um dataclass gêmeo aqui só criaria duas verdades sobre
# o mesmo model. Coletor novo produz ParsedItem, ponto.
from djen.parser import ParsedItem, normalizar_cnj  # noqa: F401 (reexport proposital)
from djen.proxies import sessao_rotativa
from tribunals.models import IngestionRun, Movimentacao, Process, Tribunal, ano_cnj_from_numero

logger = logging.getLogger('voyager.diarios.base')

# `ItemDiario` é só um apelido legível: o que o coletor devolve É um ParsedItem.
ItemDiario = ParsedItem

BATCH_SIZE = 500

# ─────────────────────────────────────────────────────────────────────────────
# 1. NAMESPACE DE FONTE E external_id — a espinha da deduplicação
# ─────────────────────────────────────────────────────────────────────────────
# `Movimentacao` tem UniqueConstraint(tribunal, external_id). Como TODAS as
# portas de um mesmo tribunal (DJEN, DJE próprio, DEJT) gravam com o MESMO
# `tribunal`, o external_id é o único lugar onde a origem pode ser distinguida.
# Sem namespace, o id 4246 do DJE/TJSP colidiria com o id 4246 do DJEN e a
# segunda porta seria silenciosamente engolida pelo ignore_conflicts.
#
# REGRA: todo coletor NOVO prefixa `<slug>:`; o DJEN é o namespace LEGADO e
# continua sem prefixo — são ~1,39 BILHÃO de linhas em produção (medido em
# 20/08/2026; o "65 milhões" que esta linha dizia estava 21× defasado), e
# re-prefixar quebraria a idempotência de toda a ingestão corrente (a
# re-ingestão passaria a duplicar tudo). Legado sem prefixo, novo com prefixo,
# e o prefixo vira o discriminador de leitura — mas NÃO por `LIKE`: a coluna
# está em collation `en_US.UTF-8`, `external_id LIKE 'tjsp-dje:%'` não usa
# índice nenhum e estoura o statement_timeout (medido). O que usa o índice
# `uniq_mov_tribunal_extid` é a FAIXA:
#     tribunal_id = 'TJSP' AND external_id >= 'tjsp-dje:' AND external_id < 'tjsp-dje;'
FONTE_DJEN = 'djen'

#: `Movimentacao.external_id` é CharField(max_length=64). O truncamento
#: silencioso é o pesadelo aqui: dois blocos diferentes viram o mesmo id e um
#: some no ignore_conflicts. Por isso estourar o limite é ERRO, não warning.
MAX_EXTERNAL_ID = 64

#: Quantas observações independentes de "a fonte não tem esta unidade" são
#: exigidas antes de fechar o watermark em `inexistente`, que é TERMINAL e
#: nunca mais é retentado.
#:
#: POR QUE ISTO EXISTE — medido em produção em 03/09/2026. As 5 unidades do
#: `tjsp-dje` que estavam marcadas `inexistente` foram reconferidas contra a
#: fonte viva, com o instrumento mais forte que existe (GET real, olhando os
#: magic bytes): **as 5 devolveram `%PDF`**. Ou seja, os cinco cadernos
#: EXISTEM, e o e-SAJ tinha servido a página de 851 bytes de "Erro ao acessar
#: o caderno selecionado" — HTTP 200, `text/html` — para caderno que ele tem.
#: Uma única observação transitória fechava o watermark PARA SEMPRE, com o run
#: em `success` e o log limpo. São ~1,9% das unidades tentadas (5 em ~260):
#: no lote de 3.823 que este backfill abre, seriam ~73 cadernos, da ordem de
#: 1,8 milhão de publicações, perdidos em silêncio e sem retentativa.
#:
#: O custo do conserto é uma requisição de 851 bytes a mais por unidade que de
#: fato não existe (os cadernos 19 e 20 antes de 2023-11-27, por exemplo) —
#: contra um download de caderno que chega a 62 MB. A assimetria é o argumento.
#:
#: Fica ABAIXO de `MAX_TENTATIVAS` (5, em `diarios/jobs.py`) de propósito: se
#: fosse igual ou maior, a unidade pararia de ser selecionada pelo tick e
#: ficaria `pendente` para sempre — dívida invisível no lugar de um status
#: terminal honesto.
CONFIRMACOES_DE_AUSENCIA = 3

#: A marca que o contador de ausências deixa em `EdicaoDiario.ultimo_erro`.
#: Contar por AÍ, e não por `tentativas`, é deliberado — e a razão veio do
#: dado: as 5 unidades falsamente `inexistente` estavam com `tentativas` 4 e 5,
#: gastas em FALHAS de outra natureza (a `NotNullViolation` do §14). Somar
#: falha com ausência faria uma única observação fechar o watermark de
#: qualquer unidade que já tivesse tropeçado antes — exatamente o caso medido.
#: Como todo outro desfecho reescreve `ultimo_erro`, o que este contador mede é
#: ausência SEGUIDA, que é o que a palavra "confirmada" quer dizer.
MARCA_AUSENCIA = 'ausência NÃO confirmada'
_RE_AUSENCIA = re.compile(re.escape(MARCA_AUSENCIA) + r' \((\d+)/')

_SLUG_RE = re.compile(r'^[a-z0-9][a-z0-9-]{2,15}$')


def _ausencias_seguidas(edicao) -> int:
    """Quantas vezes SEGUIDAS a fonte já disse que não tem esta unidade."""
    m = _RE_AUSENCIA.search(edicao.ultimo_erro or '')
    return int(m.group(1)) if m else 0


def validar_slug(slug: str) -> str:
    """Slug da fonte: `[a-z0-9-]`, 3-16 chars, e nunca 'djen' (reservado)."""
    if not _SLUG_RE.match(slug or ''):
        raise ValueError(f'slug de fonte inválido: {slug!r} (esperado [a-z0-9-]{{3,16}})')
    if slug == FONTE_DJEN:
        raise ValueError("slug 'djen' é reservado pro namespace legado sem prefixo")
    return slug


def external_id_de(fonte: str, *partes: object) -> str:
    """Monta o external_id namespaceado: `<fonte>:<parte>-<parte>-...`.

    As partes têm que ser DETERMINÍSTICAS e derivadas do conteúdo/coordenada na
    fonte — nunca de contador de execução, nunca de timestamp, nunca de índice
    de lista em memória. Motivo: a re-coleta de uma edição (que vai acontecer,
    porque backfill de milhares de edições sempre tem retry) precisa produzir
    exatamente os mesmos ids, senão a re-ingestão duplica em vez de deduplicar.

    Ver `id_bloco_impresso()` para o caso PDF, que é o mais escorregadio.
    """
    validar_slug(fonte)
    ext = f'{fonte}:' + '-'.join(str(p) for p in partes)
    if len(ext) > MAX_EXTERNAL_ID:
        raise ValueError(
            f'external_id com {len(ext)} chars estoura o limite de {MAX_EXTERNAL_ID}: {ext!r}. '
            'Encurte as partes (use hash curto), NÃO trunque — truncar cola dois atos no mesmo id.'
        )
    return ext


def id_bloco_impresso(fonte: str, *coordenada: object, texto: str) -> str:
    """external_id de um bloco recortado de PDF (TJSP e DEJT).

    `coordenada` é a posição física do bloco na fonte (ex.: edição, caderno,
    página) e `texto` é o corpo do ato. O id termina em um hash de 12 hex do
    texto normalizado — e isso é de propósito:

    O ORDINAL DO BLOCO NA PÁGINA NÃO SERVE COMO ID. Ele parece estável, mas
    depende do segmentador; qualquer ajuste no recorte (e vai haver vários,
    porque o layout do caderno mudou ao longo de 16 anos) desloca todos os
    ordinais da página e a re-ingestão duplica a página inteira. O hash do
    conteúdo é imune a isso: mesmo ato ⇒ mesmo id, independentemente de quem
    recortou. E a coordenada continua no id para que o mesmo texto curto em
    páginas diferentes não colapse num id só.
    """
    h = hashlib.sha1(normalizar_texto(texto).encode('utf-8')).hexdigest()[:12]
    return external_id_de(fonte, *coordenada, h)


def fingerprint_ato(cnj: str, quando: date | datetime, texto: str) -> str:
    """Impressão digital do ATO, independente do veículo que o publicou.

    Vai em `Movimentacao.hash` — coluna que existe e **NÃO é indexada**. O
    `models.Index(fields=['hash'])` está declarado no model e ausente do banco
    (conferido em `pg_indexes`, 20/08/2026: 9 índices na tabela, nenhum em
    `hash`). A frase anterior aqui dizia "já existe e já é indexada", e foi ela
    que fez `espelhadas_no_lote` nascer varrendo a fatia inteira do tribunal a
    cada lote. Quem for consultar por `hash` em volume: não dá, e criar o índice
    em 1,39B de linhas não é decisão de docstring.

    Serve para a pergunta "esta publicação do DJE/TJSP é o mesmo ato que aquela
    do DJEN?" — que é a pergunta da deduplicação entre portas.

    RESSALVA HONESTA, para não vender o que não entrega: as ~1,39B de linhas
    legadas do DJEN têm em `hash` o hash OPACO da própria API, não este. Logo
    este fingerprint só casa entre fontes NOVAS, ou depois de um backfill de
    fingerprint que ninguém aprovou. A dedupe entre DJEN e diário próprio é
    resolvida ANTES, por janela temporal (ver `ColetorDiario.janela_*`), que é
    de graça e foi medida; este campo é o plano B para a sobreposição
    inevitável, não o mecanismo principal.
    """
    d = quando.date() if isinstance(quando, datetime) else quando
    corpo = normalizar_texto(texto)[:4000]
    return hashlib.sha1(f'{cnj}|{d.isoformat()}|{corpo}'.encode()).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# 2. TEXTO DE PDF — o ruído que come 8% dos processos
# ─────────────────────────────────────────────────────────────────────────────
# Medido no caderno 12 do DJE/TJSP de 21/07/2025: a regex ESTRITA de CNJ acha
# 4.722 ocorrências; a tolerante a espaço acha 5.136. São 414 processos (8,1%)
# que sumiriam em silêncio — o pior tipo de perda, porque o coletor reporta
# "sucesso". A causa é o PDF justificado com kerning por caractere: o extrator
# injeta espaço no meio da palavra ('Banco Rodoben s S/A', 'Agra vado').
#
# Por isso a regex tolerante mora AQUI, e não em cada fonte: TJSP e DEJT sofrem
# do mesmo problema, e uma correção feita duas vezes vira duas correções
# diferentes.

#: CNJ com espaço espúrio permitido entre QUALQUER caractere do número.
CNJ_TOLERANTE = re.compile(
    r'\d\s*\d\s*\d\s*\d\s*\d\s*\d\s*\d\s*-\s*\d\s*\d\s*\.\s*'
    r'\d\s*\d\s*\d\s*\d\s*\.\s*\d\s*\.\s*\d\s*\d\s*\.\s*\d\s*\d\s*\d\s*\d'
)


def dv_cnj_valido(cnj: str) -> bool:
    """Confere o dígito verificador do CNJ (Res. CNJ 65/2008, módulo 97 base 10).

    POR QUE ISTO EXISTE — a tolerância a espaço tem um preço
    -------------------------------------------------------
    `CNJ_TOLERANTE` aceita espaço entre quaisquer dígitos, e isso é o que
    recupera os 8% de processos que o kerning do PDF esconde. O efeito colateral
    é que ela também casa um PEDAÇO de um número maior: no texto real
    'Processo 991000001-11.2015.8.26.0100' (dois dígitos colados pelo kerning) a
    regex devolvia '1000001-11.2015.8.26.0100', que é o CNJ de OUTRO processo —
    grudar no processo errado, que a casa proíbe explicitamente. O DV pega isso:
    um número decapitado quase nunca fecha o módulo 97.

    Incidência medida (16/08/2026): 2 CNJs com DV inválido em 35.289 itens do
    DJE/TJSP, 0 em 20.933 do DEJT, 0 em 354 dos diários de entes. Parece nada até
    multiplicar pelas centenas de milhões de linhas do backfill — e numa missão
    cujo objetivo é MEDIR acervo, processo fantasma é o pior erro possível.
    """
    d = re.sub(r'\D', '', cnj or '')
    if len(d) != 20:
        return False
    sequencial, dv, resto = d[:7], d[7:9], d[9:]
    return int(dv) == 98 - (int(sequencial + resto) * 100) % 97


def _colado_em_digito(texto: str, inicio: int, fim: int) -> bool:
    """O casamento é pedaço de um número maior?

    Adjacência ESTRITA (sem pular espaço) de propósito: no caderno do TJSP a
    tabela imprime 'Fulano 0501276-27.2026.8.02.9003 09:00 1' e o '0' do horário
    fica a um espaço do CNJ — pular o espaço reprovaria dado bom. O caso que
    esta guarda existe para matar é o do dígito COLADO
    ('Processo 991000001-11.2015.8.26.0100'), que a regex tolerante decapita e
    entrega como o CNJ de outro processo.
    """
    return bool((inicio > 0 and texto[inicio - 1].isdigit())
                or (fim < len(texto) and texto[fim].isdigit()))


def achar_cnjs(texto: str, conferir_dv: bool = True) -> list[str]:
    """Todos os CNJs do texto, tolerando o espaço espúrio do PDF, na ordem de
    aparição e já normalizados para NNNNNNN-DD.AAAA.J.TR.OOOO.

    Descarta (em silêncio, porque quem conta abstenção é o coletor) o que é
    pedaço de um número maior e o que não fecha o dígito verificador — ver
    `dv_cnj_valido`. `conferir_dv=False` existe para sonda/diagnóstico que
    queira ver o bruto, nunca para gravação.
    """
    achados = []
    vistos = set()
    texto = texto or ''
    for m in CNJ_TOLERANTE.finditer(texto):
        if _colado_em_digito(texto, m.start(), m.end()):
            continue
        cnj = re.sub(r'\s+', '', m.group(0))
        if conferir_dv and not dv_cnj_valido(cnj):
            continue
        if cnj not in vistos:
            vistos.add(cnj)
            achados.append(cnj)
    return achados


def normalizar_texto(texto: str) -> str:
    """Normalização usada SÓ para hash/comparação — nunca para gravar.

    O `texto` da `Movimentacao` é verbatim, é o que a extração vai ler, e a
    casa exige verbatim. Esta função existe só para que duas capturas do mesmo
    ato (uma com quebra de linha diferente, outra com espaço a mais) produzam
    o mesmo fingerprint.
    """
    s = unicodedata.normalize('NFKC', texto or '')
    return re.sub(r'\s+', ' ', s).strip().lower()


# ─────────────────────────────────────────────────────────────────────────────
# 3. ERROS — a diferença entre "não existe" e "falhou" é a diferença entre um
#    backfill que termina e um que retenta para sempre
# ─────────────────────────────────────────────────────────────────────────────
class ColetorError(Exception):
    """Falha de coleta recuperável: vale retentar mais tarde."""


class RespostaInvalida(ColetorError):  # noqa: N818 — nomes de erro em pt-BR, sem sufixo Error
    """HTTP 200 que NÃO é dado. Confirmado nas três fontes sondadas:
      · e-SAJ: `consultaSimples.do` devolve 200 com 1.207 bytes de <frameset>
        vazio; caderno inexistente devolve 200 + text/html + 851 bytes de
        "Erro ao acessar o caderno"; página acima da última devolve 200 com
        CORPO VAZIO (0 bytes).
      · DEJT: postar o clique na URL com conversationId abre conversa aninhada
        no Seam e devolve 200 com o formulário EM BRANCO.
      · Querido Diário / DOE-RS: qualquer path inventado devolve 200 com o
        index.html da SPA (20.942 / 2.700 bytes).
    Regra da casa daqui em diante: validar CONTEÚDO (magic bytes, tamanho
    mínimo, âncora esperada). Status code não é evidência de nada.
    """


class UnidadeInexistente(ColetorError):  # noqa: N818
    """A unidade legitimamente não existe (feriado forense, recesso, caderno
    que aquele tribunal não publica naquele dia). NÃO é falha: o watermark
    marca `inexistente` e nunca mais tenta.

    Isto é a lição do `_dia_coberto` do djen/jobs.py, que já pagou este bug:
    tratar ausência como lacuna faz o backfill retentar o mesmo dia até o fim
    dos tempos. Medido no DEJT: 14/08/2023, 12/03/2022 e 03/03/2025 devolvem
    zero linhas — são Carnaval e feriado forense, com dias vizinhos cheios.
    """


class UnidadeSemDadoAproveitavel(ColetorError):  # noqa: N818
    """A unidade EXISTE, baixou, validou — e nada dela é aproveitável.

    Nasceu de um achado da verificação adversarial (16/08/2026): o caderno 12 do
    DJE/TJSP de 15/06/2009 tem 16.952 blocos de publicação REAL, e todos são da
    era pré-CNJ ('583.00.2009.161101'). O coletor descartava os 16.952 — o que é
    certo, porque sem de-para não há como casar com `Process.numero_cnj` — mas a
    unidade fechava como `VAZIA`, cujo contrato é "baixou e NÃO HAVIA
    publicação". Com `itens_gravados=0`, `ultimo_erro=''`, status terminal e um
    log dizendo 'cobertura de CNJ 0/0 = 100.0%', 16.952 publicações viravam
    lacuna invisível — exatamente o que este projeto inteiro existe para não
    fazer.

    Este erro separa os dois fatos: NÃO HAVIA (vazia) contra HAVIA E NÃO SERVE
    (`EdicaoDiario.SEM_APROVEITAMENTO`). O status é TERMINAL como o
    `inexistente` — retentar não muda o resultado, e são cadernos de 15 MB —,
    mas fica com o motivo escrito e contável na dashboard: é dívida VISÍVEL,
    não sucesso falso.
    """


class FonteOcupada(ColetorError):  # noqa: N818
    """Circuito ABERTO para esta fonte: ela vinha 5xx-ando e as buscas estão
    pausadas por um cooldown. Fast-fail sem tocar no servidor. O job trata como
    ADIAR, nunca como falhar — mesma mecânica do `DjenBusyError` que curou o
    incidente 2026-07-10 (nós éramos parte da sobrecarga)."""


class ColetaPausada(ColetorError):  # noqa: N818
    """Kill switch acionado (global ou por fonte). Ver `pausar()`."""


# ─────────────────────────────────────────────────────────────────────────────
# 4. CIRCUIT-BREAKER E KILL SWITCH — por fonte, compartilhados pela frota
# ─────────────────────────────────────────────────────────────────────────────
# Cópia consciente da mecânica de djen/client.py, com a chave parametrizada
# pela fonte: um DEJT sobrecarregado não pode pausar o coletor do STF.
#
# E aqui vale o corolário do incidente do DJEN, que se aplica em dobro a estas
# fontes: nenhuma delas tem rate limit, nenhuma tem WAF, nenhuma tem robots.txt.
# Ou seja, o servidor NÃO vai nos defender de nós mesmos. 765 GB puxados no talo
# de um JBoss de 2010 do CSJT é negação de serviço acidental. O teto é nosso.
class CircuitBreaker:
    def __init__(self, fonte: str, limiar: int = 15, janela: int = 120, cooldown: int = 300):
        self.fonte = fonte
        self.limiar = int(getattr(settings, 'DIARIOS_CIRCUITO_LIMIAR', limiar))
        self.janela = int(getattr(settings, 'DIARIOS_CIRCUITO_JANELA', janela))
        self.cooldown = int(getattr(settings, 'DIARIOS_CIRCUITO_COOLDOWN', cooldown))
        self._k_open = f'diarios:circuito:{fonte}'
        self._k_5xx = f'diarios:5xx:{fonte}'

    def aberto(self) -> bool:
        from django.core.cache import cache
        try:
            return bool(cache.get(self._k_open))
        except Exception:
            return False

    def registrar_5xx(self) -> None:
        from django.core.cache import cache
        try:
            try:
                n = cache.incr(self._k_5xx)
            except ValueError:
                cache.set(self._k_5xx, 1, timeout=self.janela)
                n = 1
            if n >= self.limiar and not cache.get(self._k_open):
                cache.set(self._k_open, True, timeout=self.cooldown)
                logger.error('circuito ABERTO em %s — %d 5xx na janela; pausando %ds',
                             self.fonte, n, self.cooldown)
        except Exception:
            pass

    def registrar_sucesso(self) -> None:
        from django.core.cache import cache
        try:
            cache.delete(self._k_5xx)
            cache.delete(self._k_open)
        except Exception:
            pass


PAUSA_KEY = 'diarios:pausados'


def pausados() -> set[str]:
    """Fontes pausadas. `'*'` pausa tudo. Mesma ideia do
    `set_varredura_pausados` do Datajud: dá pra parar em segundos, sem deploy —
    e num backfill de centenas de GB contra servidor de terceiro isso não é
    luxo, é obrigação."""
    from django.core.cache import cache
    try:
        return set(cache.get(PAUSA_KEY) or [])
    except Exception:
        return set()


def pausar(slugs: set[str]) -> None:
    from django.core.cache import cache
    cache.set(PAUSA_KEY, sorted(slugs), timeout=None)


def checar_pausa(fonte: str) -> None:
    p = pausados()
    if '*' in p or fonte in p:
        raise ColetaPausada(f'coleta de {fonte} pausada por kill switch')


# ─────────────────────────────────────────────────────────────────────────────
# 5. SESSÃO HTTP — uma só, com as três estratégias de IP que as sondas exigiram
# ─────────────────────────────────────────────────────────────────────────────
PROXY_DIRETO = 'direto'          # sai pelo IP da máquina
PROXY_ROTATIVO = 'rotativo'      # 1 proxy do pool POR REQUEST (padrão DJEN)
PROXY_PRESO = 'preso'            # 1 proxy escolhido e MANTIDO por toda a sessão


class SessaoDiario:
    """Cliente HTTP dos coletores de diário. Um só, para as quatro fontes.

    Por que não reusar `DJENClient` direto: ele é GET-com-params-de-data sobre
    uma API JSON, com o cap de 10k e o split por ufOab embutidos. Aqui as
    fontes são POST de postback JSF com estado, download de PDF de 62 MB e
    CSRF de Spring. O que se reusa é a ESTRATÉGIA (backoff, rotação, breaker,
    marcação de proxy ruim), e o pool de proxies em si — que continua sendo o
    `ProxyScrapePool.singleton()` compartilhado via Redis.

    Sobre proxy, o que as sondas mediram (16/08/2026) e que contradiz o DJEN:
      · dje.tjsp.jus.br, dejt.jt.jus.br, digital.stf.jus.br, do-api-web-search
        e api.queridodiario responderam 200 de IP de DATACENTER
        (AS28666 HOSTLOCATION), sem um único 403/429. Nenhuma precisa de Cortex.
        Default = PROXY_DIRETO. Isso tira o SPOF do Cortex do caminho.
      · o DEJT tem sessão sticky no ALB (o JSESSIONID carrega o backend) e
        conversa Seam: trocar de IP no meio do fluxo de 3 passos QUEBRA a
        sessão. Para ele, PROXY_PRESO — o padrão de rotação por request do
        DJEN é ANTI-padrão aqui.
    """

    def __init__(self, fonte: str, modo_proxy: str = PROXY_DIRETO,
                 rps: float = 2.0, user_agent: str | None = None,
                 verificar_tls: bool = True, max_retries: int = 5):
        self.fonte = fonte
        self.modo_proxy = modo_proxy
        # Teto AUTO-IMPOSTO. Nenhuma destas fontes tem rate limit; o número
        # baixo é higiene, não obrigação técnica.
        self.rps = float(getattr(settings, f'DIARIOS_RPS_{fonte.upper().replace("-", "_")}', rps))
        # Identificar-se é decisão da casa: o dje.tjsp aceitou
        # `User-Agent: voyager-ops/1.0` com HTTP 200 (o esaj.tjsp, não). Sem
        # robots.txt não há permissão nem proibição — resta a conduta.
        self.user_agent = user_agent or getattr(
            settings, 'DIARIOS_USER_AGENT', 'voyager-ops/1.0 (+https://voyager.was.dev.br)')
        # verify=False é o padrão da casa em `dashboard/fontes_publicas.py::_get`
        # porque BA e PR servem cadeia TLS quebrada. Continua sendo exceção
        # explícita por fonte, nunca default global.
        self.verificar_tls = verificar_tls
        self.max_retries = max_retries
        self.timeout = (
            int(getattr(settings, 'DIARIOS_TIMEOUT_CONNECT', 15)),
            int(getattr(settings, 'DIARIOS_TIMEOUT_READ', 180)),  # PDF de 62 MB
        )
        self.breaker = CircuitBreaker(fonte)
        self.session = sessao_rotativa()   # cache de proxies limitado — ver AdaptadorProxyLimitado
        self._proxy_preso: str | None = None
        self._ultimo_request = 0.0

    # -- proxy ----------------------------------------------------------------
    def _proxies(self) -> dict | None:
        if self.modo_proxy == PROXY_DIRETO:
            return None
        from djen.proxies import ProxyScrapePool
        pool = ProxyScrapePool.singleton()
        if self.modo_proxy == PROXY_PRESO:
            if self._proxy_preso is None:
                self._proxy_preso = pool.get()
            url = self._proxy_preso
        else:
            url = pool.get()
        return {'http': url, 'https': url} if url else None

    def descartar_proxy(self) -> None:
        """Marca o proxy atual como ruim. Em PROXY_PRESO isso invalida a sessão
        inteira — quem chamou tem que refazer o fluxo do zero (no DEJT, refazer
        GET + POST de busca), porque o JSESSIONID vale para aquele IP."""
        if self._proxy_preso:
            from djen.proxies import ProxyScrapePool
            ProxyScrapePool.singleton().mark_bad(self._proxy_preso)
            self._proxy_preso = None

    # -- rate limit auto-imposto ---------------------------------------------
    def _respirar(self) -> None:
        if self.rps <= 0:
            return
        alvo = 1.0 / self.rps
        delta = time.monotonic() - self._ultimo_request
        if delta < alvo:
            time.sleep(alvo - delta + random.uniform(0, 0.15))  # jitter: sem thundering herd
        self._ultimo_request = time.monotonic()

    # -- request --------------------------------------------------------------
    def request(self, metodo: str, url: str, **kw) -> requests.Response:
        """GET/POST com backoff, rotação de proxy e circuit-breaker.

        Devolve a Response CRUA de propósito: quem sabe se o corpo é dado ou
        casca é a fonte (ver `exigir_pdf`/`exigir_ancora`). Esta camada só
        garante que houve resposta.
        """
        checar_pausa(self.fonte)
        if self.breaker.aberto():
            raise FonteOcupada(f'{self.fonte}: circuito aberto — coleta adiada')

        headers = {'User-Agent': self.user_agent}
        headers.update(kw.pop('headers', None) or {})
        tentativa = 0
        while True:
            self._respirar()
            try:
                resp = self.session.request(
                    metodo, url, headers=headers, proxies=self._proxies(),
                    timeout=kw.pop('timeout', self.timeout),
                    verify=self.verificar_tls, **kw,
                )
            except (requests.ConnectionError, requests.Timeout,
                    requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.ContentDecodingError) as exc:
                tentativa += 1
                self.descartar_proxy()
                if tentativa > self.max_retries:
                    raise ColetorError(f'{self.fonte}: transporte após {tentativa} tentativas: {exc}') from exc
                self._backoff(tentativa)
                continue

            if resp.status_code in (403, 429):
                tentativa += 1
                self.descartar_proxy()
                if tentativa > self.max_retries:
                    raise ColetorError(f'{self.fonte}: {resp.status_code} após {tentativa} tentativas')
                logger.warning('%s %s em %s → tentativa %d', self.fonte, resp.status_code, url, tentativa)
                self._backoff(tentativa)
                continue

            if 500 <= resp.status_code < 600:
                self.breaker.registrar_5xx()
                tentativa += 1
                if tentativa > self.max_retries:
                    raise ColetorError(f'{self.fonte}: {resp.status_code} após {tentativa} tentativas')
                self._backoff(tentativa, fator=3.0, teto=180.0)
                continue

            if 400 <= resp.status_code < 500:
                raise ColetorError(f'{self.fonte}: HTTP {resp.status_code} em {url}')

            self.breaker.registrar_sucesso()
            return resp

    def get(self, url: str, **kw) -> requests.Response:
        return self.request('GET', url, **kw)

    def post(self, url: str, **kw) -> requests.Response:
        return self.request('POST', url, **kw)

    def _backoff(self, tentativa: int, fator: float = 1.0, teto: float = 60.0) -> None:
        time.sleep(min(teto, 3.0 * fator * (2 ** tentativa) + random.uniform(0, 2)))


# -- validadores de conteúdo (o antídoto do "200 que não é dado") -------------
def exigir_pdf(corpo: bytes, min_bytes: int = 2048, contexto: str = '') -> bytes:
    """PDF de verdade: magic bytes + tamanho mínimo. Nunca status code.

    O 851-byte de "Erro ao acessar o caderno selecionado" do e-SAJ vem com
    HTTP 200; num backfill de 4.000 edições ele vira lacuna INVISÍVEL.
    """
    if not corpo or len(corpo) < min_bytes:
        raise RespostaInvalida(f'{contexto}: corpo com {len(corpo or b"")} bytes (< {min_bytes})')
    if not corpo.lstrip()[:5].startswith(b'%PDF'):
        raise RespostaInvalida(f'{contexto}: não começa com %PDF (provável HTML de erro)')
    return corpo


def exigir_ancora(texto: str, ancora: str, contexto: str = '') -> str:
    """Exige que a resposta contenha uma âncora que SÓ existe quando há dado
    (ex.: no DEJT, uma `<tr class="linhapar">`; no e-SAJ, o `var diarios =`).
    Serve para pegar a SPA-catch-all e o formulário em branco do Seam."""
    if ancora not in (texto or ''):
        raise RespostaInvalida(f'{contexto}: âncora {ancora!r} ausente ({len(texto or "")} bytes)')
    return texto


def exigir_chaves(payload: dict, chaves: set[str], contexto: str = '') -> dict:
    """Exige as chaves mínimas de um payload JSON de API não-contratada
    (STF e DOE-SP foram descobertos por engenharia reversa de bundle JS: não
    têm documentação nem versionamento — a única defesa é falhar alto quando
    o contrato mudar)."""
    faltando = chaves - set(payload or {})
    if faltando:
        raise RespostaInvalida(f'{contexto}: chaves ausentes no payload: {sorted(faltando)}')
    return payload


# ─────────────────────────────────────────────────────────────────────────────
# 6. UNIDADE DE COLETA — a granularidade do watermark
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class UnidadeColeta:
    """O menor pedaço da fonte que dá pra baixar, validar e reprocessar sozinho.

    No DJEN a unidade é (tribunal, dia) — por isso o backfill de lá é por dia.
    Aqui NÃO é dia: é edição-caderno. Um dia do DJE/TJSP são 9 cadernos
    independentes (um deles com 2.001 páginas); um dia do DEJT são 25 cadernos,
    um por tribunal. Tratar "o dia" como unidade obrigaria a re-baixar 68 MB
    porque um caderno falhou — e impediria paralelizar por caderno, que é o
    paralelismo natural e educado (por tribunal, não por página).

    `chave` é o identificador determinístico da unidade DENTRO da fonte e é o
    que vai para `EdicaoDiario.chave`. Precisa ser reconstruível a partir do
    catálogo, sem estado.
    """
    chave: str
    data: date
    tribunal_sigla: str | None = None
    rotulo: str = ''
    #: tudo que o coletor precisa para baixar esta unidade (cdCaderno, nuDiario,
    #: edição, id do POST...). Vai gravado no banco — é o que permite reprocessar
    #: uma unidade meses depois sem re-catalogar a fonte inteira.
    meta: dict = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# 7. O CONTRATO
# ─────────────────────────────────────────────────────────────────────────────
DESTINO_MOVIMENTACAO = 'movimentacao'
DESTINO_PROPRIO = 'proprio'  # a fonte grava em model dela (DOE de ente devedor)


class ColetorDiario(ABC):
    """Interface que TODA fonte de diário implementa.

    O ciclo é sempre o mesmo, e é o que o runner executa:

        catalogar(inicio, fim) → [UnidadeColeta]      (barato, idempotente)
              ↓ grava/atualiza EdicaoDiario (watermark durável)
        coletar(unidade)       → Iterator[ItemDiario] (caro, retentável)
              ↓ persistir() → Movimentacao + IngestionRun(fonte=slug)

    Separar catálogo de coleta não é purismo: nas duas fontes grandes o catálogo
    inteiro sai em UMA requisição (e-SAJ: 4.162 edições no `var diarios` do
    cabecalho.do; DEJT: 95.679 linhas numa busca de 18 anos). Catalogar primeiro
    e medir ANTES de baixar centenas de GB é o gate mais barato que existe.
    """

    # -- identidade -----------------------------------------------------------
    slug: str = ''                  # ex.: 'tjsp-dje' — vira prefixo de external_id
    nome: str = ''                  # humano, aparece na dashboard
    destino: str = DESTINO_MOVIMENTACAO

    # -- janela de EXCLUSIVIDADE (a dedupe principal; ver seção 8) ------------
    #: período em que ESTA fonte é a porta que o DJEN não cobre. Fora dela o
    #: runner recusa a coleta, a não ser com `sobrepor=True` explícito.
    janela_inicio: date | None = None
    janela_fim: date | None = None

    # -- SEGUNDO EIXO DO GATE (ver `diarios/inventario.py`) -------------------
    #: As linhas que ABREM um registro na fonte, e o `Bloco.formato` que cada
    #: uma tem que virar. Vazio = a fonte não declara marcador, e então o eixo
    #: se ABSTÉM — nunca reporta 100%.
    #:
    #: Existe porque o eixo de PROPORÇÃO (cobertura de CNJ) é estruturalmente
    #: cego para a perda pequena: medido em 02/09/2026, a pauta numerada do
    #: caderno 19 do DJE/TJSP passou calada em 22 de 22 edições verdes, com
    #: 7.917 registros, todos entre 0,60% e 4,54% dos CNJs — abaixo da folga do
    #: piso de 95%. Um formato inteiro desconhecido, de volume modesto,
    #: atravessa todas as edições sem acender luz.
    #:
    #: A contagem é feita sobre o TEXTO EXTRAÍDO, nunca sobre a saída do
    #: segmentador: comparar o parser consigo mesmo é circular.
    MARCADORES_DE_REGISTRO: tuple = ()

    # -- conduta de rede ------------------------------------------------------
    modo_proxy: str = PROXY_DIRETO
    rps: float = 2.0
    verificar_tls: bool = True
    #: (hora_inicio, hora_fim) em que o backfill pesado pode rodar. None = sempre.
    #: Existe porque não há rate limit do outro lado: a educação é nossa.
    janela_horaria: tuple[int, int] | None = None

    def __init__(self):
        validar_slug(self.slug)
        self.sessao = SessaoDiario(
            fonte=self.slug, modo_proxy=self.modo_proxy, rps=self.rps,
            verificar_tls=self.verificar_tls,
        )

    # -- o que cada fonte PRECISA implementar --------------------------------
    @abstractmethod
    def catalogar(self, data_inicio: date, data_fim: date) -> Iterator[UnidadeColeta]:
        """Enumera as unidades existentes no período, SEM baixar o conteúdo.

        Tem que ser idempotente e barato. Se a fonte declara que uma unidade não
        existe naquele dia (feriado forense), simplesmente não a devolva — o
        runner marca as ausentes como `inexistente` e não retenta.
        """

    @abstractmethod
    def coletar(self, unidade: UnidadeColeta) -> Iterator[ItemDiario]:
        """Baixa a unidade e devolve as publicações já parseadas.

        Obrigações do implementador, todas verificáveis em teste:
          · validar CONTEÚDO da resposta (`exigir_pdf`/`exigir_ancora`);
          · `external_id` via `external_id_de`/`id_bloco_impresso` — nunca
            ordinal solto, nunca contador;
          · `hash` = `fingerprint_ato(cnj, data, texto)`;
          · `texto` VERBATIM (normalização só entra no hash);
          · `meio`/`meio_completo` identificando o veículo (ex.: 'D' /
            'DJE/TJSP (e-SAJ)') — é o que deixa a origem legível na UI;
          · levantar `UnidadeInexistente` quando a fonte disser que não há
            edição, em vez de devolver zero itens em silêncio.
        """

    # -- ganchos opcionais ----------------------------------------------------
    def esperado(self, unidade: UnidadeColeta) -> int | None:
        """Quantos itens a PRÓPRIA FONTE declara para esta unidade, quando ela
        declara. É o gabarito de graça: no DEJT, a pesquisa avançada informa
        "1 até 20 de 16.717" para o TRT3 de 10/07/2024, então o segmentador tem
        um alvo mecânico em vez de opinião. Devolve None quando não há gabarito
        (o e-SAJ não tem)."""
        return None

    def tribunal_de(self, unidade: UnidadeColeta) -> Tribunal | None:
        if not unidade.tribunal_sigla:
            return None
        return Tribunal.objects.filter(sigla=unidade.tribunal_sigla).first()

    def persistir(self, itens: list[ItemDiario], unidade: UnidadeColeta,
                  run: IngestionRun | None) -> tuple[int, int]:
        """Grava o lote. Default escreve `Movimentacao`; fontes com destino
        próprio (DOE de ente devedor, que não tem tribunal) sobrescrevem."""
        tribunal = self.tribunal_de(unidade)
        if tribunal is None:
            raise ColetorError(
                f'{self.slug}: unidade {unidade.chave} sem tribunal e destino=movimentacao. '
                'Fonte sem tribunal tem que declarar destino=DESTINO_PROPRIO e sobrescrever persistir().'
            )
        return persistir_movimentacoes(itens, tribunal, run)

    # -- conveniência (é a assinatura que o resto do sistema enxerga) ---------
    def iter_publicacoes(self, data_inicio: date, data_fim: date) -> Iterator[ItemDiario]:
        """Todas as publicações do período, atravessando as unidades.

        Use em teste/sonda/reprocesso pontual. O caminho de produção é o runner
        (unidade a unidade), porque só ele tem watermark, retry e métrica.
        """
        for unidade in self.catalogar(data_inicio, data_fim):
            yield from self.coletar(unidade)

    # -- janela ---------------------------------------------------------------
    def dentro_da_janela(self, d: date) -> bool:
        if self.janela_inicio and d < self.janela_inicio:
            return False
        return not (self.janela_fim and d > self.janela_fim)


# ─────────────────────────────────────────────────────────────────────────────
# 8. DEDUPLICAÇÃO ENTRE PORTAS — o que acontece quando o mesmo ato vem por duas
# ─────────────────────────────────────────────────────────────────────────────
# Três camadas, da mais barata pra mais cara. Só a primeira é obrigatória.
#
# (1) JANELA TEMPORAL — a dedupe principal, custo zero, medida.
#     Cada fonte declara `janela_inicio`/`janela_fim` = o período em que ela é a
#     ÚNICA porta. Fora disso, o runner recusa. Não é arbítrio, é medição:
#       · TJSP: DJEN devolve count=0 em toda data até 2025-03-13 e count>0 a
#         partir de 2025-03-14 ⇒ janela do DJE/TJSP = 2007-10-01 → 2025-03-13.
#       · DEJT: o próprio site avisa que os cadernos judiciários migraram para o
#         DJEN em 01/08/2024, e o caderno do TRT3 despencou de 69 MB para 1,5 MB
#         nesse dia ⇒ janela = 2008-06-09 → 2024-07-31.
#     Com isso, no caso normal, a interseção é VAZIA e não há o que deduplicar.
#
# (2) NAMESPACE NO external_id — quando a sobreposição é desejada.
#     Há sobreposição legítima: as atas e pautas de sessão que o DEJT continua
#     publicando depois de 01/08/2024 e que o DJEN explicitamente NÃO carrega.
#     Nesse caso as duas linhas coexistem, distinguidas pelo prefixo
#     (`dejt:...` vs id nu do DJEN). Isso é CORRETO, não é bug: são dois
#     veículos, dois textos verbatim, duas evidências. Sobrescrever um com o
#     outro destruiria o verbatim, que a casa exige.
#
# (3) FINGERPRINT — para PAREAR o que coexiste, nunca para apagar.
#     `Movimentacao.hash` recebe `fingerprint_ato(cnj, data, texto)`. Duas
#     linhas do mesmo processo, mesma data e mesmo texto normalizado têm o mesmo
#     hash ⇒ a leitura deduplica com `DISTINCT ON (processo_id, hash)`, e o
#     runner conta quantas entraram já espelhadas. Não casa com o legado do
#     DJEN (que guarda ali o hash opaco da API) — dito em `fingerprint_ato`,
#     e é exatamente por isso que a camada (1) é a que decide.
#
# O QUE NÃO FAZER, e por quê:
#   · NÃO adicionar coluna `fonte` em `Movimentacao`: são ~1,39B de linhas num
#     Postgres que a documentação já classifica como disk-I/O-bound. O prefixo
#     do external_id já discrimina, e `meio_completo` já dá o rótulo humano.
#   · NÃO apagar/atualizar a linha do DJEN quando o diário próprio trouxer o
#     mesmo ato. Ingestão é append-only; quem resolve conflito é a leitura.
ESPELHADAS_TIMEOUT = '3s'


def espelhadas_no_lote(itens: list[ItemDiario], tribunal: Tribunal) -> int | None:
    """Quantos atos do lote a outra porta JÁ tinha trazido. `None` = não sei.

    Isto media por `hash` e era duas coisas erradas ao mesmo tempo (medido em
    20/08/2026, com a ingestão de diários já ligada nos 59 tribunais):

    1. **Não podia acertar.** `fingerprint_ato` devolve sha1 — 40 caracteres.
       O `hash` das linhas do DJEN, quando não é vazio, tem 30: é o hash opaco
       da API (`djen/parser.py:243`). Uma string de 40 nunca é igual a uma de
       30, então `hash__in=[...]` casava com NADA. A métrica retornava 0 por
       construção, e 0 aqui lê-se "o diário próprio não repete o DJEN" — a
       conclusão oposta da verdade, com a autoridade de um número.
       O teste não pegava porque construía o item do DJEN COM o fingerprint,
       que é o que a produção não faz.

    2. **Custava caro pra errar.** `hash` não tem índice — o
       `models.Index(fields=['hash'])` está declarado no model e ausente do
       banco. EXPLAIN do lote de 200: custo 73.427.276 no TJSP (varredura da
       fatia inteira do tribunal), 6.980.195 até no TJAC. Por lote, dentro do
       caminho de escrita, sem teto de espera.

    O par que os dois veículos REALMENTE compartilham é (processo, data): o
    mesmo ato, publicado no mesmo dia, com textos verbatim diferentes. É por aí
    que a `janela_*` já deduplica, e é o que `mov_processo_data_disp_idx` — que
    existe — sabe responder.

    Continua sendo APROXIMAÇÃO, e por cima: dois atos distintos do mesmo
    processo no mesmo dia contam como um espelhamento. Isso é dito aqui e no
    log; superestimar sobreposição erra pro lado seguro (subestimar venderia
    ineditismo que não temos).
    """
    import datetime

    from django.db import OperationalError
    from django.utils import timezone as _tz

    if not itens:
        return 0

    por_data: dict = {}
    for i in itens:
        if not i.cnj or not i.data_disponibilizacao:
            continue
        d = i.data_disponibilizacao
        # `localdate`, não `.date()`: o `__date` do ORM converte pra
        # America/Sao_Paulo antes de truncar. Extrair a data em UTC aqui faria
        # o lote de meia-noite cair no dia anterior do outro lado, e a métrica
        # perderia justamente a sobreposição que veio buscar.
        por_data.setdefault(_tz.localdate(d) if _tz.is_aware(d) else d, set()).add(i.cnj)
    if not por_data:
        return 0

    cnjs = {c for grupo in por_data.values() for c in grupo}
    por_cnj = dict(Process.objects.filter(tribunal=tribunal, numero_cnj__in=cnjs)
                   .values_list('numero_cnj', 'pk'))
    if not por_cnj:
        return 0                      # nenhum processo do lote é conhecido ⇒ tudo inédito

    ext_ids = {i.external_id for i in itens}
    total = 0
    try:
        with transaction.atomic():
            # Métrica NUNCA segura escrita: teto de espera explícito. Estourou,
            # a resposta é "não sei" — nunca 0. (CLAUDE.md, regras 6 e 7.)
            with connection.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = %s", [ESPELHADAS_TIMEOUT])
            for dia, grupo in por_data.items():
                pks = [por_cnj[c] for c in grupo if c in por_cnj]
                if not pks:
                    continue
                # RANGE no datetime cru, nunca `__date=`. O `__date` aplica
                # `::date` na coluna e o planner perde a metade-data do
                # `mov_processo_data_disp_idx` — sobra a metade-processo, e ele
                # varre TODAS as movimentações dos 500 processos pra filtrar
                # depois. Medido em produção: `__date=` 0,42 s contra 0,02 s do
                # range, mesmo resultado (n=22). Com os processos frios em disco
                # a diferença estoura o teto: a primeira coleta real absteve em
                # 14 de 14 lotes.
                ini = _tz.make_aware(datetime.datetime.combine(dia, datetime.time.min))
                total += (Movimentacao.objects
                          .filter(processo_id__in=pks,
                                  data_disponibilizacao__gte=ini,
                                  data_disponibilizacao__lt=ini + datetime.timedelta(days=1))
                          .exclude(external_id__in=ext_ids)
                          .values('processo_id').distinct().count())
    except OperationalError:
        logger.warning('espelhadas_no_lote: %s em %s itens (%s) — abstendo',
                       ESPELHADAS_TIMEOUT, len(itens), tribunal.sigla)
        return None
    return total


# ─────────────────────────────────────────────────────────────────────────────
# 9. PERSISTÊNCIA — o mesmo Movimentacao, o mesmo IngestionRun
# ─────────────────────────────────────────────────────────────────────────────
#: Lote enfileirado por job de indexação. É o mesmo 500 do
#: `search.jobs.indexar_movimentacoes_bulk` e do `sync_incremental`.
CHUNK_ES = 500


def _promover_partes(process_ids: list[int], tribunal, mov_ids=None) -> None:
    """Enfileira a promoção `destinatarios` → `ProcessoParte` do lote gravado.

    Só ENFILEIRA (fila `default`, lotes de `LOTE_PARTES`): a promoção lê as 3
    movimentações mais recentes de cada processo e escreve — trabalho de banco
    que não pode ficar no caminho da gravação de um caderno de 2.000 páginas.

    Falha aqui é WARNING, não exceção. Ver o comentário no chamador: sem parte
    a edição continua sendo acervo; sem índice, não.
    """
    if not process_ids:
        return
    try:
        from django_rq import get_queue

        from .jobs import LOTE_PARTES, promover_partes
        fila = get_queue('default')
        # GUARDA DE FAIRNESS. A `default` não é nossa: nela também vivem o tick
        # dos diários, o `reabastecer_filas_enriquecimento` e o
        # `reabastecer_fila_datajud`. RQ é FIFO sem prioridade, então um
        # backfill que enfileira 25 jobs por caderno empurra os crons para o
        # fim — medido em 02/09/2026 no reprocessamento do caderno 19: 77 jobs
        # de promoção na frente de 4 crons, e a projeção do lote inteiro era de
        # ~850 (≈3,8 h de cron faminto). É a mesma lição que criou o
        # `WATERMARK_POR_FONTE` ("quem enche primeiro monopoliza a FIFO").
        #
        # O teto olha a fila INTEIRA, não só os nossos jobs: é mais
        # conservador, e contar por `func_name` exigiria buscar job a job.
        # Bater no teto NÃO perde dado — a movimentação está gravada e o
        # `manage.py backfill_partes_djen` alcança o processo depois. Mas é
        # ALERTA registrado, nunca corte mudo.
        teto = int(getattr(settings, 'DIARIOS_FILA_PARTES_MAX', 200) or 0)
        if teto and fila.count >= teto:
            logger.warning(
                'promoção de partes ADIADA para %s: fila `default` com %d jobs '
                '(teto %d). %d processos NÃO foram enfileirados; a movimentação '
                'está gravada e o `backfill_partes_djen` os alcança.',
                getattr(tribunal, 'sigla', tribunal), fila.count, teto, len(process_ids))
            return
        for i in range(0, len(process_ids), LOTE_PARTES):
            fila.enqueue(promover_partes, process_ids[i:i + LOTE_PARTES], mov_ids)
    except Exception as exc:                                    # noqa: BLE001
        logger.warning('promoção de partes NÃO enfileirada para %s (%d processos): %s. '
                       'A movimentação está gravada; recupere com '
                       '`manage.py backfill_partes_djen`.',
                       getattr(tribunal, 'sigla', tribunal), len(process_ids), exc)


def _entregar_ao_indice(pks: list[int]) -> int:
    """Enfileira a indexação EM LOTE das linhas recém-gravadas. Propaga erro.

    Por que existe (medido em 21/08/2026, TJSP 12/03/2025): as 220.544 linhas
    dos 8 cadernos do DJE chegaram ao Elasticsearch APENAS pelo poller
    `search/sync_incremental.py`, que roda de 10 em 10 minutos e avança um
    watermark por `id`. Entre o fim da coleta (21:44:43) e o tick seguinte
    (21:53:01), **27.619** linhas ficaram acima do watermark — coletadas,
    gravadas, invisíveis para a busca, com a edição marcada `ok`. Quem mediu no
    meio dessa janela viu o mesmo número duas vezes e leu "parado", porque
    entre ticks de um poller nada se move mesmo.

    Depender só do poller é frágil por três motivos, todos concretos:
      · o watermark mora no `django.core.cache`; se a chave sumir, ele
        RE-ANCORA NO TOPO e tudo que ficou abaixo nunca mais é lido;
      · ele tem freio por tamanho de fila (`FILA_ES_ALTA`) e kill-switch
        (`sync_es:off`) — ambos legítimos, ambos invisíveis para o coletor;
      · durante a recuperação nacional do DJEN o watermark corre atrás de
        milhões de ids, e um caderno coletado entra no FIM dessa fila.

    NÃO é religar o `post_save` (`search/signals.py`): aquilo enfileiraria UM
    job por publicação — 220.544 jobs para um dia, contra 441 aqui. O lote é a
    diferença entre write-through e negação de serviço na própria fila.

    Propaga a exceção de propósito: fila fora do ar significa que a edição NÃO
    foi entregue ao índice, e engolir isso é a perda silenciosa que este
    projeto paga caro. A coleta falha, a edição volta a `pendente/falha` e é
    retentada — re-coletar é idempotente (`ignore_conflicts` + external_id
    determinístico).
    """
    if not pks:
        return 0
    import django_rq
    fila = django_rq.get_queue('es_index')
    for i in range(0, len(pks), CHUNK_ES):
        fila.enqueue('search.jobs.indexar_movimentacoes_bulk', pks[i:i + CHUNK_ES])
    return len(pks)


def persistir_movimentacoes(itens: list[ItemDiario], tribunal: Tribunal,
                            run: IngestionRun | None) -> tuple[int, int]:
    """Grava um lote de ParsedItem como Movimentacao. Devolve (novas, duplicadas).

    Mesma sequência do `djen/ingestion.py::_process_page`, com três diferenças
    deliberadas:

      · NÃO auto-enfileira enricher/Datajud. Aquele auto-enqueue é dimensionado
        para a fronteira diária; um backfill histórico descobrindo milhões de
        processos inéditos encheria as filas (já aconteceu: TJRO 3,6M / TJRJ
        2,6M em 2026-07-11). Quem quiser enriquecer o que veio do diário faz
        isso por um comando separado, com teto.
      · `bulk_create` NÃO dispara post_save ⇒ o write-through por signal
        (search/signals.py, um job POR LINHA) não é acionado, e isso continua
        sendo intencional para o volume histórico. Mas a frase que estava aqui
        — "a indexação é feita depois, em lote, por `reindexar_*`" — era falsa
        na prática: ninguém rodava `reindexar_*` para edição coletada, e as
        linhas só chegavam ao índice quando o poller de 10 minutos
        (`search/sync_incremental.py`) passasse por elas. Medido em 21/08/2026:
        27.619 das 220.544 linhas do dia 12/03/2025 do TJSP ficaram fora do
        índice esperando o próximo tick, com a edição marcada `ok`. Agora o
        próprio lote é ENTREGUE ao índice aqui, em lote de 500, no `on_commit`
        — ver `_entregar_ao_indice`.
      · idempotência total: `ignore_conflicts=True` sobre
        UniqueConstraint(tribunal, external_id). Re-coletar uma edição é seguro.
    """
    if not itens:
        return (0, 0)

    from tribunals.models import ClasseJudicial

    cnjs = {i.cnj for i in itens}
    ext_ids = [i.external_id for i in itens]

    with transaction.atomic():
        por_cnj = dict(
            Process.objects.filter(tribunal=tribunal, numero_cnj__in=cnjs)
            .values_list('numero_cnj', 'pk')
        )
        novos = [
            Process(tribunal=tribunal, numero_cnj=c, ano_cnj=ano_cnj_from_numero(c))
            for c in cnjs - por_cnj.keys()
        ]
        if novos:
            Process.objects.bulk_create(novos, ignore_conflicts=True, batch_size=BATCH_SIZE)
            por_cnj = dict(
                Process.objects.filter(tribunal=tribunal, numero_cnj__in=cnjs)
                .values_list('numero_cnj', 'pk')
            )

        ja_existem = set(
            Movimentacao.objects.filter(tribunal=tribunal, external_id__in=ext_ids)
            .values_list('external_id', flat=True)
        )

        classes = {(i.codigo_classe, i.nome_classe) for i in itens if i.codigo_classe and i.nome_classe}
        if classes:
            ClasseJudicial.objects.bulk_create(
                [ClasseJudicial(codigo=c, nome=n) for c, n in classes],
                ignore_conflicts=True, batch_size=BATCH_SIZE,
            )

        movs = []
        for i in itens:
            kwargs = i.to_movimentacao_kwargs()
            if i.codigo_classe:
                kwargs['classe_id'] = i.codigo_classe
            movs.append(Movimentacao(processo_id=por_cnj[i.cnj], tribunal=tribunal, **kwargs))
        Movimentacao.objects.bulk_create(movs, ignore_conflicts=True, batch_size=BATCH_SIZE)

        # ENTREGA AO ÍNDICE — o lote inteiro, novas E pré-existentes.
        #
        # `bulk_create(ignore_conflicts=True)` não devolve pk no Postgres, então
        # os pks vêm de um SELECT pelo índice único `uniq_mov_tribunal_extid`.
        # Custo medido em produção (21/08/2026, lote de 500 do TJSP): **0,072 s**
        # — três ordens de grandeza abaixo dos 4,13 s que o próprio `_bulk` de
        # 500 documentos leva. Re-indexar as pré-existentes é de propósito: numa
        # re-coleta o texto pode ter mudado (a troca de extrator da ADR-031 muda
        # a quebra de linha) e a indexação é idempotente por `_id`.
        if getattr(settings, 'DIARIOS_INDEXAR_AO_GRAVAR', True):
            pks = list(Movimentacao.objects
                       .filter(tribunal=tribunal, external_id__in=ext_ids)
                       .values_list('id', flat=True))
            if len(pks) != len(set(ext_ids)):
                # Gate mecânico e barato: toda linha que este lote diz ter
                # gravado tem que ter pk. Falta aqui é ERRO registrado no run —
                # nunca um número a menos passando despercebido.
                msg = (f'entrega ao índice INCOMPLETA: {len(pks)} pks para '
                       f'{len(set(ext_ids))} external_id do lote')
                logger.error('%s (%s)', msg, tribunal.sigla)
                if run is not None:
                    run.erros.append({'erro': 'indice_lote_incompleto', 'detalhe': msg})
            transaction.on_commit(lambda: _entregar_ao_indice(pks))

        # PROMOÇÃO A PARTE — o mesmo remédio do índice, num campo diferente.
        #
        # Medido em 02/09/2026, depois de o coletor da DEPRE passar a extrair o
        # ente devedor: 2.568 de 2.568 movimentações da relação de 10/03/2025
        # com `papel='ENTIDADE DEVEDORA'` e `polo='P'` no JSONB, e ZERO linhas
        # em `ProcessoParte` para esses processos. O promotor existe
        # (`tribunals/services/partes_djen.py`) mas é backfill por FAIXA DE PK
        # disparado à mão — e processo que nasce hoje de uma coleta tem pk
        # acima de qualquer faixa já varrida, então nunca é alcançado.
        # Extraído sem aterrissar é "coletado pela metade": a tela "Quem deve"
        # lê `ProcessoParte`, não `Movimentacao.destinatarios`.
        #
        # Diferença deliberada em relação à entrega ao índice: aqui a falha NÃO
        # derruba a coleta. Índice ausente torna a edição inútil (não é
        # buscável); parte ausente é enriquecimento que o backfill de partes
        # recupera depois, e a movimentação — que é o acervo — já está gravada.
        # Perder a edição inteira por causa disso seria trocar um dado a menos
        # por muitos dados a menos.
        if getattr(settings, 'DIARIOS_PROMOVER_PARTES', True):
            proc_ids = sorted({por_cnj[i.cnj] for i in itens if i.cnj in por_cnj})
            # Os pks das MOVIMENTAÇÕES do lote vão junto: a promoção lê o JSONB
            # exatamente destas linhas, em vez das 3 mais recentes do processo.
            # A diferença foi medida (02/09/2026, relação da DEPRE de
            # 10/03/2025): 823 dos 2.568 processos (32%) têm mais de 3
            # movimentações, e nenhum deles ganhou o ente devedor pela janela.
            # `pks` só existe quando a entrega ao índice está ligada; sem ele a
            # promoção cai na janela, que é pior mas não é errado.
            mov_pks = list(pks) if getattr(settings, 'DIARIOS_INDEXAR_AO_GRAVAR', True) else None
            transaction.on_commit(lambda: _promover_partes(proc_ids, tribunal, mov_pks))

        # `set(ext_ids)`, não `len(ext_ids)`: o lote pode trazer o MESMO
        # external_id duas vezes quando o diário imprime o mesmo ato duas vezes
        # (medido: 12 colisões em 31.408 blocos de um caderno do TJSP; conferido
        # na pg 188 que o ato aparece 2 contra  verbatim). Contando a lista, a segunda
        # coleta da mesma edição reportava `novas=11` para sempre — e o critério
        # de aceite da casa é literalmente "a segunda passada devolve novas=0".
        # O banco já estava certo (ignore_conflicts); era a contagem que mentia.
        #
        # Continua APROXIMADA por TOCTOU entre o SELECT e o bulk_create, mesmo
        # acordo já documentado no DJEN: o DADO nunca duplica, só a contagem
        # pode inflar com workers concorrentes na mesma unidade.
        novas = len(set(ext_ids)) - len(ja_existem)
        if run is not None:
            run.movimentacoes_novas += novas
            run.movimentacoes_duplicadas += len(ja_existem)
            run.processos_novos += len(novos)
            run.paginas_lidas += 1
            run.save(update_fields=['movimentacoes_novas', 'movimentacoes_duplicadas',
                                    'processos_novos', 'paginas_lidas', 'erros'])
        return (novas, len(ja_existem))


# ─────────────────────────────────────────────────────────────────────────────
# 10. RUNNER — o que realmente roda em produção
# ─────────────────────────────────────────────────────────────────────────────
def catalogar_fonte(coletor: ColetorDiario, data_inicio: date, data_fim: date,
                    sobrepor: bool = False) -> dict:
    """Roda o catálogo e materializa as unidades como `EdicaoDiario` pendentes.

    É o watermark durável do backfill: retomar depois de qualquer queda é
    "pegue as pendentes", sem re-perguntar nada à fonte. Idempotente — re-rodar
    o catálogo não reabre unidade já coletada.
    """
    from .models import EdicaoDiario

    checar_pausa(coletor.slug)
    novas = 0
    vistas = 0
    for u in coletor.catalogar(data_inicio, data_fim):
        vistas += 1
        if not sobrepor and not coletor.dentro_da_janela(u.data):
            continue
        _, criada = EdicaoDiario.objects.get_or_create(
            fonte=coletor.slug, chave=u.chave,
            defaults={
                'data': u.data,
                'tribunal_id': u.tribunal_sigla,
                'rotulo': u.rotulo[:200],
                'meta': u.meta,
                'status': EdicaoDiario.PENDENTE,
            },
        )
        novas += int(criada)
    logger.info('catálogo %s %s→%s: %d unidades vistas, %d novas',
                coletor.slug, data_inicio, data_fim, vistas, novas)
    return {'fonte': coletor.slug, 'vistas': vistas, 'novas': novas}


def coletar_unidade(coletor: ColetorDiario, edicao, sobrepor: bool = False,  # noqa: PLR0912
                    lote: int = BATCH_SIZE) -> dict:
    """Coleta UMA unidade: baixa, parseia, grava, fecha o watermark.

    Cria um `IngestionRun(fonte=<slug>)` por unidade — o mesmo model do DJEN, de
    propósito: a tela de saúde da ingestão, o watchdog de zumbis e as métricas
    de throughput passam a valer para as fontes novas sem código novo. O campo
    `fonte` é o que impede que um run do DJE/TJSP seja confundido com cobertura
    do DJEN (sem ele, o backfill do DJEN PULARIA o dia como já coberto).
    """
    from .models import EdicaoDiario

    checar_pausa(coletor.slug)
    unidade = edicao.como_unidade()
    if not sobrepor and not coletor.dentro_da_janela(unidade.data):
        edicao.marcar(EdicaoDiario.FORA_DA_JANELA)
        return {'chave': edicao.chave, 'status': EdicaoDiario.FORA_DA_JANELA}

    tribunal = coletor.tribunal_de(unidade)
    run = None
    if coletor.destino == DESTINO_MOVIMENTACAO:
        if tribunal is None:
            raise ColetorError(f'{coletor.slug}: unidade {edicao.chave} sem tribunal conhecido')
        run = IngestionRun.objects.create(
            tribunal=tribunal, fonte=coletor.slug, status=IngestionRun.STATUS_RUNNING,
            janela_inicio=unidade.data, janela_fim=unidade.data,
        )

    t0 = time.monotonic()
    novas = dup = espelhadas = 0
    # `espelhadas` é aproximação e pode se abster (timeout). Somar None daria
    # TypeError; tratar None como 0 seria pior — venderia ineditismo não medido.
    espelhadas_parcial = False
    buffer: list[ItemDiario] = []
    try:
        for item in coletor.coletar(unidade):
            buffer.append(item)
            if len(buffer) >= lote:
                if tribunal is not None:
                    e = espelhadas_no_lote(buffer, tribunal)
                    if e is None:
                        espelhadas_parcial = True
                    else:
                        espelhadas += e
                n, d = coletor.persistir(buffer, unidade, run)
                novas += n
                dup += d
                buffer = []
        if buffer:
            if tribunal is not None:
                e = espelhadas_no_lote(buffer, tribunal)
                if e is None:
                    espelhadas_parcial = True
                else:
                    espelhadas += e
            n, d = coletor.persistir(buffer, unidade, run)
            novas += n
            dup += d

        # Gate mecânico contra o gabarito da fonte, quando ela declara um.
        # É o que transforma "o segmentador parece bom" em "o segmentador achou
        # 16.717 de 16.717". Falhar aqui é MELHOR que gravar meia edição em
        # silêncio: a unidade fica pendente e é retentada.
        alvo = coletor.esperado(unidade)
        if alvo:
            achados = novas + dup
            piso = float(getattr(settings, 'DIARIOS_COBERTURA_MINIMA', 0.95))
            if achados < alvo * piso:
                raise ColetorError(
                    f'cobertura {achados}/{alvo} abaixo do piso de {piso:.0%} '
                    f'(gabarito da própria fonte) — segmentação suspeita'
                )
        edicao.marcar(
            EdicaoDiario.OK if (novas + dup) else EdicaoDiario.VAZIA,
            # `novas + dup` = quantas linhas desta unidade estão no banco, que é
            # o que a dashboard e o `diarios_status` perguntam. Passar só `novas`
            # (como antes) fazia a RE-coleta zerar o número: as três verificações
            # de 16/08/2026 flagraram edições `ok` com 31 mil linhas gravadas
            # exibindo `itens_gravados=0`. Contador de acervo que zera ao
            # reprocessar é pior que não ter contador.
            itens_gravados=novas + dup, itens_duplicados=dup,
            itens_esperados=alvo, ingestion_run=run,
        )
        if run is not None:
            run.status = IngestionRun.STATUS_SUCCESS
            run.finished_at = timezone.now()
            run.save(update_fields=['status', 'finished_at', 'erros'])
    except UnidadeInexistente as exc:
        # Ausência ≠ falha — mas ausência é TERMINAL, e por isso precisa ser
        # CONFIRMADA. Ver `CONFIRMACOES_DE_AUSENCIA`: em 03/09/2026 as 5
        # unidades `inexistente` do `tjsp-dje` foram reconferidas contra a
        # fonte e as 5 EXISTIAM (GET real devolvendo `%PDF`). Uma observação
        # transitória de "200 que não é dado" estava fechando o watermark para
        # sempre, com run `success`.
        vistas = _ausencias_seguidas(edicao) + 1
        if vistas < CONFIRMACOES_DE_AUSENCIA:
            # `contar_tentativa=False`: ausência NÃO é falha, e gastar o
            # orçamento de `MAX_TENTATIVAS` com ela deixaria a unidade parada
            # em `pendente` com `tentativas=5` — invisível para o tick. Quem
            # termina este laço é o contador de ausências, não o de falhas.
            edicao.marcar(
                EdicaoDiario.PENDENTE, contar_tentativa=False,
                erro=f'{MARCA_AUSENCIA} ({vistas}/{CONFIRMACOES_DE_AUSENCIA}): {exc}'[:500])
            if run is not None:
                run.status = IngestionRun.STATUS_SUCCESS
                run.finished_at = timezone.now()
                run.erros.append({'erro': 'ausencia_nao_confirmada',
                                  'vistas': vistas, 'exigidas': CONFIRMACOES_DE_AUSENCIA,
                                  'detalhe': str(exc)[:200]})
                run.save(update_fields=['status', 'finished_at', 'erros'])
            logger.warning(
                'coleta %s/%s: a fonte disse que não tem esta unidade (%s). Ausência '
                'NÃO confirmada — %d de %d observações. A unidade segue PENDENTE.',
                coletor.slug, edicao.chave, exc, vistas, CONFIRMACOES_DE_AUSENCIA)
            return {'chave': edicao.chave, 'status': EdicaoDiario.PENDENTE,
                    'ausencia_nao_confirmada': vistas}
        edicao.marcar(EdicaoDiario.INEXISTENTE,
                      erro=f'ausência confirmada em {vistas} observações: {exc}'[:500])
        if run is not None:
            run.status = IngestionRun.STATUS_SUCCESS
            run.finished_at = timezone.now()
            run.erros.append({'erro': 'unidade_inexistente', 'vistas': vistas,
                              'detalhe': str(exc)[:200]})
            run.save(update_fields=['status', 'finished_at', 'erros'])
        return {'chave': edicao.chave, 'status': EdicaoDiario.INEXISTENTE, 'vistas': vistas}
    except UnidadeSemDadoAproveitavel as exc:
        # HAVIA publicação e NADA serve (era pré-CNJ). Terminal como o
        # inexistente, mas com o motivo escrito: dívida visível ≠ dia vazio.
        edicao.marcar(EdicaoDiario.SEM_APROVEITAMENTO, erro=str(exc)[:500])
        if run is not None:
            run.status = IngestionRun.STATUS_SUCCESS
            run.finished_at = timezone.now()
            run.erros.append({'erro': 'sem_dado_aproveitavel', 'detalhe': str(exc)[:300]})
            run.save(update_fields=['status', 'finished_at', 'erros'])
        logger.warning('coleta %s/%s SEM APROVEITAMENTO: %s', coletor.slug, edicao.chave, exc)
        return {'chave': edicao.chave, 'status': EdicaoDiario.SEM_APROVEITAMENTO}
    except FonteOcupada:
        # Circuito aberto: ADIA. Não conta tentativa, não empilha em falha.
        edicao.marcar(EdicaoDiario.PENDENTE, contar_tentativa=False)
        if run is not None:
            IngestionRun.objects.filter(pk=run.pk).delete()
        raise
    except Exception as exc:
        edicao.marcar(EdicaoDiario.FALHA, erro=str(exc)[:500])
        if run is not None:
            run.status = IngestionRun.STATUS_FAILED
            run.finished_at = timezone.now()
            run.erros.append({'erro': 'coleta', 'detalhe': str(exc)[:500]})
            run.save(update_fields=['status', 'finished_at', 'erros'])
        logger.exception('coleta falhou %s/%s', coletor.slug, edicao.chave)
        raise

    # `≥` quando algum lote se absteve: o número é PISO, não total. Imprimir
    # `espelhadas=0` sobre uma medição incompleta é o número redondo de sempre.
    logger.info('coleta %s/%s → novas=%d dup=%d espelhadas%s%d %ds',
                coletor.slug, edicao.chave, novas, dup,
                '>=' if espelhadas_parcial else '=', espelhadas,
                int(time.monotonic() - t0))
    return {'chave': edicao.chave, 'novas': novas, 'duplicadas': dup,
            'espelhadas': espelhadas, 'espelhadas_parcial': espelhadas_parcial,
            'run_id': run.pk if run else None}


# ─────────────────────────────────────────────────────────────────────────────
# 11. REGISTRO DE FONTES — sem arquivo central, sem conflito de merge
# ─────────────────────────────────────────────────────────────────────────────
# A auto-descoberta (`diarios/apps.py` importa todo subpacote de
# `diarios.fontes`) existe por um motivo operacional: quatro implementadores em
# paralelo NÃO podem editar a mesma lista de registro. Cada um só cria o seu
# diretório; o registro acontece pelo import.
_REGISTRO: dict[str, type[ColetorDiario]] = {}


def registrar(cls: type[ColetorDiario]) -> type[ColetorDiario]:
    """Decorator: `@registrar` na classe do coletor, no `coletor.py` da fonte."""
    validar_slug(cls.slug)
    if cls.slug in _REGISTRO and _REGISTRO[cls.slug] is not cls:
        raise ValueError(f'slug de fonte duplicado: {cls.slug}')
    _REGISTRO[cls.slug] = cls
    return cls


def obter(slug: str) -> ColetorDiario:
    if slug not in _REGISTRO:
        raise KeyError(f'fonte desconhecida: {slug!r} (registradas: {sorted(_REGISTRO)})')
    return _REGISTRO[slug]()


def listar() -> list[str]:
    return sorted(_REGISTRO)
