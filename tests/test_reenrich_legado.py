"""O recuperador dos 3,25 M do TJSP nunca rodou — e não falhava.

`tick_reenrich_esaj_legacy` nasceu em 2026-07-06 para devolver a `pendente` os
`nao_encontrado` queimados pelo bug do "200 ambíguo terminal". Ele era guardado
por `pend >= REENRICH_PENDENTE_FLOOR` com FLOOR = **20.000**.

O TJSP tem **12.101.245** processos `pendente`. A condição é aritmeticamente
inalcançável desde a primeira execução: o mecanismo escrito para um tribunal
específico era **no-op por construção justamente para esse tribunal**, e nunca
acendeu luz — mesma família do `for pagina in range(1, 11)`.

O que a sonda ao vivo de 25/08/2026 provou sobre esse estoque (62 requisições,
3 s entre elas, semente 20260825): dos prefixos 0/1 hoje marcados
`nao_encontrado`, **30 de 32 devolveram cadastro completo ou lista AGORA**; e
**83,6%** foram queimados antes do fix (mês do `enriquecido_em`: 05 → 457,
06 → 1.781, 07 → 214, 08 → 226, de 2.678). São falsos-negativos, não ausências.

O floor não foi apagado — virou **teto por lote**, senão trocaríamos uma
armadilha por outra invertida (2,59 M voltando de uma vez é incidente).
"""
from unittest.mock import MagicMock, patch

import pytest

from enrichers import jobs


class _QS:
    """QuerySet de mentira: só o que o tick usa."""

    def __init__(self, linhas):
        self._linhas = linhas
        self.atualizados = []
        self.filtros = []

    def filter(self, **kw):
        self.filtros.append(kw)
        self._ultimo_filtro = kw
        return self

    def values_list(self, *campos, **kw):
        self._campos = campos
        return self

    def __getitem__(self, corte):
        return self._linhas[corte]

    def update(self, **kw):
        self.atualizados.append(kw)
        return len(self._ultimo_filtro.get('pk__in', []))


def _conn(terminados: int = 100_000) -> MagicMock:
    conn = MagicMock()
    conn.hincrby.return_value = 12345
    return conn


def _rodar(linhas, terminados=100_000, por_tick=2_000):
    """Roda o tick só para o TJSP, com o banco e o Redis mockados."""
    qs = _QS(linhas)
    reg = MagicMock()
    reg.__len__ = lambda self: terminados

    with patch.object(jobs, 'REENRICH_ESAJ_TRIBUNAIS', ('TJSP',)), \
         patch.object(jobs, 'REENRICH_LEGACY_POR_TICK', por_tick), \
         patch.object(jobs.Process, 'objects', qs), \
         patch('django_rq.get_connection', return_value=_conn()), \
         patch('rq.registry.FinishedJobRegistry', return_value=reg), \
         patch.object(jobs, 'enrich_pausados', return_value=set()), \
         patch.object(jobs, 'logger') as log:
        relatorio = jobs.tick_reenrich_esaj_legacy()
    return relatorio, qs, log


def _legado(n, prefixo='1', ano=2024, base=1000):
    """n linhas (pk, numero_cnj) fora da faixa eproc, por default."""
    return [(base + i, f'{prefixo}{i:06d}-11.{ano}.8.26.0100') for i in range(n)]


def test_o_tick_roda_mesmo_com_12_milhoes_de_pendentes():
    """A REGRESSÃO PRINCIPAL. Antes, `pend >= 20.000` matava a passada; hoje o
    tick não consulta `pendente` nenhuma vez."""
    relatorio, qs, _ = _rodar(_legado(5_000))
    assert 'skip' not in relatorio['TJSP']
    assert qs.atualizados == [{'enriquecimento_status': 'pendente'}]
    assert 'reset 2.000' in relatorio['TJSP'].replace(',', '.')


def test_teto_por_vazao_limita_quando_a_fila_drena_pouco():
    """Não devolvemos mais do que o tribunal consegue raspar no intervalo.
    Fila com 500 jobs terminados em 500 s = 1 job/s ⇒ 300 no tick de 5 min."""
    relatorio, qs, _ = _rodar(_legado(5_000), terminados=500)
    assert 'reset 300' in relatorio['TJSP']


def test_piso_impede_que_fila_fria_trave_a_recuperacao():
    """Registry vazio (fila parada ou recém-criada) cairia em zero e a
    recuperação nunca começaria — o defeito que estamos consertando."""
    relatorio, _, _ = _rodar(_legado(5_000), terminados=0)
    assert 'reset 2.000' in relatorio['TJSP'].replace(',', '.')


def test_faixa_eproc_nao_volta_para_a_fila():
    """Devolver a faixa que a fonte não tem só a faria dar a volta inteira para
    ser recusada de novo. No TJSP ela é 38,7% do estoque `nao_encontrado`."""
    linhas = (_legado(100, prefixo='4', ano=2026, base=1)      # eproc: recusar
              + _legado(100, prefixo='1', ano=2024, base=500))  # e-SAJ: devolver
    relatorio, qs, _ = _rodar(linhas, por_tick=1_000)
    assert 'fora da fonte 100' in relatorio['TJSP']
    assert 'reset 100' in relatorio['TJSP']


def test_lote_inteiro_fora_da_fonte_nao_faz_update():
    relatorio, qs, _ = _rodar(_legado(50, prefixo='4', ano=2025))
    assert 'todos fora da fonte' in relatorio['TJSP']
    assert qs.atualizados == []


def test_teto_atingido_sai_em_error_com_o_numero_real():
    """Regra nº 2 do CLAUDE.md: teto é alerta, nunca corte mudo. Sem isto, o
    estoque restante ficaria invisível — que é exatamente como 2,59 M ficaram
    parados por 50 dias."""
    _, _, log = _rodar(_legado(5_000), por_tick=1_000)
    assert log.error.call_count == 1
    args = log.error.call_args[0]
    assert 'TJSP' in args
    assert '1.000' in args or '1,000' in args


def test_sem_teto_atingido_nao_alarma():
    """Alerta que sempre toca não é alerta."""
    _, _, log = _rodar(_legado(100), por_tick=2_000)
    assert log.error.call_count == 0


def test_tribunal_pausado_e_pulado():
    """Kill-switch continua valendo: devolver para fila de tribunal pausado
    só engorda `pendente` sem ninguém para raspar."""
    qs = _QS(_legado(100))
    with patch.object(jobs, 'REENRICH_ESAJ_TRIBUNAIS', ('TJSP',)), \
         patch.object(jobs.Process, 'objects', qs), \
         patch('django_rq.get_connection', return_value=_conn()), \
         patch.object(jobs, 'enrich_pausados', return_value={'TJSP'}):
        relatorio = jobs.tick_reenrich_esaj_legacy()
    assert relatorio['TJSP'] == 'pausado'
    assert qs.atualizados == []


def test_sem_legado_restante_e_estado_final_silencioso():
    relatorio, qs, log = _rodar([])
    assert relatorio['TJSP'] == 'sem legado restante'
    assert qs.atualizados == []
    assert log.error.call_count == 0


def test_o_alvo_e_so_o_legado_pre_fix():
    """Sem loop, por construção: quem volta é re-raspado, ganha
    `enriquecido_em` recente e sai do alvo para sempre. Se o filtro do cutoff
    sumir, o tick vira esteira infinita."""
    _, qs, _ = _rodar(_legado(10))
    alvo = qs.filtros[0]
    assert alvo['tribunal_id'] == 'TJSP'
    assert alvo['enriquecimento_status'] == 'nao_encontrado'
    assert alvo['enriquecido_em__lt'] == jobs.REENRICH_LEGACY_CUTOFF
    assert jobs.REENRICH_LEGACY_CUTOFF.year == 2026
    assert (jobs.REENRICH_LEGACY_CUTOFF.month, jobs.REENRICH_LEGACY_CUTOFF.day) == (7, 6)
    # e NENHUMA consulta a `pendente` — era ela que matava a passada
    assert not any(f.get('enriquecimento_status') == 'pendente' for f in qs.filtros)


def test_floor_antigo_nao_existe_mais():
    """Trava contra o retorno do gate inalcançável."""
    assert not hasattr(jobs, 'REENRICH_PENDENTE_FLOOR')


@pytest.mark.parametrize('por_tick,esperado', [(200, 200), (2_000, 2_000)])
def test_passo_da_fase_e_o_governador(por_tick, esperado):
    """Faseado: o passo é o parâmetro que se abre depois de medir vazão e pool
    (a régua boa é 1.926 de 2.500 IPs saudáveis)."""
    relatorio, _, _ = _rodar(_legado(50_000), por_tick=por_tick)
    assert f'reset {esperado:,}'.replace(',', '.') in relatorio['TJSP'].replace(',', '.')
