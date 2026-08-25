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

from datajud.ingestion import GRAUS_CONHECIDOS, _meta_updates_from_source


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
