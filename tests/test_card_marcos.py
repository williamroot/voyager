"""A régua da semana não pode mostrar número velho como se fosse de agora.

O relato do que foi feito vive na timeline do Acompanhamento. Este card guarda a
RÉGUA, e a diferença importa: um relato diz "consertamos a duplicação"; a régua
diz **2,70 → 1,02**, e é ela que permite conferir daqui a um mês se regrediu.

Duas honestidades que o card tem que manter:

  1. o **agora** é sempre MEDIDO — nenhum número digitado. Se algum dia alguém
     fixar um valor "só para a tela ficar bonita", o card vira propaganda;
  2. o **antes** é constante COM DATA, porque não existe máquina do tempo: a
     duplicação de 2,70× medida em 27/08 não se recalcula hoje. Assumir isso é
     melhor que fingir precisão.

E a terceira, que é a mais fácil de esquecer: **marco que não deu para medir SAI
da lista e aparece pelo nome**. Sumir da tela seria silêncio verde — o card
pareceria completo com uma régua a menos.
"""
import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.urls import reverse

from dashboard import marcos_semana as MS

PAYLOAD = {
    'em': '2026-08-28T13:50:00+00:00',
    'marcos': [
        {'k': 'duplicacao', 'rotulo': 'Duplicação da coleta diária', 'antes': 2.70,
         'agora': 1.02, 'sufixo': '×', 'sobe': False, 'medido_em': '27/08',
         'nota': '1,0 = cada tribunal-dia lido uma vez só'},
        {'k': 'sync', 'rotulo': 'Atraso da busca', 'antes': 128.26, 'agora': 0.0,
         'sufixo': ' h', 'sobe': False, 'medido_em': '26/08', 'nota': 'estava divergindo'},
    ],
    'nao_medidos': ['Cobertura do acervo nacional'],
}


@pytest.fixture
def logado(db, client):
    u = get_user_model().objects.create_user('quem2', password='x')
    client.force_login(u)
    return client


def _html(c):
    r = c.get(reverse('dashboard:acompanhamento'))
    assert r.status_code == 200
    return r.content.decode()


@pytest.mark.django_db
def test_mostra_o_antes_e_o_agora_de_cada_regua(logado):
    cache.set(MS.CHAVE, PAYLOAD, 60)
    h = _html(logado)
    assert 'Avanços da semana' in h
    assert '2.7×' in h or '2,7×' in h or '2.70×' in h
    assert '1.02×' in h or '1,02×' in h
    assert 'Duplicação da coleta diária' in h


@pytest.mark.django_db
def test_o_antes_vem_DATADO(logado):
    """Sem a data, alguém lê o "antes" como se fosse de hoje."""
    cache.set(MS.CHAVE, PAYLOAD, 60)
    h = _html(logado)
    assert '27/08' in h and '26/08' in h


@pytest.mark.django_db
def test_marco_sem_medicao_aparece_PELO_NOME(logado):
    """Sumir da tela é silêncio verde: o card pareceria completo faltando régua."""
    cache.set(MS.CHAVE, PAYLOAD, 60)
    h = _html(logado)
    assert 'Sem medição nesta passagem' in h
    assert 'Cobertura do acervo nacional' in h


@pytest.mark.django_db
def test_sem_cache_o_card_nao_aparece(logado):
    cache.delete(MS.CHAVE)
    h = _html(logado)
    assert 'Avanços da semana' not in h


def test_todo_marco_tem_funcao_de_medicao_e_nenhum_valor_fixo_de_agora():
    """O `agora` é consultado, nunca digitado."""
    for m in MS.MARCOS:
        assert callable(m['fn']), f"{m['k']} não tem como medir o agora"
        assert 'agora' not in m, f"{m['k']} tem valor de agora FIXO — isso é propaganda"
        assert m['medido_em'], f"{m['k']} sem a data do antes"


def test_marco_que_falha_na_medicao_sai_da_lista(monkeypatch):
    """Meia régua é pior que régua nenhuma."""
    def explode():
        raise RuntimeError('banco fora')
    marcos = [dict(MS.MARCOS[0]), dict(MS.MARCOS[1])]
    marcos[0]['fn'] = explode
    marcos[1]['fn'] = lambda: 42
    monkeypatch.setattr(MS, 'MARCOS', marcos)
    p = MS.calcular()
    assert len(p['marcos']) == 1
    assert p['nao_medidos'] == [marcos[0]['rotulo']]


def test_ler_nunca_calcula(monkeypatch):
    """O portão, que é um dos marcos, custa segundos — não pode ir para a view."""
    cache.delete(MS.CHAVE)
    def explode():
        raise AssertionError('ler() chamou calcular() no caminho da requisição')
    monkeypatch.setattr(MS, 'calcular', explode)
    assert MS.ler() is None


def test_o_dia_de_referencia_ignora_fim_de_semana():
    """Sábado tem run nos 59 tribunais e ZERO páginas — não há publicação.

    Usar "o último dia" cru fez o marco de páginas mostrar `14.760 → 0` num
    sábado (medido em 29/08/2026): tecnicamente certo e enganoso — parece a
    vitória do século e é só o fim de semana. Número que engana é pior que
    número ausente.
    """
    assert 'HAVING sum(paginas_lidas) > 0' in MS._SQL_DIA_UTIL, (
        'o dia de referência voltou a aceitar dia sem coleta')
    for fn in (MS._fator_duplicacao, MS._paginas_do_dia):
        import inspect
        assert '_SQL_DIA_UTIL' in inspect.getsource(fn), (
            f'{fn.__name__} não usa o dia com coleta')
