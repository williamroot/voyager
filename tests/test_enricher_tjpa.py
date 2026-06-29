"""Testes do enricher TJPA (portal próprio "Consulta Unificada" — REST).

Cobre:
  1. Config da classe (BASE_URL, sigla, log name, construtor pool/prefer_cortex).
  2. Helpers de normalização (CNJ, epoch→BR, 'CÓDIGO - Nome' → 'Nome (código)').
  3. Fluxo `enriquecer()` com o HTTP mockado em cima das fixtures JSON reais
     coletadas contra consulta-processual-unificada-prd.tjpa.jus.br
     (data: 2026-06-29). Sem rede e sem DB — `stream.publish` é interceptado e
     `_buscar_processo` devolve a fixture parseada.

Fixtures vivem em `tests/fixtures/tjpa/` (resposta crua do endpoint
`/consilium-rest/processobycnj/{cnj}`).
"""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from enrichers.tjpa import (
    TjpaEnricher,
    TjpaEnricherError,
    _codigo_nome_para_nome_codigo,
    _cnj_digits,
    _ms_para_br,
)

FIXTURES = Path(__file__).parent / 'fixtures' / 'tjpa'


def _load(cnj: str) -> dict:
    """Carrega a fixture crua e devolve o 1º item de listaProcessos (o que
    `_buscar_processo` retornaria)."""
    raw = json.loads((FIXTURES / f'processobycnj_{cnj}.json').read_text())
    return raw['listaProcessos'][0]


def _make_enricher() -> TjpaEnricher:
    # pool mockado evita Redis; _buscar_processo é patchado nos testes de fluxo.
    return TjpaEnricher(pool=MagicMock())


# --------------------------- 1. Config ---------------------------

def test_config_endpoints_e_sigla():
    e = _make_enricher()
    assert e.TRIBUNAL_SIGLA == 'TJPA'
    assert e.BASE_URL == 'https://consulta-processual-unificada-prd.tjpa.jus.br'
    assert e.LOG_NAME == 'voyager.enrichers.tjpa'


def test_construtor_aceita_pool_e_prefer_cortex():
    e = TjpaEnricher(pool=MagicMock(), prefer_cortex=True)
    assert e.prefer_cortex is True


# --------------------------- 2. Helpers ---------------------------

def test_cnj_digits_aceita_formatado_e_digitos():
    assert _cnj_digits('0803159-28.2023.8.14.0136') == '08031592820238140136'
    assert _cnj_digits('08031592820238140136') == '08031592820238140136'


def test_cnj_digits_rejeita_invalido():
    with pytest.raises(TjpaEnricherError):
        _cnj_digits('123')


def test_ms_para_br_converte_epoch():
    # 1694606394150 ms == 13/09/2023 (UTC-3, Pará)
    assert _ms_para_br(1694606394150) == '13/09/2023'
    assert _ms_para_br(1544531979772) == '11/12/2018'
    assert _ms_para_br(None) == ''
    assert _ms_para_br(0) == ''


def test_codigo_nome_para_nome_codigo():
    # TJPA dá 'CÓDIGO - Nome'; drainer espera 'Nome (código)'.
    assert _codigo_nome_para_nome_codigo('39 - Inventário') == 'Inventário (39)'
    assert _codigo_nome_para_nome_codigo('7676 - Administração de herança') == \
        'Administração de herança (7676)'
    # Sem padrão → devolve cru.
    assert _codigo_nome_para_nome_codigo('Procedimento Comum') == 'Procedimento Comum'
    assert _codigo_nome_para_nome_codigo('') == ''


# --------------------------- 3. Fluxo enriquecer() ---------------------------

@pytest.fixture
def processo():
    return SimpleNamespace(
        pk=42, tribunal_id='TJPA',
        numero_cnj='0803159-28.2023.8.14.0136',
    )


def _run(enricher, processo, proc_raw):
    """Roda enriquecer() com _buscar_processo retornando `proc_raw` e captura
    o payload publicado no stream."""
    captured: list[dict] = []

    def fake_publish(payload, redis_client=None):  # noqa: ARG001
        captured.append(payload)
        return '0-0'

    with patch.object(TjpaEnricher, '_buscar_processo', return_value=proc_raw), \
            patch('enrichers.tjpa.stream.publish', side_effect=fake_publish):
        result = enricher.enriquecer(processo)
    return result, captured


def test_enriquecer_ok_extrai_dados(processo):
    proc_raw = _load('0803159-28.2023.8.14.0136')
    result, captured = _run(_make_enricher(), processo, proc_raw)

    assert result['status'] == 'ok'
    assert result['cnj'] == processo.numero_cnj

    assert len(captured) == 1
    payload = captured[0]
    assert payload['v'] == 1
    assert payload['status'] == 'ok'
    assert payload['process_id'] == 42
    assert payload['tribunal'] == 'TJPA'

    dados = payload['dados']
    # classe/assunto reordenados pra 'Nome (código)' (drainer extrai o código).
    assert dados['classe'] == 'Inventário (39)'
    assert dados['assunto'] == 'Administração de herança (7676)'
    # orgao = comarca — vara
    assert 'Canaã dos Carajás' in dados['orgao_julgador']
    assert 'Vara Cível' in dados['orgao_julgador']
    # data_autuacao convertida do epoch ms
    assert dados['data_autuacao'] == '13/09/2023'
    assert dados['valor_causa'] == 'R$ 120.000,00'
    assert dados['segredo_justica'] is False


def test_enriquecer_mapeia_polo_e_papel(processo):
    proc_raw = _load('0803159-28.2023.8.14.0136')
    _, captured = _run(_make_enricher(), processo, proc_raw)
    partes = captured[0]['partes']

    # polo A → ativo (REQUERENTEs); polo P → passivo (INVENTARIADOs);
    # polo T → outros (AUTORIDADE / TERCEIRO INTERESSADO).
    ativo_nomes = [p['nome'] for p in partes['ativo']]
    passivo_nomes = [p['nome'] for p in partes['passivo']]
    outros_nomes = [p['nome'] for p in partes['outros']]

    assert 'LUCAS DOS SANTOS MAMEDIO' in ativo_nomes
    assert 'ADRIANA SANTOS MAMEDIO' in ativo_nomes
    assert 'ESPOLIO DE JOSE ANTONIO DINIZ MAMEDIO' in passivo_nomes
    assert 'MUNICIPIO DE CANAA DOS CARAJAS' in outros_nomes
    assert 'ESTADO DO PARA' in outros_nomes

    # papel = `tipo` da API (preservado cru pra ProcessoParte.papel)
    requerente = next(p for p in partes['ativo'] if p['nome'] == 'ADRIANA SANTOS MAMEDIO')
    assert requerente['papel'] == 'REQUERENTE'
    # tppessoa=F → pf; tppessoa=J → pj (cpfcnpj null na consulta pública)
    assert requerente['tipo'] == 'pf'
    municipio = next(p for p in partes['outros'] if p['nome'] == 'MUNICIPIO DE CANAA DOS CARAJAS')
    assert municipio['tipo'] == 'pj'


def test_enriquecer_advogado_e_representante_como_representantes(processo):
    proc_raw = _load('0803159-28.2023.8.14.0136')
    _, captured = _run(_make_enricher(), processo, proc_raw)
    partes = captured[0]['partes']

    # ESPOLIO DE NILDE (último INVENTARIADO do polo P) recebe o
    # 'REPRESENTANTE DA PARTE' (LUCAS) como representante.
    nilde = next(p for p in partes['passivo']
                 if p['nome'] == 'ESPOLIO DE NILDE MARIA DOS SAMTOS')
    reps = nilde.get('representantes', [])
    assert any(r['nome'] == 'LUCAS DOS SANTOS MAMEDIO'
               and r['papel'] == 'REPRESENTANTE DA PARTE' for r in reps)

    # O total de ProcessoParte (principais + representantes) bate com as 9
    # partes da fixture — nada é perdido no agrupamento.
    total = sum(
        len(polo) + sum(len(p.get('representantes', [])) for p in polo)
        for polo in partes.values()
    )
    assert total == 9


def test_advogado_lider_sem_principal_vira_entrada_solta(processo):
    """No polo A o ADVOGADO (RAPHAEL) é listado ANTES dos requerentes — sem
    principal anterior no polo, vira entrada solta (não é descartado)."""
    proc_raw = _load('0803159-28.2023.8.14.0136')
    _, captured = _run(_make_enricher(), processo, proc_raw)
    partes = captured[0]['partes']

    raphael = next((p for p in partes['ativo']
                    if p['nome'] == 'RAPHAEL TAVARES COUTINHO'), None)
    assert raphael is not None
    assert raphael['papel'] == 'ADVOGADO'
    assert raphael['tipo'] == 'advogado'


def test_enriquecer_segundo_processo_3_partes():
    processo = SimpleNamespace(
        pk=7, tribunal_id='TJPA', numero_cnj='0800796-63.2018.8.14.0065')
    proc_raw = _load('0800796-63.2018.8.14.0065')
    result, captured = _run(_make_enricher(), processo, proc_raw)

    assert result['status'] == 'ok'
    partes = captured[0]['partes']

    # AUTOR (PJ) no ativo, REU (PJ) no passivo, ADVOGADO como representante do autor.
    autor = next(p for p in partes['ativo'] if p['nome'] == 'SORVETERIA CREME MEL S.A')
    assert autor['papel'] == 'AUTOR'
    assert autor['tipo'] == 'pj'
    assert any(r['nome'] == 'MARCOS THIAGO AVILA SILVA'
               for r in autor.get('representantes', []))

    reu = next(p for p in partes['passivo'] if p['nome'] == 'A ALVES DOS SANTOS EIRELI - ME')
    assert reu['papel'] == 'REU'
    assert reu['tipo'] == 'pj'

    total = sum(
        len(polo) + sum(len(p.get('representantes', [])) for p in polo)
        for polo in partes.values()
    )
    assert total == 3

    dados = captured[0]['dados']
    assert dados['classe'] == 'Procedimento Comum Cível (7)'
    assert dados['valor_causa'] == 'R$ 1.400.940,72'
    assert dados['data_autuacao'] == '11/12/2018'


def test_enriquecer_nao_encontrado(processo):
    """API sem resultado (listaProcessos vazia) → _buscar_processo None →
    payload nao_encontrado, sem dados/partes."""
    result, captured = _run(_make_enricher(), processo, None)
    assert result['status'] == 'nao_encontrado'
    assert len(captured) == 1
    assert captured[0]['status'] == 'nao_encontrado'
    assert 'dados' not in captured[0]
    assert 'partes' not in captured[0]


def test_enriquecer_rejeita_tribunal_diferente():
    proc = SimpleNamespace(pk=1, tribunal_id='TRF1', numero_cnj='x')
    with pytest.raises(TjpaEnricherError):
        _make_enricher().enriquecer(proc)
