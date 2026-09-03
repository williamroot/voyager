"""O backfill: da publicação para `Magistrado` + `MagistradoAtuacao`.

O que estes testes protegem, em ordem de gravidade:

1. **homônimo não funde** — a mesma grafia em dois tribunais são DUAS pessoas;
2. **idempotência** — rodar de novo não duplica nem infla contagem;
3. **abstenção continua abstenção** — publicação sem nome não cria linha;
4. **a contagem só existe depois de contar** — `n_publicacoes` nasce `NULL`.
"""
import datetime as dt

import pytest
from django.core.management import call_command

from tribunals.models import (
    Magistrado,
    MagistradoAtuacao,
    Movimentacao,
    Process,
    Tribunal,
)

pytestmark = pytest.mark.django_db

ASSINA = ('Intime-se. RAFAELA CALDEIRA GONÇALVES Juíza de Direito. - '
          'ADV: DANILO ANSELMO ZERBATO (OAB 439767/SP)')
ASSINA_OUTRA_CAIXA = ('Cumpra-se. Rafaela Caldeira Gonçalves Juíza de Direito. '
                      '- ADV: OZIAS DE SOUZA MENDES (OAB 320050/SP)')
SEM_NOME = 'codigoNoticia=112920 - Magistrado(a)  - Advs: Camila Costa (OAB: 1/RS)'
SO_CITACAO = ('improvido. (STJ - AgRg no AREsp: 1683006 SC, Relator: Ministro '
              'NEFI CORDEIRO, Data de Julgamento: 04/08/2020)')


def _mov(sigla, cnj, ext, texto, orgao, dia='2026-01-30'):
    trib, _ = Tribunal.objects.get_or_create(
        sigla=sigla, defaults={'nome': sigla, 'sigla_djen': sigla})
    proc, _ = Process.objects.get_or_create(numero_cnj=cnj, tribunal=trib)
    return Movimentacao.objects.create(
        processo=proc, tribunal=trib, external_id=ext, texto=texto,
        nome_orgao=orgao, data_disponibilizacao=f'{dia}T00:00:00Z')


def _rodar():
    call_command('backfill_magistrados', de=0, ate=10 ** 12, verbosity=0)


# --------------------------------------------------------------------------- #
def test_grava_a_pessoa_e_a_prova():
    m = _mov('TJSP', '0000001-11.2026.8.26.0001', 'a1', ASSINA,
             'Foro Regional XV - Butantã - Vara Reg.Oeste de Viol. Dom.')
    _rodar()

    (mag,) = Magistrado.objects.all()
    assert mag.nome == 'RAFAELA CALDEIRA GONÇALVES'
    assert mag.nome_chave == 'RAFAELA CALDEIRA GONCALVES'
    assert mag.tribunal_id == 'TJSP'
    assert mag.cargo == 'Juíza de Direito'
    assert mag.fonte == Magistrado.FONTE_TEXTO

    (atu,) = MagistradoAtuacao.objects.all()
    assert atu.magistrado_id == mag.id
    assert atu.movimentacao_id == m.id          # a PROVA aponta a publicação
    assert atu.processo_id == m.processo_id
    assert atu.publicado_em == dt.date(2026, 1, 30)


def test_grafias_diferentes_no_MESMO_orgao_sao_a_mesma_pessoa():
    _mov('TJSP', '0000001-11.2026.8.26.0001', 'a1', ASSINA, 'Vara X')
    _mov('TJSP', '0000002-11.2026.8.26.0001', 'a2', ASSINA_OUTRA_CAIXA, 'Vara X')
    _rodar()
    assert Magistrado.objects.count() == 1
    assert MagistradoAtuacao.objects.count() == 2


def test_O_MESMO_NOME_EM_DOIS_TRIBUNAIS_SAO_DUAS_PESSOAS():
    """Medido em 03/09/2026: 56 de 195 publicações com esta grafia são de
    TJCE/TJRO/TJPE/TJPI/TJMA. Chave só pelo nome funde quatro magistrados
    numa ficha só, e o resultado parece um profissional produtivo."""
    _mov('TJSP', '0000001-11.2026.8.26.0001', 'a1', ASSINA, 'Vara X')
    _mov('TJCE', '0000001-11.2026.8.06.0001', 'b1', ASSINA, 'Vara Y')
    _rodar()
    assert Magistrado.objects.count() == 2
    assert set(Magistrado.objects.values_list('tribunal_id', flat=True)) == \
        {'TJSP', 'TJCE'}


def test_o_MESMO_nome_em_dois_orgaos_do_mesmo_tribunal_sao_DUAS_linhas():
    """Decisão declarada: a chave é (tribunal, órgão, nome). Quem quiser a
    pessoa através dos órgãos agrupa por `(tribunal_id, nome_chave)` — e é uma
    escolha explícita de quem consome, não uma fusão herdada de graça."""
    _mov('TJSP', '0000001-11.2026.8.26.0001', 'a1', ASSINA, 'Vara X')
    _mov('TJSP', '0000002-11.2026.8.26.0001', 'a2', ASSINA, 'Vara Y')
    _rodar()
    assert Magistrado.objects.count() == 2
    assert Magistrado.objects.filter(
        tribunal_id='TJSP', nome_chave='RAFAELA CALDEIRA GONCALVES').count() == 2


def test_rodar_duas_vezes_nao_duplica_nada():
    _mov('TJSP', '0000001-11.2026.8.26.0001', 'a1', ASSINA, 'Vara X')
    _rodar()
    _rodar()
    assert Magistrado.objects.count() == 1
    assert MagistradoAtuacao.objects.count() == 1


@pytest.mark.parametrize('texto', [SEM_NOME, SO_CITACAO, '', 'texto qualquer'])
def test_publicacao_sem_atribuicao_nao_cria_linha(texto):
    _mov('TJSP', '0000009-11.2026.8.26.0001', 'z1', texto, 'Vara X')
    _rodar()
    assert Magistrado.objects.count() == 0
    assert MagistradoAtuacao.objects.count() == 0


def test_dry_run_le_e_nao_grava():
    _mov('TJSP', '0000001-11.2026.8.26.0001', 'a1', ASSINA, 'Vara X')
    call_command('backfill_magistrados', de=0, ate=10 ** 12, dry_run=True,
                 verbosity=0)
    assert Magistrado.objects.count() == 0


def test_a_faixa_de_pk_e_FECHADA_e_exclui_o_ate():
    m1 = _mov('TJSP', '0000001-11.2026.8.26.0001', 'a1', ASSINA, 'Vara X')
    _mov('TJSP', '0000002-11.2026.8.26.0001', 'a2', ASSINA, 'Vara Y')
    call_command('backfill_magistrados', de=m1.id, ate=m1.id + 1, verbosity=0)
    assert MagistradoAtuacao.objects.count() == 1


def test_n_publicacoes_nasce_NULL_e_so_o_modo_contar_o_preenche():
    _mov('TJSP', '0000001-11.2026.8.26.0001', 'a1', ASSINA, 'Vara X')
    _mov('TJSP', '0000002-11.2026.8.26.0001', 'a2', ASSINA_OUTRA_CAIXA,
         'Vara X', dia='2026-03-05')
    _rodar()
    mag = Magistrado.objects.get()
    assert mag.n_publicacoes is None, 'NULL = não contamos, e é o certo aqui'
    assert mag.n_publicacoes_em is None

    call_command('backfill_magistrados', contar=True, verbosity=0)
    mag.refresh_from_db()
    assert mag.n_publicacoes == 2
    assert mag.n_publicacoes_em is not None      # contagem sem data envelhece muda
    assert mag.primeira_em == dt.date(2026, 1, 30)
    assert mag.ultima_em == dt.date(2026, 3, 5)


def test_o_teto_e_ALERTA_e_nao_corte_mudo():
    """Regra nº 2 do CLAUDE.md: atingir o teto é ERRO com número, não um
    `return` discreto que parece 'acabou'."""
    for i in range(4):
        _mov('TJSP', f'000000{i}-11.2026.8.26.0001', f'a{i}', ASSINA, 'Vara X')
    with pytest.raises(SystemExit) as saida:
        call_command('backfill_magistrados', de=0, ate=10 ** 12,
                     max_publicacoes=2, verbosity=0)
    assert saida.value.code == 1
