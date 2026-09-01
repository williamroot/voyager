"""Ingestão de movimentações via Datajud (CNJ).

Diferença vs DJEN:
- DJEN é index de publicações em diário oficial — cobre **publicações**
- Datajud é o repositório CNJ do processo — cobre **TODAS** as movs

Conviver: Movimentacao tem `meio` field. DJEN salva com `meio='D'/'E'/etc`,
Datajud salva com `meio='datajud'`. Mesmo Process pode ter movs de ambas
fontes; UI mostra todas na timeline ordenada por data.

Idempotência: external_id = `datajud:<sha1(proc_id+codigo+dataHora)[:24]>`,
único por (tribunal, external_id) garante INSERT seguro com bulk_create
ignore_conflicts.

ENTREGA AO ÍNDICE (24/08/2026): esta porta escreve por DOIS caminhos e, até
esta data, NENHUM dos dois chegava ao Elasticsearch por conta própria —
`bulk_create` não dispara `post_save`, e `.update()` não dispara `post_save`
NEM mexe em `atualizado_em` (o `auto_now` é ignorado por `.update()`, e o
poller `search/sync_incremental.py::sync_processos_atualizados` é keyset por
`atualizado_em`). Ver `_entregar_ao_indice` para os números medidos.
"""
from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Max, Min
from django.utils import timezone

from tribunals.catalogo import resolver as resolver_catalogo
from tribunals.models import ClasseJudicial, Movimentacao, Process

from .client import DatajudClient
from .parser import parse_movimentos

logger = logging.getLogger('voyager.datajud.ingestion')

BATCH_SIZE = 500
#: Tamanho do lote entregue à fila `es_index` — o mesmo que o
#: `search.jobs.indexar_movimentacoes_bulk` consome num `_bulk`.
CHUNK_ES = 500


def _as_dict(x) -> dict:
    """Normaliza um campo do `_source` do Datajud que deveria ser dict mas às
    vezes vem aninhado como lista (lista-de-dict ou lista-de-lista). Desce até
    o primeiro dict; devolve {} se não houver. Evita `AttributeError: 'list'
    object has no attribute 'get'` (visto em ~23% dos failed da fila datajud)."""
    seen = 0
    while isinstance(x, list) and x and seen < 5:
        x = x[0]
        seen += 1
    return x if isinstance(x, dict) else {}


#: Domínio real de `grau`, medido no `voyager-acervo` em 25/08/2026 — o
#: esqueleto nacional inteiro, 342.046.902 documentos varridos do Datajud,
#: `_count` por termo (não `exists`, que conta string vazia como presente):
#:
#:     G1  203.782.129   1º grau
#:     G2   41.972.803   2º grau
#:     JE   73.791.952   Juizado Especial  ← 21,6% do país
#:     SUP   8.159.129   tribunal superior
#:     TR   14.272.244   Turma Recursal
#:     TRU      68.645   Turma Regional de Uniformização
#:     (soma = 342.046.902: `grau` está presente em 100% dos docs)
#:
#: **JE e TR pagam por RPV, não por precatório.** Sem este campo o funil de
#: produto do Juriscope mistura dois produtos com prazos e preços diferentes.
#: Valor fora do domínio: ABSTÉM (regra nº 6 do CLAUDE.md). Normalizar no chute
#: um grau desconhecido é pior que a coluna vazia, porque a coluna vazia a tela
#: sabe dizer que está vazia.
GRAUS_CONHECIDOS = frozenset({'G1', 'G2', 'JE', 'SUP', 'TR', 'TRU'})

#: memo por coluna: {nome: existe?}. Uma consulta por coluna por processo.
_COLUNAS_CONFERIDAS: dict[str, bool] = {}


def coluna_existe(nome: str) -> bool:
    """A coluna já está NO BANCO? (não no model — no banco.)

    Generalização de `coluna_grau_existe` — mesma armadilha, mesma cura, agora
    para `classe_cnj_codigo`/`fase_codigo` (migration 0054). Ver a docstring
    abaixo para o porquê: é ordem de deploy, não paranoia.
    """
    if nome not in _COLUNAS_CONFERIDAS:
        from django.db import connection
        try:
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'tribunals_process' AND column_name = %s",
                    [nome])
                existe = cur.fetchone() is not None
        except Exception as e:
            logger.warning('não deu pra conferir a coluna %s (%s) — não escrevo', nome, e)
            return False
        if not existe:
            logger.warning('coluna `%s` ainda não existe no banco — o Datajud '
                           'segue gravando o resto, e ela fica de fora', nome)
        _COLUNAS_CONFERIDAS[nome] = existe
    return _COLUNAS_CONFERIDAS[nome]


def coluna_grau_existe() -> bool:
    """A coluna `grau` já está NO BANCO? (não no model — no banco.)

    Esta casa já foi mordida três vezes esta semana pela família "declarado no
    ESTADO, ausente do banco": os 3 índices fantasma da 0051, o trigger
    `mov_update_process_agg`, os `pp_total_ins`/`pp_total_del`. Aqui o risco é
    o inverso e é de ORDEM DE DEPLOY: `Process.grau` existe no model desde a
    migration 0052, mas o `ALTER TABLE` sobre uma tabela de 102 M linhas sob
    escrita constante pode não ter passado ainda. Se este código subir antes do
    ALTER, o `UPDATE ... SET grau = %s` derruba a sincronização inteira — e
    junto com ela os campos que JÁ funcionavam (classe, assunto, órgão, data).

    Uma consulta por processo, memorizada. `False` = `grau` simplesmente não é
    escrito; nada mais muda.
    """
    return coluna_existe('grau')


def _meta_updates_from_source(processo: Process, source: dict) -> dict:
    """Extrai metadados do `_source` do Datajud e devolve dict de updates
    para `Process`, respeitando dados já populados (PJe enricher é fonte
    de verdade quando presente — Datajud só preenche lacunas).
    """
    upd: dict = {}

    grau = str(source.get('grau') or '').strip().upper()
    if grau in GRAUS_CONHECIDOS and not getattr(processo, 'grau', ''):
        if coluna_grau_existe():
            upd['grau'] = grau
    elif grau and grau not in GRAUS_CONHECIDOS:
        logger.warning('datajud: grau fora do domínio (%r) em %s — abstendo',
                       grau, getattr(processo, 'numero_cnj', '?'))

    classe_obj = _as_dict(source.get('classe'))
    classe_codigo = str(classe_obj.get('codigo') or '').strip()
    classe_nome = (classe_obj.get('nome') or '').strip()[:255]
    if classe_codigo and not processo.classe_codigo:
        upd['classe_codigo'] = classe_codigo
        upd['classe_nome'] = classe_nome
    # `classe_cnj_*` é a classe CADASTRAL e esta porta é a ÚNICA dona dela —
    # então aqui NÃO existe o "só preenche lacuna" do `classe_codigo` acima:
    # não há com quem conflitar, e o cadastro do CNJ muda (o próprio Datajud
    # publica os movimentos `Retificação de Classe Processual` e `Mudança de
    # Classe Processual`; 172 deles em 209 processos TRF3 amostrados em
    # 31/08/2026). Guardar a primeira leitura para sempre seria congelar um
    # fato que a fonte mudou.
    #
    # Por que a coluna existe, com o número (#105, 31/08/2026): em 222 de 830
    # processos conferíveis que rotulamos `12078`, o CNJ declara outra classe
    # — e em 98,6% deles o processo TEM a fase de cumprimento contra a
    # fazenda, provada por canal independente do campo que gerou o rótulo.
    # Não é rótulo errado: são dois fatos diferentes no mesmo campo. Ver
    # `.ia/ACERVO_CNJ.md`.
    if classe_codigo and (classe_codigo != (getattr(processo, 'classe_cnj_codigo', '') or '')
                          or classe_nome != (getattr(processo, 'classe_cnj_nome', '') or '')):
        if coluna_existe('classe_cnj_codigo'):
            upd['classe_cnj_codigo'] = classe_codigo
            upd['classe_cnj_nome'] = classe_nome

    assuntos = source.get('assuntos') or []
    if assuntos and not processo.assunto_codigo:
        a0 = _as_dict(assuntos[0])
        a_cod = str(a0.get('codigo') or '').strip()
        a_nome = (a0.get('nome') or '').strip()[:255]
        if a_cod:
            upd['assunto_codigo'] = a_cod
            upd['assunto_nome'] = a_nome

    orgao = _as_dict(source.get('orgaoJulgador'))
    o_cod = str(orgao.get('codigo') or '').strip()
    o_nome = (orgao.get('nome') or '').strip()[:255]
    if o_cod and not processo.orgao_julgador_codigo:
        upd['orgao_julgador_codigo'] = o_cod
    if o_nome and not processo.orgao_julgador_nome:
        upd['orgao_julgador_nome'] = o_nome

    # Datajud entrega dataAjuizamento como "YYYYMMDDhhmmss"
    dt_ajuiz = source.get('dataAjuizamento')
    if dt_ajuiz and not processo.data_autuacao:
        try:
            upd['data_autuacao'] = datetime.strptime(str(dt_ajuiz)[:8], '%Y%m%d').date()
        except ValueError:
            pass

    # valorCausa pode vir como número ou string; tolera ausência
    vc = source.get('valorCausa')
    if vc is not None and processo.valor_causa is None:
        try:
            upd['valor_causa'] = Decimal(str(vc))
        except (InvalidOperation, ValueError, TypeError):
            pass

    # `nivelSigilo` NÃO é lido de propósito, e o motivo é medido, não estético:
    # no `voyager-acervo` inteiro — 342.046.902 documentos varridos do Datajud —
    # `nivelSigilo` vale **0 em 342.046.902 e qualquer outro valor em 0**
    # (`_count` por termo, valores 0..5 e o `must_not exists`). O campo não
    # carrega informação nacional nenhuma: a API pública do CNJ só expõe o que
    # é público. Mapeá-lo para `segredo_justica` escreveria `False` em 102 M de
    # processos — exatamente a afirmação que ninguém verificou e que a migration
    # 0052 acabou de tornar NULL. Quem sabe dizer que há segredo é o e-SAJ, pela
    # página "informe a senha" (achado 5 de .ia/ENRICHMENT.md).

    return upd


def fechar_fks_do_catalogo(upd: dict) -> dict:
    """Espelha `classe_codigo`/`assunto_codigo` nas FKs `classe_id`/`assunto_id`.

    Muta e devolve o MESMO dict de updates. Existe porque este caminho gravava
    só a string e deixava a FK NULL (#104): o backfill de 31/08/2026 levou
    `classe_id IS NULL` de 8.054.334 a 21 às 22:05 UTC, e dezoito horas depois
    o buraco estava em **8.072** sem nada ter sido apagado — 97,6% eram linhas
    ANTIGAS reescritas por aqui, e **0 códigos** estavam fora do catálogo.
    Havia com o que resolver a FK e não se resolvia.

    Fica AQUI, e não em `_meta_updates_from_source`, porque aquilo é parser: lê
    o `_source` e não toca no banco (os testes de `assuntos` aninhado, de
    `grau` e de `classe_cnj` dependem disso). Resolver referência de catálogo é
    trabalho de quem escreve, e quem escreve é `sync_processo`.

    Só escreve a FK quando o catálogo RESOLVE o código. Órfão fica NULL, com
    log — e, principalmente, nunca vira `classe_id = NULL` por cima de uma FK
    que outro escritor já tinha fechado.
    """
    for qual in ('classe', 'assunto'):
        codigo = upd.get(f'{qual}_codigo')
        if not codigo:
            continue
        fk = resolver_catalogo(qual, codigo)
        if fk:
            upd[f'{qual}_id'] = fk
    return upd


def _entregar_ao_indice(mov_pks: list[int], processo_pk: int) -> None:
    """Entrega ao índice o que ESTA sincronização escreveu. Propaga erro.

    Por que existe, com o que foi medido em produção em 24/08/2026 (amostra
    aleatória, `_mget` por id — resposta exata, não estimativa):

      MOVIMENTAÇÕES — recorte por janela de `inserido_em` (o índice
      `mov_inserido_tribunal_idx`), filtro `external_id LIKE 'datajud:%'`:

        idade da escrita   linhas na janela   amostra   fora do índice
        0-5 min                     3.088       3.000   3.000 (100,00%)
        5-15 min                    3.927       3.000   1.268 ( 42,27%)
        15-30 min                   4.133       3.000       0
        30-60 min                  15.362       3.000       0

      Vazão da porta, por hora cheia no mesmo dia: **327.566 linhas/h em
      média**, pico de **798.824/h** (12h UTC) e vale de **28.610/h** (14h
      UTC) — 28x de amplitude. O único caminho até o índice era o poller de
      10 minutos, e a medição acima foi feita com ele SAUDÁVEL (atraso:
      122.604 ids ≈ 1 tick). Com o poller freado por `FILA_ES_ALTA`,
      desligado por `sync_es:off`, ou com a chave de watermark perdida do
      cache — caso em que ele RE-ANCORA NO TOPO — o que ficou abaixo não
      volta nunca.

      PROCESSO — critério exato: o doc está em dia com esta porta se
      `doc.enriquecido_em >= PG.data_enriquecimento_datajud`.

        janela de `data_enriquecimento_datajud`   amostra   em dia
        30-15 min                                     500   0
        2h-30min                                      500   0
        1d-2h                                         500   8  (1,6%)
        3d-1d                                         500   98 (19,6%)
        30d-7d                                        500   0

      Este segundo buraco não tinha poller NENHUM: `Process.objects.filter(
      pk=...).update(...)` não dispara `post_save` e não mexe em
      `atualizado_em` (o `auto_now` só roda em `Model.save()`), que é
      justamente a chave do keyset de `sync_processos_atualizados`. População:
      22.475.738 processos com `data_enriquecimento_datajud`, 1.703.782 nos
      últimos 30 dias, 5.628 numa hora medida.

    Entrega só as movimentações NOVAS. Na PRIMEIRA sincronização de um processo
    isso quase não muda nada (medido: 416.186 linhas para 5.628 processos numa
    hora, 74 por processo, praticamente todas novas); o que ele evita é a
    RE-sincronização, que é o caminho do botão manual e da hidratação — lá o
    processo já tem os 74 movimentos, e entregar o lote inteiro reindexaria 74
    documentos para corrigir zero. Diferente do diário, esta porta não
    reescreve texto: o `external_id` é `sha1(processo, código, dataHora)` e o
    texto sai do MESMO movimento, então re-entregar pré-existente não corrige
    nada. Quem cobre a entrega que falhou é o gate (`datajud/indice.py`), que
    confere a janela de escrita pelos dois lados.

    Propaga a exceção de propósito: fila fora do ar significa que a
    sincronização NÃO foi entregue ao índice. O job morre, o RQ retenta
    (`DATAJUD_RETRY`) e re-sincronizar é idempotente.
    """
    if not getattr(settings, 'DATAJUD_INDEXAR_AO_GRAVAR', True):
        return
    from search.gate import enfileirar_movs, enfileirar_processos

    if mov_pks:
        enfileirar_movs(mov_pks, CHUNK_ES)
    # O doc do processo muda em TODA sincronização, mesmo quando não há
    # movimento novo: `enriquecido_em` do doc é o max() das datas de
    # enriquecimento, e `data_enriquecimento_datajud` acabou de ser gravada.
    enfileirar_processos([processo_pk])


def sync_processo(processo: Process, client: Optional[DatajudClient] = None) -> dict:
    """Busca o processo no Datajud e popula Movimentacao com `meio='datajud'`.

    - 1 request HTTP no Datajud (todos os movimentos vêm em 1 hit)
    - bulk_create idempotente via uniq (tribunal, external_id)
    - Atualiza Process.ultima_sinc_djen_em + total_movimentacoes/datas
    """
    client = client or DatajudClient()
    tribunal = processo.tribunal
    sigla = tribunal.sigla
    source = client.fetch_processo(sigla, processo.numero_cnj)
    if not source:
        # Marca data_enriquecimento_datajud mesmo quando não encontrado:
        # processo passou pelo Datajud, sem hit no índice CNJ. Evita retry
        # infinito a cada bulk re-enqueue.
        now_ts = timezone.now()
        Process.objects.filter(pk=processo.pk).update(
            data_enriquecimento_datajud=now_ts,
            # `atualizado_em` é `auto_now`, e `auto_now` só roda em
            # `Model.save()` — `.update()` o IGNORA. Sem carimbar à mão, a
            # linha muda sem que nada registre que mudou, e o poller
            # `sync_processos_atualizados` (keyset por `atualizado_em`) nunca
            # a enxerga. Ver `_entregar_ao_indice`.
            atualizado_em=now_ts,
        )
        _entregar_ao_indice([], processo.pk)
        return {'cnj': processo.numero_cnj, 'novos': 0, 'duplicados': 0,
                'fonte': 'datajud', 'encontrado': False}

    items = parse_movimentos(source)
    meta_updates = fechar_fks_do_catalogo(_meta_updates_from_source(processo, source))

    if not items:
        now_ts = timezone.now()
        # `atualizado_em`: ver o comentário do branch "não encontrado" acima.
        update_kwargs = dict(ultima_sinc_djen_em=now_ts, data_enriquecimento_datajud=now_ts,
                             atualizado_em=now_ts)
        update_kwargs.update(meta_updates)
        Process.objects.filter(pk=processo.pk).update(**update_kwargs)
        _entregar_ao_indice([], processo.pk)
        return {'cnj': processo.numero_cnj, 'novos': 0, 'duplicados': 0,
                'fonte': 'datajud', 'encontrado': True}

    ext_ids = [it['external_id'] for it in items]

    with transaction.atomic():
        ja_existem = set(
            Movimentacao.objects
            .filter(tribunal=tribunal, external_id__in=ext_ids)
            .values_list('external_id', flat=True)
        )

        # Catálogo de classes — bulk_create se houver código novo
        novos_classes = {(it['codigo_classe'], it['nome_classe'])
                         for it in items if it.get('codigo_classe') and it.get('nome_classe')}
        if novos_classes:
            ClasseJudicial.objects.bulk_create(
                [ClasseJudicial(codigo=c, nome=n) for c, n in novos_classes],
                ignore_conflicts=True,
                batch_size=BATCH_SIZE,
            )

        movs_to_create = []
        for it in items:
            if it['external_id'] in ja_existem:
                continue
            kwargs = dict(it)
            if kwargs.get('codigo_classe'):
                kwargs['classe_id'] = kwargs['codigo_classe']
            movs_to_create.append(
                Movimentacao(processo_id=processo.pk, tribunal=tribunal, **kwargs)
            )

        novos_ext_ids: list[str] = []
        if movs_to_create:
            Movimentacao.objects.bulk_create(
                movs_to_create, ignore_conflicts=True, batch_size=BATCH_SIZE,
            )
            # `bulk_create(ignore_conflicts=True)` NÃO devolve pk no Postgres,
            # então os pks vêm de um SELECT pelo índice único
            # `uniq_mov_tribunal_extid` — é um index scan por (tribunal,
            # external_id), não uma varredura.
            novos_ext_ids = sorted({m.external_id for m in movs_to_create})
            pks_novos = list(
                Movimentacao.objects
                .filter(tribunal=tribunal, external_id__in=novos_ext_ids)
                .values_list('id', flat=True)
            )
            if len(pks_novos) != len(novos_ext_ids):
                # Gate mecânico e barato: toda linha que este lote diz ter
                # gravado tem que ter pk. Falta aqui é ERRO registrado — nunca
                # um número a menos passando despercebido (regra nº 2).
                logger.error(
                    'datajud %s %s: entrega ao índice INCOMPLETA — %d pks para '
                    '%d external_id gravados',
                    tribunal.sigla, processo.numero_cnj, len(pks_novos),
                    len(novos_ext_ids),
                )
        else:
            pks_novos = []

        # Atualiza resumo do Process (primeira/ultima/total) — única query
        # com aggregates considerando TODAS as fontes (DJEN + Datajud).
        agg = (
            Movimentacao.objects.filter(processo=processo)
            .aggregate(
                primeira=Min('data_disponibilizacao'),
                ultima=Max('data_disponibilizacao'),
                total=Count('id'),
            )
        )
        now_ts = timezone.now()
        update_kwargs = dict(
            primeira_movimentacao_em=agg['primeira'],
            ultima_movimentacao_em=agg['ultima'],
            total_movimentacoes=agg['total'] or 0,
            data_enriquecimento_datajud=now_ts,
            # ultima_sinc_djen_em é compartilhado historicamente; mantém
            # atualizado pra UI/queries antigas continuarem funcionando.
            ultima_sinc_djen_em=now_ts,
            # `atualizado_em`: ver o comentário do branch "não encontrado".
            atualizado_em=now_ts,
        )
        update_kwargs.update(meta_updates)
        Process.objects.filter(pk=processo.pk).update(**update_kwargs)

        # ENTREGA AO ÍNDICE, no COMMIT. Dentro da transação seria entregar pks
        # de linhas que ainda podem sofrer rollback — job enfileirado sobre
        # fantasma. Ver `_entregar_ao_indice` para o que foi medido.
        transaction.on_commit(lambda: _entregar_ao_indice(pks_novos, processo.pk))

    novos = len(movs_to_create)
    duplicados = len(items) - novos
    logger.info('datajud sync %s: novos=%d duplicados=%d',
                processo.numero_cnj, novos, duplicados)

    return {
        'cnj': processo.numero_cnj,
        'novos': novos,
        'duplicados': duplicados,
        'fonte': 'datajud',
        'encontrado': True,
    }
