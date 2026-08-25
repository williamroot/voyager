"""Os três campos da auditoria de completude do DADO (24-25/08/2026).

Cada asserção aqui existe porque um número foi medido em produção:

  · `segredo_justica` — `true` em **0 de 91.638.494** documentos do índice
    (`_count`, não `exists`; `exists` conta string vazia como presente) e 0 em
    120.000 processos amostrados no banco. O `default=False` do BooleanField
    virou uma AFIRMAÇÃO que ninguém tinha feito, para 102 M de processos,
    enquanto o e-SAJ devolvia a página "informe a senha… segredo de justiça"
    em 10 de 11 sondas ao vivo de processos TJSP marcados `ok` SEM NENHUMA
    PARTE (≈ 302 k processos). Voltar o default para `False` reintroduz a
    mentira: NULL tem que significar "não perguntamos" (regra nº 6 do
    CLAUDE.md, abster > chutar).

  · `ProcessoParte.fonte` — 84,8% do acervo (9.467 de 11.160 na amostra
    aleatória de semente 20260824, ≈ 86,7 M processos) tem parte gravada em
    `Movimentacao.destinatarios` e NENHUMA `ProcessoParte`. A coluna separa a
    parte que veio da publicação (ampla e rasa: polo 100%, OAB 99,87%, mas sem
    CPF/CNPJ e sem vínculo advogado→representado) da que veio do enricher
    (estreita e profunda). Sem ela a tela não pode DIZER o que tem, e o
    backfill não tem rollback por faixa.

  · `Process.grau` — presente em 20/20 dos `_source` sondados ao vivo no
    Datajud, e **5 dos 20 eram `JE`**. JE = Juizado Especial = RPV, não
    precatório: sem o campo o funil de produto mistura dois produtos com
    prazos e preços diferentes.
"""
import pytest

from tribunals.models import Process, ProcessoParte


def test_segredo_justica_e_tri_state_e_nasce_null():
    """NULL = não perguntamos. Se voltar a `default=False`, este teste quebra."""
    campo = Process._meta.get_field('segredo_justica')
    assert campo.null is True, (
        'segredo_justica voltou a ser NOT NULL — o default False é uma '
        'afirmação sobre 102 M de processos que ninguém verificou'
    )
    assert campo.default is None, (
        f'default é {campo.default!r}; tem que ser None. False significa '
        '"perguntamos e a fonte disse que não", e isso era falso em 91,6 M docs'
    )
    assert Process(numero_cnj='0000001-11.2020.8.26.0100').segredo_justica is None


@pytest.mark.django_db
def test_processo_nasce_sem_afirmar_segredo():
    """Controle positivo: o valor tem que chegar NULL ao banco, não False."""
    from tribunals.models import Tribunal

    trib = Tribunal.objects.create(sigla='TSTX', nome='Tribunal de teste')
    proc = Process.objects.create(numero_cnj='0000001-11.2020.8.26.0100', tribunal=trib)
    proc.refresh_from_db()
    assert proc.segredo_justica is None

    # Controle negativo — a coluna continua sabendo guardar os dois valores.
    proc.segredo_justica = True
    proc.save(update_fields=['segredo_justica'])
    proc.refresh_from_db()
    assert proc.segredo_justica is True


def test_processoparte_tem_procedencia():
    """NULL = legado/enricher. 'djen' = promovida do destinatário da publicação."""
    campo = ProcessoParte._meta.get_field('fonte')
    assert campo.null is True, (
        'fonte NOT NULL forçaria reescrita de ~84 M de linhas numa tabela '
        'quente — a coluna nasceu nullable exatamente para ser metadata-only'
    )
    assert campo.max_length >= len('enricher')


def test_grau_existe_e_cabe_os_quatro_valores_do_datajud():
    """G1/G2/JE/SUP. Vazio = não sabemos — nunca chutar G1."""
    campo = Process._meta.get_field('grau')
    assert campo.max_length >= 3
    assert campo.blank is True
    assert Process(numero_cnj='0000001-11.2020.8.26.0100').grau == ''
