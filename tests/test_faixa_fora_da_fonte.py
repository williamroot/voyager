"""Um tribunal, mais de um sistema — e o enricher pergunta ao sistema errado.

Este arquivo tranca o resultado da varredura de 29/08/2026 (pendência #99):
**quantos tribunais migraram de sistema sem a gente notar**. O achado anterior
(25/08) tinha coberto 24 dos 60 tribunais e cortado só o TJSP; a varredura
completa achou a mesma fatia sem porta em mais quatro — TJMG, TJRJ, TJAC e
TJAL — e o corte deles vive aqui junto da evidência.

Três provas por faixa, e a terceira é a que manda (ver `enrichers/faixas.py`):
sistema pelo host do `link` do DJEN, estado pelo `enriquecimento_status` em
amostra de página aleatória, e **sonda ao vivo com CONTROLE NEGATIVO**.

O que este arquivo protege, em ordem de importância:

1. o corte é `prefixo` **E** `ano >= N` — nunca o prefixo sozinho (no TJMG,
   prefixo 1 de 2015-2021 está no PJe: 7 de 16 ao vivo);
2. tribunal **não medido** não recusa nada (abster > chutar);
3. a faixa **não gasta requisição nem IP** do pool COMPARTILHADO;
4. a recusa é **contada**, nunca corte mudo (regra nº 2 do CLAUDE.md).
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from enrichers.esaj import TjacEnricher, TjalEnricher, TjspEnricher
from enrichers.faixas import faixa_fora_da_fonte
from enrichers.jobs import _ENRICHERS, _hook_fora_da_fonte
from enrichers.tjmg import TjmgEnricher
from enrichers.tjrj import TjrjEnricher

# --------------------- 1. As faixas medidas ---------------------
#
# `(enricher, cnj, motivo)` — CNJ reais das sondas ao vivo, todos com
# "não existe" na fonte do tribunal.

@pytest.mark.parametrize('cls,cnj', [
    # TJMG: 16 de 16 "não existe" no pje-consulta-publica.tjmg.jus.br
    (TjmgEnricher, '10002947220268130103'),
    (TjmgEnricher, '10034648920268130317'),
    (TjmgEnricher, '17780131020268130000'),   # 2º grau entra pela mesma regra
    (TjmgEnricher, '11013148620258130024'),
    (TjmgEnricher, '1005790-19.2026.8.13.0027'),   # formatado também
    # TJRJ: 16 de 16 em 2025-26 e 16 de 16 em 2024
    (TjrjEnricher, '31262268320268190001'),
    (TjrjEnricher, '30122136220268190004'),
    (TjrjEnricher, '30124045320258190001'),
    (TjrjEnricher, '30014466520248190058'),        # fronteira: 2024 também
    (TjrjEnricher, '3009877-10.2026.8.19.0029'),
    # TJAC: 16 de 16 no esaj.tjac.jus.br
    (TjacEnricher, '50003724920268010000'),
    (TjacEnricher, '50093305520258010001'),
    (TjacEnricher, '5012975-54.2026.8.01.0001'),
    # TJAL: 14 de 14 — a migração no primeiro ano
    (TjalEnricher, '50006537220268020001'),
    (TjalEnricher, '5000099-40.2026.8.02.0001'),
    # TJSP: o corte de 25/08/2026, agora pela mesma tabela
    (TjspEnricher, '40037684020268260005'),
    (TjspEnricher, '4002010-12.2026.8.26.0624'),
])
def test_faixa_medida_e_recusada(cls, cnj):
    assert cls.fora_da_fonte(cnj) == 'eproc'


@pytest.mark.parametrize('cls,cnj', [
    # CONTROLE NEGATIVO 1 — o prefixo sozinho NÃO é a regra. Estes têm o
    # prefixo da faixa mas ano anterior à migração, e a fonte OS TEM.
    (TjmgEnricher, '11003677120218130024'),   # pref 1, 2021: 7 de 16 achou ao vivo
    (TjmgEnricher, '12029261920158130024'),   # pref 1, 2015
    (TjspEnricher, '40040488720138260224'),   # pref 4, 2013: 33 de 33 `ok`
    (TjalEnricher, '50000994020258020001'),   # pref 5, 2025: TJAL só migra em 2026
    # CONTROLE NEGATIVO 2 — as faixas que cada fonte SERVE
    (TjmgEnricher, '50007180720268130116'),   # TJMG pref 5: 15 de 16 achou
    (TjrjEnricher, '08810807120258190001'),   # TJRJ pref 0: 13 de 16 achou
    (TjacEnricher, '07035324720258010070'),   # TJAC pref 0: 16 de 16 com cadastro
    (TjspEnricher, '10542715120248260114'),
    # CONTROLE NEGATIVO 3 — lixo não vira recusa
    (TjmgEnricher, ''),
    (TjrjEnricher, 'nao-e-um-cnj'),
    (TjacEnricher, '123'),
    (TjspEnricher, None),
])
def test_fora_da_faixa_nao_e_recusado(cls, cnj):
    assert cls.fora_da_fonte(cnj) is None


def _cnj(prefixo: str, ano: int, tr: str = '13', foro: str = '0100') -> str:
    """NNNNNNN-DD.AAAA.J.TR.OOOO sem pontuação — 20 dígitos, sempre."""
    numero = f'{prefixo}000010' + '12' + f'{ano}' + '8' + tr + foro
    assert len(numero) == 20, numero
    return numero


@pytest.mark.parametrize('cls,ano_de_corte,tr', [
    (TjmgEnricher, 2025, '13'),
    (TjrjEnricher, 2024, '19'),
    (TjacEnricher, 2025, '01'),
    (TjalEnricher, 2026, '02'),
    (TjspEnricher, 2025, '26'),
])
def test_a_fronteira_do_ano_e_medida_nao_arredondada(cls, ano_de_corte, tr):
    """Cada tribunal migrou num ano diferente. Um ano global seria chute."""
    prefixo = cls.FORA_DA_FONTE_FAIXAS[0][0]
    assert cls.fora_da_fonte(_cnj(prefixo, ano_de_corte - 1, tr)) is None
    assert cls.fora_da_fonte(_cnj(prefixo, ano_de_corte, tr)) == 'eproc'
    assert cls.FORA_DA_FONTE_FAIXAS[0][1] == ano_de_corte


def test_tribunal_nao_medido_nao_recusa_nada():
    """Abster > chutar (regra nº 6). A varredura de 29/08/2026 viu `eproc` no
    `link` de TJMS, TJPR, TJSE, TJRS, TJSC, TJTO e TRF2/4/6 — e NENHUM deles
    tem enricher, então não há requisição a poupar. Onde há enricher e a
    separação por CNJ não é limpa (TJCE, TJAP), a resposta é não recusar."""
    outros = [cls for sig, cls in _ENRICHERS.items()
              if sig not in {'TJSP', 'TJMG', 'TJRJ', 'TJAC', 'TJAL'}]
    assert outros, 'o registro de enrichers não pode estar vazio'
    for cls in outros:
        assert getattr(cls, 'FORA_DA_FONTE_FAIXAS', ()) == (), cls.__name__
        hook = getattr(cls, 'fora_da_fonte', None)
        if hook:
            assert hook('10002947220268130103') is None


def test_predicado_e_puro_e_nao_aceita_cnj_torto():
    faixas = (('1', 2025, 'eproc'),)
    assert faixa_fora_da_fonte(_cnj('1', 2025), faixas) == 'eproc'
    assert faixa_fora_da_fonte(_cnj('1', 2025)[:16], faixas) is None    # curto
    assert faixa_fora_da_fonte('x' * 20, faixas) is None
    assert faixa_fora_da_fonte(_cnj('1', 2025), ()) is None


# --------------------- 2. O efeito no enricher PJe ---------------------

def test_faixa_no_pje_nao_gasta_requisicao_nem_ip():
    """O ponto inteiro: zero requisição, zero IP do pool COMPARTILHADO.

    O guard do e-SAJ já existia desde 25/08; o do PJe é novo, e sem ele os
    ≈ 1,9 M processos de TJMG+TJRJ seguiriam perguntando ao PJe errado.
    """
    proc = SimpleNamespace(pk=7, tribunal_id='TJMG',
                           numero_cnj='1000294-72.2026.8.13.0103')
    e = TjmgEnricher(pool=MagicMock())
    publicados: list[dict] = []
    with patch.object(e.session, 'get',
                      side_effect=AssertionError('não pode tocar a rede')), \
         patch.object(e.session, 'post',
                      side_effect=AssertionError('não pode tocar a rede')), \
         patch('enrichers.pje.stream.publish',
               side_effect=lambda p, redis_client=None: publicados.append(p)), \
         patch('enrichers.jobs.registrar_fora_do_esaj') as contador:
        resultado = e.enriquecer(proc)

    assert resultado['status'] == 'nao_encontrado'
    assert resultado['fora_do_esaj'] == 'eproc'
    assert resultado['requisicoes'] == 0
    assert e.pool.get.call_count == 0, 'não pode consumir IP do pool'
    assert publicados and publicados[0]['status'] == 'nao_encontrado'
    contador.assert_called_once_with('TJMG', 'eproc')


def test_pje_fora_da_faixa_segue_perguntando_a_fonte():
    """CONTROLE NEGATIVO do guard: quem está fora da faixa não pode ser barrado."""
    proc = SimpleNamespace(pk=8, tribunal_id='TJMG',
                           numero_cnj='5000718-07.2026.8.13.0116')
    e = TjmgEnricher(pool=MagicMock())
    with patch.object(TjmgEnricher, '_buscar_processo', return_value=None) as busca, \
         patch('enrichers.pje.stream.publish'):
        resultado = e.enriquecer(proc)
    assert busca.call_count == 1
    assert 'fora_do_esaj' not in resultado


# --------------------- 3. O hook resolvido pelo refill ---------------------

@pytest.mark.parametrize('sigla,cnj', [
    ('TJMG', '10002947220268130103'),
    ('TJRJ', '31262268320268190001'),
    ('TJAC', '50003724920268010000'),
    ('TJAL', '50006537220268020001'),
    ('TJSP', '40037684020268260005'),
])
def test_refill_enxerga_a_faixa_dos_cinco_tribunais(sigla, cnj):
    """O fechamento em LOTE (`_separar_fora_da_fonte`) resolve o hook por
    `getattr`. Se o nome não bater, a faixa some sem erro nenhum — e o refill
    volta a queimar requisição em silêncio."""
    hook = _hook_fora_da_fonte(sigla)
    assert hook is not None, f'{sigla} perdeu o hook'
    assert hook(cnj) == 'eproc'


def test_hook_aceita_o_nome_historico():
    """`fora_do_esaj` continua resolvendo: o runbook e o management command
    ainda o citam, e enricher de terceiro pode tê-lo implementado."""
    class Legado:
        @classmethod
        def fora_do_esaj(cls, cnj):
            return 'motivo-legado'

    with patch.dict(_ENRICHERS, {'TJX': Legado}, clear=False):
        assert _hook_fora_da_fonte('TJX')('qualquer') == 'motivo-legado'
    assert _hook_fora_da_fonte('NAO_EXISTE') is None


# ---------------------------------------------------------------------------
# O job que JÁ ESTAVA NA FILA quando a faixa foi medida.
#
# O filtro de faixa nasceu no ENFILEIRAMENTO (`_separar_fora_da_fonte` e o
# refill do legado). Isso não alcança o job que entrou na fila ANTES da
# medição. Medido em 29/08/2026, minutos depois do deploy das faixas: 99,8%
# dos 11.392 jobs enfileirados do TJMG e 6,8% dos 11.171 do TJRJ já eram da
# faixa morta — e cada um iria gravar `nao_encontrado`, que é uma afirmação
# SOBRE A FONTE, para um processo que perguntamos ao sistema errado.
# ---------------------------------------------------------------------------

def _processo_falso(sigla: str, cnj: str):
    return SimpleNamespace(pk=1, tribunal_id=sigla, numero_cnj=cnj,
                           tribunal=SimpleNamespace(sigla=sigla))


@pytest.mark.parametrize('sigla,cnj_morto,cnj_vivo', [
    # (faixa medida,                        controle negativo da MESMA fonte)
    ('TJMG', '10000100120268130100', '10000100120218130100'),   # pref 1: 2026 x / 2021 ok
    ('TJRJ', '30000100120268190100', '30000100120238190100'),   # pref 3: 2026 x / 2023 ok
    ('TJSP', '40000100120268260100', '40000100120138260100'),   # pref 4: 2026 x / 2013 ok
])
def test_job_da_fila_antiga_na_faixa_morta_nao_consulta_a_fonte(sigla, cnj_morto, cnj_vivo):
    """Recusa ANTES de instanciar o enricher — e sem tocar no status.

    O controle negativo é o que dá sentido ao teste: se o CNJ vivo também
    fosse recusado, o gate estaria apagando processo bom em vez de poupar
    requisição. Por isso o dublê **herda do enricher real** — só a coleta é
    substituída. Um `MagicMock` no lugar da classe faria `fora_da_fonte`
    devolver um mock (verdadeiro) e o teste passaria recusando tudo, provando
    o contrário do que se propõe.
    """
    from enrichers import jobs

    real = _ENRICHERS[sigla]
    coletados: list = []

    class DubleEnricher(real):                    # noqa: D401 — dublê de teste
        def __init__(self, *a, **k):
            pass

        def enriquecer(self, processo, **kw):
            coletados.append(processo.numero_cnj)
            return {'status': 'ok'}

    for cnj, deve_recusar in ((cnj_morto, True), (cnj_vivo, False)):
        p = _processo_falso(sigla, cnj)
        with patch.object(jobs.Process.objects, 'select_related') as sel, \
             patch.dict(jobs._ENRICHERS, {sigla: DubleEnricher}), \
             patch.object(jobs, 'enrich_pausado', return_value=False), \
             patch.object(jobs, 'registrar_fora_do_esaj') as contador:
            sel.return_value.get.return_value = p
            res = jobs.enriquecer_processo(1)

        if deve_recusar:
            assert res['skip'] == 'fora_da_fonte', f'{sigla} {cnj} devia recusar'
            assert res['motivo'] == 'eproc'
            # a recusa e CONTADA (regra no 2), nunca corte mudo
            contador.assert_called_once_with(sigla, 'eproc')
            # e NENHUM status e escrito: `nao_encontrado` seria confianca falsa
            assert 'status' not in res
            # a fonte nao foi consultada
            assert coletados == []
        else:
            assert res.get('skip') != 'fora_da_fonte', f'{sigla} {cnj} NAO devia recusar'
            assert coletados == [cnj_vivo], 'o CNJ vivo tem que chegar na fonte'
            contador.assert_not_called()
