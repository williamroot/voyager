"""Lote sem teto de espera vira bloqueio de meio dia — e trava outros três jobs.

INCIDENTE (27/08/2026, produção). `backfill_sinal_precatorio --tribunais TJSP`
com `--batch 20000`: um único `UPDATE ... SET tem_sinal_precatorio = EXISTS(
SELECT 1 FROM tribunals_movimentacao WHERE processo_id = p.id AND texto ~* …)`
rodou **45.705 segundos = 12,7 horas**.

O custo do `EXISTS` é proporcional ao número de MOVIMENTAÇÕES do processo, não
ao número de processos — e processo de TJSP tem muitas. O mesmo `--batch 20000`
que fecha em segundos no TJPR virou meio dia no TJSP.

Enquanto durou, a transação segurou row-lock em 20.000 linhas de
`tribunals_process` e deixou três jobs em `wait_event_type=Lock`:

    DROP INDEX CONCURRENTLY proc_atualizado_em_idx ....... 12,6 h esperando
    UPDATE tribunals_process SET assunto_id = … ..........  2,6 h
    UPDATE tribunals_process SET classificacao = … (×2) ...  1,5 h cada

Regra nº 7 do CLAUDE.md: nada sem teto de espera. Com teto, o lote morre
sozinho, a transação solta os locks e o comando registra o erro — em vez de
virar um bloqueio invisível de meio dia.
"""
import re


FONTE = open('tribunals/management/commands/backfill_sinal_precatorio.py').read()


def test_o_lote_tem_statement_timeout():
    i = FONTE.find('with transaction.atomic():')
    assert i > 0
    bloco = FONTE[i:i + 2000]
    assert 'SET LOCAL statement_timeout' in bloco, (
        'lote sem teto — foi assim que um UPDATE rodou 12,7 h e travou 3 jobs')
    assert 'SET LOCAL lock_timeout' in bloco


def test_o_teto_e_LOCAL_e_nao_de_sessao():
    """`SET` solto vaza pela conexão do pgbouncer para quem reusar depois.

    Já matou o `IngestionRun 223570` do TJDFT com "canceling statement due to
    statement timeout" herdado de outra sessão: 63 de 250 conexões carregavam
    teto alheio.
    """
    for m in re.finditer(r"c\.execute\('SET (LOCAL )?(statement|lock)_timeout",
                         FONTE):
        assert m.group(1) == 'LOCAL ', f'SET de sessão em {m.group(0)!r} — vaza no pool'


def test_o_batch_default_encolheu():
    """20.000 era o default e é o tamanho que produziu as 12,7 h."""
    m = re.search(r"--batch', type=int, default=(\d+)", FONTE)
    assert m, 'o argumento --batch sumiu — teste desatualizado'
    assert int(m.group(1)) <= 5000, (
        f'default {m.group(1)} — o custo é por movimentação e varia 100x entre tribunais')


def test_o_teto_e_ajustavel_sem_editar_codigo():
    assert '--statement-timeout' in FONTE and '--lock-timeout' in FONTE
