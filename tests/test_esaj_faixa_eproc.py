"""TJSP tem um SEGUNDO sistema, e o e-SAJ não sabe dele.

Achado de 25/08/2026. O TJSP roda **eproc** em paralelo ao e-SAJ. Os processos
nascidos no eproc recebem sequencial de CNJ começando em `4`, e o `link` da
própria publicação DJEN denuncia o sistema (`eproc1g.tjsp.jus.br` /
`eproc2g.tjsp.jus.br` contra `www.dje.tjsp.jus.br` dos demais).

Sonda ao vivo, 3 s entre requisições, amostra de semente 20260825:
**16 de 16** CNJ de prefixo 4 dos anos 2025 e 2026 devolveram a MESMA página
determinística de 70.439 bytes, "Não existem informações disponíveis". Não é
intermitência, não é WAF, não é o parser.

Tamanho: prefixo 4 é **18,0%** do TJSP (2.781 de 15.443 linhas amostradas)
≈ **2.940.182 processos**, com **0,1% de `ok`** contra 14,5% do prefixo 1.
A fronteira do refill estava dentro dessa faixa: **10.750 `nao_encontrado`/h,
0,6% de `ok`**, e cada job gastava até `MAX_PROXY_ROTATIONS` IPs do pool que é
**compartilhado com todos os tribunais**.

Duas coisas que este arquivo tranca:

1. O corte é `prefixo 4` **E** `ano >= 2025`. Medido na mesma janela: prefixo 4
   de 2013 ESTÁ no e-SAJ e devolve `ok` (33 de 33 em 45 min). Generalizar o
   prefixo apagaria processo bom.
2. A recusa é CONTADA, nunca muda (regra nº 2 do CLAUDE.md). Recorte que não se
   anuncia é o `for pagina in range(1, 11)` de novo — 43,6% do TJSP perdidos
   por 17 meses atrás de um `return` discreto.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from enrichers.esaj import TjacEnricher, TjalEnricher, TjspEnricher

# --------------------- 1. O predicado da faixa ---------------------

@pytest.mark.parametrize('cnj,motivo', [
    # os 16 estratos sondados ao vivo — todos "Não existem informações"
    ('40037684020268260005', 'eproc'),
    ('40257234220268260001', 'eproc'),
    ('40006557020258260407', 'eproc'),
    ('40356635920258260100', 'eproc'),
    # 2º grau do eproc (foro 0000) entra pela mesma regra
    ('40134559020258260000', 'eproc'),
    # formatado também
    ('4002010-12.2026.8.26.0624', 'eproc'),
])
def test_faixa_eproc_e_recusada(cnj, motivo):
    assert TjspEnricher.fora_do_esaj(cnj) == motivo


@pytest.mark.parametrize('cnj', [
    # CONTROLE NEGATIVO 1 — prefixo 4 ANTIGO está no e-SAJ e dá `ok`
    # (33 de 33 numa janela de 45 min em produção). Cortar por prefixo
    # sozinho apagaria estes.
    '40040488720138260224',
    '4004126-08.2013.8.26.0604',
    '40041708020138260510',
    # CONTROLE NEGATIVO 2 — as faixas que o e-SAJ SERVE
    '10542715120248260114',   # prefixo 1, detalhe completo ao vivo
    '00039316220258260704',   # prefixo 0, detalhe completo ao vivo
    '23273870920258260000',   # prefixo 2, 2º grau, detalhe ao vivo
    # CONTROLE NEGATIVO 3 — lixo não vira recusa
    '',
    'nao-e-um-cnj',
    '123',
])
def test_fora_da_faixa_nao_e_recusado(cnj):
    assert TjspEnricher.fora_do_esaj(cnj) is None


def test_fronteira_do_ano_e_exatamente_2025():
    """2024 ainda é e-SAJ; 2025 é eproc. A fronteira é medida, não arredondada."""
    assert TjspEnricher.fora_do_esaj('40000010120248260100') is None
    assert TjspEnricher.fora_do_esaj('40000010120258260100') == 'eproc'


def test_a_faixa_do_tjsp_nao_vaza_para_os_vizinhos():
    """Cada tribunal tem o SEU prefixo e o SEU ano de migração — o do TJSP não
    se aplica por analogia. (TJAL e TJAC ganharam faixa própria em 29/08/2026,
    prefixo 5; ver `tests/test_faixa_fora_da_fonte.py`.)"""
    cnj = '40037684020268260005'          # prefixo 4 = a faixa do TJSP
    assert TjalEnricher.fora_do_esaj(cnj) is None
    assert TjacEnricher.fora_do_esaj(cnj) is None


# --------------------- 2. O efeito no enricher ---------------------

def _enricher() -> TjspEnricher:
    return TjspEnricher(pool=MagicMock())


def test_faixa_eproc_nao_gasta_nenhuma_requisicao_nem_ip():
    """O ponto inteiro: zero requisição, zero IP do pool COMPARTILHADO."""
    proc = SimpleNamespace(pk=1, tribunal_id='TJSP',
                           numero_cnj='40037684020268260005')
    e = _enricher()
    publicados: list[dict] = []
    with patch.object(e.session, 'get',
                      side_effect=AssertionError('não pode tocar a rede')), \
         patch('enrichers.esaj.stream.publish',
               side_effect=lambda p, redis_client=None: publicados.append(p)), \
         patch('enrichers.jobs.registrar_fora_do_esaj') as contador:
        resultado = e.enriquecer(proc)

    assert resultado['status'] == 'nao_encontrado'
    assert resultado['fora_do_esaj'] == 'eproc'
    assert resultado['requisicoes'] == 0
    assert e.pool.get.call_count == 0, 'não pode consumir IP do pool'
    assert publicados and publicados[0]['status'] == 'nao_encontrado'
    contador.assert_called_once_with('TJSP', 'eproc')


def test_recusa_e_contada_e_nao_e_corte_mudo():
    """Regra nº 2: teto é alerta. Sem contador, 2,94 M de processos sairiam de
    `pendente` sem ninguém nunca ver por quê."""
    from enrichers import jobs

    conn = MagicMock()
    conn.hgetall.return_value = {b'TJSP|eproc': b'2940182'}
    with patch('django_rq.get_connection', return_value=conn):
        jobs.registrar_fora_do_esaj('TJSP', 'eproc')
        conn.hincrby.assert_called_once_with(jobs._FORA_DO_ESAJ_KEY, 'TJSP|eproc', 1)
        assert jobs.censo_fora_do_esaj() == {'TJSP|eproc': 2940182}


def test_contador_quebrado_nao_derruba_o_job():
    """Best-effort: telemetria não pode custar o trabalho."""
    from enrichers import jobs
    with patch('django_rq.get_connection', side_effect=RuntimeError('redis fora')):
        jobs.registrar_fora_do_esaj('TJSP', 'eproc')   # não levanta
        assert jobs.censo_fora_do_esaj() == {}


def test_alerta_sai_em_error_e_so_quando_cresce():
    """O ERROR carrega o número REAL por tribunal — e não vira carimbo de 30
    linhas/h depois que a faixa drenar."""
    from enrichers import jobs

    jobs._ultimo_censo_fora.clear()
    with patch('enrichers.jobs.censo_fora_do_esaj',
               return_value={'TJSP|eproc': 1000}), \
         patch('enrichers.jobs.logger') as log:
        jobs._alertar_fora_da_fonte()
        assert log.error.call_count == 1
        assert 'TJSP' in log.error.call_args[0]
        log.error.reset_mock()
        jobs._alertar_fora_da_fonte()          # mesmo número → silêncio
        assert log.error.call_count == 0
    with patch('enrichers.jobs.censo_fora_do_esaj',
               return_value={'TJSP|eproc': 1500}), \
         patch('enrichers.jobs.logger') as log:
        jobs._alertar_fora_da_fonte()          # cresceu → volta a avisar
        assert log.error.call_count == 1
    jobs._ultimo_censo_fora.clear()
