"""Hidratação: esqueleto do CNJ → processo de verdade.

O contrato que estes testes protegem é de HONESTIDADE, não de mecânica: a
hidratação não pode prometer partes e valor onde eles não vão vir. O Datajud
não expõe esses campos, e só 16 dos 60 tribunais têm enricher — quem hidrata um
processo de tribunal sem enricher precisa ouvir isso da resposta, não descobrir
dias depois olhando um campo vazio.
"""
import pytest

from datajud import hidratacao as H

CNJ = '5229078-89.2022.8.13.0024'
DIGITOS = '52290788920228130024'


@pytest.fixture
def sem_es(monkeypatch):
    """ES fora do caminho: a hidratação não pode depender dele pra funcionar."""
    monkeypatch.setattr(H, 'esqueleto', lambda cnj: {
        'proc': CNJ, 'tribunal': 'TJMG', 'classe_codigo': '156',
        'classe_nome': 'Cumprimento de sentença',
        'orgao_nome': 'Direção do Foro da Comarca de Belo Horizonte',
        'assunto_codigos': ['9419'], 'assunto_nomes': ['Execução Previdenciária'],
    })
    monkeypatch.setattr(H, '_marcar_no_acervo', lambda cnj: None)


@pytest.fixture
def datajud_fake(monkeypatch):
    chamados = []

    def _sync(proc, client=None):
        chamados.append(proc.numero_cnj)
        return {'cnj': proc.numero_cnj, 'novos': 10, 'encontrado': True}

    import datajud.ingestion as I
    monkeypatch.setattr(I, 'sync_processo', _sync)
    return chamados


@pytest.mark.django_db
def test_cria_o_processo_com_o_que_o_esqueleto_sabe(sem_es, datajud_fake, monkeypatch):
    from tribunals.models import Process, Tribunal
    Tribunal.objects.get_or_create(sigla='TJMG', defaults={'nome': 'TJ Minas', 'sigla_djen': 'TJMG'})
    monkeypatch.setattr('enrichers.jobs.enqueue_enriquecimento_manual', lambda pk: None)

    out = H.hidratar_cnj(CNJ)
    assert out['estado'] == 'criado'
    p = Process.objects.get(numero_cnj=CNJ)
    assert p.tribunal_id == 'TJMG'
    assert p.classe_nome == 'Cumprimento de sentença'
    assert p.orgao_julgador_nome.startswith('Direção do Foro')
    assert out['movimentos_novos'] == 10
    assert datajud_fake == [CNJ]


@pytest.mark.django_db
def test_idempotente_nao_duplica(sem_es, datajud_fake, monkeypatch):
    from tribunals.models import Process, Tribunal
    Tribunal.objects.get_or_create(sigla='TJMG', defaults={'nome': 'TJ Minas', 'sigla_djen': 'TJMG'})
    monkeypatch.setattr('enrichers.jobs.enqueue_enriquecimento_manual', lambda pk: None)

    H.hidratar_cnj(CNJ)
    out = H.hidratar_cnj(CNJ)
    assert out['estado'] == 'ja_no_acervo'
    assert Process.objects.filter(numero_cnj=CNJ).count() == 1


@pytest.mark.django_db
def test_tribunal_sem_enricher_avisa_que_partes_nao_virao(sem_es, datajud_fake, monkeypatch):
    """TRT20 não tem enricher. Dizer 'hidratado' e deixar o usuário esperando
    partes que nunca chegam é pior do que não hidratar."""
    from tribunals.models import Tribunal
    Tribunal.objects.get_or_create(sigla='TRT20', defaults={'nome': 'TRT 20ª', 'sigla_djen': 'TRT20'})
    monkeypatch.setattr(H, 'esqueleto', lambda cnj: {'tribunal': 'TRT20'})

    out = H.hidratar_cnj('0010610-14.2015.5.03.0092')
    assert out['enricher_enfileirado'] is False
    assert out['tera_partes'] is False


@pytest.mark.django_db
def test_tribunal_desconhecido_nao_chuta(sem_es, datajud_fake, monkeypatch):
    """Sem tribunal cadastrado não dá pra criar (FK PROTECT) — e chutar faria o
    enricher bater na porta de outro tribunal."""
    monkeypatch.setattr(H, 'esqueleto', lambda cnj: None)
    out = H.hidratar_cnj(CNJ)
    assert out['estado'] == 'tribunal_desconhecido'
    assert out['sigla'] == 'TJMG'          # derivado do número, ainda assim informado


@pytest.mark.django_db
def test_cnj_invalido_e_recusado_antes_de_qualquer_escrita():
    out = H.hidratar_cnj('123')
    assert out['estado'] == 'cnj_invalido'
