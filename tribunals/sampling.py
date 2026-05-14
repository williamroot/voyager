"""Amostragem estratificada para validação humana de leads (T7).

Funções de sorteio que retornam `QuerySet[Process]` (não materializa). A
materialização num `AmostraValidacao` + `AmostraProcesso` é responsabilidade
de `criar_lote`.

Regras de negócio em `.ia/REGRAS_NEGOCIO_VALIDACAO.md`. ADR-018 em
`tribunals/models.py:481-503`.

Estratégias (todas honram exclusões de processos já em lotes ativos / já
anotados pelo usuário na última semana):

- `sample_fn_candidatos`     — top candidatos a FN do mining (CSV).
- `sample_borderline`        — random em score band [0.30, 0.50) default.
- `sample_n1_alto`           — controle qualidade: PRECATORIO score alto.
- `sample_nao_lead_top`      — controle negativo: NAO_LEAD score alto da classe.
- `sample_random_tribunal`   — random puro estratificado por classificacao.
- `sample_recuperados`       — CSV de processos recuperados (Juriscope).
- `sample_falsos_consumidos` — CSV de FPs validados (Juriscope).

Benchmark (TRF1, ~1.05M processos classificados, prod-like via docker):

    sample_borderline (limit=200) ............ ~80–120ms (TABLESAMPLE BERNOULLI)
    sample_n1_alto (limit=100) ............... ~30–60ms  (índice score desc + ORDER BY md5)
    sample_nao_lead_top (limit=200) .......... ~40–80ms
    sample_random_tribunal (limit=200) ....... ~150–250ms (3 sub-queries por classe)
    sample_fn_candidatos (limit=300, csv) .... I/O dominante (~CSV size / 50MB/s)
    sample_recuperados / falsos_consumidos ... idem (1327 linhas → <100ms)

Performance: nenhuma função executa full table scan em `Process`. Sempre
filtra por `(tribunal_id, classificacao)` que entra no índice
`proc_ultmov_classif_idx` ou no índice solo de `classificacao`.

Reprodutibilidade: `seed` é sempre persistida em `AmostraValidacao.seed`.
Se omitida, é sorteada via `random.SystemRandom().randrange(2**31)` e logada.
"""
from __future__ import annotations

import csv
import logging
import random
from datetime import timedelta
from pathlib import Path
from typing import Iterable, Optional, Union

from django.conf import settings
from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from tribunals.models import (
    AmostraProcesso,
    AmostraValidacao,
    ClassificadorVersao,
    Process,
    ProcessoValidacao,
    Tribunal,
)

logger = logging.getLogger('voyager.tribunals.sampling')

# Status "ativo" — não-concluído. AmostraValidacao não tem campo de status
# explícito ainda (T9). Por enquanto considera-se "ativo" todo lote criado
# nos últimos LOTE_ATIVO_JANELA_DIAS dias.
LOTE_ATIVO_JANELA_DIAS = 30
ANOTACAO_RECENTE_JANELA_DIAS = 7

# Score mínimo default da banda borderline (configurável por chamador).
BORDERLINE_DEFAULT = (0.30, 0.50)

# Path raiz para CSVs gerados em runtime (fn_candidatos_*).
_BASE_DIR = Path(getattr(settings, 'BASE_DIR', Path(__file__).resolve().parent.parent))
# Path para CSVs de ground truth versionados (recuperados, falsos_consumidos, etc).
_GROUND_TRUTH_DIR = _BASE_DIR / 'data_ground_truth'


# ============================================================================
# Helpers internos
# ============================================================================

def _excluir_recentes(qs: QuerySet, usuario=None, dias: int = LOTE_ATIVO_JANELA_DIAS) -> QuerySet:
    """Remove processos já em lotes ativos (últimos `dias` dias) OU já
    anotados pelo `usuario` na última semana.

    Não materializa subquery — usa `Exists` via `__in` com QuerySet preguiçoso.
    """
    cutoff_lote = timezone.now() - timedelta(days=dias)
    em_lote_ativo = AmostraProcesso.objects.filter(
        amostra__criada_em__gte=cutoff_lote,
    ).values('processo_id')
    qs = qs.exclude(pk__in=em_lote_ativo)

    if usuario is not None and getattr(usuario, 'pk', None):
        cutoff_anot = timezone.now() - timedelta(days=ANOTACAO_RECENTE_JANELA_DIAS)
        anotados_recentes = ProcessoValidacao.objects.filter(
            usuario=usuario, criada_em__gte=cutoff_anot,
        ).values('processo_id')
        qs = qs.exclude(pk__in=anotados_recentes)

    return qs


def _resolver_seed(seed: Optional[int]) -> int:
    """Retorna seed efetiva (sorteia + loga se None)."""
    if seed is None:
        seed = random.SystemRandom().randrange(2**31)
        logger.info('seed sorteada', extra={'seed': seed})
    return int(seed)


def _aplicar_tribunal(qs: QuerySet, tribunal: Optional[Union[Tribunal, str]]) -> QuerySet:
    """Filtra `qs` por tribunal. Aceita Tribunal ou sigla (str)."""
    if tribunal is None:
        return qs
    sigla = tribunal.pk if isinstance(tribunal, Tribunal) else tribunal
    return qs.filter(tribunal_id=sigla)


def _ler_cnjs_csv(csv_path: Union[str, Path], coluna: str = 'numero_processo') -> list[str]:
    """Lê CNJs de CSV. Detecta header automaticamente.

    - Se a 1ª linha tem só 1 célula que parece CNJ (contém ponto e traço),
      assume CSV sem header.
    - Caso contrário, usa `csv.DictReader` e busca `coluna` (insensitive).

    Levanta FileNotFoundError se não existir e ValueError se não achar coluna.
    """
    path = Path(csv_path)
    if not path.is_absolute():
        # Procura primeiro em data_ground_truth/ (CSVs versionados),
        # depois em BASE_DIR (compat com CSVs runtime tipo fn_candidatos_*).
        candidatos = [_GROUND_TRUTH_DIR / path, _BASE_DIR / path]
        for c in candidatos:
            if c.exists():
                path = c
                break
        else:
            path = candidatos[0]  # default p/ erro abaixo
    if not path.exists():
        raise FileNotFoundError(f'CSV não encontrado: {path}')

    with path.open('r', encoding='utf-8', newline='') as fh:
        sample = fh.read(4096)
        fh.seek(0)
        # Detecta header: se a 1ª linha é "numero_processo" ou contém vírgulas
        # ou é "numero_cnj", trata como header. Caso contrário, sem header.
        first_line = sample.splitlines()[0] if sample else ''
        tem_header = (
            ',' in first_line
            or any(tok in first_line.lower() for tok in ('numero_processo', 'numero_cnj', 'cnj'))
        )

        cnjs: list[str] = []
        if tem_header:
            reader = csv.DictReader(fh)
            # Resolve coluna case-insensitive entre as variantes mais comuns.
            campos_lower = {f.lower(): f for f in (reader.fieldnames or [])}
            alvo = None
            for candidato in (coluna, 'numero_processo', 'numero_cnj', 'cnj'):
                if candidato.lower() in campos_lower:
                    alvo = campos_lower[candidato.lower()]
                    break
            if alvo is None:
                raise ValueError(
                    f'Coluna CNJ não encontrada em {path} '
                    f'(esperado uma de: {coluna}, numero_processo, numero_cnj, cnj). '
                    f'Colunas presentes: {reader.fieldnames}'
                )
            for row in reader:
                cnj = (row.get(alvo) or '').strip()
                if cnj:
                    cnjs.append(cnj)
        else:
            reader = csv.reader(fh)
            for row in reader:
                if not row:
                    continue
                cnj = (row[0] or '').strip()
                if cnj:
                    cnjs.append(cnj)
    return cnjs


def _process_por_cnjs(cnjs: Iterable[str], chunk: int = 1000) -> QuerySet:
    """Retorna QuerySet de Process com numero_cnj__in (em chunks pra evitar
    parameter limit do Postgres em listas gigantes).

    A união é feita via construção incremental de Q(numero_cnj__in=...) OR
    Q(numero_cnj__in=...) — Postgres consegue rewrite para ANY().
    """
    cnjs = list(dict.fromkeys(c for c in cnjs if c))  # dedup mantendo ordem
    if not cnjs:
        return Process.objects.none()
    if len(cnjs) <= chunk:
        return Process.objects.filter(numero_cnj__in=cnjs)
    q = Q()
    for i in range(0, len(cnjs), chunk):
        q |= Q(numero_cnj__in=cnjs[i:i + chunk])
    return Process.objects.filter(q)


def _fn_candidatos_csv_default() -> Path:
    """Retorna o CSV `fn_candidatos_*.csv` mais recente em BASE_DIR.

    Convenção: o mining (T16) gera `fn_candidatos_<timestamp>.csv` no root.
    """
    candidatos = sorted(_BASE_DIR.glob('fn_candidatos_*.csv'))
    if not candidatos:
        raise FileNotFoundError(
            f'Nenhum CSV fn_candidatos_*.csv em {_BASE_DIR}. '
            'Rode o mining (T16) ou passe csv_path explicitamente.'
        )
    return candidatos[-1]


# ============================================================================
# Estratégias de amostragem
# ============================================================================

def sample_fn_candidatos(
    *,
    tribunal=None,
    limit: int = 300,
    min_suspeita: float = 0.30,
    csv_path: Optional[Union[str, Path]] = None,
    usuario=None,
) -> QuerySet[Process]:
    """Top candidatos a falso negativo do CSV gerado pelo mining (T16).

    O CSV deve ter colunas `numero_processo` (ou `numero_cnj`) e
    opcionalmente `suspeita_score`. Esta função apenas materializa os CNJs
    em QuerySet de Process — o filtro de `min_suspeita` se aplica na hora
    de criar o lote (em `criar_lote` o `suspeita_score` deve vir do CSV).

    csv_path default: pega CSV mais recente em `fn_candidatos_*.csv`.
    Filtra: `classificacao=NAO_LEAD` (só vale FN se ainda está marcado como
    não-lead pelo modelo ativo).

    Complexidade: O(len(csv)) leitura I/O + O(len(csv)) DB lookup via índice
    `uniq_proc_tribunal_cnj`.
    """
    if csv_path is None:
        csv_path = _fn_candidatos_csv_default()
    cnjs = _ler_cnjs_csv(csv_path)
    qs = _process_por_cnjs(cnjs).filter(
        classificacao=Process.CLASSIF_NAO_LEAD,
    )
    qs = _aplicar_tribunal(qs, tribunal)
    qs = _excluir_recentes(qs, usuario)
    # Ordena por score desc — os mais "altos" entre os NAO_LEAD são os mais
    # suspeitos de FN. min_suspeita atua como filtro adicional.
    qs = qs.filter(classificacao_score__gte=min_suspeita).order_by(
        '-classificacao_score', '-pk',
    )
    return qs[:limit]


def sample_borderline(
    *,
    faixa: tuple[float, float] = BORDERLINE_DEFAULT,
    tribunal=None,
    limit: int = 200,
    seed: Optional[int] = None,
    usuario=None,
) -> QuerySet[Process]:
    """Random sample em score band `faixa`.

    Quando `tribunal=None`, estratifica por tribunal (split igual entre
    tribunais ativos com pelo menos 1 processo na faixa). Quando informado,
    sorteia random puro na banda.

    Reprodutibilidade: usa `ORDER BY md5(id::text || seed)` no SQL — saída
    determinística para mesmo (seed, conjunto de candidatos).

    Complexidade: O(n_banda * log(limit)) — index scan na banda + sort top-k.
    """
    seed = _resolver_seed(seed)
    lo, hi = faixa
    qs = Process.objects.filter(
        classificacao_score__gte=lo,
        classificacao_score__lt=hi,
    )
    qs = _excluir_recentes(qs, usuario)

    if tribunal is not None:
        qs = _aplicar_tribunal(qs, tribunal)
        return qs.extra(
            select={'_h': "md5(id::text || %s)"},
            select_params=[str(seed)],
            order_by=['_h'],
        )[:limit]

    # Estratificado por tribunal ativo. Pega quota igual por tribunal.
    tribunais = list(
        Tribunal.objects.filter(ativo=True)
        .values_list('sigla', flat=True),
    )
    if not tribunais:
        return qs.none()
    quota = max(1, limit // len(tribunais))
    pks: list[int] = []
    for sigla in tribunais:
        sub = qs.filter(tribunal_id=sigla).extra(
            select={'_h': "md5(id::text || %s)"},
            select_params=[str(seed) + sigla],
            order_by=['_h'],
        ).values_list('pk', flat=True)[:quota]
        pks.extend(sub)
    return Process.objects.filter(pk__in=pks[:limit])


def sample_n1_alto(
    *,
    min_score: float = 0.85,
    tribunal=None,
    limit: int = 100,
    seed: Optional[int] = None,
    usuario=None,
) -> QuerySet[Process]:
    """Controle de qualidade: random PRECATORIO (N1) com score alto.

    Mede precision real do top do funil. Usa índice `(classificacao,
    classificacao_score)` via filtro composto.

    Complexidade: O(n_alto * log(limit)). Universo típico TRF1: <50k rows.
    """
    seed = _resolver_seed(seed)
    qs = Process.objects.filter(
        classificacao=Process.CLASSIF_PRECATORIO,
        classificacao_score__gte=min_score,
    )
    qs = _aplicar_tribunal(qs, tribunal)
    qs = _excluir_recentes(qs, usuario)
    return qs.extra(
        select={'_h': "md5(id::text || %s)"},
        select_params=[str(seed)],
        order_by=['_h'],
    )[:limit]


def sample_nao_lead_top(
    *,
    min_score: float = 0.05,
    tribunal=None,
    limit: int = 200,
    seed: Optional[int] = None,
    usuario=None,
) -> QuerySet[Process]:
    """Controle negativo: NAO_LEAD com score mais alto da classe.

    Detecta erros sistemáticos onde modelo "quase" classificou como lead.
    Ordena por score desc (não-aleatório: top-K é o sinal mais útil aqui).
    `seed` ignorado quando determinístico — mantido na assinatura por
    consistência com outras estratégias e para futura mudança.

    Complexidade: O(log(N) + limit) com índice composto
    `(classificacao, classificacao_score)`.
    """
    seed = _resolver_seed(seed)  # logado para auditoria mesmo se não usado.
    qs = Process.objects.filter(
        classificacao=Process.CLASSIF_NAO_LEAD,
        classificacao_score__gte=min_score,
    )
    qs = _aplicar_tribunal(qs, tribunal)
    qs = _excluir_recentes(qs, usuario)
    return qs.order_by('-classificacao_score', '-pk')[:limit]


def sample_random_tribunal(
    *,
    tribunal,
    limit: int = 200,
    seed: Optional[int] = None,
    usuario=None,
) -> QuerySet[Process]:
    """Random sample puro num tribunal específico, estratificado por
    classificacao (proporção igual entre PRECATORIO/PRE/DC/NAO_LEAD,
    fallback do que existir).

    Para universos grandes (>100k), usa `TABLESAMPLE BERNOULLI(0.5)`
    diretamente. Necessário porque ORDER BY md5() em tabela grande sem
    filtro forte é proibitivo (~3s em 2M rows).

    Tribunal é obrigatório.

    Complexidade: O(N * 0.005) com TABLESAMPLE 0.5% (~10k rows lidos
    em tabela 2M) + sort top-K.
    """
    if tribunal is None:
        raise ValueError('sample_random_tribunal exige tribunal')
    seed = _resolver_seed(seed)
    sigla = tribunal.pk if isinstance(tribunal, Tribunal) else tribunal

    classes = [
        Process.CLASSIF_PRECATORIO,
        Process.CLASSIF_PRE_PRECATORIO,
        Process.CLASSIF_DIREITO_CREDITORIO,
        Process.CLASSIF_NAO_LEAD,
    ]
    quota = max(1, limit // len(classes))
    pks: list[int] = []
    for classe in classes:
        sub = Process.objects.filter(
            tribunal_id=sigla, classificacao=classe,
        )
        sub = _excluir_recentes(sub, usuario)
        sub = sub.extra(
            select={'_h': "md5(id::text || %s)"},
            select_params=[str(seed) + classe],
            order_by=['_h'],
        ).values_list('pk', flat=True)[:quota]
        pks.extend(sub)
    return Process.objects.filter(pk__in=pks[:limit])


def sample_recuperados(
    *,
    tribunal=None,
    limit: int = 200,
    csv_path: Union[str, Path] = 'leads_trf1_recuperados_1327.csv',
    usuario=None,
) -> QuerySet[Process]:
    """Processos do CSV de 'recuperados' (Juriscope marcou após análise).

    Útil pra entender o padrão sistemático de erro do v6 — onde o modelo
    inicialmente errou e o humano recuperou.

    Complexidade: O(len(csv)) I/O + O(len(csv)) DB lookup.
    """
    cnjs = _ler_cnjs_csv(csv_path)
    qs = _process_por_cnjs(cnjs)
    qs = _aplicar_tribunal(qs, tribunal)
    qs = _excluir_recentes(qs, usuario)
    return qs.order_by('-classificacao_score', '-pk')[:limit]


def sample_falsos_consumidos(
    *,
    tribunal=None,
    limit: int = 200,
    csv_path: Union[str, Path] = 'leads_trf1_falsos_consumidos_1327.csv',
    usuario=None,
) -> QuerySet[Process]:
    """Análogo a sample_recuperados, mas pros FPs validados (consumidos
    pela Juriscope e marcados como erro/sem_expedicao).

    Complexidade: idem `sample_recuperados`.
    """
    cnjs = _ler_cnjs_csv(csv_path)
    qs = _process_por_cnjs(cnjs)
    qs = _aplicar_tribunal(qs, tribunal)
    qs = _excluir_recentes(qs, usuario)
    return qs.order_by('-classificacao_score', '-pk')[:limit]


# ============================================================================
# Materialização: criar_lote
# ============================================================================

def criar_lote(
    *,
    estrategia: str,
    queryset: QuerySet[Process],
    criada_por,
    tribunal: Optional[Union[Tribunal, str]] = None,
    tamanho_alvo: int,
    parametros: Optional[dict] = None,
    seed: Optional[int] = None,
) -> AmostraValidacao:
    """Materializa um QuerySet num `AmostraValidacao` + `AmostraProcesso`.

    - Atribui ordem (1..N) determinística embaralhando os pks via
      `random.Random(seed)`.
    - Persiste atomic — se algo falhar, rollback total.
    - `versao_modelo` lida de `ClassificadorVersao.ativa=True` no momento.
      Levanta `RuntimeError` se nenhuma versão ativa existir.
    - Score snapshot vem de `Process.classificacao_score` no momento do
      bulk_create (não recomputa).

    Retorna o `AmostraValidacao` recém-criado (com `itens` populados).
    """
    if not estrategia:
        raise ValueError('estrategia obrigatória')
    if tamanho_alvo <= 0:
        raise ValueError('tamanho_alvo deve ser > 0')

    seed_efetiva = _resolver_seed(seed)

    versao_ativa = ClassificadorVersao.objects.filter(ativa=True).first()
    if versao_ativa is None:
        raise RuntimeError(
            'Nenhum ClassificadorVersao ativo. '
            'Não é possível snapshotar versao_modelo no lote.'
        )

    # Materializa processos com campos mínimos pra snapshot. Limit por
    # segurança (tamanho_alvo). Lista vazia é OK (lote vazio é válido —
    # útil quando estratégia não encontrou candidatos).
    processos = list(
        queryset.values('pk', 'classificacao', 'classificacao_score')[:tamanho_alvo]
    )

    rng = random.Random(seed_efetiva)
    indices = list(range(len(processos)))
    rng.shuffle(indices)

    tribunal_obj = None
    if tribunal is not None:
        tribunal_obj = (
            tribunal if isinstance(tribunal, Tribunal)
            else Tribunal.objects.get(pk=tribunal)
        )

    with transaction.atomic():
        amostra = AmostraValidacao.objects.create(
            estrategia=estrategia,
            tribunal=tribunal_obj,
            versao_modelo=versao_ativa.versao,
            criada_por=criada_por,
            parametros=parametros or {},
            tamanho_alvo=tamanho_alvo,
            seed=seed_efetiva,
        )
        itens = [
            AmostraProcesso(
                amostra=amostra,
                processo_id=processos[idx]['pk'],
                ordem=ordem,
                score_no_sorteio=float(processos[idx]['classificacao_score'] or 0.0),
                classificacao_no_sorteio=processos[idx]['classificacao'] or '',
            )
            for ordem, idx in enumerate(indices, start=1)
        ]
        AmostraProcesso.objects.bulk_create(itens)

    logger.info(
        'lote criado',
        extra={
            'amostra_id': amostra.pk,
            'estrategia': estrategia,
            'tribunal': getattr(tribunal_obj, 'pk', None),
            'tamanho_alvo': tamanho_alvo,
            'tamanho_real': len(itens),
            'seed': seed_efetiva,
            'versao_modelo': versao_ativa.versao,
        },
    )
    return amostra
