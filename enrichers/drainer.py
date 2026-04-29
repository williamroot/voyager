"""Batch drainer do stream de resultados de enrichment.

Consumer único: lê eventos de `voyager:enrichment:results` e aplica todos
os writes em uma transação por batch. Elimina contenção (BufferMapping
LWLock) que ~500 workers escrevendo direto causavam.

Erros por-evento usam savepoint pra não envenenar o batch. Entries com
schema inválido vão pra DLQ stream em vez de loop infinito.
"""
from __future__ import annotations

import logging
import os
import re
import signal
import socket
import time

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from tribunals.models import (
    Assunto, ClasseJudicial, Movimentacao, Parte, Process, ProcessoParte,
)

from .parsers import (
    is_documento_mascarado,
    parse_data_br,
    parse_valor_brl,
    real_casa_com_mascara,
)
from .stream import (
    GROUP_NAME, STATUS_ERRO, STATUS_NAO_ENCONTRADO, STATUS_OK,
    STREAM_KEY, ensure_consumer_group, get_redis, parse_entry,
)

logger = logging.getLogger('voyager.enrichers.drainer')

DLQ_STREAM = 'voyager:enrichment:dlq'
DLQ_MAXLEN = 10_000

# Match estrito: "Procedimento Comum (1234)" → ('Procedimento Comum', '1234').
# Sem parênteses → fica o nome inteiro, codigo=''. Evita o pitfall do
# regex anterior `(.*?)(?:\s*\(?\s*(\d{2,5})\s*\)?)?` que era opcional e
# casava dígitos no meio do texto como "código" (ex.: "Tributário 12345
# algo" capturaria 12345).
CLASSE_COM_CODIGO_RE = re.compile(r'^(.+?)\s*\((\d{2,5})\)\s*$')


def _split_nome_codigo(texto: str) -> tuple[str, str]:
    if not texto:
        return '', ''
    m = CLASSE_COM_CODIGO_RE.match(texto)
    if m:
        return m.group(1).strip()[:255], m.group(2)[:20]
    return texto.strip()[:255], ''


# ---------- normalização ----------

def normalize_dados(dados: dict) -> dict:
    """Bruto extraído pelo worker → campos prontos pra persistir.

    Valores ausentes/inválidos não entram no dict (não sobrescrevem com
    None ao aplicar com setattr).
    """
    out: dict = {}
    if 'classe' in dados:
        out['classe_nome'], out['classe_codigo'] = _split_nome_codigo(dados['classe'] or '')
    if 'assunto' in dados:
        out['assunto_nome'], out['assunto_codigo'] = _split_nome_codigo(dados['assunto'] or '')
    if 'data_autuacao' in dados:
        dt = parse_data_br(dados['data_autuacao'])
        if dt is not None:
            out['data_autuacao'] = dt.date()
    if 'valor_causa' in dados:
        valor = parse_valor_brl(dados['valor_causa'])
        if valor is not None:
            out['valor_causa'] = valor
    if 'orgao_julgador' in dados:
        out['orgao_julgador_nome'] = (dados['orgao_julgador'] or '')[:255]
    if 'juizo' in dados:
        out['juizo'] = (dados['juizo'] or '')[:255]
    if 'segredo_justica' in dados:
        out['segredo_justica'] = bool(dados['segredo_justica'])
    return out


def upsert_catalogo(model, codigo: str, nome: str):
    """Upsert idempotente em ClasseJudicial/Assunto."""
    nome_final = (nome or codigo)[:255]
    model.objects.bulk_create(
        [model(codigo=codigo, nome=nome_final)],
        ignore_conflicts=True,
    )
    return model.objects.get(codigo=codigo)


def fallback_classe_via_djen(processo_id: int, classe_codigo_atual: str) -> dict:
    """Quando PJe não retorna classe, herda da movimentação DJEN mais
    recente. Idempotente: só preenche se Process estiver sem classe.

    Retorna dict pronto pra setattr (chaves: classe_codigo, classe_nome,
    classe_id) ou {} se já tem classe ou não tem movimentação."""
    if classe_codigo_atual:
        return {}
    ultima = (
        Movimentacao.objects
        .filter(processo_id=processo_id).exclude(codigo_classe='')
        .order_by('-data_disponibilizacao')
        .values('codigo_classe', 'nome_classe', 'classe_id')
        .first()
    )
    if not ultima:
        return {}
    return {
        'classe_codigo': ultima['codigo_classe'],
        'classe_nome': ultima['nome_classe'],
        'classe_id': ultima['classe_id'],
    }


# ---------- upsert de Parte ----------
# Preserva os 4 caminhos do código original (oab, doc real, doc
# mascarado+nome, sem-doc-sem-oab) — partial unique constraints garantem
# que bulk_create(ignore_conflicts) é race-safe. Aqui só roda 1 consumer,
# então "race" é limitado a outros writers eventuais (ingestão DJEN).

def upsert_parte(info: dict) -> Parte:
    documento = info.get('documento') or ''
    oab = info.get('oab') or ''
    nome = (info.get('nome') or '')[:255]
    base = {
        'nome': nome,
        'tipo_documento': info.get('tipo_documento') or '',
        'tipo': info.get('tipo') or 'desconhecido',
    }

    if oab:
        return _safe_upsert_parte(
            lookup={'oab': oab},
            defaults={**base, 'documento': documento},
        )

    if documento:
        if is_documento_mascarado(documento):
            candidatos = (
                Parte.objects
                .filter(nome=nome).exclude(documento='')
                .exclude(Q(documento__contains='X')
                         | Q(documento__contains='x')
                         | Q(documento__contains='*'))
            )
            for c in candidatos:
                if real_casa_com_mascara(c.documento, documento):
                    return c
            return _safe_upsert_parte(
                lookup={'nome': nome, 'documento': documento},
                defaults={**base, 'oab': ''},
            )
        return _safe_upsert_parte(
            lookup={'documento': documento},
            defaults={**base, 'oab': ''},
        )

    # CNPJ formatado completo (sem máscara) — match em Parte com mesmo
    # nome + CNPJ real preenchido. `__regex` usa parametrização do ORM
    # (`extra(where=...)` é deprecated no Django 4+).
    candidatos = Parte.objects.filter(
        nome=nome,
        documento__regex=r'^[0-9]{2}\.[0-9]{3}\.[0-9]{3}/[0-9]{4}-[0-9]{2}$',
    )
    if candidatos.count() == 1:
        return candidatos.first()

    return _safe_upsert_parte(
        lookup={'documento': '', 'oab': '', 'nome': nome, 'tipo': base['tipo']},
        defaults={'tipo_documento': base['tipo_documento']},
    )


def _safe_upsert_parte(*, lookup: dict, defaults: dict) -> Parte:
    """Lookup + insert idempotente. Usa `.first()` (com order_by pk) em
    vez de `.get()` pra ser robusto a duplicatas pré-existentes — antes
    das partial unique constraints existirem o caminho sem-doc-sem-oab
    chegou a gerar 64k+ duplicatas. `.get()` levantaria
    MultipleObjectsReturned em qualquer linha desse legado.
    """
    existing = Parte.objects.filter(**lookup).order_by('pk').first()
    if existing is not None:
        return _merge_and_save(existing, defaults)

    Parte.objects.bulk_create(
        [Parte(**{**lookup, **defaults})],
        ignore_conflicts=True,
    )
    existing = Parte.objects.filter(**lookup).order_by('pk').first()
    if existing is None:
        # bulk_create(ignore_conflicts) virou no-op + lookup não acha
        # nada — anomalia, log e re-tentar lookup amplo só pelo lookup
        # original (já é a query mais estreita). Levanta pra savepoint.
        raise IntegrityError(f'Parte não encontrada após upsert: lookup={lookup}')
    return _merge_and_save(existing, defaults)


def _merge_and_save(existing: Parte, defaults: dict) -> Parte:
    merged = _merge_doc_defaults(existing, defaults)
    dirty = {k: v for k, v in merged.items() if getattr(existing, k) != v}
    if dirty:
        for k, v in dirty.items():
            setattr(existing, k, v)
        try:
            existing.save(update_fields=list(dirty))
        except IntegrityError:
            pass  # outro writer atualizou — eventual consistency
    return existing


def _merge_doc_defaults(existing: Parte, defaults: dict) -> dict:
    """Protege doc real do existing contra downgrade pra mascarado/vazio."""
    if 'documento' not in defaults:
        return defaults
    doc_atual = existing.documento or ''
    doc_novo = defaults.get('documento') or ''
    atual_real = bool(doc_atual) and not is_documento_mascarado(doc_atual)
    novo_real = bool(doc_novo) and not is_documento_mascarado(doc_novo)
    if atual_real and not novo_real:
        return {**defaults, 'documento': doc_atual}
    if doc_atual and not doc_novo:
        return {**defaults, 'documento': doc_atual}
    return defaults


# ---------- aplicação ----------

def apply_event(event: dict) -> None:
    """Aplica um evento individual. Levanta exception em caso de falha —
    o caller usa savepoint pra isolar."""
    pid = event['process_id']
    try:
        processo = Process.objects.get(pk=pid)
    except Process.DoesNotExist:
        logger.warning('process desaparecido', extra={'process_id': pid})
        return

    # Idempotência em re-entrega (XACK falhou pós-commit / autoclaim depois
    # de um restart). Se o Process já foi enriquecido em momento posterior
    # ao scraped_at deste evento, pulamos. Isso impede que o contador
    # `enriquecimento_tentativas` cresça em loops de retry e que partes/
    # dados antigos sobrescrevam dados mais recentes.
    scraped_at = parse_datetime(event.get('scraped_at') or '')
    if (scraped_at is not None and processo.enriquecido_em is not None
            and processo.enriquecido_em >= scraped_at):
        logger.info('event mais antigo que enriquecido_em — skip', extra={
            'process_id': pid, 'event_scraped_at': event.get('scraped_at'),
            'enriquecido_em': processo.enriquecido_em.isoformat(),
        })
        return

    status = event['status']
    update_fields: list[str] = []

    if status == STATUS_OK:
        dados_norm = normalize_dados(event.get('dados') or {})

        if dados_norm.get('classe_codigo'):
            classe = upsert_catalogo(
                ClasseJudicial,
                dados_norm['classe_codigo'],
                dados_norm.get('classe_nome', ''),
            )
            processo.classe = classe
            update_fields.append('classe')
        if dados_norm.get('assunto_codigo'):
            assunto = upsert_catalogo(
                Assunto,
                dados_norm['assunto_codigo'],
                dados_norm.get('assunto_nome', ''),
            )
            processo.assunto = assunto
            update_fields.append('assunto')

        for fld in ('classe_codigo', 'classe_nome', 'assunto_codigo', 'assunto_nome',
                    'data_autuacao', 'valor_causa', 'orgao_julgador_nome',
                    'juizo', 'segredo_justica'):
            if fld in dados_norm:
                setattr(processo, fld, dados_norm[fld])
                update_fields.append(fld)

        # Partes — wipe + reinsert mantém ordem do enricher original
        ProcessoParte.objects.filter(processo_id=pid).delete()
        for polo, lista in (event.get('partes') or {}).items():
            for principal in lista:
                p_principal = upsert_parte(principal)
                pp_principal = ProcessoParte.objects.create(
                    processo=processo, parte=p_principal,
                    polo=polo, papel=principal.get('papel') or '',
                    representa=None,
                )
                for rep in principal.get('representantes') or []:
                    p_rep = upsert_parte(rep)
                    if p_rep.pk == p_principal.pk:
                        continue
                    ProcessoParte.objects.create(
                        processo=processo, parte=p_rep,
                        polo=polo, papel=rep.get('papel') or 'ADVOGADO',
                        representa=pp_principal,
                    )

        now_ts = timezone.now()
        processo.enriquecido_em = now_ts
        processo.data_enriquecimento_tribunal = now_ts
        processo.enriquecimento_status = Process.ENRIQ_OK
        processo.enriquecimento_erro = ''
        update_fields.extend([
            'enriquecido_em', 'data_enriquecimento_tribunal',
            'enriquecimento_status', 'enriquecimento_erro',
        ])

    elif status == STATUS_NAO_ENCONTRADO:
        for k, v in fallback_classe_via_djen(pid, processo.classe_codigo).items():
            setattr(processo, k, v)
            update_fields.append('classe' if k == 'classe_id' else k)
        processo.enriquecido_em = timezone.now()
        processo.enriquecimento_status = Process.ENRIQ_NAO_ENCONTRADO
        processo.enriquecimento_erro = ''
        update_fields.extend([
            'enriquecido_em', 'enriquecimento_status', 'enriquecimento_erro',
        ])

    elif status == STATUS_ERRO:
        for k, v in fallback_classe_via_djen(pid, processo.classe_codigo).items():
            setattr(processo, k, v)
            update_fields.append('classe' if k == 'classe_id' else k)
        processo.enriquecido_em = timezone.now()
        processo.enriquecimento_status = Process.ENRIQ_ERRO
        processo.enriquecimento_erro = (event.get('erro') or '')[:1000]
        processo.enriquecimento_tentativas = (processo.enriquecimento_tentativas or 0) + 1
        update_fields.extend([
            'enriquecido_em', 'enriquecimento_status',
            'enriquecimento_erro', 'enriquecimento_tentativas',
        ])

    if update_fields:
        # Dedup preservando ordem (Python 3.7+ dict ordenado)
        update_fields = list(dict.fromkeys(update_fields))
        processo.save(update_fields=update_fields)


def _route_parte(spec: dict) -> tuple[str, object]:
    """Roteia spec de Parte pra um dos 4 caminhos de unique constraint.
    Retorna (path, key) onde key é hashable.
    """
    doc = spec.get('documento') or ''
    oab = spec.get('oab') or ''
    nome = (spec.get('nome') or '')[:255]
    tipo = spec.get('tipo') or 'desconhecido'
    if oab:
        return ('oab', oab)
    if doc:
        if is_documento_mascarado(doc):
            return ('doc_masc', (nome, doc))
        return ('doc_real', doc)
    return ('sem_id', (nome, tipo))


def _bulk_upsert_partes(events_by_pid: dict) -> dict:
    """Recebe events ok e devolve mapping (path, key) → Parte.pk.

    Faz upsert em bulk por caminho de constraint (ignore_conflicts é
    race-safe via partial unique constraints), depois SELECT pra mapear
    chaves → IDs. Substitui ~30 queries por evento por ~10 queries no
    batch inteiro.
    """
    paths: dict[str, dict] = {p: {} for p in ('oab', 'doc_real', 'doc_masc', 'sem_id')}

    for ev in events_by_pid.values():
        if ev['status'] != STATUS_OK:
            continue
        for polo_lista in (ev.get('partes') or {}).values():
            for principal in polo_lista:
                path, key = _route_parte(principal)
                paths[path][key] = principal
                for rep in principal.get('representantes') or []:
                    rpath, rkey = _route_parte(rep)
                    paths[rpath][rkey] = rep

    spec_to_id: dict = {}

    # doc_masc: tenta primeiro casar com Parte existente que tenha doc REAL
    # com o mesmo nome (TRF1 expõe CNPJ completo, TRF3 mascara).
    if paths['doc_masc']:
        masked_names = list({nome for (nome, _) in paths['doc_masc'].keys()})
        real_cands = list(
            Parte.objects.filter(nome__in=masked_names)
            .exclude(documento='')
            .exclude(documento__contains='X')
            .exclude(documento__contains='x')
            .exclude(documento__contains='*')
            .values('pk', 'nome', 'documento')
        )
        cands_by_name: dict = {}
        for c in real_cands:
            cands_by_name.setdefault(c['nome'], []).append(c)
        not_matched_keys = []
        for key in list(paths['doc_masc'].keys()):
            nome, doc_masc = key
            matched = next(
                (c for c in cands_by_name.get(nome, [])
                 if real_casa_com_mascara(c['documento'], doc_masc)),
                None,
            )
            if matched:
                spec_to_id[('doc_masc', key)] = matched['pk']
            else:
                not_matched_keys.append(key)
        # Bulk insert pra os não-matched
        if not_matched_keys:
            Parte.objects.bulk_create([
                Parte(
                    nome=k[0], documento=k[1],
                    tipo_documento=paths['doc_masc'][k].get('tipo_documento') or '',
                    tipo=paths['doc_masc'][k].get('tipo') or 'desconhecido',
                    oab='',
                ) for k in not_matched_keys
            ], ignore_conflicts=True)

    # sem_id: tenta primeiro casar com 1 Parte existente do mesmo nome com
    # CNPJ formatado completo (regra "Procuradoria/Defensoria com PJ ID").
    if paths['sem_id']:
        sem_id_names = list({nome for (nome, _) in paths['sem_id'].keys()})
        cnpj_cands = list(
            Parte.objects.filter(
                nome__in=sem_id_names,
                documento__regex=r'^[0-9]{2}\.[0-9]{3}\.[0-9]{3}/[0-9]{4}-[0-9]{2}$',
            ).values('pk', 'nome')
        )
        cnpj_by_name: dict = {}
        for c in cnpj_cands:
            cnpj_by_name.setdefault(c['nome'], []).append(c['pk'])
        not_matched_keys = []
        for key in list(paths['sem_id'].keys()):
            nome, tipo = key
            cands = cnpj_by_name.get(nome, [])
            if len(cands) == 1:
                spec_to_id[('sem_id', key)] = cands[0]
            else:
                not_matched_keys.append(key)
        if not_matched_keys:
            Parte.objects.bulk_create([
                Parte(
                    nome=k[0], tipo=k[1], documento='', oab='',
                    tipo_documento=paths['sem_id'][k].get('tipo_documento') or '',
                ) for k in not_matched_keys
            ], ignore_conflicts=True)

    # oab: bulk_create simples — partial unique constraint dedup
    if paths['oab']:
        Parte.objects.bulk_create([
            Parte(
                oab=oab, nome=(s.get('nome') or '')[:255],
                documento=s.get('documento') or '',
                tipo_documento=s.get('tipo_documento') or '',
                tipo=s.get('tipo') or 'desconhecido',
            ) for oab, s in paths['oab'].items()
        ], ignore_conflicts=True)

    # doc_real: bulk_create simples
    if paths['doc_real']:
        Parte.objects.bulk_create([
            Parte(
                documento=doc, nome=(s.get('nome') or '')[:255],
                tipo_documento=s.get('tipo_documento') or '',
                tipo=s.get('tipo') or 'desconhecido', oab='',
            ) for doc, s in paths['doc_real'].items()
        ], ignore_conflicts=True)

    # SELECTs em bulk pra mapear chaves → IDs
    if paths['oab']:
        for p in Parte.objects.filter(oab__in=list(paths['oab'].keys())).values('pk', 'oab'):
            spec_to_id[('oab', p['oab'])] = p['pk']
    if paths['doc_real']:
        for p in Parte.objects.filter(
            documento__in=list(paths['doc_real'].keys())
        ).exclude(
            Q(documento__contains='X') | Q(documento__contains='x') | Q(documento__contains='*')
        ).values('pk', 'documento'):
            spec_to_id[('doc_real', p['documento'])] = p['pk']
    masc_to_query = [k for k in paths['doc_masc'].keys() if ('doc_masc', k) not in spec_to_id]
    if masc_to_query:
        q = Q()
        for nome, doc in masc_to_query:
            q |= Q(nome=nome, documento=doc)
        for p in Parte.objects.filter(q).values('pk', 'nome', 'documento'):
            spec_to_id[('doc_masc', (p['nome'], p['documento']))] = p['pk']
    semid_to_query = [k for k in paths['sem_id'].keys() if ('sem_id', k) not in spec_to_id]
    if semid_to_query:
        q = Q()
        for nome, tipo in semid_to_query:
            q |= Q(nome=nome, tipo=tipo)
        for p in Parte.objects.filter(q, documento='', oab='').values('pk', 'nome', 'tipo'):
            spec_to_id[('sem_id', (p['nome'], p['tipo']))] = p['pk']

    return spec_to_id


def _bulk_upsert_catalogos(events_by_pid: dict) -> tuple[dict, dict]:
    """Bulk upsert de Classe/Assunto. Retorna (classe_by_code, assunto_by_code)."""
    classes: dict = {}
    assuntos: dict = {}
    for ev in events_by_pid.values():
        if ev['status'] != STATUS_OK:
            continue
        d = ev.get('_dados_norm') or {}
        if d.get('classe_codigo'):
            classes[d['classe_codigo']] = (d.get('classe_nome') or d['classe_codigo'])[:255]
        if d.get('assunto_codigo'):
            assuntos[d['assunto_codigo']] = (d.get('assunto_nome') or d['assunto_codigo'])[:255]

    if classes:
        ClasseJudicial.objects.bulk_create(
            [ClasseJudicial(codigo=c, nome=n) for c, n in classes.items()],
            ignore_conflicts=True,
        )
    if assuntos:
        Assunto.objects.bulk_create(
            [Assunto(codigo=c, nome=n) for c, n in assuntos.items()],
            ignore_conflicts=True,
        )

    classe_by_code = (
        {c.codigo: c for c in ClasseJudicial.objects.filter(codigo__in=list(classes))}
        if classes else {}
    )
    assunto_by_code = (
        {a.codigo: a for a in Assunto.objects.filter(codigo__in=list(assuntos))}
        if assuntos else {}
    )
    return classe_by_code, assunto_by_code


def _bulk_fallback_classe(events_by_pid: dict, processos: dict) -> dict:
    """Pra events erro/nao_encontrado de processos sem classe, busca a
    classe da última Movimentacao via SELECT DISTINCT ON (1 query)."""
    candidates = [
        ev['process_id'] for ev in events_by_pid.values()
        if ev['status'] in (STATUS_ERRO, STATUS_NAO_ENCONTRADO)
        and processos.get(ev['process_id'])
        and not processos[ev['process_id']].classe_codigo
    ]
    if not candidates:
        return {}
    from django.db import connection
    fallback: dict = {}
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (processo_id)
                processo_id, codigo_classe, nome_classe, classe_id
            FROM tribunals_movimentacao
            WHERE processo_id = ANY(%s) AND codigo_classe <> ''
            ORDER BY processo_id, data_disponibilizacao DESC
            """,
            [candidates],
        )
        for row in cur.fetchall():
            fallback[row[0]] = {
                'classe_codigo': row[1] or '',
                'classe_nome': row[2] or '',
                'classe_id': row[3],
            }
    return fallback


def _apply_to_proc(proc: Process, ev: dict, classe_by_code: dict,
                   assunto_by_code: dict, fallback: dict,
                   now_ts) -> set[str]:
    """Mutaa o objeto Process in-place. Retorna o set de fields alterados."""
    changed: set[str] = set()
    status = ev['status']

    if status == STATUS_OK:
        d = ev.get('_dados_norm') or {}
        if d.get('classe_codigo'):
            proc.classe = classe_by_code.get(d['classe_codigo'])
            changed.add('classe')
        if d.get('assunto_codigo'):
            proc.assunto = assunto_by_code.get(d['assunto_codigo'])
            changed.add('assunto')
        for fld in ('classe_codigo', 'classe_nome', 'assunto_codigo', 'assunto_nome',
                    'data_autuacao', 'valor_causa', 'orgao_julgador_nome',
                    'juizo', 'segredo_justica'):
            if fld in d:
                setattr(proc, fld, d[fld])
                changed.add(fld)
        proc.enriquecido_em = now_ts
        proc.data_enriquecimento_tribunal = now_ts
        proc.enriquecimento_status = Process.ENRIQ_OK
        proc.enriquecimento_erro = ''
        changed.update({
            'enriquecido_em', 'data_enriquecimento_tribunal',
            'enriquecimento_status', 'enriquecimento_erro',
        })

    elif status == STATUS_NAO_ENCONTRADO:
        fb = fallback.get(proc.pk) or {}
        for k, v in fb.items():
            attr = 'classe_id' if k == 'classe_id' else k
            setattr(proc, attr, v)
            changed.add('classe' if attr == 'classe_id' else attr)
        proc.enriquecido_em = now_ts
        proc.enriquecimento_status = Process.ENRIQ_NAO_ENCONTRADO
        proc.enriquecimento_erro = ''
        changed.update({'enriquecido_em', 'enriquecimento_status', 'enriquecimento_erro'})

    elif status == STATUS_ERRO:
        fb = fallback.get(proc.pk) or {}
        for k, v in fb.items():
            attr = 'classe_id' if k == 'classe_id' else k
            setattr(proc, attr, v)
            changed.add('classe' if attr == 'classe_id' else attr)
        proc.enriquecido_em = now_ts
        proc.enriquecimento_status = Process.ENRIQ_ERRO
        proc.enriquecimento_erro = (ev.get('erro') or '')[:1000]
        proc.enriquecimento_tentativas = (proc.enriquecimento_tentativas or 0) + 1
        changed.update({
            'enriquecido_em', 'enriquecimento_status',
            'enriquecimento_erro', 'enriquecimento_tentativas',
        })

    return changed


def apply_batch(events: list[dict]) -> tuple[int, int]:
    """Bulk apply de N events em ~10 queries (versão otimizada).

    Substitui o per-event apply (~30 queries × 1000 events = 30k queries)
    por bulk ops:
      1. Carrega todos os Process num in_bulk
      2. Bulk upsert ClasseJudicial + Assunto
      3. Bulk upsert Parte (por caminho de unique constraint)
      4. Bulk DELETE ProcessoParte
      5. Bulk INSERT ProcessoParte (principais + reps)
      6. Bulk fallback classe (DISTINCT ON em 1 query)
      7. Bulk update Process

    Tudo em 1 transação. Falha total = rollback total (pra mover
    deadlock pra retry via XAUTOCLAIM).
    """
    if not events:
        return (0, 0)

    by_pid: dict[int, dict] = {}
    for e in events:
        pid = e.get('process_id')
        if not pid:
            continue
        cur = by_pid.get(pid)
        if cur is None or (e.get('scraped_at') or '') > (cur.get('scraped_at') or ''):
            by_pid[pid] = e

    if not by_pid:
        return (0, 0)

    pids = list(by_pid.keys())

    with transaction.atomic():
        processos = Process.objects.in_bulk(pids)

        # Filtra missing + idempotência (event mais antigo que enriquecido_em)
        valid: dict[int, dict] = {}
        skipped = 0
        for pid, ev in by_pid.items():
            proc = processos.get(pid)
            if not proc:
                continue
            sa = parse_datetime(ev.get('scraped_at') or '')
            if sa and proc.enriquecido_em and proc.enriquecido_em >= sa:
                skipped += 1
                continue
            valid[pid] = ev
            # cache normalize_dados pra ok events (evita re-parse)
            if ev['status'] == STATUS_OK:
                ev['_dados_norm'] = normalize_dados(ev.get('dados') or {})

        if not valid:
            return (skipped, 0)

        # Catálogos
        classe_by_code, assunto_by_code = _bulk_upsert_catalogos(valid)

        # Partes (4 caminhos)
        spec_to_id = _bulk_upsert_partes(valid)

        # Wipe ProcessoParte de todos os ok-events em 1 DELETE
        ok_pids = [pid for pid, ev in valid.items() if ev['status'] == STATUS_OK]
        if ok_pids:
            ProcessoParte.objects.filter(processo_id__in=ok_pids).delete()

        # Bulk INSERT ProcessoParte — 2 fases (principais → reps com representa_id)
        principal_rows = []
        rep_pending = []  # (processo_id, parte_id, polo, papel, principal_key)
        for pid in ok_pids:
            ev = valid[pid]
            for polo, lista in (ev.get('partes') or {}).items():
                for principal in lista:
                    p_key = _route_parte(principal)
                    p_id = spec_to_id.get(p_key)
                    if not p_id:
                        continue
                    p_papel = principal.get('papel') or ''
                    principal_rows.append(ProcessoParte(
                        processo_id=pid, parte_id=p_id,
                        polo=polo, papel=p_papel, representa_id=None,
                    ))
                    for rep in principal.get('representantes') or []:
                        r_key = _route_parte(rep)
                        r_id = spec_to_id.get(r_key)
                        if not r_id or r_id == p_id:
                            continue
                        rep_pending.append((
                            pid, r_id, polo, rep.get('papel') or 'ADVOGADO',
                            (pid, p_id, polo, p_papel),
                        ))

        if principal_rows:
            created_principals = ProcessoParte.objects.bulk_create(principal_rows)
            principal_pp_id = {
                (pp.processo_id, pp.parte_id, pp.polo, pp.papel): pp.pk
                for pp in created_principals
            }
            if rep_pending:
                rep_rows = []
                for proc_id, parte_id, polo, papel, principal_key in rep_pending:
                    pp_pid = principal_pp_id.get(principal_key)
                    if not pp_pid:
                        continue
                    rep_rows.append(ProcessoParte(
                        processo_id=proc_id, parte_id=parte_id,
                        polo=polo, papel=papel, representa_id=pp_pid,
                    ))
                if rep_rows:
                    ProcessoParte.objects.bulk_create(rep_rows)

        # Fallback classe pra erro/nao_encontrado
        fallback_by_pid = _bulk_fallback_classe(valid, processos)

        # Aplica mudanças nos Process objects + bulk_update
        now_ts = timezone.now()
        all_changed: set[str] = set()
        to_update: list[Process] = []
        for pid, ev in valid.items():
            proc = processos[pid]
            try:
                changed = _apply_to_proc(proc, ev, classe_by_code, assunto_by_code,
                                          fallback_by_pid, now_ts)
                if changed:
                    all_changed.update(changed)
                    to_update.append(proc)
            except Exception:
                logger.exception('falha aplicando event no proc',
                                 extra={'process_id': pid})

        if to_update:
            update_fields = list(all_changed)
            Process.objects.bulk_update(to_update, fields=update_fields, batch_size=500)

    return (len(valid) + skipped, 0)


# ---------- loop principal ----------

_should_stop = False


def _install_signal_handlers():
    def _stop(*_args):
        global _should_stop
        _should_stop = True
        logger.info('drainer recebeu sinal de parada')
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)


def _decode_fields(fields) -> dict:
    """bytes → str pro parse_entry. Idempotente se já vier como str."""
    if not isinstance(fields, dict):
        return fields
    return {
        (k.decode() if isinstance(k, bytes) else k):
        (v.decode() if isinstance(v, bytes) else v)
        for k, v in fields.items()
    }


def _send_to_dlq(redis_client, raw_fields: dict, reason: str) -> bool:
    """Envia entry pra DLQ. Retorna True em sucesso, False se falhou.

    Caller usa o retorno pra decidir se XACKa a entry: em caso de falha,
    deixar pendente é melhor que perder silenciosamente (entry volta
    via XAUTOCLAIM pra novo retry).
    """
    try:
        redis_client.xadd(DLQ_STREAM, {
            'data': raw_fields.get('data') or '',
            'reason': reason[:200],
        }, maxlen=DLQ_MAXLEN, approximate=True)
        return True
    except Exception:
        logger.exception('falha ao enviar pra DLQ — entry NAO sera acked')
        return False


def _autoclaim(r, consumer: str, min_idle_ms: int, count: int, start_id: str = '0'):
    """XAUTOCLAIM: pega entries lidas por outro consumer mas não acked
    em min_idle_ms (consumer crashou).

    Retorna (next_start_id, ids, raw_fields). Caller persiste
    `next_start_id` entre iterações pra paginar — sem isso, partindo
    sempre de '0' a cada loop reprocessa o mesmo set quando há mais
    entries idle do que `count`.
    """
    result = r.xautoclaim(
        STREAM_KEY, GROUP_NAME, consumer,
        min_idle_time=min_idle_ms, start_id=start_id, count=count,
    )
    if not result:
        return '0', [], []
    next_id = result[0].decode() if isinstance(result[0], bytes) else result[0]
    entries = result[1] if len(result) >= 2 else []
    ids: list[str] = []
    raws: list[dict] = []
    for entry_id, fields in entries:
        ids.append(entry_id.decode() if isinstance(entry_id, bytes) else entry_id)
        raws.append(_decode_fields(fields))
    return next_id, ids, raws


def _read_new(r, consumer: str, count: int, block_ms: int):
    result = r.xreadgroup(
        GROUP_NAME, consumer, {STREAM_KEY: '>'},
        count=count, block=block_ms,
    )
    if not result:
        return [], []
    entries = result[0][1]
    ids: list[str] = []
    raws: list[dict] = []
    for entry_id, fields in entries:
        ids.append(entry_id.decode() if isinstance(entry_id, bytes) else entry_id)
        raws.append(_decode_fields(fields))
    return ids, raws


def run(*, batch_size: int = 200, block_ms: int = 2000,
        idle_ms: int = 60_000, trim_after_ack: bool = True) -> None:
    """Loop principal. Cada iteração:
      1. XAUTOCLAIM entries idle (consumer travou em outro pod)
      2. XREADGROUP entries novas (block até block_ms)
      3. apply_batch numa transação
      4. XACK + XDEL pras entries processadas (mantém stream bounded)
    """
    _install_signal_handlers()
    r = get_redis()
    ensure_consumer_group(r)
    consumer = f'{socket.gethostname()}-{os.getpid()}'
    logger.info('drainer iniciado', extra={
        'consumer': consumer, 'batch_size': batch_size,
        'stream': STREAM_KEY, 'group': GROUP_NAME,
    })

    autoclaim_cursor = '0'
    while not _should_stop:
        try:
            autoclaim_cursor, claimed_ids, claimed_raws = _autoclaim(
                r, consumer, idle_ms, batch_size, start_id=autoclaim_cursor,
            )
        except Exception:
            logger.exception('falha em XAUTOCLAIM')
            autoclaim_cursor, claimed_ids, claimed_raws = '0', [], []

        try:
            new_ids, new_raws = _read_new(r, consumer, batch_size, block_ms)
        except Exception:
            logger.exception('falha em XREADGROUP')
            time.sleep(1)
            continue

        all_ids = claimed_ids + new_ids
        all_raws = claimed_raws + new_raws
        if not all_ids:
            continue

        events: list[dict] = []
        # Track quais ids podemos XACKar com segurança. Entries com payload
        # ruim só vão pro ack se o XADD na DLQ teve sucesso — caso
        # contrário ficam pendentes pra retry.
        ackable_ids: list[str] = list(all_ids)
        bad_kept: int = 0
        for entry_id, raw in zip(all_ids, all_raws):
            payload = parse_entry(raw)
            if payload is None:
                if not _send_to_dlq(r, raw, reason='parse_failed'):
                    ackable_ids.remove(entry_id)
                    bad_kept += 1
                continue
            events.append(payload)

        try:
            ok, falhas = apply_batch(events)
        except Exception:
            logger.exception('apply_batch lançou exception não capturada — entries não serão acked')
            time.sleep(1)
            continue

        if ackable_ids:
            try:
                r.xack(STREAM_KEY, GROUP_NAME, *ackable_ids)
                if trim_after_ack:
                    r.xdel(STREAM_KEY, *ackable_ids)
            except Exception:
                logger.exception('falha em XACK/XDEL')

        logger.info('batch aplicado', extra={
            'ok': ok, 'falhas': falhas,
            'bad_acked': len(all_ids) - len(events) - bad_kept,
            'bad_pending_retry': bad_kept,
            'total': len(all_ids),
        })
