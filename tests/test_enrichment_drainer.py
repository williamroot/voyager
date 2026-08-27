"""Testes do drainer (apply_event / apply_batch / normalização).

Usa DB real (pytest-django) — drainer escreve em Process/Parte/ProcessoParte.
"""
from datetime import date
from decimal import Decimal

import pytest

from enrichers import drainer, stream
from tribunals.models import (
    Assunto, ClasseJudicial, Parte, Process, ProcessoParte, Tribunal,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def trf1():
    t, _ = Tribunal.objects.get_or_create(
        sigla='TRF1', defaults={'nome': 'TRF1', 'ativo': True},
    )
    return t


@pytest.fixture
def proc(trf1):
    return Process.objects.create(tribunal=trf1, numero_cnj='0001234-56.2025.4.01.0000')


# ---------- normalize_dados ----------

def test_normalize_dados_classe_com_codigo():
    out = drainer.normalize_dados({'classe': 'Procedimento Comum (1234)'})
    assert out['classe_codigo'] == '1234'
    assert out['classe_nome'] == 'Procedimento Comum'


def test_normalize_dados_assunto_sem_codigo():
    """Sem código, a CHAVE NÃO ENTRA no dict — e isso é o contrato, não detalhe.

    `apply_event` aplica o dict com `setattr`. Devolver `assunto_codigo=''`
    APAGARIA o código herdado do DJEN toda vez que o e-SAJ público (que só dá o
    nome) respondesse — foi assim que leads voltaram a `NAO_LEAD` em 06/07/2026.
    Ausência da chave = "não sei", que é diferente de "é vazio".
    """
    out = drainer.normalize_dados({'assunto': 'DIREITO TRIBUTÁRIO'})
    assert 'assunto_codigo' not in out, 'chave vazia APAGA o código herdado do DJEN'
    assert out['assunto_nome'].startswith('DIREITO')


def test_normalize_dados_classe_sem_parens_nao_extrai_digitos_do_meio():
    """Regression: regex anterior pegava '12345' dentro do texto como código."""
    out = drainer.normalize_dados({'classe': 'Tributário 12345 algo'})
    assert 'classe_codigo' not in out      # ver test_normalize_dados_assunto_sem_codigo
    assert out['classe_nome'] == 'Tributário 12345 algo'


def test_normalize_dados_classe_codigo_so_entre_parens():
    out = drainer.normalize_dados({'classe': 'Procedimento (1234) Comum'})
    # Parênteses no meio não capturam — só no fim.
    assert 'classe_codigo' not in out      # ver test_normalize_dados_assunto_sem_codigo
    assert out['classe_nome'] == 'Procedimento (1234) Comum'


def test_normalize_dados_valor_brl():
    out = drainer.normalize_dados({'valor_causa': 'R$ 1.234,56'})
    assert out['valor_causa'] == Decimal('1234.56')


def test_normalize_dados_data_br():
    out = drainer.normalize_dados({'data_autuacao': '25/04/2026'})
    assert out['data_autuacao'] == date(2026, 4, 25)


def test_normalize_dados_ignora_invalidos():
    out = drainer.normalize_dados({'valor_causa': 'sem valor', 'data_autuacao': 'sem data'})
    assert 'valor_causa' not in out
    assert 'data_autuacao' not in out


# ---------- upsert_catalogo ----------

def test_upsert_catalogo_idempotente():
    c1 = drainer.upsert_catalogo(ClasseJudicial, '1234', 'Procedimento Comum')
    c2 = drainer.upsert_catalogo(ClasseJudicial, '1234', 'Outro Nome Que Será Ignorado')
    assert c1.pk == c2.pk
    # Primeira inserção venceu — bulk_create(ignore_conflicts) não atualiza.
    assert ClasseJudicial.objects.get(codigo='1234').nome == 'Procedimento Comum'


# ---------- upsert_parte ----------

def test_upsert_parte_por_oab_dedupa():
    info = {'nome': 'Adv X', 'documento': '', 'tipo_documento': '',
            'oab': 'SP123456', 'tipo': 'advogado'}
    p1 = drainer.upsert_parte(info)
    p2 = drainer.upsert_parte({**info, 'nome': 'Adv X (com nome diferente)'})
    assert p1.pk == p2.pk


def test_upsert_parte_doc_real_dedupa_por_documento():
    info = {'nome': 'Empresa', 'documento': '12.345.678/0001-99',
            'tipo_documento': 'CNPJ', 'oab': '', 'tipo': 'pj'}
    p1 = drainer.upsert_parte(info)
    p2 = drainer.upsert_parte({**info, 'nome': 'EMPRESA LTDA'})
    assert p1.pk == p2.pk


def test_upsert_parte_doc_mascarado_casa_com_real_existente():
    real = drainer.upsert_parte({
        'nome': 'João Silva', 'documento': '123.456.789-00',
        'tipo_documento': 'CPF', 'oab': '', 'tipo': 'pf',
    })
    masked = drainer.upsert_parte({
        'nome': 'João Silva', 'documento': '123.XXX.XXX-XX',
        'tipo_documento': 'CPF', 'oab': '', 'tipo': 'pf',
    })
    assert masked.pk == real.pk


def test_upsert_parte_sem_doc_sem_oab_dedupa_por_nome_tipo():
    info = {'nome': 'Procuradoria Federal', 'documento': '',
            'tipo_documento': '', 'oab': '', 'tipo': 'pj'}
    p1 = drainer.upsert_parte(info)
    p2 = drainer.upsert_parte(info)
    assert p1.pk == p2.pk


def test_upsert_parte_doc_mascarado_inserido_quando_nao_acha_real():
    p = drainer.upsert_parte({
        'nome': 'Sem Match', 'documento': '999.XXX.XXX-XX',
        'tipo_documento': 'CPF', 'oab': '', 'tipo': 'pf',
    })
    assert p.pk
    assert p.documento == '999.XXX.XXX-XX'


# ---------- apply_event: ok ----------

def test_apply_event_ok_atualiza_processo_e_partes(proc):
    event = stream.build_ok_payload(
        process_id=proc.pk, tribunal=proc.tribunal_id, numero_cnj=proc.numero_cnj,
        scraped_at='2026-04-29T01:00:00',
        dados={
            'classe': 'Procedimento Comum (1234)',
            'assunto': 'Tributário (5952)',
            'valor_causa': 'R$ 50.000,00',
            'orgao_julgador': 'Vara Federal',
        },
        partes={
            'ativo': [{
                'nome': 'João', 'documento': '111.222.333-44',
                'tipo_documento': 'CPF', 'oab': '', 'papel': 'AUTOR', 'tipo': 'pf',
                'representantes': [{
                    'nome': 'Adv', 'documento': '', 'tipo_documento': '',
                    'oab': 'SP1', 'papel': 'ADVOGADO', 'tipo': 'advogado',
                }],
            }],
            'passivo': [],
            'outros': [],
        },
    )
    drainer.apply_event(event)
    proc.refresh_from_db()
    assert proc.enriquecimento_status == Process.ENRIQ_OK
    assert proc.classe_codigo == '1234'
    assert proc.classe_nome == 'Procedimento Comum'
    assert proc.assunto_codigo == '5952'
    assert proc.valor_causa == Decimal('50000.00')
    assert proc.orgao_julgador_nome == 'Vara Federal'
    assert proc.classe_id == ClasseJudicial.objects.get(codigo='1234').pk
    assert proc.assunto_id == Assunto.objects.get(codigo='5952').pk

    pps = list(ProcessoParte.objects.filter(processo=proc).order_by('id'))
    assert len(pps) == 2  # 1 principal + 1 advogado
    principal = next(pp for pp in pps if pp.representa_id is None)
    rep = next(pp for pp in pps if pp.representa_id == principal.pk)
    assert principal.parte.documento == '111.222.333-44'
    assert rep.parte.oab == 'SP1'


def test_apply_event_ok_reaplica_substitui_partes(proc):
    """Drainer faz wipe+reinsert de ProcessoParte. Reaplicar deve reset.

    ⚠️ Os `scraped_at` aqui são RELATIVOS ao relógio, de propósito. Com data fixa
    (`2026-04-29T…`) este teste virava bomba-relógio: a guarda de idempotência
    compara `processo.enriquecido_em` — que é `timezone.now()` no momento em que
    o drainer aplica — com o `scraped_at` do evento. Passado o dia da fixture,
    `now()` supera qualquer `scraped_at` fixo e o segundo evento é PULADO, então
    o teste falha sozinho um belo dia sem ninguém ter mexido no código.
    """
    from django.utils import timezone as _tz
    import datetime as _dt
    t1 = (_tz.now() + _dt.timedelta(minutes=1)).isoformat()
    t2 = (_tz.now() + _dt.timedelta(minutes=2)).isoformat()
    event = stream.build_ok_payload(
        process_id=proc.pk, tribunal=proc.tribunal_id, numero_cnj=proc.numero_cnj,
        scraped_at=t1, dados={},
        partes={'ativo': [
            {'nome': 'A', 'documento': '', 'tipo_documento': '', 'oab': 'SP1',
             'papel': '', 'tipo': 'advogado', 'representantes': []},
        ], 'passivo': [], 'outros': []},
    )
    drainer.apply_event(event)
    assert ProcessoParte.objects.filter(processo=proc).count() == 1
    # Re-aplica com lista diferente
    event2 = stream.build_ok_payload(
        process_id=proc.pk, tribunal=proc.tribunal_id, numero_cnj=proc.numero_cnj,
        scraped_at=t2, dados={},
        partes={'ativo': [], 'passivo': [
            {'nome': 'B', 'documento': '', 'tipo_documento': '', 'oab': 'SP2',
             'papel': '', 'tipo': 'advogado', 'representantes': []},
        ], 'outros': []},
    )
    drainer.apply_event(event2)
    pps = ProcessoParte.objects.filter(processo=proc)
    assert pps.count() == 1
    assert pps.first().parte.oab == 'SP2'


def test_apply_event_erro_incrementa_tentativas(proc):
    proc.enriquecimento_tentativas = 0
    proc.save(update_fields=['enriquecimento_tentativas'])
    event = stream.build_erro_payload(
        process_id=proc.pk, tribunal=proc.tribunal_id, numero_cnj=proc.numero_cnj,
        scraped_at='2026-04-29T01:00:00', erro='timeout',
    )
    drainer.apply_event(event)
    proc.refresh_from_db()
    assert proc.enriquecimento_status == Process.ENRIQ_ERRO
    assert proc.enriquecimento_erro == 'timeout'
    assert proc.enriquecimento_tentativas == 1


def test_apply_event_nao_encontrado_seta_status(proc):
    event = stream.build_nao_encontrado_payload(
        process_id=proc.pk, tribunal=proc.tribunal_id, numero_cnj=proc.numero_cnj,
        scraped_at='2026-04-29T01:00:00',
    )
    drainer.apply_event(event)
    proc.refresh_from_db()
    assert proc.enriquecimento_status == Process.ENRIQ_NAO_ENCONTRADO


def test_apply_event_processo_inexistente_nao_quebra():
    event = stream.build_erro_payload(
        process_id=999_999_999, tribunal='TRF1', numero_cnj='0', scraped_at='t',
        erro='x',
    )
    # Não deve levantar.
    drainer.apply_event(event)


def test_apply_event_skip_se_event_mais_antigo_que_enriquecido_em(proc):
    """Idempotência em re-entrega: se o Process já foi enriquecido em t1,
    aplicar evento com scraped_at=t0 (anterior) não deve fazer nada —
    em particular não incrementar enriquecimento_tentativas."""
    # Aplica evento "atual" (T+1)
    drainer.apply_event(stream.build_erro_payload(
        process_id=proc.pk, tribunal=proc.tribunal_id, numero_cnj=proc.numero_cnj,
        scraped_at='2026-04-29T10:00:00+00:00', erro='atual',
    ))
    proc.refresh_from_db()
    assert proc.enriquecimento_tentativas == 1
    enriquecido_atual = proc.enriquecido_em

    # Re-entrega de evento anterior (T+0) — deve ser ignorado
    drainer.apply_event(stream.build_erro_payload(
        process_id=proc.pk, tribunal=proc.tribunal_id, numero_cnj=proc.numero_cnj,
        scraped_at='2026-04-29T09:00:00+00:00', erro='velho re-entregue',
    ))
    proc.refresh_from_db()
    assert proc.enriquecimento_tentativas == 1, 'tentativas não pode duplicar'
    assert proc.enriquecimento_erro == 'atual'
    assert proc.enriquecido_em == enriquecido_atual


# ---------- apply_batch ----------

def test_apply_batch_dedup_por_process_id_mantem_mais_recente(proc):
    e_velho = stream.build_erro_payload(
        process_id=proc.pk, tribunal=proc.tribunal_id, numero_cnj=proc.numero_cnj,
        scraped_at='2026-04-29T01:00:00', erro='velho',
    )
    e_novo = stream.build_ok_payload(
        process_id=proc.pk, tribunal=proc.tribunal_id, numero_cnj=proc.numero_cnj,
        scraped_at='2026-04-29T02:00:00', dados={}, partes={},
    )
    ok, falhas = drainer.apply_batch([e_velho, e_novo])
    assert ok == 1 and falhas == 0
    proc.refresh_from_db()
    assert proc.enriquecimento_status == Process.ENRIQ_OK


def test_apply_batch_falha_isolada_nao_envenena(proc):
    e_bom = stream.build_nao_encontrado_payload(
        process_id=proc.pk, tribunal=proc.tribunal_id, numero_cnj=proc.numero_cnj,
        scraped_at='2026-04-29T01:00:00',
    )
    # Forja evento "ok" malformado pra forçar exceção DENTRO de apply_event.
    # ⚠️ Tem que apontar para um process que EXISTE. A versão anterior usava
    # `process_id=9_999_999_999`, e aí `apply_event` batia antes na guarda de
    # "process desaparecido", que faz `logger.warning` + `return` — sem exceção.
    # Resultado: `falhas` vinha 0 e o teste dizia estar provando isolamento de
    # falha quando na verdade não havia falha nenhuma para isolar.
    # ⚠️ process DIFERENTE do e_bom: `apply_batch` deduplica por `process_id` e
    # fica com o de maior `scraped_at` (comparação de STRING). Como 'x' > '2026…',
    # apontar os dois para o mesmo processo faria o evento ruim ENGOLIR o bom, e
    # o teste mediria a deduplicação em vez do isolamento de falha.
    outro = Process.objects.create(
        tribunal=proc.tribunal, numero_cnj='9999999-99.2026.8.26.0100', ano_cnj=2026)
    e_ruim = {
        'v': stream.SCHEMA_VERSION, 'status': stream.STATUS_OK,
        'process_id': outro.pk,
        'scraped_at': 'x',
        'dados': None,
        'partes': 'string-em-vez-de-dict',  # quebra no `.items()`
    }
    ok, falhas = drainer.apply_batch([e_bom, e_ruim])
    # `apply_batch` é all-or-nothing DENTRO da transação (deadlock rola tudo e o
    # XAUTOCLAIM reentrega). Evento malformado não é transitório, então ele é
    # posto em QUARENTENA antes da transação — senão um `partes` que veio string
    # mata os outros 999 eventos bons do lote, em toda reentrega, para sempre.
    assert ok == 1 and falhas == 1
    proc.refresh_from_db()
    assert proc.enriquecimento_status == Process.ENRIQ_NAO_ENCONTRADO


def test_apply_batch_lista_vazia():
    assert drainer.apply_batch([]) == (0, 0)


def test_classe_aceita_1_digito_e_assunto_NAO_e_isso_e_de_proposito():
    """A assimetria entre os dois regexes é MEDIDA, não descuido.

    **Classe aceita 1 dígito.** `PROCEDIMENTO COMUM CÍVEL (7)` é a classe mais
    comum do país — 28.790.468 documentos do acervo, 8,4% —, e o `\\d{2,5}` de
    antes nunca a casou. Medido em 200.000 processos: dos 3.887 com
    `classe_nome` preenchido e `classe_codigo` vazio, **3.717 (95,6%) eram
    exatamente essa classe**. O `{2,5}` era 95,6% de todo o buraco.

    **Assunto NÃO aceita 1 dígito, e abrir seria erro puro.** Dos 193 assuntos
    com cauda de um dígito, **os 193** eram código de 2+ dígitos cortado no
    limite de 255 caracteres da coluna: `DIREITO PREVIDENCIÁRIO (1` é `(195`.
    Aceitar 1 dígito ali gravaria `1` onde o código é `195` — trocaria um campo
    vazio (que a tela diz que está vazio) por um campo ERRADO, que é pior.

    Este teste existe porque a fixture antiga usava `Tributário (5)`, um código
    de assunto de um dígito que **não existe na TPU** — e a fixture irreal fez o
    teste cobrar do código o comportamento que a medição diz ser errado.
    """
    from enrichers import drainer

    # classe de 1 dígito: TEM que casar (é a classe mais comum do Brasil)
    out = drainer.normalize_dados({'classe': 'PROCEDIMENTO COMUM CÍVEL (7)'})
    assert out['classe_codigo'] == '7', 'voltou o `{2,5}` — 1,9 M de processos sem código'
    assert out['classe_nome'] == 'PROCEDIMENTO COMUM CÍVEL'

    # assunto de 1 dígito: NÃO casa, e o nome fica inteiro (abster > chutar)
    out = drainer.normalize_dados({'assunto': 'DIREITO PREVIDENCIÁRIO (1'})
    assert 'assunto_codigo' not in out, 'gravou código truncado — pior que vazio'
