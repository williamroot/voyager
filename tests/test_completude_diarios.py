"""A lista de fontes de diário tem UMA linha por fonte.

CONTEXTO (21/08/2026). A tela de completude imprimia **8 cartões `tjsp-dje`
idênticos**, cada um repetindo o mesmo agregado — "0 resolvidas, 8 pendentes de
8". Quem olhasse leria 64 pendências onde havia 8.

A causa é a armadilha clássica do Django: `EdicaoDiario.Meta` tem
`ordering = ['-data', 'chave']`, e o ORM injeta as colunas do ORDER BY no
SELECT quando você pede DISTINCT. Então

    E.objects.values_list('fonte', flat=True).distinct()

vira `SELECT DISTINCT fonte, data, chave` — e devolve uma linha por EDIÇÃO, não
por fonte. Nada estoura, nada loga: a tela só mostra um número inventado, que é
a assinatura de erro que este projeto mais paga caro.

A cura é `.order_by()` NUA antes do `.values_list()`.
"""
import datetime

import pytest


@pytest.fixture
def oito_edicoes(db):
    from diarios.models import EdicaoDiario
    from tribunals.models import Tribunal
    t, _ = Tribunal.objects.get_or_create(
        sigla='TJSP', defaults={'nome': 'TJSP', 'sigla_djen': 'TJSP'})
    for i in range(10, 18):
        EdicaoDiario.objects.create(
            fonte='tjsp-dje', chave=f'4161-{i}', data=datetime.date(2025, 3, 12),
            tribunal=t, status=EdicaoDiario.PENDENTE)
    return t


@pytest.mark.django_db
def test_uma_linha_por_fonte_e_nao_por_edicao(oito_edicoes):
    from dashboard.completude_warm import _diarios
    fontes = _diarios()
    assert len(fontes) == 1, (
        f'{len(fontes)} cartões para 1 fonte — o DISTINCT pegou (fonte, data, chave). '
        'Falta `.order_by()` nua antes do values_list.')
    assert fontes[0]['slug'] == 'tjsp-dje'
    assert fontes[0]['total'] == 8
    assert fontes[0]['pendentes'] == 8
    assert fontes[0]['resolvidas'] == 0


@pytest.mark.django_db
def test_duas_fontes_dao_duas_linhas(oito_edicoes):
    """Sem isto, o fix poderia ter virado um `[:1]` que esconde fonte de verdade."""
    from dashboard.completude_warm import _diarios
    from diarios.models import EdicaoDiario
    EdicaoDiario.objects.create(
        fonte='dejt', chave='x-1', data=datetime.date(2025, 3, 12),
        tribunal=oito_edicoes, status=EdicaoDiario.PENDENTE)
    assert {f['slug'] for f in _diarios()} == {'tjsp-dje', 'dejt'}


@pytest.mark.django_db
def test_feriado_forense_nao_conta_como_buraco(oito_edicoes):
    """`inexistente` é resposta conhecida, não lacuna — contar recesso como
    buraco seria inventar um problema."""
    from dashboard.completude_warm import _diarios
    from diarios.models import EdicaoDiario
    EdicaoDiario.objects.filter(chave='4161-10').update(status=EdicaoDiario.INEXISTENTE)
    f = _diarios()[0]
    assert f['resolvidas'] == 1 and f['pendentes'] == 7
