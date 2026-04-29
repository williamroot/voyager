"""Parser de respostas Datajud → kwargs pra criar Movimentacao.

Datajud retorna estrutura distinta do DJEN:
  source: {
    numeroProcesso, classe: {codigo, nome},
    orgaoJulgador: {nome, codigo},
    movimentos: [{
      codigo, nome, dataHora,
      complementosTabelados: [{nome, valor, descricao}],
    }],
    ...
  }

Mapeamos cada movimento em uma row Movimentacao com `meio='datajud'`
e `external_id='datajud:<id_proc>:<idx>'`. Não conflita com os IDs do
DJEN (que são numéricos puros).
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone as tz
from typing import Optional


def _parse_dt(raw: Optional[str]) -> Optional[datetime]:
    """ISO 8601 com timezone implícito UTC (ou timezone explícito)."""
    if not raw:
        return None
    try:
        # Datajud envia "2025-11-27T10:00:00.000Z" ou "2025-11-27T10:00:00"
        s = raw.rstrip('Z')
        if '.' in s:
            s = s.split('.', 1)[0]
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz.utc)
        return dt
    except (ValueError, TypeError):
        return None


def build_external_id(processo_id_datajud: Optional[str], idx: int,
                       data_hora: Optional[str], codigo: int) -> str:
    """external_id determinístico — Datajud não tem ID estável por
    movimento. Combina id do processo + (codigo, dataHora) hashado pra
    estabilidade entre execuções (idempotência)."""
    base = f'{processo_id_datajud or ""}:{codigo}:{data_hora or ""}'
    h = hashlib.sha1(base.encode('utf-8')).hexdigest()[:24]
    return f'datajud:{h}'


def build_texto(mov: dict) -> str:
    """Monta texto legível: nome do movimento + complementos tabelados."""
    partes = [mov.get('nome') or '']
    for c in (mov.get('complementosTabelados') or []):
        nome = c.get('nome') or ''
        valor = c.get('valor') or ''
        descricao = c.get('descricao') or ''
        if descricao:
            partes.append(f'{nome}: {descricao}' if nome else descricao)
        elif valor:
            partes.append(f'{nome}: {valor}' if nome else str(valor))
    return ' · '.join(p for p in partes if p)


def parse_movimentos(source: dict) -> list[dict]:
    """Datajud `_source` → lista de kwargs prontos pra Movimentacao.

    Movimentos sem `dataHora` são descartados (sem dt não há como ordenar
    nem mostrar na timeline).
    """
    if not source:
        return []
    proc_id = source.get('idProcesso') or source.get('numeroProcesso') or ''
    classe_obj = source.get('classe') or {}
    codigo_classe = str(classe_obj.get('codigo') or '')
    nome_classe = (classe_obj.get('nome') or '')[:255]
    orgao = source.get('orgaoJulgador') or {}
    nome_orgao = (orgao.get('nome') or '')[:255]
    id_orgao = orgao.get('codigo')
    try:
        id_orgao = int(id_orgao) if id_orgao is not None else None
    except (TypeError, ValueError):
        id_orgao = None

    out = []
    for idx, mov in enumerate(source.get('movimentos') or []):
        dt = _parse_dt(mov.get('dataHora'))
        if not dt:
            continue
        codigo = mov.get('codigo')
        try:
            codigo_int = int(codigo) if codigo is not None else 0
        except (TypeError, ValueError):
            codigo_int = 0
        out.append({
            'external_id': build_external_id(str(proc_id), idx, mov.get('dataHora'), codigo_int),
            'data_disponibilizacao': dt,
            'data_envio': dt.date(),
            'tipo_comunicacao': '',
            'tipo_documento': '',
            'nome_orgao': nome_orgao,
            'id_orgao': id_orgao,
            'nome_classe': nome_classe,
            'codigo_classe': codigo_classe,
            'link': '',
            'destinatarios': [],
            'destinatario_advogados': [],
            'texto': build_texto(mov),
            'numero_comunicacao': '',
            # `hash` e `meio` ajudam diferenciar fonte na UI/queries.
            'hash': '',
            'meio': 'datajud',
            'meio_completo': 'Datajud (CNJ)',
            'status': '',
            'ativo': True,
        })
    return out
