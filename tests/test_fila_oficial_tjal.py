"""Fila oficial de precatórios do TJAL (4ª porta): coletor, descoberta,
classificação e o caminho até a API de leads que o JURISCOPE consome.

Os textos de lista abaixo reproduzem os dois layouts reais medidos em
23/10/2026 (`pdftotext -raw` das listas 890 — Delmiro Gouveia — e 739 — Estado
de Alagoas): número numa linha só, e número quebrado em duas linhas com a
coluna de origem. As listas não trazem nome de credor; o SAPRE traz nome e CPF,
e os de teste são fictícios.
"""
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.utils import timezone

from filas_oficiais import descoberta, execucao
from filas_oficiais.base import ResultadoEntidade
from filas_oficiais.descoberta import Placar, gravar_entidade, marcar_fora_da_fila
from filas_oficiais.fontes import tjal
from tribunals import classificador
from tribunals.classificador import classificar_e_persistir
from tribunals.models import (
    ApiClient,
    IngestionRun,
    PrecatorioFilaOficial,
    Process,
    Tribunal,
)

CPF = '123.456.789-09'
NOME = 'FULANA DE TESTE DA SILVA'

LISTA_DELMIRO = """Delmiro Gouveia Pendente
Nº Número do Precatório Apresentação
1º 0500371-61.2022.8.02.9003 21/03/2022 14:32:56 Alimentar 2023 TJ-AL 35.354,05 R$ 30/09/26 N
2º 0500372-46.2022.8.02.9003 21/03/2022 14:47:39 Alimentar 2023 TJ-AL 102.904,25 R$ 31/07/25 N
Lista Unificada de Precatórios Pendentes de Pagamento - Município de Delmiro Gouveia/AL
2º 0500372-46.2022.8.02.9003 21/03/2022 14:47:39 Alimentar 2023 TJ-AL 102.904,25 R$ 31/07/25 N
3º 00179/2023 26/04/22 15:19:13 Alimentar 2024 TRT-19 356.014,46 R$ 27/08/21 N
4º 0500028-42.2023.8.02.0043 18/07/2023 10:46:00 Comum 2024 TJ-AL 1.305.178,15 R$ 31/07/25 N
"""

LISTA_ESTADO = """N° Nº Precatório Recebimento
1º
0500026-
90.2025.8.02.9003/3
23/1/2025 16:24:00 Alimentar 2026
0716855-
47.2021.8.02.0001
R$ 25.426,65 2/4/2025 PD
2º
0500006-
72.2023.8.02.0046
18/7/2023 10:46:00 Alimentar 2026
0701803-
36.2022.8.02.0046
R$ 24.472,23 2/4/2025 PI
"""


def _linha_sapre(id_, nuprocesso, situacao=3, origem='0716855-47.2021.8.02.0001',
                 valor='1000.00', cpf=CPF):
    return {
        'id': id_, 'nuprocessosaj': nuprocesso,
        'numprocessoconhecimento': origem, 'numprocessoexecucao': None,
        'nomerequerente': NOME, 'cpfcnpjrequerente': cpf,
        'valorglobal': valor, 'anoorcamento': '2023', 'naturezaprecatorio': 0,
        'situacaoprecatorio': situacao,
        'entidade_devedora': {'nome': 'Município de Delmiro Gouveia'},
        'ultima_classificacao_orcamentaria': {'ordemclassificacao': 7},
    }


# ---------------------------------------------------------------------------
# Parser — e o que ele NÃO guarda (LGPD)
# ---------------------------------------------------------------------------
def test_item_do_sapre_normaliza_e_descarta_credor():
    it = tjal.item_do_sapre(_linha_sapre(1, '0500366-61.2022.8.02.0043/01'), 'DELMIRO GOUVEIA')
    assert it.chave == '0500366-61.2022.8.02.0043'
    assert it.situacao == 'pendente' and it.situacao_raw == 'Cadastrado'
    assert it.natureza == 'ALIMENTAR' and it.ano_orcamento == 2023 and it.ordem == 7
    assert it.origem_cnj == '0716855-47.2021.8.02.0001'
    assert it.entidade_devedora == 'DELMIRO GOUVEIA'
    assert not it.chave_e_origem
    # Nem o nome nem o CPF (com ou sem máscara) sobrevivem em campo nenhum.
    tudo = repr(dataclasses.asdict(it))
    assert NOME not in tudo
    assert CPF not in tudo and '12345678909' not in tudo


def test_situacoes_do_sapre():
    assert tjal.situacao_sapre(16)[0] == 'pago'
    assert tjal.situacao_sapre(4)[0] == 'extinto'
    assert tjal.situacao_sapre(21)[0] == 'pendente'
    assert tjal.situacao_sapre(5)[0] == 'outro'       # "Com pagamento": ambíguo
    assert tjal.situacao_sapre(99)[0] == 'outro'


def test_chave_que_e_o_processo_de_origem():
    cnj = '0000028-53.2014.8.02.0031'
    it = tjal.item_do_sapre(_linha_sapre(2, cnj, origem=cnj), 'PORTO DE PEDRAS')
    assert it.chave_e_origem and it.origem_cnj == ''
    assert tjal.e_numero_de_origem(cnj)
    assert not tjal.e_numero_de_origem('0500371-61.2022.8.02.9003')
    assert not tjal.e_numero_de_origem('0500028-42.2023.8.02.0043')


def test_agrupar_tira_linha_identica_e_soma_beneficiarios():
    cnj = '0000028-53.2014.8.02.0031'
    linhas = [_linha_sapre(1, cnj, origem=cnj, valor='100.00'),
              _linha_sapre(2, cnj, origem=cnj, valor='100.00'),          # idêntica
              _linha_sapre(3, cnj, origem=cnj, valor='50.00', cpf='98765432100',
                           situacao=16)]
    itens = tjal.agrupar_por_chave([tjal.item_do_sapre(r, 'X') for r in linhas])
    assert len(itens) == 1
    assert itens[0].n_itens == 2
    assert itens[0].valor_original == Decimal('150.00')
    assert itens[0].situacao == 'pendente'      # basta uma requisição devida


def test_lista_pdf_layout_em_linha_pula_trt_e_repeticao():
    itens = {i.chave: i for i in tjal.ler_lista_pdf(LISTA_DELMIRO, 'DELMIRO GOUVEIA', 'especial')}
    assert set(itens) == {'0500371-61.2022.8.02.9003', '0500372-46.2022.8.02.9003',
                          '0500028-42.2023.8.02.0043'}
    a = itens['0500028-42.2023.8.02.0043']
    assert a.valor_atualizado == Decimal('1305178.15')
    assert a.natureza == 'COMUM' and a.ano_orcamento == 2024 and a.ordem == 4
    assert a.regime == 'especial' and a.situacao == 'pendente'


def test_lista_pdf_layout_quebrado_com_origem():
    itens = {i.chave: i for i in tjal.ler_lista_pdf(LISTA_ESTADO, 'ESTADO DE ALAGOAS')}
    assert set(itens) == {'0500026-90.2025.8.02.9003', '0500006-72.2023.8.02.0046'}
    assert itens['0500026-90.2025.8.02.9003'].origem_cnj == '0716855-47.2021.8.02.0001'
    assert itens['0500006-72.2023.8.02.0046'].valor_atualizado == Decimal('24472.23')


def test_texto_do_pdf_sem_pdftotext_usa_pymupdf(monkeypatch):
    """A imagem do Voyager não tem o poppler: o caminho de produção é o PyMuPDF."""
    import fitz
    monkeypatch.setattr(tjal.shutil, 'which', lambda _: None)
    doc = fitz.open()
    pagina = doc.new_page()
    pagina.insert_text((40, 60), '1º 0500371-61.2022.8.02.9003 21/03/2022 14:32:56 '
                                 'Alimentar 2023 TJ-AL 35.354,05 R$ 30/09/26 N', fontsize=6)
    conteudo = doc.tobytes()
    itens = tjal.ler_lista_pdf(tjal.texto_do_pdf(conteudo), 'X')
    assert [i.chave for i in itens] == ['0500371-61.2022.8.02.9003']


def test_unir_sapre_e_lista():
    s = tjal.item_do_sapre(_linha_sapre(1, '0500372-46.2022.8.02.9003'), 'DELMIRO GOUVEIA')
    lista = tjal.ler_lista_pdf(LISTA_DELMIRO, 'DELMIRO GOUVEIA')
    u = {i.chave: i for i in tjal.unir([s], lista)}
    assert len(u) == 3
    assert u['0500372-46.2022.8.02.9003'].fonte == 'tjal_sapre+lista'
    assert u['0500372-46.2022.8.02.9003'].valor_atualizado == Decimal('102904.25')
    assert u['0500371-61.2022.8.02.9003'].fonte == 'tjal_lista_pdf'


# ---------------------------------------------------------------------------
# Coletor (HTTP falso): 504 fatia, completude por entidade
# ---------------------------------------------------------------------------
class _Resp:
    def __init__(self, status=200, dados=None, conteudo=b''):
        self.status_code, self._dados, self.content = status, dados, conteudo

    def json(self):
        return self._dados

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _Sessao:
    """SAPRE de mentira: entidade 26 responde inteira; a 48 dá 504 sem busca
    e responde por fatia; a 28 é só PDF."""

    def __init__(self, fatia_falha=None):
        self.headers = {}
        self.chamadas = []
        self.fatia_falha = fatia_falha

    def request(self, metodo, url, timeout=None, json=None, params=None):  # noqa: PLR0911
        self.chamadas.append((metodo, url, json, params))
        if url.endswith('/entidades'):
            return _Resp(dados=[{'id': 26, 'nome': 'DELMIRO GOUVEIA'},
                                {'id': 48, 'nome': 'MACEIÓ'},
                                {'id': 28, 'nome': 'ESTADO DE ALAGOAS'}])
        if 'por-regime' in url:
            if params['regime'] == 'e':
                return _Resp(dados={'Delmiro Gouveia': {'dados': {'pendente': [
                    {'id': 890, 'data_envio': '14/10/2026'}]}}})
            return _Resp(dados={'Estado de Alagoas': {'dados': {'pendente': [
                {'id': 700, 'data_envio': '01/01/2024'},
                {'id': 739, 'data_envio': '11/03/2024'}]}}})
        if '/baixar/' in url:
            return _Resp(conteudo=url.rsplit('/', 1)[1].encode())
        assert 'por-entidade' in url
        eid, busca = json['idEntidadePrincipal'], json['busca']
        if eid == 26:
            return _Resp(dados={'last_page': 1, 'data': [
                _linha_sapre(1, '0500372-46.2022.8.02.9003'),
                _linha_sapre(2, '0500900-11.2022.8.02.9003')]})
        if eid == 48:
            if busca is None or busca == self.fatia_falha:
                return _Resp(504)
            if busca == '-07.':
                return _Resp(dados={'last_page': 1, 'data': [
                    _linha_sapre(9, '0501111-07.2025.8.02.9003')]})
            return _Resp(dados={'last_page': 1, 'data': []})
        raise AssertionError(f'entidade inesperada {eid}')


@pytest.fixture
def pdf_falso(monkeypatch):
    textos = {b'890': LISTA_DELMIRO, b'739': LISTA_ESTADO}
    monkeypatch.setattr(tjal, 'texto_do_pdf', lambda c: textos[c])


def _coletor(sessao, **kw):
    return tjal.ColetorTjal(session=sessao, dormir=lambda s: None, so_pdf=[28],
                            profundidade=kw.pop('profundidade', 2), **kw)


def test_coletor_fatia_no_504_e_marca_completude(pdf_falso):
    sessao = _Sessao()
    res = {r.entidade: r for r in _coletor(sessao).iterar()}
    assert set(res) == {'DELMIRO GOUVEIA', 'MACEIO', 'ESTADO DE ALAGOAS'}
    # Delmiro: SAPRE (2) + lista (3), uma em comum.
    assert {i.chave for i in res['DELMIRO GOUVEIA'].itens} == {
        '0500372-46.2022.8.02.9003', '0500900-11.2022.8.02.9003',
        '0500371-61.2022.8.02.9003', '0500028-42.2023.8.02.0043'}
    assert res['DELMIRO GOUVEIA'].completa
    # Maceió: 504 na consulta inteira, as 100 fatias do dígito verificador.
    assert [i.chave for i in res['MACEIO'].itens] == ['0501111-07.2025.8.02.9003']
    assert res['MACEIO'].completa
    # Estado: só PDF, e pegou a lista mais RECENTE (739, não 700).
    assert res['ESTADO DE ALAGOAS'].completa
    assert res['ESTADO DE ALAGOAS'].info['lista']['id'] == 739
    assert not any(c[2] and c[2]['idEntidadePrincipal'] == 28 for c in sessao.chamadas)


def test_fatia_que_ainda_da_504_deixa_a_entidade_incompleta(pdf_falso):
    res = {r.entidade: r for r in _coletor(_Sessao(fatia_falha='-07.'),
                                           profundidade=0).iterar(entidades=['48'])}
    assert not res['MACEIO'].completa
    assert res['MACEIO'].falhas


def test_retomada_pula_entidade(pdf_falso):
    res = list(_coletor(_Sessao()).iterar(pular=lambda chave: chave == 'sapre:48'))
    assert 'MACEIO' not in {r.entidade for r in res}


def test_pausa_minima_de_um_segundo():
    assert tjal.ColetorTjal(session=_Sessao(), pausa=0.1).pausa == 1.0


# ---------------------------------------------------------------------------
# Descoberta + classificação (banco)
# ---------------------------------------------------------------------------
@pytest.fixture
def tjal_trib(db):
    return Tribunal.objects.get_or_create(sigla='TJAL', defaults={'nome': 'TJAL'})[0]


@pytest.fixture
def sem_datajud(monkeypatch):
    """`hidratar_cnj` sem rede: cria o processo como o Datajud criaria o 050."""
    chamadas = []

    def fake(cnj, com_enricher=True, fila_enricher='manual'):
        chamadas.append((cnj, com_enricher, fila_enricher))
        t = Tribunal.objects.get(sigla='TJAL')
        p, _ = Process.objects.get_or_create(
            tribunal=t, numero_cnj=cnj,
            defaults={'classe_codigo': '1298', 'classe_nome': 'Processo Administrativo'})
        return {'estado': 'criado', 'process_id': p.pk}
    monkeypatch.setattr(descoberta, 'hidratar_cnj', fake)
    return chamadas


def _it(chave, situacao='pendente', **kw):
    from filas_oficiais.base import ItemFila
    return ItemFila(chave=chave, fonte='tjal_sapre', entidade=kw.pop('entidade', 'DELMIRO GOUVEIA'),
                    situacao=situacao, chave_e_origem=tjal.e_numero_de_origem(chave), **kw)


@pytest.mark.django_db
def test_pendente_9003_nasce_como_precatorio(tjal_trib, sem_datajud):
    placar = Placar()
    gravar_entidade(tjal_trib, [_it('0500371-61.2022.8.02.9003',
                                    origem_cnj='0716855-47.2021.8.02.0001')], placar)
    p = Process.objects.get(numero_cnj='0500371-61.2022.8.02.9003')
    assert p.tribunal_id == 'TJAL'
    assert (p.classe_codigo, p.classe_nome) == ('1265', 'Precatório')
    assert p.classificacao == Process.CLASSIF_PRECATORIO
    assert p.classificacao_score == 1.0
    reg = PrecatorioFilaOficial.objects.get(chave=p.numero_cnj)
    assert reg.processo_id == p.pk and reg.situacao == 'pendente'
    assert reg.origem_cnj == '0716855-47.2021.8.02.0001'
    assert sem_datajud == []          # 9003 não vai ao Datajud nem ao enricher
    assert placar.criados == {'foro_9003': 1}


@pytest.mark.django_db
def test_pendente_de_comarca_hidrata_e_vira_precatorio(tjal_trib, sem_datajud):
    gravar_entidade(tjal_trib, [_it('0500028-42.2023.8.02.0043')], Placar())
    assert sem_datajud == [('0500028-42.2023.8.02.0043', True, 'tribunal')]
    p = Process.objects.get(numero_cnj='0500028-42.2023.8.02.0043')
    # A classe cadastral do Datajud fica; quem promove é a fila oficial.
    assert p.classe_codigo == '1298'
    assert p.classificacao == Process.CLASSIF_PRECATORIO


@pytest.mark.django_db
def test_origem_ja_no_acervo_como_nao_lead_vira_precatorio(tjal_trib, sem_datajud):
    cnj = '0000028-53.2014.8.02.0031'
    p = Process.objects.create(tribunal=tjal_trib, numero_cnj=cnj, classe_codigo='156',
                               classe_nome='Cumprimento de sentença',
                               classificacao=Process.CLASSIF_NAO_LEAD)
    placar = Placar()
    gravar_entidade(tjal_trib, [_it(cnj, entidade='PORTO DE PEDRAS')], placar)
    p.refresh_from_db()
    assert p.classificacao == Process.CLASSIF_PRECATORIO
    assert PrecatorioFilaOficial.objects.get(chave=cnj).chave_e_origem
    assert placar.criados == {} and placar.ja_no_acervo == {'NAO_LEAD': 1}


@pytest.mark.django_db
def test_pago_na_fonte_nao_cria_e_veta(tjal_trib, sem_datajud):
    ja = Process.objects.create(tribunal=tjal_trib, numero_cnj='0500900-11.2022.8.02.9003',
                                classe_codigo='1265', classe_nome='Precatório',
                                classificacao=Process.CLASSIF_PRECATORIO,
                                classificacao_score=1.0)
    gravar_entidade(tjal_trib, [_it('0500900-11.2022.8.02.9003', situacao='pago'),
                                _it('0500901-93.2022.8.02.9003', situacao='extinto')],
                    Placar())
    ja.refresh_from_db()
    assert ja.classificacao == Process.CLASSIF_NAO_LEAD
    assert not Process.objects.filter(numero_cnj='0500901-93.2022.8.02.9003').exists()
    assert PrecatorioFilaOficial.objects.get(chave='0500901-93.2022.8.02.9003').processo is None


@pytest.mark.django_db
def test_pendente_com_veto_do_djen_nao_promove(tjal_trib, sem_datajud, monkeypatch):
    cnj = '0000028-53.2014.8.02.0031'
    Process.objects.create(tribunal=tjal_trib, numero_cnj=cnj, classe_codigo='156')
    feats = classificador._empty_features(2014, 1, 0)
    feats['F24_pago_pos_exped_ANTI'] = 1
    monkeypatch.setattr(classificador, 'compute_features', lambda p: dict(feats))
    gravar_entidade(tjal_trib, [_it(cnj)], Placar())
    assert Process.objects.get(numero_cnj=cnj).classificacao != Process.CLASSIF_PRECATORIO


@pytest.mark.django_db
def test_outro_tribunal_e_outra_situacao_nao_criam(tjal_trib, sem_datajud):
    placar = Placar()
    gravar_entidade(tjal_trib, [_it('0000741-50.2024.5.19.0000'),
                                _it('0500902-78.2022.8.02.9003', situacao='outro')], placar)
    assert placar.outro_tribunal == 1
    assert not Process.objects.exists()


@pytest.mark.django_db
def test_idempotente(tjal_trib, sem_datajud):
    itens = [_it('0500371-61.2022.8.02.9003'), _it('0500028-42.2023.8.02.0043')]
    gravar_entidade(tjal_trib, itens, Placar())
    segunda = Placar()
    reclass = gravar_entidade(tjal_trib, itens, segunda)
    assert segunda.criados == {} and segunda.registros_novos == 0
    assert reclass == []                      # nada mudou, ninguém reclassifica
    assert Process.objects.count() == 2 and PrecatorioFilaOficial.objects.count() == 2


@pytest.mark.django_db
def test_saiu_da_fila_completa_abstem(tjal_trib, sem_datajud):
    origem = '0000028-53.2014.8.02.0031'
    Process.objects.create(tribunal=tjal_trib, numero_cnj=origem, classe_codigo='156')
    antes = timezone.now() - timedelta(days=1)
    gravar_entidade(tjal_trib, [_it(origem), _it('0500371-61.2022.8.02.9003')],
                    Placar(), agora=antes)
    assert Process.objects.get(numero_cnj=origem).classificacao == Process.CLASSIF_PRECATORIO
    placar = Placar()
    gravar_entidade(tjal_trib, [_it('0500371-61.2022.8.02.9003')], placar)
    n = marcar_fora_da_fila(tjal_trib, 'DELMIRO GOUVEIA', timezone.now() - timedelta(hours=1),
                            placar)
    assert n == 1
    assert PrecatorioFilaOficial.objects.get(chave=origem).situacao == 'fora_da_fila'
    # Sem a fila oficial, o processo de origem volta ao veredito de sempre (LR).
    assert Process.objects.get(numero_cnj=origem).classificacao != Process.CLASSIF_PRECATORIO
    # O 9003 continua: foi visto nesta coleta.
    assert PrecatorioFilaOficial.objects.get(
        chave='0500371-61.2022.8.02.9003').situacao == 'pendente'


@pytest.mark.django_db
def test_regra_so_no_tjal(db):
    t = Tribunal.objects.get_or_create(sigla='TJSP', defaults={'nome': 'TJSP'})[0]
    p = Process.objects.create(tribunal=t, numero_cnj='0000001-00.2020.8.26.0001',
                               classe_codigo='156')
    PrecatorioFilaOficial.objects.create(tribunal=t, chave=p.numero_cnj, processo=p,
                                         situacao='pendente', primeira_vez_em=timezone.now(),
                                         visto_em=timezone.now())
    assert classificar_e_persistir(p, registrar_log=False)[0] != Process.CLASSIF_PRECATORIO


def test_tabela_nao_tem_coluna_de_dado_pessoal():
    nomes = {f.name for f in PrecatorioFilaOficial._meta.get_fields()}
    assert not {n for n in nomes if any(x in n for x in ('cpf', 'documento', 'requerente', 'nome'))}


# ---------------------------------------------------------------------------
# Execução: dry-run não escreve; executar registra o run; retomada
# ---------------------------------------------------------------------------
class _ColetorFixo:
    def __init__(self, resultados):
        self.resultados, self.requisicoes = resultados, 7

    def iterar(self, entidades=None, pular=None, falhas=None):
        for r in self.resultados:
            if pular and pular(r.info['chave']):
                continue
            yield r


def _resultados(completa=True):
    return [ResultadoEntidade(entidade='DELMIRO GOUVEIA', completa=completa,
                              info={'chave': 'sapre:26'},
                              itens=[_it('0500371-61.2022.8.02.9003'),
                                     _it('0500028-42.2023.8.02.0043')])]


@pytest.fixture
def cache_limpo():
    from django.core.cache import cache
    cache.clear()
    yield
    cache.clear()


@pytest.mark.django_db
def test_dry_run_nao_escreve_nada(tjal_trib, sem_datajud, cache_limpo):
    linhas = []
    resumo = execucao.executar('TJAL', escrever=False, coletor=_ColetorFixo(_resultados()),
                               saida=linhas.append)
    assert resumo['criados'] == {'foro_9003': 1, 'comarca_050': 1}
    assert not Process.objects.exists()
    assert not PrecatorioFilaOficial.objects.exists()
    assert not IngestionRun.objects.exists()
    assert sem_datajud == []
    assert {linha['acao'] for linha in linhas} == {'criaria'}


@pytest.mark.django_db
def test_executar_registra_run_e_retoma(tjal_trib, sem_datajud, cache_limpo):
    resumo = execucao.executar('TJAL', escrever=True, coletor=_ColetorFixo(_resultados()))
    run = IngestionRun.objects.get(pk=resumo['run_id'])
    assert run.fonte == 'fila_oficial' and run.status == 'success'
    assert run.processos_novos == 2 and run.paginas_lidas == 7
    # Mesmo dia, retomando: a entidade concluída é pulada.
    de_novo = execucao.executar('TJAL', escrever=True, coletor=_ColetorFixo(_resultados()))
    assert de_novo['entidades'] == 0


@pytest.mark.django_db
def test_entidade_incompleta_nao_marca_fora_da_fila_e_falha_o_run(tjal_trib, sem_datajud,
                                                                    cache_limpo):
    antigo = timezone.now() - timedelta(days=2)
    gravar_entidade(tjal_trib, [_it('0500999-22.2022.8.02.9003')], Placar(), agora=antigo)
    resumo = execucao.executar('TJAL', escrever=True,
                               coletor=_ColetorFixo(_resultados(completa=False)))
    assert PrecatorioFilaOficial.objects.get(chave='0500999-22.2022.8.02.9003').situacao == 'pendente'
    assert IngestionRun.objects.get(pk=resumo['run_id']).status == 'failed'
    assert resumo['entidades_incompletas'] == ['DELMIRO GOUVEIA']


@pytest.mark.django_db
def test_watchdog_nao_mata_o_run_da_fila_oficial(tjal_trib):
    from djen import jobs as djen_jobs
    velho = timezone.now() - timedelta(hours=3)
    r = IngestionRun.objects.create(tribunal=tjal_trib, fonte='fila_oficial',
                                    janela_inicio=velho.date(), janela_fim=velho.date())
    IngestionRun.objects.filter(pk=r.pk).update(started_at=velho)
    djen_jobs._matar_zumbis(timezone.now())
    assert IngestionRun.objects.get(pk=r.pk).status == 'running'


# ---------------------------------------------------------------------------
# O caminho até o JURISCOPE: o pull pede PRECATORIO do TJAL com score ≥ 0,70
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_descoberto_pela_fila_e_servido_no_pull_do_tjal(tjal_trib, sem_datajud):
    ApiClient.objects.create(nome='juriscope-fila', api_key='k-fila', ativo=True)
    Process.objects.create(tribunal=tjal_trib, numero_cnj='0503003-55.2025.8.02.9003',
                           classe_codigo='1265', classe_nome='Precatório',
                           classificacao=Process.CLASSIF_PRECATORIO, classificacao_score=1.0,
                           ultima_movimentacao_em=datetime(2026, 10, 22, tzinfo=UTC))
    gravar_entidade(tjal_trib, [_it('0500371-61.2022.8.02.9003')], Placar())
    resp = Client().get('/api/v1/leads/?nivel=PRECATORIO&tribunal=TJAL&limit=10'
                        '&min_score=0.7', HTTP_X_API_KEY='k-fila')
    assert resp.status_code == 200
    cnjs = [r['cnj'] for r in resp.json()['results']]
    # Sem movimentação (`ultima_movimentacao_em` NULL), o descoberto vem
    # PRIMEIRO: o Postgres põe NULL na frente em ORDER BY ... DESC.
    assert cnjs == ['0500371-61.2022.8.02.9003', '0503003-55.2025.8.02.9003']
