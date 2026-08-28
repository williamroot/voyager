"""O alarme de frota enxergava 17% da frota — e dizia estar tudo bem.

INCIDENTE DE OBSERVABILIDADE (medido em 28/08/2026, produção):

    chaves `rq:worker:*` com heartbeat vivo ...... 256   (scan: 0,44 s)
    o que `Worker.all()` enxergava ...............  44
    CEGUEIRA .................................... 212 = 82,8%

`Worker.all()` lê o SET `rq:workers`, que é um REGISTRO. Registro incompleto não
levanta erro — só devolve menos. O watchdog reportava
`frota: {'total': 44, 'velhos': 22, 'atraso_horas': 42.7}` com 256 workers de
pé: denominador errado, percentual errado, **run verde, log limpo, número
redondo**.

E o agravante é o alvo do alarme: ele existe para pegar worker rodando código
velho — o incidente de 21/08/2026, em que 2 workers de 14/08 fizeram o watchdog
NUNCA rodar e deixaram 3.007 dias parados. Com 17% de visão, ele veria esse
mesmo incidente em 1 de cada 6 containers.

⚠️ A ARMADILHA É PIOR QUE O DEFEITO. `hget(chave, 'birth')` cru devolve **None**
em boa parte das chaves (medido: 3 de 5 na amostra de produção). Quem trocar a
fonte de enumeração e ler o campo na mão conclui que "nenhum worker é velho" —
que é exatamente o que o alarme diz quando está tudo bem.
"""
from unittest.mock import MagicMock, patch

from djen import jobs as J


class _Conn:
    """Redis de mentira: `scan_iter` vê tudo, o registro do RQ vê um pedaço."""

    def __init__(self, chaves):
        self._chaves = chaves

    def scan_iter(self, padrao, count=None):
        assert padrao == 'rq:worker:*'
        return iter(self._chaves)


def test_a_frota_e_enumerada_por_SCAN_e_nao_pelo_registro():
    """O teste que reproduz a cegueira: 256 no scan, 44 no registro."""
    chaves = [f'rq:worker:{i:032x}'.encode() for i in range(256)]
    conn = _Conn(chaves)

    achados = []

    def find_by_key(nome, connection=None):
        achados.append(nome)
        w = MagicMock()
        w.birth_date = None
        return w

    with patch('rq.Worker.find_by_key', side_effect=find_by_key), \
         patch('rq.Worker.all', return_value=[MagicMock() for _ in range(44)]):
        frota = J._frota_viva(conn)

    assert len(frota) == 256, (
        f'enxergou {len(frota)} de 256 — voltou a ler o registro `rq:workers`')
    assert len(achados) == 256


def test_chave_morta_e_descartada_e_nao_derruba():
    """`find_by_key` volta None em chave morta — descarta, não explode."""
    chaves = [b'rq:worker:vivo', b'rq:worker:morto', b'rq:worker:corrompido']

    def find_by_key(nome, connection=None):
        if nome.endswith('morto'):
            return None
        if nome.endswith('corrompido'):
            raise ValueError('lixo no hash')
        w = MagicMock()
        w.birth_date = None
        return w

    with patch('rq.Worker.find_by_key', side_effect=find_by_key):
        frota = J._frota_viva(_Conn(chaves))
    assert len(frota) == 1


def test_o_scan_tem_TETO_e_avisa_quando_estoura():
    """Diagnóstico não pode derrubar o vigia — mas teto mudo é pior.

    Se o scan for cortado, o denominador sai SUBESTIMADO, que é exatamente o
    defeito que estamos consertando. Então ele avisa.
    """
    chaves = [f'rq:worker:{i:032x}'.encode() for i in range(50_000)]

    def find_by_key(nome, connection=None):
        w = MagicMock()
        w.birth_date = None
        return w

    with patch('rq.Worker.find_by_key', side_effect=find_by_key), \
         patch.object(J, 'FROTA_SCAN_TETO_S', 0.0), \
         patch.object(J.logger, 'warning') as aviso:
        J._frota_viva(_Conn(chaves))
    assert aviso.called, 'cortou a lista em silêncio — denominador subestimado'
    assert 'INCOMPLETA' in aviso.call_args.args[0]


def test_NAO_le_o_campo_birth_cru():
    """A armadilha: `hget(k, 'birth')` volta None e vira "ninguém é velho".

    Medido em produção: 3 de 5 chaves da amostra devolveram `None` no campo
    cru. Ler na mão produziria o pior resultado possível — o alarme silencioso.
    """
    import ast
    fonte = open('djen/jobs.py').read()
    arvore = next(n for n in ast.walk(ast.parse(fonte))
                  if isinstance(n, ast.FunctionDef) and n.name == '_frota_viva')
    # olha as CHAMADAS, não o texto: a docstring cita `hget` de propósito, para
    # avisar do risco, e um teste que casa string acusaria o próprio aviso.
    chamadas = {n.func.attr for n in ast.walk(arvore)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert 'hget' not in chamadas, 'leu o hash cru — `birth` volta None e mente'
    assert 'hgetall' not in chamadas
    assert 'find_by_key' in chamadas, 'precisa do desserializador do RQ'


def test_o_alerta_usa_a_frota_viva():
    fonte = open('djen/jobs.py').read()
    i = fonte.find('def _alerta_workers_velhos')
    corpo = fonte[i:i + 1200]
    assert '_frota_viva(' in corpo
    assert 'Worker.all(' not in corpo, 'voltou ao registro incompleto'


# ─────────────── enumerar NÃO é avaliar (a quase-armadilha do conserto)

def _frota_fake(com_birth, sem_birth):
    """`com_birth` workers com `birth_date` antigo; `sem_birth` com o campo vazio."""
    import datetime
    velho = datetime.datetime(2020, 1, 1)
    fora = []
    for _ in range(com_birth):
        w = MagicMock()
        w.birth_date = velho
        w.queue_names.return_value = ['default']
        fora.append(w)
    for _ in range(sem_birth):
        w = MagicMock()
        w.birth_date = None
        fora.append(w)
    return fora


def test_worker_sem_birth_conta_como_NAO_AVALIADO_e_nao_como_ok():
    """Consertar a enumeração e não a avaliação PIORA o alarme.

    MEDIDO em 28/08/2026, depois de trocar `Worker.all()` pelo scan: a frota
    passou de 44 para **256** workers vistos, mas só **44 têm `birth_date`** —
    os mesmos 44 de antes. Reportar "total: 256, velhos: 0" seria pior que o
    defeito original: denominador maior com o mesmo numerador faz o percentual
    parecer melhor sem enxergar um worker a mais.
    """
    import time
    with patch.object(J, '_frota_viva', return_value=_frota_fake(44, 212)), \
         patch('django_rq.get_connection', return_value=MagicMock()), \
         patch.object(J.logger, 'error'), patch.object(J.logger, 'warning'):
        r = J._alerta_workers_velhos(time.time())
    assert r['total'] == 256
    assert r['avaliaveis'] == 44, 'contou como avaliado quem não tem birth_date'
    assert r['nao_avaliados'] == 212, 'a cegueira restante ficou invisível'


def test_a_cobertura_baixa_do_alarme_VIRA_ERRO():
    """Alarme que não sabe o quanto NÃO vê é o silêncio verde de novo."""
    import time
    with patch.object(J, '_frota_viva', return_value=_frota_fake(44, 212)), \
         patch('django_rq.get_connection', return_value=MagicMock()), \
         patch.object(J.logger, 'error') as erro, patch.object(J.logger, 'warning'):
        J._alerta_workers_velhos(time.time())
    assert erro.called, '82,8% sem avaliação passou sem ERROR'
    # o alarme dispara DOIS erros (cobertura e workers velhos); o de cobertura
    # é o que precisa dizer o tamanho da cegueira.
    msgs = [c.args[0] % c.args[1:] for c in erro.call_args_list]
    cobertura = [m for m in msgs if 'birth_date' in m]
    assert cobertura, 'não houve ERROR sobre a cobertura do alarme'
    assert '212' in cobertura[0] and '256' in cobertura[0], (
        'não disse quantos ficaram de fora')


def test_frota_toda_avaliavel_nao_alarma_cobertura():
    import time
    with patch.object(J, '_frota_viva', return_value=_frota_fake(0, 0)), \
         patch('django_rq.get_connection', return_value=MagicMock()), \
         patch.object(J.logger, 'error') as erro, patch.object(J.logger, 'warning') as av:
        J._alerta_workers_velhos(time.time())
    assert not erro.called and not av.called
