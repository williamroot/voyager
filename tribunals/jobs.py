"""Jobs RQ pro app tribunals — classificação de leads em batch."""
import logging
import math
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.db.models import F, Max, Q
from django.utils import timezone
from django_rq import job

from .classificador import VERSAO, classificar_e_persistir, classificar_shadow
from .models import Movimentacao, Process

logger = logging.getLogger('voyager.tribunals.jobs')


@job('classificacao', timeout=14400)
def reclassificar_recentes(dias: int = 7, batch_size: int = 1000,
                           cap_nunca_classificados: int = 5_000_000,
                           paralelizar: bool = True) -> dict:
    """Re-classifica processos com mov inserida nos últimos N dias.

    Idempotente — atualiza Process.classificacao_em a cada run.
    Re-classifica também processos NUNCA classificados (classificacao_em IS NULL)
    pra cobrir backlog inicial. Cap default 500k é suficiente pra rodar
    de hora em hora e drenar ~2.4M backlog em alguns dias.

    Quando `paralelizar=True` (default), splitta o trabalho em N jobs
    `reclassificar_batch` enfileirados na fila `classificacao` — workers
    paralelos drenam. Quando False, processa sequencialmente neste job
    (útil pra runs pequenos / debug).
    """
    cutoff = timezone.now() - timedelta(days=dias)

    pids_qs = (
        Movimentacao.objects.filter(inserido_em__gte=cutoff)
        .values_list('processo_id', flat=True)
        .distinct()
    )
    pids_recentes = set(pids_qs)
    nunca_classificados = set(
        Process.objects.filter(classificacao_em__isnull=True)
        .values_list('id', flat=True)[:cap_nunca_classificados]
    )
    pids_alvo = list(pids_recentes | nunca_classificados)
    logger.info(
        'reclassificar_recentes: alvo=%d (recentes=%d novos=%d) paralelizar=%s',
        len(pids_alvo), len(pids_recentes), len(nunca_classificados), paralelizar,
    )

    if paralelizar and len(pids_alvo) > batch_size:
        # Enfileira N batches na fila classificacao — workers drenam paralelo
        n_jobs = 0
        for i in range(0, len(pids_alvo), batch_size):
            batch = pids_alvo[i:i+batch_size]
            reclassificar_batch.delay(batch)
            n_jobs += 1
        logger.info('reclassificar_recentes split em %d batches de %d', n_jobs, batch_size)
        return {'enfileirados': n_jobs, 'pids_total': len(pids_alvo), 'versao': VERSAO}

    n_done = 0; n_fail = 0
    for i in range(0, len(pids_alvo), batch_size):
        batch_pids = pids_alvo[i:i+batch_size]
        for p in Process.objects.filter(pk__in=batch_pids).iterator(chunk_size=200):
            try:
                classificar_e_persistir(p, registrar_log=True)
                n_done += 1
            except Exception as exc:
                logger.warning('classif fail pid=%d: %s', p.pk, exc)
                n_fail += 1
        if (i // batch_size) % 10 == 0:
            logger.info('  progresso %d/%d', n_done, len(pids_alvo))

    logger.info('reclassificar_recentes done: ok=%d fail=%d versao=%s',
                n_done, n_fail, VERSAO)
    return {'classificados': n_done, 'falhas': n_fail, 'versao': VERSAO}


@job('classificacao', timeout=600)
def reclassificar_batch(process_ids: list[int]) -> dict:
    """Reclassifica um lote de processos passados por ID."""
    n_done = 0; n_fail = 0
    for p in Process.objects.filter(pk__in=process_ids).iterator(chunk_size=200):
        try:
            classificar_e_persistir(p, registrar_log=True)
            n_done += 1
        except Exception as exc:
            logger.warning('reclassificar_batch fail pid=%d: %s', p.pk, exc)
            n_fail += 1
    return {'classificados': n_done, 'falhas': n_fail}


_CLASSIF_BATCH_SIZE = 500
_CLASSIF_CAP_POR_RUN = 50_000


@job('classificacao', timeout=14400)
def reclassificar_por_prioridade(
    cap: int = _CLASSIF_CAP_POR_RUN,
    batch_size: int = _CLASSIF_BATCH_SIZE,
) -> dict:
    """Cron único de classificação por prioridade.

    Grupo 1 (prioridade): processos com classificacao_em < ultima_movimentacao_em
    ou nunca classificados — ordenados por ultima_movimentacao_em DESC (mais recentes primeiro).
    Grupo 2 (fallback): processos já classificados, ordenados por classificacao_em ASC
    (reclassifica os mais antigos quando o grupo 1 esgota).
    Idle quando todos os processos estão com classificação atualizada.
    """
    pids = list(
        Process.objects
        .filter(ultima_movimentacao_em__isnull=False)
        .filter(
            Q(classificacao_em__isnull=True) |
            Q(classificacao_em__lt=F('ultima_movimentacao_em'))
        )
        .order_by('-ultima_movimentacao_em')
        .values_list('id', flat=True)[:cap]
    )
    grupo = 'desatualizados'

    if not pids:
        pids = list(
            Process.objects
            .filter(classificacao_em__isnull=False)
            .order_by('classificacao_em')
            .values_list('id', flat=True)[:cap]
        )
        grupo = 'mais_antigos'

    if not pids:
        logger.info('reclassificar_por_prioridade: idle — todos processos atualizados')
        return {'status': 'idle', 'enfileirados': 0, 'versao': VERSAO}

    n_jobs = 0
    for i in range(0, len(pids), batch_size):
        reclassificar_batch.delay(pids[i:i + batch_size])
        n_jobs += 1

    logger.info(
        'reclassificar_por_prioridade: grupo=%s pids=%d batches=%d versao=%s',
        grupo, len(pids), n_jobs, VERSAO,
    )
    return {'grupo': grupo, 'pids_total': len(pids), 'enfileirados': n_jobs, 'versao': VERSAO}


# ============================================================================
# Shadow mode (T19) — jobs assíncronos e comparação v_ativa x v_shadow
# ============================================================================


@job('classificacao', timeout=60)
def classificar_shadow_async(processo_id: int) -> int:
    """Aplica shadow models pra um processo. Tolerante a processo deletado."""
    try:
        p = Process.objects.get(pk=processo_id)
    except Process.DoesNotExist:
        return 0
    try:
        return classificar_shadow(p)
    except Exception as exc:
        logger.warning('classificar_shadow_async pid=%d falhou: %s', processo_id, exc)
        return 0


def _ks_2samp(a: list[float], b: list[float]) -> float:
    """Estatística do teste KS (max |CDF_a - CDF_b|) sem scipy.

    Retorna 0.0 se uma das amostras estiver vazia (não há diferença mensurável).
    """
    if not a or not b:
        return 0.0
    a_sorted = sorted(a)
    b_sorted = sorted(b)
    todos = sorted(set(a_sorted) | set(b_sorted))
    na, nb = float(len(a_sorted)), float(len(b_sorted))
    d_max = 0.0
    i = j = 0
    for v in todos:
        while i < len(a_sorted) and a_sorted[i] <= v:
            i += 1
        while j < len(b_sorted) and b_sorted[j] <= v:
            j += 1
        d = abs(i / na - j / nb)
        if d > d_max:
            d_max = d
    return d_max


def _confusion_matrix(pairs: list[tuple[str, str]]) -> dict:
    """Constrói matriz {ativa_cat: {shadow_cat: count}} + agreement."""
    matriz: dict[str, dict[str, int]] = {}
    concord = 0
    total = 0
    for ativa, shadow in pairs:
        ativa_k = ativa or 'NULL'
        shadow_k = shadow or 'NULL'
        matriz.setdefault(ativa_k, {}).setdefault(shadow_k, 0)
        matriz[ativa_k][shadow_k] += 1
        total += 1
        if ativa_k == shadow_k:
            concord += 1
    return {
        'matriz': matriz,
        'total': total,
        'concordantes': concord,
        'agreement_rate': (concord / total) if total else 0.0,
    }


def _format_relatorio_markdown(stats: dict) -> str:
    """Renderiza o relatório de comparação em markdown."""
    lines = []
    lines.append(f"# Shadow comparison · {stats['versao_a']} x {stats['versao_b']}")
    lines.append('')
    lines.append(f"Gerado em: {stats['gerado_em']}")
    lines.append(f"Janela: últimos {stats['dias']} dia(s)")
    lines.append(f"Total de pares comparados: **{stats['total']}**")
    lines.append('')
    lines.append('## Concordância (categoria)')
    lines.append('')
    lines.append(f"- Agreement rate: **{stats['agreement_rate']:.4f}** "
                 f"({stats['concordantes']}/{stats['total']})")
    lines.append(f"- Disagreements: **{stats['total_disagreements']}**")
    lines.append('')
    lines.append('### Confusion matrix (linha = atual · coluna = shadow)')
    lines.append('')
    if stats['matriz']:
        cats = sorted({c for row in stats['matriz'].values() for c in row}
                      | set(stats['matriz'].keys()))
        header = '| atual\\shadow | ' + ' | '.join(cats) + ' |'
        sep = '|' + '---|' * (len(cats) + 1)
        lines.append(header)
        lines.append(sep)
        for ativa_cat in cats:
            row = stats['matriz'].get(ativa_cat, {})
            cells = [str(row.get(c, 0)) for c in cats]
            lines.append(f'| {ativa_cat} | ' + ' | '.join(cells) + ' |')
    else:
        lines.append('_sem dados_')
    lines.append('')
    lines.append('## Distribuição de scores (KS test)')
    lines.append('')
    lines.append(f"- KS statistic: **{stats['ks_statistic']:.4f}**")
    lines.append(f"- Score médio {stats['versao_a']}: {stats['score_med_a']:.4f}")
    lines.append(f"- Score médio {stats['versao_b']}: {stats['score_med_b']:.4f}")
    lines.append(f"- Delta médio (b - a): {stats['delta_med']:+.4f}")
    lines.append('')
    lines.append('## Por tribunal')
    lines.append('')
    if stats['por_tribunal']:
        lines.append('| tribunal | total | agreement | disagree | ks |')
        lines.append('|---|---|---|---|---|')
        for tid, t_stats in sorted(stats['por_tribunal'].items()):
            lines.append(
                f"| {tid} | {t_stats['total']} | "
                f"{t_stats['agreement_rate']:.4f} | "
                f"{t_stats['disagreements']} | "
                f"{t_stats['ks']:.4f} |"
            )
    else:
        lines.append('_sem dados_')
    lines.append('')
    lines.append('## Top disagreements (|score_b - score_a| maior)')
    lines.append('')
    if stats['top_disagreements']:
        lines.append(
            '| # | CNJ | tribunal | cat_atual | cat_shadow | score_atual | score_shadow | delta |'
        )
        lines.append('|---|---|---|---|---|---|---|---|')
        for i, d in enumerate(stats['top_disagreements'], 1):
            lines.append(
                f"| {i} | {d['cnj']} | {d['tribunal']} | "
                f"{d['cat_atual']} | {d['cat_shadow']} | "
                f"{d['score_atual']:.4f} | {d['score_shadow']:.4f} | "
                f"{d['delta']:+.4f} |"
            )
    else:
        lines.append('_nenhum_')
    lines.append('')
    return '\n'.join(lines)


@job('classificacao', timeout=600)
def comparar_shadow(versao_a: str = 'v6', versao_b: str = 'v7',
                    dias: int = 7, output_path: str | None = None) -> dict:
    """Compara classificação atual (Process.classificacao, versão `versao_a`)
    contra ClassificacaoShadowLog (versão `versao_b`).

    Produz relatório markdown em `.ia/SHADOW_COMPARISON_YYYYMMDD.md`.
    Retorna estatísticas resumidas.
    """
    from .models import ClassificacaoShadowLog  # noqa: PLC0415

    since = timezone.now() - timedelta(days=max(1, int(dias)))

    rows = list(
        ClassificacaoShadowLog.objects
        .filter(versao_shadow=versao_b, criada_em__gte=since)
        .select_related('processo')
        .values(
            'processo_id', 'processo__numero_cnj', 'processo__tribunal_id',
            'processo__classificacao', 'processo__classificacao_score',
            'score', 'categoria', 'criada_em',
        )
        .order_by('processo_id', '-criada_em')
    )

    # Mantém só o shadow log mais recente por processo (rows já ordenadas DESC).
    seen: set[int] = set()
    latest: list[dict] = []
    for r in rows:
        pid = r['processo_id']
        if pid in seen:
            continue
        seen.add(pid)
        latest.append(r)

    pairs: list[tuple[str, str]] = []
    scores_a: list[float] = []
    scores_b: list[float] = []
    per_tribunal: dict[int, dict] = {}
    disagreements: list[dict] = []

    for r in latest:
        cat_atual = r['processo__classificacao'] or ''
        cat_shadow = r['categoria'] or ''
        score_atual = r['processo__classificacao_score']
        score_shadow = r['score']
        tribunal_id = r['processo__tribunal_id']

        pairs.append((cat_atual, cat_shadow))
        if score_atual is not None:
            scores_a.append(float(score_atual))
        scores_b.append(float(score_shadow))

        t = per_tribunal.setdefault(tribunal_id, {
            'pairs': [], 'scores_a': [], 'scores_b': [],
        })
        t['pairs'].append((cat_atual, cat_shadow))
        if score_atual is not None:
            t['scores_a'].append(float(score_atual))
        t['scores_b'].append(float(score_shadow))

        if cat_atual != cat_shadow and score_atual is not None:
            disagreements.append({
                'cnj': r['processo__numero_cnj'],
                'tribunal': tribunal_id,
                'cat_atual': cat_atual,
                'cat_shadow': cat_shadow,
                'score_atual': float(score_atual),
                'score_shadow': float(score_shadow),
                'delta': float(score_shadow) - float(score_atual),
            })

    cm = _confusion_matrix(pairs)
    ks = _ks_2samp(scores_a, scores_b)
    score_med_a = sum(scores_a) / len(scores_a) if scores_a else 0.0
    score_med_b = sum(scores_b) / len(scores_b) if scores_b else 0.0

    per_tribunal_stats: dict[int, dict] = {}
    for tid, t in per_tribunal.items():
        t_cm = _confusion_matrix(t['pairs'])
        per_tribunal_stats[tid] = {
            'total': t_cm['total'],
            'agreement_rate': t_cm['agreement_rate'],
            'disagreements': t_cm['total'] - t_cm['concordantes'],
            'ks': _ks_2samp(t['scores_a'], t['scores_b']),
        }

    disagreements.sort(key=lambda d: abs(d['delta']), reverse=True)
    top_disagreements = disagreements[:50]

    stats = {
        'versao_a': versao_a,
        'versao_b': versao_b,
        'dias': dias,
        'gerado_em': timezone.now().isoformat(timespec='seconds'),
        'total': cm['total'],
        'concordantes': cm['concordantes'],
        'agreement_rate': cm['agreement_rate'],
        'total_disagreements': cm['total'] - cm['concordantes'],
        'matriz': cm['matriz'],
        'ks_statistic': ks,
        'score_med_a': score_med_a,
        'score_med_b': score_med_b,
        'delta_med': score_med_b - score_med_a,
        'por_tribunal': per_tribunal_stats,
        'top_disagreements': top_disagreements,
    }

    if output_path is None:
        base_dir = Path(getattr(settings, 'BASE_DIR', Path('.'))) / '.ia'
        base_dir.mkdir(parents=True, exist_ok=True)
        output_path = str(base_dir / f'SHADOW_COMPARISON_{date.today():%Y%m%d}.md')

    try:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(_format_relatorio_markdown(stats), encoding='utf-8')
    except Exception as e:
        logger.warning('comparar_shadow: write_text falhou %s: %s', output_path, e)

    logger.info(
        'comparar_shadow: %s x %s total=%d agreement=%.4f ks=%.4f relatorio=%s',
        versao_a, versao_b, stats['total'], stats['agreement_rate'], ks, output_path,
    )
    return {
        'agreement_rate': stats['agreement_rate'],
        'total': stats['total'],
        'total_disagreements': stats['total_disagreements'],
        'ks_statistic': stats['ks_statistic'],
        'report_path': str(output_path),
    }


def comparar_shadow_wrapper() -> None:
    """Wrapper de scheduler — enfileira o job na fila `classificacao`."""
    try:
        comparar_shadow.delay(versao_a='v6', versao_b='v7', dias=1)
    except Exception as exc:
        logger.warning('comparar_shadow_wrapper enqueue falhou: %s', exc)


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline semanal: mineração FN + criação de lotes de validação (T21)
# ─────────────────────────────────────────────────────────────────────────────

_FN_CSV_GLOB = 'fn_candidatos_*.csv'
_SYSTEM_USER_USERNAME = 'system_validacao'


def _repo_root() -> Path:
    """Raiz do repo (mesmo cálculo usado em minerar_fn)."""
    return Path(__file__).resolve().parents[1]


def _ultimo_csv_mining(tribunal_sigla: str | None = None) -> Path | None:
    """Encontra CSV mais recente gerado pelo `minerar_fn`.

    `minerar_fn` escreve em `<repo>/data/fn_candidatos_YYYYMMDD.csv` por
    default (sem distinção por tribunal — o filtro por sigla é feito em
    `sample_fn_candidatos` via FK `tribunal` no Process). Aceita também
    arquivos na raiz do repo como fallback (variantes históricas).
    """
    root = _repo_root()
    candidatos: list[Path] = []
    for base in (root / 'data', root):
        if base.exists():
            candidatos.extend(base.glob(_FN_CSV_GLOB))
    if not candidatos:
        return None
    candidatos.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidatos[0]


def _get_or_create_system_user():
    """Retorna o usuário 'system_validacao' (cria com is_active=False).

    Idempotente — múltiplas chamadas não duplicam.
    """
    from django.contrib.auth import get_user_model
    User = get_user_model()
    user, created = User.objects.get_or_create(
        username=_SYSTEM_USER_USERNAME,
        defaults={'is_active': False},
    )
    if created:
        logger.info('usuário %s criado (is_active=False)', _SYSTEM_USER_USERNAME)
    return user


def _notificar_lotes_semanais(resultados: dict) -> dict:
    """Envia notificação aos usuários com can_validate_lead.

    Canais (best-effort, falha em um não bloqueia o outro):
    - Slack se SLACK_WEBHOOK_URL definido
    - Email Django se houver destinatários com permission can_validate_lead

    Retorna dict com {'slack': bool, 'email': int} para auditoria/teste.
    """
    from django.contrib.auth import get_user_model
    from django.contrib.auth.models import Permission
    from django.core.mail import send_mail

    out = {'slack': False, 'email': 0}

    total_lotes = sum(1 for r in resultados.values() if r.get('lote_id'))
    total_procs = sum(r.get('count', 0) for r in resultados.values())

    if total_lotes == 0:
        logger.info('notificar_lotes_semanais: nenhum lote criado, pulando')
        return out

    # Slack — best-effort
    webhook = getattr(settings, 'SLACK_WEBHOOK_URL', '') or ''
    if webhook:
        try:
            import requests
            tribs = ', '.join(
                f'{s}={r.get("count", 0)}'
                for s, r in sorted(resultados.items())
                if r.get('lote_id')
            )
            requests.post(
                webhook,
                json={
                    'text': (
                        f'Voyager: {total_lotes} novos lotes de validação '
                        f'({total_procs} processos). Tribunais: {tribs}. '
                        f'Acesse /dashboard/leads/validacao/'
                    ),
                },
                timeout=5,
            )
            out['slack'] = True
        except Exception:
            logger.exception('Slack notif falhou')

    # Email — best-effort
    try:
        User = get_user_model()
        perm = Permission.objects.filter(codename='can_validate_lead').first()
        if perm is not None:
            usuarios = (
                User.objects.filter(
                    Q(groups__permissions=perm) | Q(user_permissions=perm),
                    is_active=True,
                )
                .exclude(email='')
                .exclude(email__isnull=True)
                .distinct()
            )
            emails = list(usuarios.values_list('email', flat=True))
            if emails:
                send_mail(
                    subject=f'[Voyager] {total_lotes} novos lotes de validação',
                    message=(
                        f'Foram criados {total_lotes} novos lotes semanais '
                        f'com {total_procs} processos para validação.\n\n'
                        f'Acesse: /dashboard/leads/validacao/'
                    ),
                    from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@voyager'),
                    recipient_list=emails,
                    fail_silently=True,
                )
                out['email'] = len(emails)
    except Exception:
        logger.exception('Email notif falhou')

    return out


@job('default', timeout=3600)
def gerar_lotes_semanais_fn(
    *,
    tribunais=None,
    tamanho_por_tribunal: int = 300,
    notificar: bool = True,
) -> dict:
    """Pipeline semanal: minera FN candidates → cria lotes → notifica.

    Para cada tribunal ativo (ou os passados em `tribunais`):
      1. Roda `minerar_fn` (gera CSV com top candidatos).
      2. Se houver ≥1 NAO_LEAD encontrado, cria `AmostraValidacao` via
         `sampling.criar_lote(estrategia='fn_candidatos')`.
      3. Coleta count + lote_id no dict de retorno.
      4. Erros num tribunal não bloqueiam os demais (try/except por sigla).

    Por fim notifica usuários com `can_validate_lead` (Slack + email,
    best-effort) se `notificar=True`.

    Retorna dict {sigla: {csv_path, lote_id, count, error?}}.
    """
    from django.core.management import call_command

    from tribunals import sampling
    from tribunals.models import Tribunal

    if tribunais is None:
        tribunais = list(
            Tribunal.objects.filter(ativo=True).values_list('sigla', flat=True)
        )

    resultados: dict[str, dict] = {}
    user_sistema = _get_or_create_system_user()

    for sigla in tribunais:
        try:
            tribunal_obj = Tribunal.objects.get(pk=sigla)
        except Tribunal.DoesNotExist:
            logger.warning('gerar_lotes_semanais_fn: tribunal "%s" não existe', sigla)
            resultados[sigla] = {
                'lote_id': None, 'count': 0, 'error': 'tribunal_inexistente',
            }
            continue

        try:
            # 1. Mining (gera CSV)
            call_command(
                'minerar_fn',
                '--tribunal', sigla,
                '--limit', str(tamanho_por_tribunal * 3),
            )
            csv_path = _ultimo_csv_mining(sigla)
            if csv_path is None:
                logger.warning(
                    'gerar_lotes_semanais_fn: nenhum CSV encontrado para %s', sigla,
                )
                resultados[sigla] = {
                    'lote_id': None, 'count': 0, 'csv_path': None,
                }
                continue

            # 2. Sample + criar lote
            qs = sampling.sample_fn_candidatos(
                tribunal=tribunal_obj,
                limit=tamanho_por_tribunal,
                csv_path=csv_path,
            )
            count = qs.count()
            if count == 0:
                logger.warning(
                    'gerar_lotes_semanais_fn: 0 candidatos FN pra %s, pulando lote',
                    sigla,
                )
                resultados[sigla] = {
                    'lote_id': None, 'count': 0, 'csv_path': str(csv_path),
                }
                continue

            lote = sampling.criar_lote(
                estrategia='fn_candidatos',
                queryset=qs,
                criada_por=user_sistema,
                tribunal=tribunal_obj,
                tamanho_alvo=tamanho_por_tribunal,
                parametros={
                    'csv_input': str(csv_path),
                    'cron': 'semanal',
                    'gerado_em': timezone.now().isoformat(timespec='seconds'),
                },
            )
            resultados[sigla] = {
                'lote_id': lote.pk,
                'count': count,
                'csv_path': str(csv_path),
            }
            logger.info(
                'lote semanal criado: tribunal=%s lote=%s count=%s',
                sigla, lote.pk, count,
            )
        except Exception:
            logger.exception('Erro gerando lote semanal pra %s', sigla)
            resultados[sigla] = {'lote_id': None, 'count': 0, 'error': True}

    # 3. Notificar
    if notificar:
        try:
            _notificar_lotes_semanais(resultados)
        except Exception:
            logger.exception('Falha notificando lotes semanais')

    return resultados


# ─────────────────────────────────────────────────────────────────────────────
# Consumo de leads assíncrono — gravação idempotente por lote_id
# ─────────────────────────────────────────────────────────────────────────────


@job('leads_consumo', timeout=1800)
def registrar_consumo_leads(cliente_id: int, consumos: list[dict],
                            lote_id: str) -> dict:
    """Grava LeadConsumption idempotente por (cliente, processo, lote_id).

    Replay (retry RQ) não duplica: filtra CNJs já gravados nesse lote.
    `consumos`: [{'cnj': str, 'resultado': str}, ...].
    """
    lote_uuid = uuid.UUID(str(lote_id))
    from .models import LeadConsumption
    validos = dict(LeadConsumption.RESULTADO_CHOICES)
    res_por_cnj = {}
    for c in consumos:
        if not isinstance(c, dict):
            continue
        cnj = (c.get('cnj') or '').strip()
        resultado = (c.get('resultado') or LeadConsumption.RESULTADO_PENDENTE).strip()
        if cnj and resultado in validos:
            res_por_cnj[cnj] = resultado
    if not res_por_cnj:
        return {'criados': 0, 'duplicados': 0, 'nao_encontrados': []}

    procs = {p.numero_cnj: p for p in
             Process.objects.filter(numero_cnj__in=list(res_por_cnj)).only('id', 'numero_cnj')}
    nao_encontrados = [c for c in res_por_cnj if c not in procs]

    ja = set(LeadConsumption.objects
             .filter(cliente_id=cliente_id, lote_id=lote_uuid,
                     processo__numero_cnj__in=list(procs))
             .values_list('processo__numero_cnj', flat=True))
    a_criar = [
        LeadConsumption(processo=p, cliente_id=cliente_id,
                        resultado=res_por_cnj[cnj], lote_id=lote_uuid)
        for cnj, p in procs.items() if cnj not in ja
    ]
    existentes_antes = len(ja)
    LeadConsumption.objects.bulk_create(a_criar, batch_size=1000,
                                        ignore_conflicts=True)
    total_agora = (LeadConsumption.objects
                   .filter(cliente_id=cliente_id, lote_id=lote_uuid,
                           processo__numero_cnj__in=list(procs))
                   .count())
    criados = total_agora - existentes_antes
    duplicados = len(procs) - criados
    return {'criados': criados, 'duplicados': duplicados,
            'nao_encontrados': nao_encontrados}
