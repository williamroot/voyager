import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from django.db import IntegrityError, transaction
from django.utils import timezone

from tribunals.models import IngestionRun, SchemaDriftAlert, Tribunal

logger = logging.getLogger('voyager.djen.parser')

EXPECTED_KEYS = frozenset({
    'id', 'numero_processo', 'numeroprocessocommascara',
    'siglaTribunal', 'nomeOrgao', 'tipoComunicacao', 'tipoDocumento',
    'data_disponibilizacao', 'texto', 'destinatarios',
    'nomeClasse', 'codigoClasse', 'link',
})

CNJ_REGEX = re.compile(r'\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}')
CNJ_RAW_REGEX = re.compile(r'^\d{20}$')


@dataclass
class ParsedItem:
    """Representação leve dos campos da movimentação prontos para criar o ORM model."""
    cnj: str
    external_id: str
    data_disponibilizacao: datetime
    tipo_comunicacao: str = ''
    tipo_documento: str = ''
    nome_orgao: str = ''
    nome_classe: str = ''
    codigo_classe: str = ''
    link: str = ''
    destinatarios: list = field(default_factory=list)
    texto: str = ''

    def to_movimentacao_kwargs(self) -> dict:
        return {
            'external_id': self.external_id,
            'data_disponibilizacao': self.data_disponibilizacao,
            'tipo_comunicacao': self.tipo_comunicacao,
            'tipo_documento': self.tipo_documento,
            'nome_orgao': self.nome_orgao,
            'nome_classe': self.nome_classe,
            'codigo_classe': self.codigo_classe,
            'link': self.link,
            'destinatarios': self.destinatarios,
            'texto': self.texto,
        }


def normalizar_cnj(*candidates: Optional[str]) -> Optional[str]:
    for raw in candidates:
        if not raw:
            continue
        s = str(raw).strip()
        if CNJ_REGEX.match(s):
            return CNJ_REGEX.search(s).group(0)
        if CNJ_RAW_REGEX.match(s):
            return f'{s[0:7]}-{s[7:9]}.{s[9:13]}.{s[13]}.{s[14:16]}.{s[16:20]}'
        m = CNJ_REGEX.search(s)
        if m:
            return m.group(0)
    return None


def parse_dt(value: str) -> Optional[datetime]:
    if not value:
        return None
    s = str(value).strip()
    for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            dt = datetime.strptime(s, fmt)
            if timezone.is_naive(dt):
                dt = timezone.make_aware(dt)
            return dt
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt)
        return dt
    except ValueError:
        return None


def _hash_chaves(chaves: list[str]) -> str:
    payload = json.dumps(sorted(chaves), separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:32]


def _truncar_exemplo(item: dict, max_texto: int = 500) -> dict:
    saneado = {}
    for k, v in item.items():
        if k == 'texto' and isinstance(v, str) and len(v) > max_texto:
            saneado[k] = v[:max_texto] + '…'
        else:
            saneado[k] = v
    return saneado


def registrar_drift(tribunal: Tribunal, tipo: str, chaves: list[str], exemplo: dict,
                    run: Optional[IngestionRun]) -> None:
    chaves_sorted = sorted(chaves)
    chaves_hash = _hash_chaves(chaves_sorted)
    exemplo_truncado = _truncar_exemplo(exemplo)
    defaults = {'chaves': chaves_sorted, 'exemplo': exemplo_truncado, 'ingestion_run': run}
    try:
        with transaction.atomic():
            obj, created = SchemaDriftAlert.objects.get_or_create(
                tribunal=tribunal, tipo=tipo, chaves_hash=chaves_hash, resolvido=False,
                defaults=defaults,
            )
            if not created:
                SchemaDriftAlert.objects.filter(pk=obj.pk).update(
                    exemplo=exemplo_truncado, ingestion_run=run,
                )
    except IntegrityError:
        # Race com outro worker — outro inseriu primeiro. Apenas atualiza.
        SchemaDriftAlert.objects.filter(
            tribunal=tribunal, tipo=tipo, chaves_hash=chaves_hash, resolvido=False,
        ).update(exemplo=exemplo_truncado, ingestion_run=run)


def parse_item(item: dict, tribunal: Tribunal, run: IngestionRun) -> Optional[ParsedItem]:
    keys = set(item.keys())
    extra = keys - EXPECTED_KEYS
    missing = EXPECTED_KEYS - keys
    if extra:
        registrar_drift(tribunal, SchemaDriftAlert.TIPO_EXTRA, list(extra), item, run)
    if missing:
        registrar_drift(tribunal, SchemaDriftAlert.TIPO_MISSING, list(missing), item, run)

    cnj = normalizar_cnj(
        item.get('numeroprocessocommascara'),
        item.get('numero_processo'),
        item.get('texto'),
    )
    external_id = item.get('id')
    if not cnj or external_id is None:
        run.erros.append({
            'pagina': run.paginas_lidas,
            'erro': 'cnj_indisponivel' if not cnj else 'external_id_ausente',
            'external_id': str(external_id) if external_id else None,
        })
        return None

    dt = parse_dt(item.get('data_disponibilizacao'))
    if dt is None:
        run.erros.append({
            'pagina': run.paginas_lidas,
            'erro': 'data_disponibilizacao_invalida',
            'external_id': str(external_id),
        })
        return None

    return ParsedItem(
        cnj=cnj,
        external_id=str(external_id)[:64],
        data_disponibilizacao=dt,
        tipo_comunicacao=str(item.get('tipoComunicacao') or '')[:120],
        tipo_documento=str(item.get('tipoDocumento') or '')[:120],
        nome_orgao=str(item.get('nomeOrgao') or '')[:255],
        nome_classe=str(item.get('nomeClasse') or '')[:255],
        codigo_classe=str(item.get('codigoClasse') or '')[:20],
        link=str(item.get('link') or '')[:500],
        destinatarios=item.get('destinatarios') or [],
        texto=str(item.get('texto') or ''),
    )
