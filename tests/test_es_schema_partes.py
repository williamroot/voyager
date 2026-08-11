"""Testes do schema ES novo (auditoria ES-SCHEMA 2026-08).

Cobrem: participacoes nested no doc de processo, proc_digits (busca colável de
CNJ), tipo_documento no doc de movimentação, campos de enriquecimento/
classificação, o job bulk `indexar_processos_bulk` (ES mockado) e o enqueue de
write-through do drainer `apply_batch`. Sem ES real.
"""
import datetime
from unittest.mock import MagicMock, patch

import pytest

from tribunals.models import (
    Movimentacao, Parte, Process, ProcessoParte, Tribunal,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def tjsp():
    t, _ = Tribunal.objects.get_or_create(
        sigla='TJSP', defaults={'nome': 'TJSP', 'sigla_djen': 'TJSP'},
    )
    return t


@pytest.fixture
def proc_com_partes(tjsp):
    """Processo com autor PF, ente público no passivo e advogado com OAB."""
    p = Process.objects.create(
        numero_cnj='0001234-56.2025.8.26.0100', tribunal=tjsp,
        juizo='1ª Vara de Fazenda Pública',
        classificacao='PRECATORIO', classificacao_score=0.91,
        classificacao_versao='v6',
        enriquecimento_status=Process.ENRIQ_OK,
    )
    autor = Parte.objects.create(
        nome='João da Silva', documento='111.222.333-44',
        tipo_documento='CPF', tipo='pf',
    )
    ente = Parte.objects.create(nome='Fazenda Pública do Estado de São Paulo', tipo='pj')
    adv = Parte.objects.create(nome='Maria Advogada', oab='SP123456', tipo='advogado')
    pp_autor = ProcessoParte.objects.create(
        processo=p, parte=autor, polo='ativo', papel='EXEQUENTE',
    )
    ProcessoParte.objects.create(processo=p, parte=ente, polo='passivo', papel='EXECUTADO')
    ProcessoParte.objects.create(
        processo=p, parte=adv, polo='ativo', papel='ADVOGADO', representa=pp_autor,
    )
    return p


# ---------- processo_to_doc: participacoes nested ----------

def test_processo_to_doc_participacoes_nested(proc_com_partes):
    from search.documents import processo_to_doc

    doc = processo_to_doc(proc_com_partes)
    parts = doc['participacoes']
    assert len(parts) == 3

    autor = next(x for x in parts if x['papel'] == 'EXEQUENTE')
    assert autor['nome'] == 'João da Silva'
    assert autor['documento'] == '111.222.333-44'
    assert autor['polo'] == 'ativo'
    assert autor['tipo'] == 'pf'
    assert autor['eh_advogado'] is False
    assert autor['parte_id']

    executado = next(x for x in parts if x['papel'] == 'EXECUTADO')
    assert executado['polo'] == 'passivo'
    assert executado['documento'] is None   # sem doc → None (não string vazia)

    adv = next(x for x in parts if x['papel'] == 'ADVOGADO')
    assert adv['oab'] == 'SP123456'
    assert adv['eh_advogado'] is True

    # strings concatenadas (compat Jusbrasil) continuam coerentes
    assert 'João da Silva' in doc['partes']
    assert 'Maria Advogada (OAB SP123456)' in doc['advs']
    # ente público no passivo → derivado junto (mesma passada)
    assert doc['tem_ente_publico_passivo'] is True


def test_processo_to_doc_sem_partes_lista_vazia(tjsp):
    from search.documents import processo_to_doc

    p = Process.objects.create(numero_cnj='0009999-88.2024.8.26.0001', tribunal=tjsp)
    doc = processo_to_doc(p)
    assert doc['participacoes'] == []
    assert doc['tem_ente_publico_passivo'] is False


# ---------- campos novos escalares ----------

def test_processo_to_doc_campos_novos(proc_com_partes):
    from search.documents import processo_to_doc

    doc = processo_to_doc(proc_com_partes)
    assert doc['proc_digits'] == '00012345620258260100'
    assert doc['juizo'] == '1ª Vara de Fazenda Pública'
    assert doc['enriquecimento_status'] == Process.ENRIQ_OK
    assert doc['classificacao_versao'] == 'v6'


def test_movimentacao_to_doc_tipo_documento_e_proc_digits(tjsp):
    from search.documents import movimentacao_to_doc, movimentacao_to_doc_sem_partes

    p = Process.objects.create(numero_cnj='0001234-56.2025.8.26.0100', tribunal=tjsp)
    mov = Movimentacao.objects.create(
        processo=p, tribunal=tjsp, external_id='ext-1',
        data_disponibilizacao=datetime.datetime(2026, 8, 1, 10, 0,
                                                  tzinfo=datetime.timezone.utc),
        texto='Sentença publicada', tipo_comunicacao='Intimação',
        tipo_documento='Sentença',
    )
    for builder in (movimentacao_to_doc, movimentacao_to_doc_sem_partes):
        doc = builder(mov)
        assert doc['tipo_documento'] == 'Sentença'
        assert doc['proc_digits'] == '00012345620258260100'


# ---------- mapping ↔ doc builder coerentes ----------

def test_mapping_processos_cobre_todo_campo_do_doc(proc_com_partes):
    """Regra da casa: não adicionar campo no builder sem mapping (e vice-versa)."""
    from search.documents import processo_to_doc
    from search.mappings import PROC_MAPPING

    doc = processo_to_doc(proc_com_partes)
    mapped = set(PROC_MAPPING['mappings']['properties'].keys())
    assert set(doc.keys()) <= mapped

    nested_props = set(
        PROC_MAPPING['mappings']['properties']['participacoes']['properties'].keys()
    )
    for parte in doc['participacoes']:
        assert set(parte.keys()) <= nested_props


def test_mapping_movimentacoes_cobre_todo_campo_do_doc(tjsp):
    from search.documents import movimentacao_to_doc
    from search.mappings import MOV_MAPPING

    p = Process.objects.create(numero_cnj='0001234-56.2025.8.26.0100', tribunal=tjsp)
    mov = Movimentacao.objects.create(
        processo=p, tribunal=tjsp, external_id='ext-2',
        data_disponibilizacao=datetime.datetime(2026, 8, 1, 10, 0,
                                                tzinfo=datetime.timezone.utc),
    )
    doc = movimentacao_to_doc(mov)
    mapped = set(MOV_MAPPING['mappings']['properties'].keys())
    assert set(doc.keys()) <= mapped


# ---------- job bulk (ES mockado) ----------

def test_indexar_processos_bulk_um_bulk_so(proc_com_partes, tjsp):
    from search import jobs

    p2 = Process.objects.create(numero_cnj='0002222-33.2024.8.26.0002', tribunal=tjsp)
    es_mock = MagicMock()
    es_mock.bulk.return_value = {'errors': False, 'items': []}
    with patch.object(jobs, 'get_es', return_value=es_mock):
        jobs.indexar_processos_bulk([proc_com_partes.pk, p2.pk])

    assert es_mock.bulk.call_count == 1
    ops = es_mock.bulk.call_args.kwargs['operations']
    assert len(ops) == 4  # 2 docs × (action + source)
    acao = ops[0]['index']
    assert acao['_index'].endswith('-processos')
    assert acao['_id'] in (proc_com_partes.pk, p2.pk)
    docs = [ops[1], ops[3]]
    doc_p1 = next(d for d in docs if d['id'] == proc_com_partes.pk)
    assert len(doc_p1['participacoes']) == 3


def test_indexar_processos_bulk_vazio_nao_chama_es():
    from search import jobs

    with patch.object(jobs, 'get_es') as get_es_mock:
        jobs.indexar_processos_bulk([])
    get_es_mock.assert_not_called()


# ---------- drainer: write-through do caminho bulk ----------

def test_apply_batch_enfileira_indexacao_es(tjsp):
    """apply_batch escreve via bulk_update (sem post_save) — precisa enfileirar
    indexar_processos_bulk explicitamente pros docs enriquecidos chegarem no ES."""
    from enrichers import drainer, stream

    p = Process.objects.create(numero_cnj='0003333-44.2024.8.26.0003', tribunal=tjsp)
    event = stream.build_ok_payload(
        process_id=p.pk, tribunal=tjsp.sigla, numero_cnj=p.numero_cnj,
        scraped_at='2026-08-10T01:00:00',
        dados={'classe': 'Cumprimento de Sentença (156)'},
        partes={'ativo': [{
            'nome': 'Credor Um', 'documento': '999.888.777-66',
            'tipo_documento': 'CPF', 'oab': '', 'papel': 'EXEQUENTE',
            'tipo': 'pf', 'representantes': [],
        }], 'passivo': [], 'outros': []},
    )
    queue_mock = MagicMock()
    with patch.object(drainer.django_rq, 'get_queue', return_value=queue_mock):
        applied, skipped = drainer.apply_batch([event])

    assert applied == 1
    chamadas = [c for c in queue_mock.enqueue.call_args_list
                if c.args and c.args[0] == 'search.jobs.indexar_processos_bulk']
    assert len(chamadas) == 1
    assert chamadas[0].args[1] == [p.pk]
