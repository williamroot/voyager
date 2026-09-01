"""A FK do catálogo fecha na ORIGEM da escrita — e ABSTÉM quando não dá.

O #104 fechou `classe_id IS NULL` em 21 linhas às 22:05 UTC de 31/08/2026 e o
buraco estava em 8.072 dezoito horas depois, sem que nada tivesse sido apagado:
97,6% eram linhas ANTIGAS reescritas ao vivo por um caminho que grava
`classe_codigo` e nunca resolve `classe_id` — com **0 códigos** fora do
catálogo. Backfill não conserta escritor; escritor conserta escritor.

Os dois caminhos culpados eram `datajud/hidratacao.py` (INSERT) e
`datajud/ingestion.py::sync_processo` (UPDATE). Cada um tem aqui
o par obrigatório:

  - controle POSITIVO: código no catálogo ⇒ a FK nasce ligada;
  - controle NEGATIVO: código FORA do catálogo ⇒ a FK fica NULL. Não se
    inventa vínculo (regra nº 6 do `CLAUDE.md`) e, principalmente, não se
    CRIA linha num catálogo nacional que é destino de FK — foi assim que o
    `99999999` do TJSP entrou nele em 31/08.

Conferido por mutação: apagar o `if fk:` de qualquer um dos dois arquivos
derruba os positivos; trocar o `resolver` por um `return codigo` cego derruba
os negativos.
"""
import pytest
from types import SimpleNamespace

from datajud import hidratacao as H
from datajud.ingestion import _meta_updates_from_source, fechar_fks_do_catalogo
from tribunals import catalogo

CNJ = '5229078-89.2022.8.13.0024'


@pytest.fixture(autouse=True)
def catalogo_limpo():
    catalogo.limpar_cache()
    yield
    catalogo.limpar_cache()


@pytest.fixture
def sem_es(monkeypatch):
    monkeypatch.setattr(H, 'esqueleto', lambda cnj: {
        'proc': CNJ, 'tribunal': 'TJMG',
        'classe_codigo': '156', 'classe_nome': 'Cumprimento de sentença',
        'assunto_codigos': ['9419'], 'assunto_nomes': ['Execução Previdenciária'],
    })
    monkeypatch.setattr(H, '_marcar_no_acervo', lambda cnj: None)


@pytest.fixture
def datajud_mudo(monkeypatch):
    import datajud.ingestion as I
    monkeypatch.setattr(
        I, 'sync_processo',
        lambda proc, client=None: {'cnj': proc.numero_cnj, 'novos': 0, 'encontrado': False})


def _proc_vazio():
    return SimpleNamespace(
        classe_codigo='', assunto_codigo='', orgao_julgador_codigo='',
        orgao_julgador_nome='', data_autuacao=None, valor_causa=None,
        classe_cnj_codigo='', classe_cnj_nome='', grau='',
    )


SOURCE = {'classe': {'codigo': 156, 'nome': 'Cumprimento de sentença'},
          'assuntos': [{'codigo': 9419, 'nome': 'Execução Previdenciária'}]}


# ─────────────────────────── hidratação (INSERT) ───────────────────────────

@pytest.mark.django_db
def test_hidratacao_nasce_com_a_fk_ligada(sem_es, datajud_mudo):
    from tribunals.models import Assunto, ClasseJudicial, Process, Tribunal
    Tribunal.objects.get_or_create(sigla='TJMG',
                                   defaults={'nome': 'TJ Minas', 'sigla_djen': 'TJMG'})
    ClasseJudicial.objects.create(codigo='156', nome='Cumprimento de sentença')
    Assunto.objects.create(codigo='9419', nome='Execução Previdenciária')

    out = H.hidratar_cnj(CNJ, com_enricher=False)
    assert out['estado'] == 'criado'

    p = Process.objects.get(numero_cnj=CNJ)
    # campo de CONTROLE: a string sempre foi gravada, e continua
    assert p.classe_codigo == '156' and p.assunto_codigo == '9419'
    # o que o fix acrescenta
    assert p.classe_id == '156'
    assert p.assunto_id == '9419'


@pytest.mark.django_db
def test_hidratacao_abstem_quando_o_codigo_nao_esta_no_catalogo(sem_es, datajud_mudo):
    """Controle NEGATIVO: catálogo vazio ⇒ FK NULL, e nada é criado nele."""
    from tribunals.models import Assunto, ClasseJudicial, Process, Tribunal
    Tribunal.objects.get_or_create(sigla='TJMG',
                                   defaults={'nome': 'TJ Minas', 'sigla_djen': 'TJMG'})

    H.hidratar_cnj(CNJ, com_enricher=False)

    p = Process.objects.get(numero_cnj=CNJ)
    assert p.classe_codigo == '156'          # controle: a string entrou
    assert p.classe_id is None               # e a FK ficou honesta
    assert p.assunto_id is None
    assert ClasseJudicial.objects.count() == 0   # catálogo nacional intocado
    assert Assunto.objects.count() == 0


# ─────────────────────────── sync_processo (UPDATE) ─────────────────────────

@pytest.mark.django_db
def test_meta_updates_fecha_a_fk_do_catalogo():
    from tribunals.models import Assunto, ClasseJudicial
    ClasseJudicial.objects.create(codigo='156', nome='Cumprimento de sentença')
    Assunto.objects.create(codigo='9419', nome='Execução Previdenciária')

    upd = fechar_fks_do_catalogo(_meta_updates_from_source(_proc_vazio(), SOURCE))

    assert upd['classe_codigo'] == '156' and upd['assunto_codigo'] == '9419'
    assert upd['classe_id'] == '156'
    assert upd['assunto_id'] == '9419'


@pytest.mark.django_db
def test_meta_updates_abstem_sem_catalogo():
    upd = fechar_fks_do_catalogo(_meta_updates_from_source(_proc_vazio(), SOURCE))

    assert upd['classe_codigo'] == '156'     # controle
    assert 'classe_id' not in upd
    assert 'assunto_id' not in upd


@pytest.mark.django_db
def test_meta_updates_nunca_apaga_fk_ja_fechada():
    """Órfão não pode virar UPDATE `classe_id = NULL`.

    O caminho é real: a linha pode ter a FK fechada pelo enricher e o Datajud
    trazer, depois, um código que o catálogo não conhece. Abster é deixar como
    está — nunca degradar o que já estava certo.
    """
    proc = _proc_vazio()
    proc.classe_id, proc.assunto_id = '999', '888'   # já ligadas pelo enricher

    upd = fechar_fks_do_catalogo(                    # catálogo sem 156/9419
        _meta_updates_from_source(proc, SOURCE))

    assert upd['classe_codigo'] == '156'             # controle: a string entra
    assert 'classe_id' not in upd                    # e a FK não é degradada
    assert 'assunto_id' not in upd


# ─────────────────────────── o resolvedor em si ─────────────────────────────

@pytest.mark.django_db
def test_resolver_enxerga_codigo_semeado_depois_do_primeiro_miss():
    """O cache não pode transformar uma ausência em veredito permanente
    dentro da janela: o miss faz UMA sonda pela PK antes de abster."""
    from tribunals.models import ClasseJudicial

    assert catalogo.resolver('classe', '156') is None
    ClasseJudicial.objects.create(codigo='156', nome='Cumprimento de sentença')
    catalogo.limpar_cache()                       # nova janela
    assert catalogo.resolver('classe', '156') == '156'


@pytest.mark.django_db
def test_resolver_ignora_vazio_e_espaco():
    assert catalogo.resolver('classe', '') is None
    assert catalogo.resolver('classe', None) is None
    assert catalogo.resolver('classe', '   ') is None


@pytest.mark.django_db
def test_resolver_recusa_catalogo_desconhecido():
    with pytest.raises(ValueError):
        catalogo.resolver('orgao', '1')


# ────────────── o writer de verdade: sync_processo grava a FK ───────────────

@pytest.mark.django_db
def test_sync_processo_grava_a_fk_no_banco(settings, monkeypatch):
    """Prova de ponta: não basta o helper existir, `sync_processo` tem que
    chamá-lo. Sem esta, apagar a chamada e deixar a função no arquivo passaria.
    """
    import datajud.ingestion as I
    from tribunals.models import Assunto, ClasseJudicial, Process, Tribunal

    settings.DATAJUD_INDEXAR_AO_GRAVAR = False
    trib, _ = Tribunal.objects.get_or_create(
        sigla='TJMG', defaults={'nome': 'TJ Minas', 'sigla_djen': 'TJMG'})
    ClasseJudicial.objects.create(codigo='156', nome='Cumprimento de sentença')
    Assunto.objects.create(codigo='9419', nome='Execução Previdenciária')
    p = Process.objects.create(tribunal=trib, numero_cnj=CNJ)
    assert p.classe_id is None and p.assunto_id is None      # o antes

    monkeypatch.setattr(I, 'parse_movimentos', lambda source: [])
    cliente = SimpleNamespace(fetch_processo=lambda sigla, cnj: SOURCE)

    I.sync_processo(p, client=cliente)

    p.refresh_from_db()
    assert p.classe_codigo == '156' and p.assunto_codigo == '9419'   # controle
    assert p.classe_id == '156'
    assert p.assunto_id == '9419'
