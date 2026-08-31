"""`grau` do Datajud — o campo que separa RPV de precatório, e que descartávamos.

Auditoria de completude do DADO (24-25/08/2026): `grau` presente em 20/20 dos
`_source` sondados ao vivo e **5 dos 20 eram `JE`**. `_meta_updates_from_source`
não lia nenhum deles, e não havia coluna.

Medição própria em 25/08/2026, no `voyager-acervo` INTEIRO — o esqueleto
nacional que a varredura já gravou a partir do Datajud, 342.046.902 documentos,
`_count` por termo (nunca `exists`, que conta string vazia como presente):

    G1  203.782.129        JE   73.791.952  ← 21,6% do país
    G2   41.972.803        SUP   8.159.129
    TR   14.272.244        TRU      68.645
    soma = 342.046.902 = total do índice ⇒ `grau` presente em 100% dos docs

**JE (Juizado Especial) e TR (Turma Recursal) pagam por RPV, não por
precatório.** Sem o campo, o funil de produto do Juriscope mistura dois
produtos com prazos e preços diferentes.

`nivelSigilo` no mesmo levantamento: **0 em 342.046.902 documentos e qualquer
outro valor em 0** — o campo não carrega informação nacional nenhuma (a API
pública do CNJ só expõe o que é público). Mapeá-lo para `segredo_justica`
escreveria `False` em 102 M de processos, que é exatamente a afirmação que a
migration 0052 acabou de desfazer ao tornar a coluna NULL.
"""
import logging
from types import SimpleNamespace

import pytest

from datajud import ingestion
from datajud.ingestion import GRAUS_CONHECIDOS, _meta_updates_from_source


@pytest.fixture(autouse=True)
def _coluna_grau_existe(monkeypatch):
    """Estes testes são de PARSER, não de banco. A guarda de coluna é testada
    à parte, em `test_sem_coluna_no_banco_nao_escreve_grau`."""
    monkeypatch.setattr(ingestion, '_COLUNAS_CONFERIDAS',
                        {'grau': True, 'classe_cnj_codigo': True})


def _proc(grau=''):
    return SimpleNamespace(
        numero_cnj='0000001-11.2020.8.26.0100', grau=grau,
        classe_codigo='', assunto_codigo='', orgao_julgador_codigo='',
        orgao_julgador_nome='', data_autuacao=None, valor_causa=None,
    )


def test_dominio_medido_no_acervo_nacional():
    """Se alguém acrescentar um grau sem medir, este teste avisa."""
    assert sorted(GRAUS_CONHECIDOS) == ['G1', 'G2', 'JE', 'SUP', 'TR', 'TRU']
    assert all(len(g) <= 4 for g in GRAUS_CONHECIDOS), (
        'Process.grau é varchar(4) — um grau mais longo seria truncado em silêncio'
    )


def test_grau_je_e_gravado():
    """O caso que motiva o campo: JE = Juizado Especial = RPV, não precatório."""
    upd = _meta_updates_from_source(_proc(), {'grau': 'JE'})
    assert upd['grau'] == 'JE'


def test_todos_os_graus_medidos_passam():
    for g in sorted(GRAUS_CONHECIDOS):
        assert _meta_updates_from_source(_proc(), {'grau': g})['grau'] == g


def test_grau_minusculo_e_com_espaco_normaliza():
    assert _meta_updates_from_source(_proc(), {'grau': ' je '})['grau'] == 'JE'


class _Coletor(logging.Handler):
    def __init__(self):
        super().__init__()
        self.msgs = []

    def emit(self, record):
        self.msgs.append(record.getMessage())


def test_grau_fora_do_dominio_abstem():
    """Abster > chutar (regra nº 6). E abster em VOZ ALTA, não em silêncio.

    O handler é acoplado direto ao logger: o `caplog` do pytest depende de
    propagação até a raiz, e o logging do projeto é configurado por app.
    """
    log = logging.getLogger('voyager.datajud.ingestion')
    h = _Coletor()
    log.addHandler(h)
    try:
        upd = _meta_updates_from_source(_proc(), {'grau': 'G3'})
        # controle positivo: um grau MEDIDO não pode gerar aviso nenhum
        ok = _meta_updates_from_source(_proc(), {'grau': 'JE'})
    finally:
        log.removeHandler(h)
    assert 'grau' not in upd, 'gravou um grau que ninguém mediu'
    assert ok['grau'] == 'JE'
    assert len(h.msgs) == 1 and 'fora do domínio' in h.msgs[0], h.msgs


def test_grau_ausente_nao_escreve_nada():
    assert 'grau' not in _meta_updates_from_source(_proc(), {})
    assert 'grau' not in _meta_updates_from_source(_proc(), {'grau': ''})
    assert 'grau' not in _meta_updates_from_source(_proc(), {'grau': None})


def test_grau_ja_preenchido_nao_e_sobrescrito():
    """Mesma política dos outros campos: o Datajud só preenche lacuna."""
    upd = _meta_updates_from_source(_proc(grau='G2'), {'grau': 'G1'})
    assert 'grau' not in upd


def test_nivel_sigilo_nao_vira_segredo_justica():
    """Controle negativo com o valor que a fonte REALMENTE manda.

    `nivelSigilo=0` em 342.046.902 de 342.046.902 documentos. Se alguém mapear
    isso para `segredo_justica`, 102 M de processos voltam a AFIRMAR "não corre
    em segredo" sem ninguém ter perguntado — e o e-SAJ responde a página
    "informe a senha" em 10 de 11 sondas de processos TJSP marcados `ok`.
    """
    upd = _meta_updates_from_source(_proc(), {'nivelSigilo': 0, 'grau': 'G1'})
    assert 'segredo_justica' not in upd
    upd = _meta_updates_from_source(_proc(), {'nivelSigilo': 5})
    assert 'segredo_justica' not in upd


def test_grau_nao_atrapalha_os_campos_que_ja_funcionavam():
    """Controle positivo: o resto do `_source` continua sendo lido."""
    upd = _meta_updates_from_source(_proc(), {
        'grau': 'G1',
        'classe': {'codigo': 12078, 'nome': 'Cumprimento de Sentença'},
        'assuntos': [{'codigo': 10441, 'nome': 'Acidente de Trânsito'}],
    })
    assert upd['grau'] == 'G1'
    assert upd['classe_codigo'] == '12078'
    assert upd['assunto_codigo'] == '10441'


def test_sem_coluna_no_banco_nao_escreve_grau(monkeypatch):
    """Ordem de deploy: o model tem `grau` desde a 0052, mas o `ALTER TABLE`
    sobre 102 M de linhas sob escrita pode não ter passado.

    Se este código subir antes do ALTER, um `UPDATE ... SET grau = %s` derruba
    a sincronização INTEIRA — e leva junto os campos que já funcionavam. Aqui a
    guarda tem que abrir mão só do `grau`.
    """
    monkeypatch.setattr(ingestion, '_COLUNAS_CONFERIDAS',
                        {'grau': False, 'classe_cnj_codigo': False})
    upd = _meta_updates_from_source(_proc(), {
        'grau': 'JE',
        'classe': {'codigo': 12078, 'nome': 'Cumprimento de Sentença'},
    })
    assert 'grau' not in upd, 'ia escrever numa coluna que não existe no banco'
    # a MESMA guarda vale para as colunas da 0054 (`classe_cnj_*`)
    assert 'classe_cnj_codigo' not in upd
    # controle positivo: o resto NÃO pode ser sacrificado junto
    assert upd['classe_codigo'] == '12078'


# ---------- backfill a partir do índice: escolha do grau de ORIGEM ----------

def test_escolher_grau_prioriza_a_origem():
    """G1 e G2 do mesmo processo são documentos DIFERENTES no Datajud.

    Medido em 25/08/2026 numa amostra aleatória UNIFORME de pk: 12,2% dos
    processos achados no `voyager-acervo` têm mais de um grau. `Process.grau`
    é escalar, então precisa de regra — e a regra é o grau de ORIGEM, porque é
    ele que decide o produto: quem nasceu no Juizado Especial paga por RPV,
    quem nasceu na vara comum paga por precatório. `TR`/`TRU` são as instâncias
    recursais DO juizado, então também indicam origem no juizado.
    """
    from datajud.management.commands.backfill_grau import escolher_grau

    assert escolher_grau(['G1', 'G2'])[0] == 'G1'
    assert escolher_grau(['G2', 'G1'])[0] == 'G1'
    assert escolher_grau(['JE', 'TR'])[0] == 'JE'
    assert escolher_grau(['TR'])[0] == 'TR', 'só turma recursal ainda é juizado'
    assert escolher_grau(['G1', 'G2', 'SUP'])[0] == 'G1'
    assert escolher_grau(['G2', 'SUP'])[0] == 'G2', (
        'sem G1 no índice, fica o que a fonte mostra — inventar o G1 que ela '
        'não trouxe seria chute'
    )
    # SUP nunca é origem, e os dados confirmam: JE+SUP+TR é Juizado → Turma
    # Recursal → Pedido de Uniformização no STJ (2 casos reais na amostra).
    assert escolher_grau(['JE', 'SUP', 'TR'])[0] == 'JE'
    assert escolher_grau(['JE', 'SUP', 'TR', 'TRU'])[0] == 'JE'
    # TR/TRU PROVAM juizado mesmo quando o 1º grau veio rotulado G1: medidos 52
    # casos de `G1+TR` em que o "G1" é `01 VARA JUIZADO ESP. DA FAZENDA PUBLICA`
    assert escolher_grau(['G1', 'G2', 'JE', 'TR'])[0] == 'JE'
    assert escolher_grau(['G1', 'TR'])[0] == 'TR'


def test_escolher_grau_descarta_valor_fora_do_dominio():
    """Grau desconhecido não é normalizado no chute: é descartado."""
    from datajud.management.commands.backfill_grau import escolher_grau

    assert escolher_grau(['G9'])[0] == ''
    assert escolher_grau([None, '', 'G9'])[0] == ''
    assert escolher_grau(['G9', 'JE'])[0] == 'JE'   # controle positivo


def test_escolher_grau_abstem_quando_a_fonte_contradiz_g1_e_je():
    """`G1 + JE` sem `TR`/`TRU`: a FONTE se contradiz sobre o MESMO órgão.

    Medido (20.000 pks, semente 20260825, 16.357 CNJs achados): 104 casos
    (0,64%) — 94 de `G1+JE`, 7 de `G1+G2+JE`, 3 de `G1+G2+JE+SUP`. Exemplo real:

        5017073-48.2024.4.04.7003 [TRF4]
          G1  Procedimento Comum Cível                2a Vara Federal de Maringa
          JE  Procedimento do Juizado Especial Cível  2ª Vara Federal de Maringá

    São varas adjuntas que o tribunal rotula ora G1, ora JE. Marcar `JE` num
    processo cuja classe principal é `Procedimento Comum Cível` diria RPV onde
    é precatório — classificaria o PRODUTO errado. Abster > chutar.
    """
    from datajud.management.commands.backfill_grau import escolher_grau

    grau, motivo = escolher_grau(['G1', 'JE'])
    assert grau == '', 'escolheu um grau onde a fonte se contradiz'
    assert 'contradiz' in motivo
    assert escolher_grau(['G1', 'G2', 'JE'])[0] == ''
    assert escolher_grau(['G1', 'G2', 'JE', 'SUP'])[0] == ''
    # controle positivo: com TR/TRU a ambiguidade some, porque recurso
    # inominado não existe fora do juizado
    assert escolher_grau(['G1', 'JE', 'TR'])[0] == 'JE'
    # controle negativo: JE sozinho ou com G2 (sem G1) não é ambíguo
    assert escolher_grau(['JE'])[0] == 'JE'
    assert escolher_grau(['G2', 'JE'])[0] == 'JE'
