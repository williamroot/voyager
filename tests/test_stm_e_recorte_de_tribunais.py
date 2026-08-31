"""O recorte não pode medir o próprio buraco (#107) — e o CNJ do número manda (#108).

Duas famílias de defeito, a mesma causa:

1. **O que não está na tabela `Tribunal` não aparece como buraco.** Todo gate
   de completude percorre `Tribunal.objects`. O STM respondia 27.055 documentos
   no `api_publica_stm` e nós tínhamos zero — e nenhum alarme cobrava, porque
   ele não existia na tabela. Falta que não é medida não é falta: é nada.
2. **`ativo` é a chave da ingestão DJEN, não da varredura do Datajud.** O tick
   incremental filtrava por `ativo=True`, então um tribunal varrido com o DJEN
   desligado teria o esqueleto congelado sem ninguém acusando.

E o `sigla_do_cnj`, que é a régua do #108: o J=9 (Justiça Militar Estadual)
caía na tabela do J=8 e respondia `TJMG` para um processo do `TJMMG` — sigla
errada com cara de certa, no mesmo estado, com uma letra de diferença.
"""
import pytest

from tribunals.cnj import sigla_do_cnj
from tribunals.models import Tribunal


# -- #107: o STM existe, e o gate o alcança ------------------------------- #

@pytest.mark.django_db
def test_stm_esta_na_tabela():
    """Sem isto, `datajud_conferir_acervo` nunca lista o STM — nem como 0%."""
    t = Tribunal.objects.filter(sigla='STM').first()
    assert t is not None, 'STM ausente: o gate não tem como acusá-lo'
    assert t.nome == 'Superior Tribunal Militar'
    assert t.sigla_djen == 'STM'
    # `ativo` governa a ingestão DJEN (cron diário + tick de backfill). Ligar
    # isso é decisão de volume e não é o que esta pendência pede.
    assert t.ativo is False


@pytest.mark.django_db
def test_gate_percorre_todos_os_tribunais_inclusive_os_inativos():
    """O recorte do gate é a tabela INTEIRA — `ativo` não pode filtrar aqui.

    Espelha a consulta de `datajud_conferir_acervo.handle`.
    """
    siglas = set(Tribunal.objects.order_by('sigla').values_list('sigla', flat=True))
    assert 'STM' in siglas
    assert 'STF' in siglas          # inativo desde a 0041 e ainda assim medido


# -- #107: a varredura incremental não pode depender de `ativo` ----------- #

@pytest.mark.django_db
def test_incremental_alcanca_tribunal_varrido_mesmo_inativo(monkeypatch):
    from datajud import jobs

    Tribunal.objects.filter(sigla='STM').update(datajud_varredura_cursor=1)

    class FilaFake:
        def __init__(self):
            self.siglas = []

        def enqueue(self, _fn, sigla, **kw):
            self.siglas.append(sigla)

    fila = FilaFake()
    monkeypatch.setattr(jobs.django_rq, 'get_queue', lambda _n: fila)
    monkeypatch.setattr(jobs, 'varredura_parada', lambda: False)
    monkeypatch.setattr(jobs, 'varredura_pausados', lambda: set())

    r = jobs.tick_varredura_incremental()
    assert 'STM' in r['enfileirados'], (
        'tribunal varrido (tem cursor) ficou de fora do incremental por causa '
        'do `ativo`, que é a chave de OUTRA porta'
    )


@pytest.mark.django_db
def test_incremental_ignora_quem_nunca_foi_varrido(monkeypatch):
    """Contrapeso do teste acima: sem cursor, não entra — rodar o incremental
    antes da varredura completa jogaria o cursor pro presente e o histórico
    nunca mais seria visitado."""
    from datajud import jobs

    Tribunal.objects.filter(sigla='STM').update(datajud_varredura_cursor=None)

    class FilaFake:
        def __init__(self):
            self.siglas = []

        def enqueue(self, _fn, sigla, **kw):
            self.siglas.append(sigla)

    fila = FilaFake()
    monkeypatch.setattr(jobs.django_rq, 'get_queue', lambda _n: fila)
    monkeypatch.setattr(jobs, 'varredura_parada', lambda: False)
    monkeypatch.setattr(jobs, 'varredura_pausados', lambda: set())

    assert 'STM' not in jobs.tick_varredura_incremental()['enfileirados']


# -- #108: a sigla que o próprio número diz ------------------------------- #

def test_stm_derivado_do_numero():
    """J=7 é Justiça Militar da União — o TR é 00 e o segmento basta."""
    assert sigla_do_cnj('0000123-45.2024.7.00.0000') == 'STM'


@pytest.mark.parametrize('cnj,esperado', [
    ('0000123-45.2024.9.13.0001', 'TJMMG'),   # NÃO TJMG
    ('0000123-45.2024.9.21.0001', 'TJMRS'),   # NÃO TJRS
    ('0000123-45.2024.9.26.0001', 'TJMSP'),   # NÃO TJSP
])
def test_militar_estadual_nao_vira_o_tj_comum(cnj, esperado):
    """A colisão é de UMA letra e no MESMO estado: era o defeito perfeito —
    plausível na tela, errado no dado. Os três índices existem no Datajud
    (`api_publica_tjm{mg,rs,sp}`), conferidos ao vivo em 31/08/2026."""
    assert sigla_do_cnj(cnj) == esperado


def test_militar_estadual_em_uf_sem_tjm_abstem():
    """Só MG, RS e SP têm TJM próprio. J=9 em qualquer outra UF é número
    inconsistente — abster > devolver o TJ comum, que seria inventar."""
    assert sigla_do_cnj('0000123-45.2024.9.05.0001') is None   # BA não tem TJM


def test_estadual_comum_segue_intacto():
    """Controle: o conserto do J=9 não pode ter mexido no J=8."""
    assert sigla_do_cnj('0000123-45.2024.8.13.0001') == 'TJMG'
    assert sigla_do_cnj('0000123-45.2024.8.21.0001') == 'TJRS'
    assert sigla_do_cnj('0000123-45.2024.8.26.0001') == 'TJSP'
    assert sigla_do_cnj('0000123-45.2024.8.07.0001') == 'TJDFT'


# -- #108: o comando que reproduz a tabela -------------------------------- #

class _ESFake:
    """ES de mentira que devolve os buckets de `jtr` que a agregação daria."""

    def __init__(self, por_tribunal, sum_other=0):
        self.por_tribunal = por_tribunal
        self.sum_other = sum_other

    def options(self, **kw):
        return self

    def search(self, **kw):
        sigla = kw['query']['term']['tribunal']
        jtr = self.por_tribunal[sigla]
        return {
            'hits': {'total': {'value': sum(jtr.values())}},
            'aggregations': {'jtr': {
                'sum_other_doc_count': self.sum_other,
                'buckets': [{'key': k, 'doc_count': v} for k, v in jtr.items()],
            }},
        }


def _medir(es, sigla):
    from tribunals.management.commands.conferir_siglas_cnj import Command
    return Command()._medir(es, 'voyager-processos', sigla)


def test_tabela_separa_bate_de_diverge_e_diz_para_onde():
    """O número do CNJ é o juiz: 8.07 é TJDFT, 8.26 é TJSP."""
    es = _ESFake({'TJDFT': {'807': 1_506_100, '826': 69}})
    L = _medir(es, 'TJDFT')
    assert L['docs'] == 1_506_169
    assert L['bate'] == 1_506_100
    assert L['diverge'] == 69
    assert L['destinos'] == {'TJSP': 69}
    assert L['controle_quebrados'] == 0 and L['controle_sum_other'] == 0


def test_tribunal_de_sobreposicao_e_marcado():
    """O TST não tem número nativo: 100% de divergência nele é a Resolução
    65/2008, não defeito. Sem a marca, a linha seria lida como incidente."""
    es = _ESFake({'TST': {'502': 900, '515': 100}})
    L = _medir(es, 'TST')
    assert L['diverge'] == 1000 and L['bate'] == 0
    assert L['sobreposicao'] is True
    assert L['destinos'] == {'TRT2': 900, 'TRT15': 100}


def test_controle_acusa_documento_quebrado_em_vez_de_engolir():
    """Doc sem `proc_digits` ou com comprimento errado não pode virar
    'divergência' nem sumir: ele é CONTROLE, e controle diferente de zero
    invalida a régua inteira."""
    es = _ESFake({'TJSP': {'826': 500, 'MISSING': 3, 'LEN_18': 2}}, sum_other=7)
    L = _medir(es, 'TJSP')
    assert L['controle_quebrados'] == 5
    assert L['controle_sum_other'] == 7
    assert L['destinos'] == {'MISSING': 3, 'LEN_18': 2}

    from tribunals.management.commands.conferir_siglas_cnj import Command
    assert Command()._controle([L])['ok'] is False


def test_jtr_de_segmento_inexistente_nao_vira_tribunal():
    """`003` não é segmento nenhum. Abster e MOSTRAR (`?003`) é o certo —
    escolher um tribunal aqui seria inventar origem para dado sujo."""
    es = _ESFake({'TJMT': {'811': 100, '003': 1}})
    assert _medir(es, 'TJMT')['destinos'] == {'?003': 1}
