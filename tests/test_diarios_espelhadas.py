"""A métrica de sobreposição entre portas tinha que ser capaz de acertar.

CONTEXTO (20/08/2026, com a ingestão de diários já ligada nos 59 tribunais).

`espelhadas_no_lote` responde "quantos atos deste lote a outra porta já tinha
trazido". É a régua do princípio nº 5 — medir a completude dos dois lados. Ela
estava errada de duas formas ao mesmo tempo, e as duas passavam verdes:

1. NÃO PODIA ACERTAR. Comparava `fingerprint_ato` (sha1, 40 chars) com
   `Movimentacao.hash`, que nas linhas do DJEN é o hash opaco da API — 30 chars
   (`djen/parser.py:243`; medido por amostra em prod: onde não é vazio, len=30).
   Uma string de 40 nunca é igual a uma de 30 ⇒ o resultado era 0 por
   construção. E `espelhadas=0` lê-se "o diário próprio não repete o DJEN", que
   é a conclusão oposta da verdade, dita com a autoridade de um número.

   O teste antigo não pegava porque construía o item do DJEN COM o fingerprint.

2. CUSTAVA CARO PRA ERRAR. `hash` não tem índice (declarado no model, ausente
   do banco). EXPLAIN do lote de 200: custo 73.427.276 no TJSP, 6.980.195 até
   no TJAC — por lote, dentro do caminho de escrita, sem teto de espera.

Agora pareia por (processo, data), que é o que os dois veículos de fato
compartilham, e usa `mov_processo_data_disp_idx`, que existe.
"""
from datetime import UTC, datetime, timedelta

import pytest


@pytest.fixture
def cenario(db):
    """Um ato já gravado pelo diário próprio; o DJEN traz o mesmo ato depois."""
    from django.utils import timezone

    from diarios.base import fingerprint_ato
    from tribunals.models import Movimentacao, Process, Tribunal
    t, _ = Tribunal.objects.get_or_create(
        sigla='TJSP', defaults={'nome': 'TJSP', 'sigla_djen': 'TJSP'})
    Movimentacao.objects.filter(tribunal=t).delete()
    cnj = '1099663-22.2025.8.26.0100'
    quando = timezone.make_aware(datetime(2025, 7, 21, 9, 0))
    p, _ = Process.objects.get_or_create(tribunal=t, numero_cnj=cnj)
    texto = 'REQTE: Gisela Aparecida Paulino. Vista às partes.'
    Movimentacao.objects.create(
        processo=p, tribunal=t, external_id='tjsp-dje:4246:12:3',
        data_disponibilizacao=quando, texto=texto,
        hash=fingerprint_ato(cnj, quando, texto),
    )
    return t, cnj, quando, texto


def _item(cnj, quando, texto, ext_id='695042804'):
    from djen.parser import ParsedItem
    return ParsedItem(
        cnj=cnj, external_id=ext_id, data_disponibilizacao=quando, texto=texto,
        # 30 chars opacos: o que a produção grava, não um fingerprint.
        hash='7e9MjpmEYnBUkdVulTlPJE8Yqr',
        meio='D', meio_completo='Diário de Justiça Eletrônico Nacional',
    )


@pytest.mark.django_db
def test_ve_a_sobreposicao_mesmo_com_hash_incompativel(cenario):
    """O caso que a métrica antiga não podia enxergar."""
    from diarios.base import espelhadas_no_lote
    t, cnj, quando, texto = cenario
    assert espelhadas_no_lote([_item(cnj, quando, texto)], t) == 1


@pytest.mark.django_db
def test_nao_conta_o_proprio_lote(cenario):
    """Re-coletar a mesma edição não é sobreposição entre portas."""
    from diarios.base import espelhadas_no_lote
    t, cnj, quando, texto = cenario
    mesmo = _item(cnj, quando, texto, ext_id='tjsp-dje:4246:12:3')
    assert espelhadas_no_lote([mesmo], t) == 0


@pytest.mark.django_db
def test_outro_dia_nao_conta(cenario):
    """Mesmo processo, dia diferente = outro ato."""
    from diarios.base import espelhadas_no_lote
    t, cnj, quando, texto = cenario
    assert espelhadas_no_lote([_item(cnj, quando + timedelta(days=3), texto)], t) == 0


@pytest.mark.django_db
def test_processo_desconhecido_e_inedito(cenario):
    from diarios.base import espelhadas_no_lote
    t, _, quando, texto = cenario
    assert espelhadas_no_lote([_item('0000001-11.2025.8.26.0100', quando, texto)], t) == 0


@pytest.mark.django_db
def test_meia_noite_nao_escorrega_de_dia(cenario):
    """`.date()` em UTC punha o lote no dia anterior do outro lado, e a métrica
    perdia exatamente a sobreposição que veio buscar."""
    from diarios.base import espelhadas_no_lote
    from tribunals.models import Movimentacao, Process
    t, cnj, _, texto = cenario
    meia_noite = datetime(2025, 7, 25, 3, 0, tzinfo=UTC)   # = 00:00 em SP
    p = Process.objects.get(tribunal=t, numero_cnj=cnj)
    Movimentacao.objects.create(
        processo=p, tribunal=t, external_id='tjsp-dje:9:9:9',
        data_disponibilizacao=meia_noite, texto=texto, hash='x')
    assert espelhadas_no_lote([_item(cnj, meia_noite, texto)], t) == 1


@pytest.mark.django_db
def test_nao_consulta_a_coluna_hash(cenario):
    """Regressão dura: voltar a filtrar por `hash` é voltar aos 73 milhões."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from diarios.base import espelhadas_no_lote
    t, cnj, quando, texto = cenario
    with CaptureQueriesContext(connection) as capturado:
        espelhadas_no_lote([_item(cnj, quando, texto)], t)
    sql = ' '.join(q['sql'] for q in capturado.captured_queries)
    assert '"hash"' not in sql, 'a métrica voltou a varrer uma coluna sem índice'


@pytest.mark.django_db
def test_timeout_se_abstem_em_vez_de_dizer_zero(cenario):
    """Métrica nunca segura escrita — e "não sei" nunca vira 0 (regras 6 e 7)."""
    from unittest.mock import patch

    from django.db import OperationalError

    from diarios.base import espelhadas_no_lote
    t, cnj, quando, texto = cenario
    with patch('diarios.base.Movimentacao.objects.filter',
               side_effect=OperationalError('canceling statement due to statement timeout')):
        assert espelhadas_no_lote([_item(cnj, quando, texto)], t) is None
