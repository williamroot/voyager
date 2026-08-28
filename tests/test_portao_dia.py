"""O portão tem que pegar o "success verde com um terço do dia".

CONTEXTO (medido em 25-27/08/2026). A ingestão do dia 25/08 morreu inteira:
10.410 `IngestionRun` em `failed`, zero `success`. Ninguém viu por 21 horas — o
que denunciou foi um KPI da tela, não um portão.

E quando o dia voltou, voltou **incompleto**: 1.180.554 publicações contra
1.529.530 do dia vizinho. O run fechou `success`, o log ficou limpo, o número
parecia plausível. É o padrão que o CLAUDE.md chama de **run verde, log limpo,
número redondo**, e ele já custou 47.141 publicações uma vez.

O que estes testes protegem:
  1. dia sem `success` é problema, mesmo com publicação no banco;
  2. dia COM `success` mas com um terço do volume do próprio tribunal é
     problema — é o caso que motivou o comando existir;
  3. a comparação é POR TRIBUNAL: um TJSP inteiro sumido não pode se dissolver
     no agregado nacional;
  4. fim de semana e tribunal de baixo volume NÃO viram alarme falso — portão
     que grita sempre é portão que ninguém lê;
  5. `failed` sem `success` posterior é problema; com `success` posterior, não;
  6. quando há buraco, o comando SAI COM ERRO — para servir a cron e CI sem
     depender de alguém ler a saída.
"""
import datetime

import pytest
from django.core.management import call_command

from tribunals import portao as CD


class _Cmd:
    """As três leituras de banco trocadas por dados de teste.

    Injeção em vez de mock de ORM: um teste que precisa de banco para provar
    aritmética de mediana envelhece mal e esconde o que está sendo testado.
    """

    def __init__(self, cont, runs, tribs):
        self._cont, self._runs_, self._tribs = cont, runs, tribs

    def _conferir(self, dia, o):
        return CD.conferir(dia, fracao=o['fracao'], piso=o['piso'],
                           leitores=(lambda i, f: self._cont,
                                     lambda d: self._runs_,
                                     lambda d: self._tribs))


DIA = datetime.date(2026, 8, 25)          # uma terça-feira
OPC = {'piso': CD.PISO_MEDIANA, 'fracao': CD.FRACAO_MINIMA}


def _mesmas_semanas(t, valor, dia=DIA):
    """Contagens `valor` no MESMO dia da semana, nas semanas vizinhas.

    É assim que a mediana tem que ser montada: terça contra terça. Comparar com
    dias úteis vizinhos misturava terça com sexta e, no TJPR, isso é 38× de
    diferença (medido em 28/08/2026).
    """
    fora = {}
    for k in range(-CD.SEMANAS, CD.SEMANAS + 1):
        d = dia + datetime.timedelta(weeks=k)
        if d != dia:
            fora[(t, d)] = valor
    return fora


def test_dia_com_um_terco_do_volume_e_PROBLEMA():
    """O caso real: TJSP com 1,18 M onde a mediana dele é 1,53 M."""
    cont = _mesmas_semanas('TJSP', 1_529_530)
    cont[('TJSP', DIA)] = 500_000            # 32,7% do normal
    r = _Cmd(cont, {'TJSP': {'success': 1}}, ['TJSP'])._conferir(DIA, OPC)
    assert len(r['problemas']) == 1
    p = r['problemas'][0]
    assert p['t'] == 'TJSP'
    assert '33% do normal' in ' '.join(p['motivos']) or '% do normal' in ' '.join(p['motivos'])
    assert p['falta'] > 1_000_000, 'não disse QUANTO falta — número sem tamanho não age'


def test_dia_completo_com_success_FECHA():
    cont = _mesmas_semanas('TJSP', 1_500_000)
    cont[('TJSP', DIA)] = 1_480_000          # variação normal
    r = _Cmd(cont, {'TJSP': {'success': 1}}, ['TJSP'])._conferir(DIA, OPC)
    assert r['problemas'] == []
    assert r['fechados'] == 1


def test_sem_run_success_e_PROBLEMA_mesmo_com_publicacao():
    """Publicação no banco sem run de sucesso = alguém escreveu por fora."""
    cont = _mesmas_semanas('TJRS', 60_000)
    cont[('TJRS', DIA)] = 60_000
    r = _Cmd(cont, {}, ['TJRS'])._conferir(DIA, OPC)
    assert 'sem run success' in ' '.join(r['problemas'][0]['motivos'])


def test_failed_com_success_POSTERIOR_nao_e_problema():
    """Quebrou e o watchdog refez: isso é o sistema funcionando."""
    cont = _mesmas_semanas('TJPR', 40_000)
    cont[('TJPR', DIA)] = 39_000
    r = _Cmd(cont, {'TJPR': {'failed': 1, 'success': 2}}, ['TJPR'])._conferir(DIA, OPC)
    assert r['problemas'] == []


def test_failed_SEM_success_posterior_e_problema():
    cont = _mesmas_semanas('TJPR', 40_000)
    cont[('TJPR', DIA)] = 39_000
    r = _Cmd(cont, {'TJPR': {'success': 1, 'failed': 5}}, ['TJPR'])._conferir(DIA, OPC)
    assert 'failed sem success posterior' in ' '.join(r['problemas'][0]['motivos'])


def test_tribunal_de_baixo_volume_NAO_vira_alarme_falso():
    """Portão que grita sempre é portão que ninguém lê."""
    cont = _mesmas_semanas('TJRR', 12)
    cont[('TJRR', DIA)] = 3
    r = _Cmd(cont, {}, ['TJRR'])._conferir(DIA, OPC)
    assert r['problemas'] == []
    assert r['fechados'] == 1


def test_dia_zerado_na_amostra_nao_derruba_a_mediana():
    """Feriado (0 publicações) sai da amostra em vez de virar "o normal é zero".

    Se um 0 entrasse na mediana, ela desabaria e um buraco REAL passaria batido.
    """
    cont = _mesmas_semanas('TJSP', 1_000_000)
    # duas das semanas vizinhas foram feriado
    cont[('TJSP', DIA - datetime.timedelta(weeks=1))] = 0
    cont[('TJSP', DIA - datetime.timedelta(weeks=2))] = 0
    cont[('TJSP', DIA)] = 100_000                 # 10% do normal
    r = _Cmd(cont, {'TJSP': {'success': 1}}, ['TJSP'])._conferir(DIA, OPC)
    assert r['problemas'], 'o feriado derrubou a mediana e escondeu o buraco'


def test_TJPR_na_terca_NAO_e_acusado_o_falso_positivo_que_eu_criei():
    """O caso real que provou a régua antiga errada (medido em 28/08/2026).

    O TJPR publica ~6,4 mil na terça e até 237 mil na sexta — 38× dentro da
    MESMA semana. A régua anterior usava "dias úteis vizinhos" e acusou a terça
    25/08 de estar com "14% do normal", quando 6.875 era a MAIOR das três
    terças medidas. Conferido contra a fonte: coleta íntegra, gap 0.

    Portão com falso positivo é portão que ninguém lê — e aí não protege nada no
    dia em que o buraco é real.
    """
    cont = {}
    for k in range(-CD.SEMANAS, CD.SEMANAS + 1):
        d = DIA + datetime.timedelta(weeks=k)     # terças
        cont[('TJPR', d)] = 6_400
        # e as sextas da mesma semana, com o volume alto que enganava a régua
        cont[('TJPR', d + datetime.timedelta(days=3))] = 90_000
    cont[('TJPR', DIA)] = 6_875
    r = _Cmd(cont, {'TJPR': {'success': 1}}, ['TJPR'])._conferir(DIA, OPC)
    assert r['problemas'] == [], (
        'acusou terça comparando com sexta — o falso positivo voltou')


def test_sem_amostra_do_mesmo_dia_da_semana_ABSTEM_do_criterio_de_volume():
    """Mediana de duas terças não é mediana, é palpite com cara de estatística."""
    cont = {('TJSP', DIA - datetime.timedelta(weeks=1)): 1_000_000,
            ('TJSP', DIA): 10}
    r = _Cmd(cont, {'TJSP': {'success': 1}}, ['TJSP'])._conferir(DIA, OPC)
    assert r['problemas'] == [], 'acusou com uma amostra só'
    assert r['sem_amostra'] == ['TJSP'], (
        'a abstenção ficou invisível — "fechado" que é "não consegui olhar" '
        'é o silêncio verde de novo')


def test_a_comparacao_e_POR_TRIBUNAL_e_nao_no_agregado():
    """Um TJSP inteiro sumido não pode se dissolver nos outros 58."""
    cont = {}
    for t in ('TJMG', 'TJRJ', 'TJRS', 'TJPR', 'TJSC'):
        cont.update(_mesmas_semanas(t, 300_000))
        cont[(t, DIA)] = 300_000
    cont.update(_mesmas_semanas('TJSP', 1_500_000))
    cont[('TJSP', DIA)] = 0                       # sumiu inteiro
    tribs = ['TJMG', 'TJPR', 'TJRJ', 'TJRS', 'TJSC', 'TJSP']
    runs = {t: {'success': 1} for t in tribs}
    r = _Cmd(cont, runs, tribs)._conferir(DIA, OPC)
    assert [p['t'] for p in r['problemas']] == ['TJSP']
    assert r['fechados'] == 5


@pytest.mark.django_db
def test_o_comando_SAI_COM_ERRO_quando_ha_buraco(monkeypatch):
    """Cron e CI não podem depender de alguém LER a saída."""
    cont = _mesmas_semanas('TJSP', 1_000_000)
    cont[('TJSP', DIA)] = 10_000
    monkeypatch.setattr(CD, '_contagens', lambda i, f, teto='240s': cont)
    monkeypatch.setattr(CD, '_runs', lambda d: {'TJSP': {'success': 1}})
    monkeypatch.setattr(CD, '_tribunais', lambda d: ['TJSP'])
    with pytest.raises(SystemExit) as e:
        call_command('conferir_dia', '2026-08-25')
    assert e.value.code == 1


@pytest.mark.django_db
def test_o_comando_sai_ZERO_quando_o_dia_fecha(monkeypatch):
    cont = _mesmas_semanas('TJSP', 1_000_000)
    cont[('TJSP', DIA)] = 990_000
    monkeypatch.setattr(CD, '_contagens', lambda i, f, teto='240s': cont)
    monkeypatch.setattr(CD, '_runs', lambda d: {'TJSP': {'success': 1}})
    monkeypatch.setattr(CD, '_tribunais', lambda d: ['TJSP'])
    call_command('conferir_dia', '2026-08-25')     # não levanta


# ─────────────────────────────── o VIGIA: o portão que roda sozinho

@pytest.mark.django_db
def test_o_vigia_GRITA_com_nome_e_numero(monkeypatch):
    """"alguns tribunais incompletos" não faz ninguém agir.

    O que faz agir é "TJPR 6.875/50.066, faltam 43.190". Um log de ERROR sem o
    número é a mesma omissão que o portão existe para acusar.
    """
    from unittest.mock import patch
    rel = {'dia': '2026-08-25', 'tribunais': 59, 'fechados': 58, 'total_dia': 1_180_554,
           'falta_estimado': 43_190,
           'problemas': [{'t': 'TJPR', 'n': 6_875, 'med': 50_066, 'falta': 43_190,
                          'motivos': ['14% do normal']}]}
    monkeypatch.setattr(CD, 'conferir', lambda dia, **kw: rel)
    with patch.object(CD.logger, 'error') as erro:
        CD.vigiar()
    assert erro.called, 'dia com buraco passou sem ERROR'
    msg = erro.call_args.args[0] % erro.call_args.args[1:]
    assert 'TJPR' in msg, 'não disse QUEM'
    assert '43,190' in msg or '43.190' in msg, 'não disse QUANTO falta'


@pytest.mark.django_db
def test_o_vigia_nao_grita_quando_o_dia_fecha(monkeypatch):
    """Alarme que dispara sempre vira ruído e ninguém lê o dia em que importa."""
    from unittest.mock import patch
    rel = {'dia': '2026-08-24', 'tribunais': 59, 'fechados': 59, 'total_dia': 1_529_530,
           'falta_estimado': 0, 'problemas': []}
    monkeypatch.setattr(CD, 'conferir', lambda dia, **kw: rel)
    with patch.object(CD.logger, 'error') as erro:
        CD.vigiar()
    assert not erro.called


@pytest.mark.django_db
def test_o_vigia_NUNCA_levanta(monkeypatch):
    """Vigia que derruba o scheduler leva junto os outros jobs."""
    def explode(dia, **kw):
        raise RuntimeError('banco fora')
    monkeypatch.setattr(CD, 'conferir', explode)
    r = CD.vigiar()          # não pode levantar
    assert all(d.get('erro') for d in r['dias'])


def test_o_comando_e_o_vigia_usam_a_MESMA_regua():
    """Duas réguas para a mesma pergunta produzem discordância honesta e cara.

    Em 27/08/2026 duas implementações independentes olharam o dia 25/08,
    concordaram na contagem crua do TJPR (6.875 nas duas) e discordaram no
    tamanho do buraco (43.190 contra 81.721) só porque montavam a mediana de
    jeitos diferentes.
    """
    cmd = open('tribunals/management/commands/conferir_dia.py').read()
    assert 'from tribunals import portao' in cmd
    assert 'portao.conferir(' in cmd
    assert 'def conferir' not in cmd, 'o comando voltou a ter régua própria'
