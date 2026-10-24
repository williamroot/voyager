"""Fila oficial de precatórios do TJMT (SRP): coletor, descoberta,
classificação e a trava do pull.

Os PDFs em `tests/fixtures/tjmt/` são relatórios REAIS do SRP baixados em
23/10/2026 (`GET /srp/requisicao-precatorio/<id>`). Não têm dado pessoal: o
relatório só traz CNJ do precatório, ente, situação e valores.

- 104 (Município de Planalto da Serra) e 173 (IMPAS, o fundo de previdência
  dele — `idOrgaoPagador=104`): o mesmo relatório pelas duas contas, com duas
  superpreferências em linha à parte (R$ 127.080,00 = a superpreferência do
  ente) e um "Pago parcialmente" com valor pago MAIOR que o requisitado.
- 14 (Município de Alto Paraguai): "Pagamento em Controverso" quebrado em duas
  linhas, "Provisionado" com R$ 0,00 / R$ 0,00 e "Autuado".
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from django.test import Client, override_settings
from django.utils import timezone

from api import leads_cache
from filas_oficiais import execucao
from filas_oficiais.base import ItemFila, ResultadoEntidade
from filas_oficiais.descoberta import Placar, gravar_entidade, marcar_fora_da_fila
from filas_oficiais.fontes import tjmt
from tribunals.classificador import classificar_e_persistir
from tribunals.models import (
    ApiClient,
    ClassificacaoLog,
    IngestionRun,
    PrecatorioFilaOficial,
    Process,
    Tribunal,
)

FIX = Path(__file__).parent / 'fixtures' / 'tjmt'
PDF_104 = (FIX / 'srp_104_planalto_da_serra.pdf').read_bytes()
PDF_173 = (FIX / 'srp_173_impas_planalto.pdf').read_bytes()
PDF_14 = (FIX / 'srp_14_alto_paraguai.pdf').read_bytes()

D = Decimal


# ---------------------------------------------------------------------------
# Parser do relatório (PyMuPDF por coordenadas)
# ---------------------------------------------------------------------------
def test_relatorio_planalto_linha_a_linha():
    rel = tjmt.ler_relatorio(PDF_104)
    assert rel.sequencia_ok and not rel.rejeitadas
    assert [x.ordem for x in rel.linhas] == [1, 2, 3, 4, 5]
    um = rel.linhas[0]
    assert um.cnj == '1037270-24.2024.8.11.0000'
    assert um.situacao_raw == 'Aguardando Pagamento'
    assert (um.natureza, um.preferencia, um.exercicio) == ('ALIMENTAR', 'Idade', 2026)
    assert (um.valor_requisitado, um.valor_pago) == (D('127080.00'), D('0.00'))
    assert um.devedor.startswith('MUNICÍPIO DE')
    dois = rel.linhas[1]
    assert dois.situacao_raw == 'Pago parcialmente'
    assert (dois.valor_requisitado, dois.valor_pago) == (D('44836.13'), D('59629.52'))
    assert rel.gerado_em.startswith('23/10/2026')


def test_superpreferencia_em_linha_a_parte_e_somada():
    """Os 375 CNJs "divergentes" eram isto: a parcela superpreferencial
    (valor = `valorSuperpreferenciaCalculoPagamento` do ente) e o restante,
    no MESMO relatório."""
    agrup = tjmt.agrupar_relatorio(tjmt.ler_relatorio(PDF_104).linhas)
    assert set(agrup) == {'1037270-24.2024.8.11.0000', '1037269-39.2024.8.11.0000',
                          '1013636-28.2026.8.11.0000'}
    r = agrup['1037270-24.2024.8.11.0000']
    assert r['n_itens'] == 2 and r['ordem'] == 1 and r['preferencia'] == 'Idade'
    assert r['valor_requisitado'] == D('127080.00') + D('257917.80')
    assert agrup['1013636-28.2026.8.11.0000']['valor_requisitado'] == D('154009.10')


def test_situacao_em_duas_linhas_e_provisionado():
    rel = tjmt.ler_relatorio(PDF_14)
    assert rel.sequencia_ok and len(rel.linhas) == 27 and not rel.rejeitadas
    por_ordem = {x.ordem: x for x in rel.linhas}
    assert por_ordem[1].situacao_raw == 'Pagamento em Controverso'
    assert por_ordem[1].cnj == '1014225-59.2022.8.11.0000'
    assert por_ordem[2].situacao_raw == 'Provisionado'
    assert (por_ordem[2].valor_requisitado, por_ordem[2].valor_pago) == (D('0'), D('0'))


def test_pdf_vazio_e_ente_sem_itens():
    rel = tjmt.ler_relatorio(b'')
    assert rel.linhas == [] and rel.sequencia_ok


def test_pdf_que_nao_e_o_relatorio_falha_alto():
    import fitz
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), 'Manutenção programada')
    with pytest.raises(tjmt.LayoutDesconhecido):
        tjmt.ler_relatorio(doc.tobytes())


@pytest.mark.parametrize('raw,saldo,esperado', [
    ('Aguardando Pagamento', D('10.00'), 'pendente'),
    ('Pagamento em Controverso', D('1.00'), 'pendente'),
    ('Em Trâmite de Quitação', D('5.00'), 'pendente'),
    ('Aguardando Pagamento', D('0.00'), 'outro'),         # sem saldo
    ('Pago parcialmente', D('-14793.39'), 'outro'),       # pago > requisitado
    ('Provisionado', D('0.00'), 'outro'),
    ('Situação Nova Qualquer', D('100.00'), 'outro'),     # abster > chutar
])
def test_pendente_e_situacao_de_fila_com_saldo(raw, saldo, esperado):
    assert tjmt.situacao_srp(raw, saldo) == esperado


# ---------------------------------------------------------------------------
# Coletor: grupo pagador, dedupe, completude, carga
# ---------------------------------------------------------------------------
ORGAOS = [
    {'id': 2, 'nome': 'IBAMA', 'idOrgaoPagador': None, 'codigoOpcaoRegimePagamento': 2},
    {'id': 14, 'nome': 'MUNICÍPIO DE ALTO PARAGUAI', 'idOrgaoPagador': None,
     'codigoOpcaoRegimePagamento': 5},
    {'id': 104, 'nome': 'MUNICÍPIO DE PLANALTO DA SERRA', 'idOrgaoPagador': None,
     'codigoOpcaoRegimePagamento': 2},
    {'id': 173, 'nome': 'FUNDO MUNICIPAL DE PREVIDENCIA - IMPAS', 'idOrgaoPagador': 104,
     'codigoOpcaoRegimePagamento': 2},
]


class _Resp:
    def __init__(self, status=200, content=b'', js=None):
        self.status_code, self.content, self._js = status, content, js

    def json(self):
        return self._js

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _Sessao:
    def __init__(self, pdfs=None, orgaos=None, total=None, falhar=()):
        self.headers, self.urls = {}, []
        self.pdfs = pdfs if pdfs is not None else {104: PDF_104, 173: PDF_173, 14: PDF_14}
        self.orgaos = orgaos if orgaos is not None else ORGAOS
        self.total = total
        self.falhar = set(falhar)

    def get(self, url, timeout=None):
        self.urls.append(url)
        if url.endswith('/orgaos-devedores'):
            return _Resp(js={'content': self.orgaos,
                             'totalElements': self.total or len(self.orgaos)})
        oid = int(url.rsplit('/', 1)[1])
        if oid in self.falhar:
            return _Resp(status=503)
        return _Resp(content=self.pdfs.get(oid, b''))


def _coletor(sessao=None):
    pausas = []
    c = tjmt.ColetorTjmt(session=sessao or _Sessao(), dormir=pausas.append)
    return c, pausas


def test_grupos_pelo_pagador():
    g = tjmt.ColetorTjmt.grupos(ORGAOS)
    assert [o['id'] for o in g[104]] == [104, 173]     # o pagador primeiro
    assert set(g) == {2, 14, 104}


def test_coletor_deduplica_o_grupo_e_marca_completo():
    c, pausas = _coletor()
    res = {r.info['pagador']: r for r in c.iterar()}
    assert set(res) == {2, 14, 104}
    planalto = res[104]
    assert planalto.completa and planalto.entidade == 'PLANALTO DA SERRA'
    assert set(planalto.info['relatorios']) == {104, 173}
    assert planalto.info['divergentes'] == 0 and not planalto.falhas
    itens = {i.chave: i for i in planalto.itens}
    assert len(itens) == 3                               # 10 linhas, 2 relatórios
    um = itens['1037270-24.2024.8.11.0000']
    assert (um.situacao, um.fonte, um.n_itens) == ('pendente', 'tjmt_srp', 2)
    assert um.valor_original == D('384997.80') and um.valor_pago == D('0.00')
    assert um.ano_orcamento == 2026 and um.natureza == 'ALIMENTAR'
    assert itens['1037269-39.2024.8.11.0000'].situacao == 'outro'   # pago > requisitado
    assert res[2].completa and res[2].itens == []        # 0 bytes = ente sem itens
    assert res[14].completa and len(res[14].itens) == 27
    # 1 lista de entes + 4 relatórios, uma requisição por vez, pausa >= 1 s.
    assert c.requisicoes == 5 and len(pausas) == 5 and min(pausas) >= 1.0


def test_relatorio_que_falha_deixa_o_grupo_incompleto():
    c, _ = _coletor(_Sessao(falhar={173}))
    planalto = next(r for r in c.iterar() if r.info['pagador'] == 104)
    assert not planalto.completa
    assert len(planalto.itens) == 3                      # o que veio é entregue
    assert any('173' in f for f in planalto.falhas)


def test_lista_de_entes_truncada_e_erro_nao_corte():
    c, _ = _coletor(_Sessao(total=999))
    falhas = []
    assert list(c.iterar(falhas=falhas)) == []
    assert falhas and 'truncado' in falhas[0]


def test_restringe_por_ente_e_retoma():
    c, _ = _coletor()
    assert [r.info['pagador'] for r in c.iterar(entidades=['173'])] == [104]
    c, _ = _coletor()
    assert [r.info['pagador'] for r in c.iterar(pular=lambda k: k != 'srp:14')] == [14]


def test_pausa_minima_de_um_segundo():
    assert tjmt.ColetorTjmt(session=_Sessao(), pausa=0.1).pausa == 1.0


# ---------------------------------------------------------------------------
# Descoberta + classificação (banco)
# ---------------------------------------------------------------------------
@pytest.fixture
def tjmt_trib(db):
    return Tribunal.objects.get_or_create(sigla='TJMT', defaults={'nome': 'TJMT'})[0]


def _it(chave, situacao='pendente', entidade='PLANALTO DA SERRA', **kw):
    kw.setdefault('valor_original', D('1000.00'))
    kw.setdefault('valor_pago', D('0.00'))
    return ItemFila(chave=chave, fonte='tjmt_srp', entidade=entidade,
                    situacao=situacao, situacao_raw=kw.pop('raw', 'Aguardando Pagamento'),
                    **kw)


def _1265(trib, cnj, **kw):
    kw.setdefault('classificacao', Process.CLASSIF_NAO_LEAD)
    kw.setdefault('classificacao_score', 0.05)
    return Process.objects.create(tribunal=trib, numero_cnj=cnj, classe_codigo='1265',
                                  classe_nome='Precatório', fase_codigo='1265', **kw)


@pytest.mark.django_db
def test_pendente_que_nunca_publicou_nasce_precatorio_2g(tjmt_trib):
    placar = Placar()
    gravar_entidade(tjmt_trib, [_it('1013636-28.2026.8.11.0000', preferencia='Idade',
                                    valor_original=D('154009.10'), n_itens=2)], placar)
    p = Process.objects.get(numero_cnj='1013636-28.2026.8.11.0000')
    assert (p.tribunal_id, p.classe_codigo, p.classe_nome, p.grau) == (
        'TJMT', '1265', 'Precatório', 'G2')
    assert p.classificacao == Process.CLASSIF_PRECATORIO and p.classificacao_score == 1.0
    reg = PrecatorioFilaOficial.objects.get(chave=p.numero_cnj)
    assert reg.processo_id == p.pk and reg.situacao == 'pendente'
    assert (reg.valor_original, reg.valor_pago, reg.preferencia, reg.n_itens) == (
        D('154009.10'), D('0.00'), 'Idade', 2)
    assert placar.criados == {'tjmt_2g': 1}


@pytest.mark.django_db
def test_caso1_1265_na_fila_com_saldo_vira_precatorio(tjmt_trib):
    p = _1265(tjmt_trib, '1037270-24.2024.8.11.0000')
    placar = Placar()
    gravar_entidade(tjmt_trib, [_it(p.numero_cnj)], placar)
    p.refresh_from_db()
    assert p.classificacao == Process.CLASSIF_PRECATORIO and p.classificacao_score == 1.0
    assert placar.criados == {} and placar.ja_no_acervo == {'NAO_LEAD': 1}


@pytest.mark.django_db
def test_caso2_saiu_da_fila_sem_pagamento_rebaixa_e_anota(tjmt_trib):
    p = _1265(tjmt_trib, '1037270-24.2024.8.11.0000')
    fica = '1013636-28.2026.8.11.0000'
    ontem = timezone.now() - timedelta(days=1)
    gravar_entidade(tjmt_trib, [_it(p.numero_cnj), _it(fica)], Placar(), agora=ontem)
    p.refresh_from_db()
    assert p.classificacao == Process.CLASSIF_PRECATORIO
    # Coleta completa de hoje: só `fica` veio.
    inicio = timezone.now()
    placar = Placar()
    gravar_entidade(tjmt_trib, [_it(fica)], placar, agora=inicio)
    assert marcar_fora_da_fila(tjmt_trib, 'PLANALTO DA SERRA', inicio, placar) == 1
    p.refresh_from_db()
    assert p.classificacao == Process.CLASSIF_NAO_LEAD and p.classificacao_score == 0.10
    reg = PrecatorioFilaOficial.objects.get(chave=p.numero_cnj)
    assert reg.situacao == 'fora_da_fila' and reg.saiu_em is not None
    assert 'sem pagamento' in reg.motivo_saida and 'PLANALTO DA SERRA' in reg.motivo_saida
    log = ClassificacaoLog.objects.filter(processo=p).order_by('-pk').first()
    assert log.classificacao == Process.CLASSIF_NAO_LEAD
    assert log.features_snapshot.get('FILA_OFICIAL_saiu_sem_pagamento_ANTI') == 1
    # Nunca removido: processo e registro continuam.
    assert Process.objects.filter(pk=p.pk).exists()
    assert PrecatorioFilaOficial.objects.get(chave=fica).situacao == 'pendente'
    # Voltou à fila: promove de novo e a marca de saída é limpa.
    gravar_entidade(tjmt_trib, [_it(p.numero_cnj)], Placar())
    p.refresh_from_db()
    reg.refresh_from_db()
    assert p.classificacao == Process.CLASSIF_PRECATORIO
    assert reg.situacao == 'pendente' and reg.saiu_em is None and reg.motivo_saida == ''


@pytest.mark.django_db
def test_caso3_1265_fora_da_fila_nao_e_promovido(tjmt_trib):
    """Os 12.559 processos 1265 do TJMT que não estão na fila SRP."""
    p = _1265(tjmt_trib, '1004350-31.2023.8.11.0000')
    gravar_entidade(tjmt_trib, [_it('1037270-24.2024.8.11.0000')], Placar())
    cat, _ = classificar_e_persistir(p, registrar_log=False)
    assert cat != Process.CLASSIF_PRECATORIO
    assert not PrecatorioFilaOficial.objects.filter(processo=p).exists()


@pytest.mark.django_db
def test_na_fila_sem_saldo_nao_cria_e_rebaixa_quem_era_precatorio(tjmt_trib):
    ja = _1265(tjmt_trib, '1037269-39.2024.8.11.0000',
               classificacao=Process.CLASSIF_PRECATORIO, classificacao_score=1.0)
    gravar_entidade(tjmt_trib, [
        _it(ja.numero_cnj, situacao='outro', raw='Pago parcialmente',
            valor_original=D('44836.13'), valor_pago=D('59629.52')),
        _it('1026233-34.2023.8.11.0000', situacao='outro', raw='Provisionado',
            valor_original=D('0'), valor_pago=D('0')),
    ], Placar())
    ja.refresh_from_db()
    assert ja.classificacao == Process.CLASSIF_NAO_LEAD
    assert not Process.objects.filter(numero_cnj='1026233-34.2023.8.11.0000').exists()
    log = ClassificacaoLog.objects.filter(processo=ja).order_by('-pk').first()
    assert log.features_snapshot.get('FILA_OFICIAL_sem_saldo_ANTI') == 1


@pytest.mark.django_db
def test_protocolo_fora_do_2o_grau_nao_cria(tjmt_trib):
    placar = Placar()
    gravar_entidade(tjmt_trib, [_it('1000418-65.2023.8.11.0087')], placar)
    assert not Process.objects.exists() and placar.falhas == 1


@pytest.mark.django_db
def test_tjal_continua_se_abstendo_na_saida(db):
    t = Tribunal.objects.get_or_create(sigla='TJAL', defaults={'nome': 'TJAL'})[0]
    p = Process.objects.create(tribunal=t, numero_cnj='0500371-61.2022.8.02.9003',
                               classe_codigo='1265')
    PrecatorioFilaOficial.objects.create(tribunal=t, chave=p.numero_cnj, processo=p,
                                         situacao='fora_da_fila',
                                         primeira_vez_em=timezone.now(),
                                         visto_em=timezone.now())
    from tribunals import classificador
    _, _, feats = classificador.classificar(p)
    assert 'FILA_OFICIAL_saiu_sem_pagamento_ANTI' not in feats


# ---------------------------------------------------------------------------
# Execução: dry-run, run, varredura do tribunal inteiro
# ---------------------------------------------------------------------------
class _ColetorFixo:
    cobertura_total = True

    def __init__(self, resultados):
        self.resultados, self.requisicoes = resultados, 3

    def iterar(self, entidades=None, pular=None, falhas=None):
        for r in self.resultados:
            if pular and pular(r.info['chave']):
                continue
            yield r


def _res(itens, entidade='PLANALTO DA SERRA', completa=True):
    return ResultadoEntidade(entidade=entidade, itens=itens, completa=completa,
                             info={'chave': f'srp:{entidade}'})


@pytest.fixture
def cache_limpo():
    from django.core.cache import cache
    cache.clear()
    yield
    cache.clear()


@pytest.mark.django_db
def test_dry_run_mede_sem_escrever(tjmt_trib, cache_limpo):
    _1265(tjmt_trib, '1037270-24.2024.8.11.0000')
    linhas = []
    resumo = execucao.executar('TJMT', escrever=False, saida=linhas.append, coletor=_ColetorFixo(
        [_res([_it('1037270-24.2024.8.11.0000'), _it('1013636-28.2026.8.11.0000'),
               _it('1026233-34.2023.8.11.0000', situacao='outro')])]))
    assert resumo['criados'] == {'tjmt_2g': 1}
    assert resumo['ja_no_acervo'] == {'NAO_LEAD': 1}
    assert Process.objects.count() == 1
    assert not PrecatorioFilaOficial.objects.exists() and not IngestionRun.objects.exists()
    assert {x['acao'] for x in linhas} == {'criaria', 'ja_no_acervo', 'nao_pendente'}


@pytest.mark.django_db
def test_executar_varre_o_tribunal_quando_tudo_veio(tjmt_trib, cache_limpo):
    """Ente que sumiu do cadastro do SRP (ou mudou de nome): o pendente dele
    não é visto por entidade nenhuma — sai da fila na varredura final."""
    antigo = timezone.now() - timedelta(days=2)
    gravar_entidade(tjmt_trib, [_it('1030521-25.2023.8.11.0000', entidade='NOME ANTIGO')],
                    Placar(), agora=antigo)
    resumo = execucao.executar('TJMT', escrever=True, coletor=_ColetorFixo(
        [_res([_it('1013636-28.2026.8.11.0000')])]))
    assert IngestionRun.objects.get(pk=resumo['run_id']).status == 'success'
    reg = PrecatorioFilaOficial.objects.get(chave='1030521-25.2023.8.11.0000')
    assert reg.situacao == 'fora_da_fila' and 'do tribunal' in reg.motivo_saida
    assert Process.objects.get(numero_cnj=reg.chave).classificacao == Process.CLASSIF_NAO_LEAD


@pytest.mark.django_db
def test_sem_varredura_com_grupo_incompleto_ou_recorte(tjmt_trib, cache_limpo):
    antigo = timezone.now() - timedelta(days=2)
    gravar_entidade(tjmt_trib, [_it('1030521-25.2023.8.11.0000', entidade='NOME ANTIGO')],
                    Placar(), agora=antigo)
    execucao.executar('TJMT', escrever=True, coletor=_ColetorFixo(
        [_res([_it('1013636-28.2026.8.11.0000')], completa=False)]))
    execucao.executar('TJMT', escrever=True, retomar=False, entidades=['104'],
                      coletor=_ColetorFixo([_res([_it('1013636-28.2026.8.11.0000')])]))
    assert PrecatorioFilaOficial.objects.get(
        chave='1030521-25.2023.8.11.0000').situacao == 'pendente'


# ---------------------------------------------------------------------------
# TRAVA do pull: TJMT_PRECATORIO_2G_NO_PULL
# ---------------------------------------------------------------------------
@pytest.fixture
def redis_limpo():
    conn = leads_cache._conn()

    def _limpar():
        for k in list(conn.scan_iter('leads:*')):
            conn.delete(k)
    _limpar()
    yield
    _limpar()


@pytest.fixture
def cenario_pull(tjmt_trib, redis_limpo):
    ApiClient.objects.create(nome='juriscope-tjmt', api_key='k-tjmt', ativo=True)
    # 1º grau, PRECATORIO por sinal (o que o JURISCOPE já lê): nunca travado.
    Process.objects.create(tribunal=tjmt_trib, numero_cnj='1000418-65.2023.8.11.0087',
                           classe_codigo='12078', classificacao=Process.CLASSIF_PRECATORIO,
                           classificacao_score=1.0,
                           ultima_movimentacao_em=datetime(2026, 10, 22, tzinfo=UTC))
    _1265(tjmt_trib, '1037270-24.2024.8.11.0000')
    gravar_entidade(tjmt_trib, [_it('1037270-24.2024.8.11.0000'),
                                _it('1013636-28.2026.8.11.0000')], Placar())
    assert Process.objects.filter(classificacao=Process.CLASSIF_PRECATORIO).count() == 3


def _pull(tribunal='TJMT'):
    resp = Client().get(f'/api/v1/leads/?nivel=PRECATORIO&tribunal={tribunal}&limit=50'
                        '&min_score=0.7', HTTP_X_API_KEY='k-tjmt')
    assert resp.status_code == 200
    return resp.json()


@pytest.mark.django_db
@override_settings(TJMT_PRECATORIO_2G_NO_PULL=False)
def test_trava_desligada_nao_serve_o_2g_da_fila(cenario_pull):
    assert [r['cnj'] for r in _pull()['results']] == ['1000418-65.2023.8.11.0087']
    assert [r['cnj'] for r in _pull('')['results']] == ['1000418-65.2023.8.11.0087']
    # Continuam PRECATORIO e contados (gate/stats/tela veem).
    stats = Client().get('/api/v1/leads/stats/', HTTP_X_API_KEY='k-tjmt').json()
    assert stats['pending']['PRECATORIO'] == 3


@pytest.mark.django_db
@override_settings(TJMT_PRECATORIO_2G_NO_PULL=True)
def test_trava_ligada_serve_o_2g_da_fila(cenario_pull):
    assert {r['cnj'] for r in _pull()['results']} == {
        '1000418-65.2023.8.11.0087', '1037270-24.2024.8.11.0000',
        '1013636-28.2026.8.11.0000'}


@pytest.mark.django_db
def test_trava_vale_tambem_para_o_cache_calculado_com_ela_aberta(cenario_pull):
    cliente = ApiClient.objects.get(api_key='k-tjmt')
    combo = leads_cache.chave_combo('PRECATORIO', 'TJMT', 0.7, False)
    with override_settings(TJMT_PRECATORIO_2G_NO_PULL=True):
        assert leads_cache.precomputar_combo(cliente.pk, combo)['ids'] == 3
    with override_settings(TJMT_PRECATORIO_2G_NO_PULL=False, LEADS_CACHE_ENABLED=True):
        corpo = _pull()
    assert corpo['fonte'] == 'cache'
    assert [r['cnj'] for r in corpo['results']] == ['1000418-65.2023.8.11.0087']


def test_trava_nao_toca_outros_tribunais():
    with override_settings(TJMT_PRECATORIO_2G_NO_PULL=False):
        assert leads_cache.trava_tjmt_2g_ativa('TJMT') and leads_cache.trava_tjmt_2g_ativa('')
        assert not leads_cache.trava_tjmt_2g_ativa('TJAL')
    with override_settings(TJMT_PRECATORIO_2G_NO_PULL=True):
        assert not leads_cache.trava_tjmt_2g_ativa('TJMT')


def test_resumo_serializavel():
    """O resumo do run vai para o log e para o `--json` do comando."""
    json.dumps(Placar().como_dict())
