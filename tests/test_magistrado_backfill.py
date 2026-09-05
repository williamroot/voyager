"""O backfill: da publicação para `Magistrado` + `MagistradoAtuacao`.

O que estes testes protegem, em ordem de gravidade:

1. **homônimo não funde** — a mesma grafia em dois tribunais são DUAS pessoas;
2. **idempotência** — rodar de novo não duplica nem infla contagem;
3. **abstenção continua abstenção** — publicação sem nome não cria linha;
4. **a contagem só existe depois de contar** — `n_publicacoes` nasce `NULL`.
"""
import datetime as dt

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

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


#: O comando GRAVA estado no cache (o zero do orçamento, o cursor do shard, o
#: kill switch). Com o cache de verdade, um teste herdaria o zero do anterior e
#: o orçamento mediria a soma de todos eles — teste que passa por contágio.
CACHE_LOCAL = override_settings(CACHES={'default': {
    'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
    'LOCATION': 'test-magistrado-backfill'}})


def _rodar(**extra):
    """Faixa FECHADA e sem checkpoint — que é o que o teste realmente quer.

    `--sem-checkpoint` é declaração de intenção, não burocracia: sem ele o
    comando recusa faixa aberta na largada, porque um restart sem cursor volta
    ao pk inicial (351 reinícios com progresso zero, medidos em 02/09/2026).
    """
    call_command('backfill_magistrados', de=0, ate=10 ** 12,
                 sem_checkpoint=True, verbosity=0, **extra)


# --------------------------------------------------------------------------- #
@CACHE_LOCAL
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


@CACHE_LOCAL
def test_grafias_diferentes_no_MESMO_orgao_sao_a_mesma_pessoa():
    _mov('TJSP', '0000001-11.2026.8.26.0001', 'a1', ASSINA, 'Vara X')
    _mov('TJSP', '0000002-11.2026.8.26.0001', 'a2', ASSINA_OUTRA_CAIXA, 'Vara X')
    _rodar()
    assert Magistrado.objects.count() == 1
    assert MagistradoAtuacao.objects.count() == 2


@CACHE_LOCAL
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


@CACHE_LOCAL
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


@CACHE_LOCAL
def test_rodar_duas_vezes_nao_duplica_nada():
    _mov('TJSP', '0000001-11.2026.8.26.0001', 'a1', ASSINA, 'Vara X')
    _rodar()
    _rodar()
    assert Magistrado.objects.count() == 1
    assert MagistradoAtuacao.objects.count() == 1


@pytest.mark.parametrize('texto', [SEM_NOME, SO_CITACAO, '', 'texto qualquer'])
@CACHE_LOCAL
def test_publicacao_sem_atribuicao_nao_cria_linha(texto):
    _mov('TJSP', '0000009-11.2026.8.26.0001', 'z1', texto, 'Vara X')
    _rodar()
    assert Magistrado.objects.count() == 0
    assert MagistradoAtuacao.objects.count() == 0


@CACHE_LOCAL
def test_dry_run_le_e_nao_grava():
    _mov('TJSP', '0000001-11.2026.8.26.0001', 'a1', ASSINA, 'Vara X')
    call_command('backfill_magistrados', de=0, ate=10 ** 12, dry_run=True, sem_checkpoint=True,
                 verbosity=0)
    assert Magistrado.objects.count() == 0


@CACHE_LOCAL
def test_a_faixa_de_pk_e_FECHADA_e_exclui_o_ate():
    m1 = _mov('TJSP', '0000001-11.2026.8.26.0001', 'a1', ASSINA, 'Vara X')
    _mov('TJSP', '0000002-11.2026.8.26.0001', 'a2', ASSINA, 'Vara Y')
    call_command('backfill_magistrados', de=m1.id, ate=m1.id + 1, sem_checkpoint=True, verbosity=0)
    assert MagistradoAtuacao.objects.count() == 1


@CACHE_LOCAL
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


@CACHE_LOCAL
def test_o_teto_e_ALERTA_e_nao_corte_mudo():
    """Regra nº 2 do CLAUDE.md: atingir o teto é ERRO com número, não um
    `return` discreto que parece 'acabou'."""
    for i in range(4):
        _mov('TJSP', f'000000{i}-11.2026.8.26.0001', f'a{i}', ASSINA, 'Vara X')
    # `CommandError` é o mesmo `exit 1` do `backfill_partes_djen` visto da
    # linha de comando, e é testável sem `SystemExit` — o que importa é que
    # NÃO sai com 0: era o `exit 0` que tornava teto indistinguível de fim.
    with pytest.raises(CommandError) as saida:
        _rodar(max_publicacoes=2)
    assert 'TETO ATINGIDO' in str(saida.value)


# --------------------------------------------------------------------------- #
# O que o #125 acrescentou: varredura COMPLETA, orçamento em BYTES, controle
# --------------------------------------------------------------------------- #
@CACHE_LOCAL
def test_a_janela_nao_pula_linha_quando_o_lote_enche():
    """O defeito que este teste existe para impedir foi MEDIDO, não imaginado.

    A primeira versão da janela avançava o cursor para o TOPO da faixa de pk
    varrida, e não para depois da última linha lida. Quando a janela continha
    mais publicações do que o `--lote` levava, o resto era pulado **em
    silêncio**: no dev, em 03/09/2026, 33.500 de 147.589 — **22,7%**, com run
    verde, log limpo e um total redondo. É a assinatura das três perdas do
    `CLAUDE.md`.

    Com `--lote 10` e 60 publicações contíguas, a versão defeituosa grava 10.
    """
    for i in range(60):
        _mov('TJSP', f'{i:07d}-11.2026.8.26.0001', f'w{i}', ASSINA, 'Vara X')
    _rodar(lote=10)
    assert MagistradoAtuacao.objects.count() == 60, \
        'a janela pulou linha: o cursor voltou a andar pelo topo da faixa'


@CACHE_LOCAL
def test_o_controle_conta_dos_DOIS_lados(capsys):
    """Regra nº 5: contagem própria não prova varredura — comparar com a fonte
    prova. É este controle que pegou a janela pulando linha."""
    for i in range(5):
        _mov('TJSP', f'{i:07d}-11.2026.8.26.0001', f'c{i}', ASSINA, 'Vara X')
    _rodar(conferir=True)
    assert 'cobertura 100.00%' in capsys.readouterr().out


@CACHE_LOCAL
def test_o_orcamento_de_BYTES_para_no_MEIO_da_faixa(monkeypatch):
    """Teto em BYTES, não em linhas — e bater nele é ERRO (regra nº 2).

    O tamanho da tabela é a única régua que sobra quando o disco livre do host
    do banco não é observável (sem `ssh` no `.101`, sem `pg_read_all_settings`
    — conferido em 03/09/2026).

    O tamanho é FALSIFICADO aqui de propósito. Medir crescimento real de
    arquivo num teste seria medir a alocação de páginas do Postgres — que não
    volta no rollback e já vem suja do teste anterior. O que este teste
    protege é o LAÇO: que o orçamento é conferido a cada lote e que estourá-lo
    interrompe a faixa com erro, não que o `pg_total_relation_size` funciona.
    """
    import tribunals.management.commands.backfill_magistrados as B
    monkeypatch.setattr(B, 'LOTE_ESCRITA', 1)
    cache.delete(B.ZERO_KEY)
    passos = iter([0, 5 * 1024 ** 3, 21 * 1024 ** 3])
    ultimo = {'v': 0}

    def _falso():
        try:
            ultimo['v'] = next(passos)
        except StopIteration:
            pass
        return ultimo['v']

    monkeypatch.setattr(B, 'bytes_ocupados', _falso)
    for i in range(5):
        _mov('TJSP', f'{i:07d}-11.2026.8.26.0001', f'o{i}', ASSINA, 'Vara X')
    with pytest.raises(CommandError) as saida:
        _rodar(orcamento_bytes='20GiB', lote=1)
    assert 'orçamento de BYTES' in str(saida.value)
    assert 'NÃO terminou' in str(saida.value)


@CACHE_LOCAL
def test_orcamento_ja_estourado_nao_le_uma_linha_sequer(monkeypatch):
    """Estourado antes de começar não é motivo para "dar mais uma passadinha":
    o comando recusa a largada e não lê nada."""
    import tribunals.management.commands.backfill_magistrados as B
    monkeypatch.setattr(B, 'bytes_ocupados', lambda: 30 * 1024 ** 3)
    cache.set(B.ZERO_KEY, {'bytes': 0, 'em': None, 'tabela_vazia': True}, None)
    _mov('TJSP', '0000001-11.2026.8.26.0001', 'x1', ASSINA, 'Vara X')
    with pytest.raises(CommandError) as saida:
        _rodar(orcamento_bytes='20GiB')
    assert 'JÁ ESTOURADO' in str(saida.value)
    assert MagistradoAtuacao.objects.count() == 0


@CACHE_LOCAL
def test_dry_run_nao_carimba_o_zero_do_orcamento():
    """O zero é ESTADO. Um ensaio que o grava faz a primeira rodada de verdade
    contar a partir do lugar errado, e em silêncio."""
    from tribunals.management.commands.backfill_magistrados import ZERO_KEY
    cache.delete(ZERO_KEY)
    _mov('TJSP', '0000001-11.2026.8.26.0001', 'a1', ASSINA, 'Vara X')
    _rodar(dry_run=True)
    assert cache.get(ZERO_KEY) is None


@pytest.mark.parametrize('txt,esperado', [
    ('20GB', 20 * 1000 ** 3), ('20GiB', 20 * 1024 ** 3), ('500MB', 500 * 1000 ** 2),
    ('21474836480', 21474836480), ('1,5GiB', int(1.5 * 1024 ** 3)),
])
def test_o_orcamento_aceita_sufixo(txt, esperado):
    """Orçamento em bytes crus é um número de 11 dígitos; digitado à mão ele
    erra de 10× sem avisar, e é o único freio que este comando tem."""
    from tribunals.management.commands.backfill_magistrados import tamanho_em_bytes
    assert tamanho_em_bytes(txt) == esperado


def test_o_orcamento_recusa_lixo():
    from tribunals.management.commands.backfill_magistrados import tamanho_em_bytes
    with pytest.raises(CommandError):
        tamanho_em_bytes('vinte gigas')


@CACHE_LOCAL
def test_faixa_aberta_sem_shard_e_recusada_na_largada():
    """Sem checkpoint, todo restart volta ao pk inicial — 351 reinícios com
    progresso líquido zero, medidos em 02/09/2026. Aqui isso é erro na
    largada, não descoberta três dias depois."""
    with pytest.raises(CommandError) as saida:
        call_command('backfill_magistrados', de=0, ate=10 ** 6, verbosity=0)
    assert 'checkpoint' in str(saida.value)


# --------------------------------------------------------------------------- #
# --tribunal: varrer SÓ um tribunal
#
# Medido em 05/09/2026: ler o campo `texto` é 97% do custo do lote (13,7 ms
# contra 0,37 ms sem ele, no MESMO trecho de pk). O TJSP é 11,3% do acervo, e a
# varredura completa lia o `texto` dos outros 88,7% para descartá-los. Com o
# filtro no SQL o `tribunal_id` — que mora na tupla, sem TOAST — decide antes,
# e o texto só é lido para quem interessa.
#
# O cursor NÃO muda: continua a mesma faixa de pk. Por isso o perigo aqui não é
# performance, é COBERTURA — um filtro que perde linha entrega um cadastro
# incompleto com run verde, que é a assinatura das três perdas do CLAUDE.md.
# --------------------------------------------------------------------------- #
@CACHE_LOCAL
def test_o_filtro_por_tribunal_le_TUDO_do_tribunal_e_nada_dos_outros():
    _mov('TJSP', '1000000-11.2026.8.26.0100', 'sp-1', ASSINA, '1ª Vara Cível')
    _mov('TJSP', '1000000-22.2026.8.26.0100', 'sp-2', ASSINA_OUTRA_CAIXA, '1ª Vara Cível')
    _mov('TJMG', '2000000-33.2026.8.13.0024', 'mg-1', ASSINA, '2ª Vara Cível')

    _rodar(tribunal='TJSP')

    siglas = set(Magistrado.objects.values_list('tribunal_id', flat=True))
    assert siglas == {'TJSP'}, f'vazou tribunal de fora do filtro: {siglas}'
    # as DUAS publicações do TJSP entraram — filtro que perde metade também
    # deixaria só uma sigla e passaria no assert de cima
    assert MagistradoAtuacao.objects.count() == 2


@CACHE_LOCAL
def test_o_controle_dos_dois_lados_conta_o_MESMO_universo_do_filtro(capsys):
    """`--conferir` sem o filtro acusaria "li 2 de 3" e reprovaria uma
    varredura correta. Controle que grita errado ensina a ignorar controle."""
    _mov('TJSP', '1000000-11.2026.8.26.0100', 'sp-1', ASSINA, '1ª Vara Cível')
    _mov('TJSP', '1000000-22.2026.8.26.0100', 'sp-2', ASSINA_OUTRA_CAIXA, '1ª Vara Cível')
    _mov('TJMG', '2000000-33.2026.8.13.0024', 'mg-1', ASSINA, '2ª Vara Cível')

    _rodar(tribunal='TJSP', conferir=True, verbosity=1)

    saida = capsys.readouterr()
    assert 'cobertura 100.00%' in saida.out + saida.err
    assert 'CONTROLE REPROVADO' not in saida.out + saida.err


@CACHE_LOCAL
def test_sigla_que_nao_existe_e_recusada_na_largada():
    """Sigla errada varreria 2,4 bilhões de pk para achar zero linha — e
    terminaria verde, dizendo 'faixa concluída'."""
    with pytest.raises(CommandError, match='não existe'):
        _rodar(tribunal='TJXX')


@CACHE_LOCAL
def test_shard_que_nao_menciona_o_tribunal_e_recusado():
    """O cursor é POR SHARD. Reaproveitar o cursor de uma varredura COMPLETA
    numa varredura filtrada marcaria como visto o que nunca foi lido — e o
    contrário também: a faixa 'concluída' pelo filtro não foi varrida para os
    outros tribunais."""
    _mov('TJSP', '1000000-11.2026.8.26.0100', 'sp-1', ASSINA, '1ª Vara Cível')
    with pytest.raises(CommandError, match='não menciona'):
        call_command('backfill_magistrados', de=0, ate=10 ** 12,
                     shard='nacional', tribunal='TJSP', verbosity=0)
