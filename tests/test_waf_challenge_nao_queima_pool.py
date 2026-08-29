"""O desafio do AWS WAF não é do IP — e por isso não pode queimar o pool.

Regressão da pendência #100 (29/08/2026). O TJPE (`pje.cloud.tjpe.jus.br/1g/`)
está atrás de um AWS WAF cuja ação é `challenge`: HTTP 202 + `awsWafCookie` no
lugar do form JSF. O código tratava isso como bloqueio POR PROXY — rotacionava
`MAX_PROXY_ROTATIONS` (10) vezes e chamava `pool.mark_bad()` em cada uma.

O pool é COMPARTILHADO com a ingestão DJEN e com os outros 15 enrichers, então
cada job do TJPE tirava até 10 IPs de circulação por 120 s para gravar `erro`
do mesmo jeito. Censo de 10 min na `.102`: TJPE = 2.791 requisições e 1.379
`mark_bad` (o maior de todos os tribunais) para 7 `ok` e 129 `erro`.

Que trocar de IP não resolve foi medido, não suposto: 29 de 30 requisições pelo
Cortex residencial (IP novo a cada request) vieram desafiadas, e 16 de 20 pelo
IP direto do host de workers.
"""
import pytest
import requests

from enrichers.pje import BasePjeEnricher, PjeWafChallenge, _detectar_desafio_waf


class _RespFake:
    def __init__(self, status=202, headers=None, text=''):
        self.status_code = status
        self.headers = headers or {}
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class _PoolFake:
    """Pool com IPs infinitos: se o código quiser queimar, ele consegue."""

    def __init__(self):
        self.marcados: list[str] = []
        self._n = 0

    def get(self):
        self._n += 1
        return f'http://10.0.0.{self._n}:3128'

    def mark_bad(self, url):
        self.marcados.append(url)

    def cortex_is_bad(self):
        return False


class _SessaoFake:
    def __init__(self, resposta):
        self.resposta = resposta
        self.headers: dict = {}
        self.chamadas = 0

    def request(self, method, url, **kwargs):
        self.chamadas += 1
        return self.resposta


class _Enricher(BasePjeEnricher):
    BASE_URL = 'https://exemplo.jus.br'
    LIST_URL = 'https://exemplo.jus.br/1g/ConsultaPublica/listView.seam'
    DETALHE_PATH = '/1g/ConsultaPublica/DetalheProcessoConsultaPublica'
    TRIBUNAL_SIGLA = 'TJPE'


def _montar(resposta):
    e = _Enricher.__new__(_Enricher)
    e.pool = _PoolFake()
    e.session = _SessaoFake(resposta)
    e.timeout = (10, 60)
    import logging
    e.logger = logging.getLogger('teste.waf')
    e.prefer_cortex = False
    e.cortex_only = False
    return e


DESAFIO_HEADER = _RespFake(202, {'x-amzn-waf-action': 'challenge'}, '<html>ok</html>')
DESAFIO_CORPO = _RespFake(202, {}, '<script>window.awsWafCookieDomainList</script>')


@pytest.mark.parametrize('resposta', [DESAFIO_HEADER, DESAFIO_CORPO],
                         ids=['header-x-amzn-waf-action', 'corpo-awsWafCookie'])
def test_desafio_do_waf_nao_marca_nenhum_proxy_como_ruim(resposta, settings):
    settings.CORTEX_FALLBACK_ENABLED = False
    e = _montar(resposta)
    with pytest.raises(PjeWafChallenge):
        e._request_with_rotation('GET', e.LIST_URL)
    assert e.pool.marcados == [], (
        f'desafio do WAF queimou {len(e.pool.marcados)} IPs do pool compartilhado')


def test_desafio_do_waf_tem_teto_proprio_e_nao_gasta_as_10_rotacoes(settings):
    settings.CORTEX_FALLBACK_ENABLED = False
    e = _montar(DESAFIO_HEADER)
    with pytest.raises(PjeWafChallenge) as exc:
        e._request_with_rotation('GET', e.LIST_URL)
    assert e.session.chamadas == e.WAF_MAX_TENTATIVAS < e.MAX_PROXY_ROTATIONS
    # Teto é ALERTA com o número real, nunca corte mudo.
    assert 'aws_waf_challenge' in str(exc.value)
    assert str(e.WAF_MAX_TENTATIVAS) in str(exc.value)


def test_403_de_verdade_continua_queimando_o_proxy(settings):
    """O conserto é cirúrgico: 403/429 SEM assinatura de WAF é bloqueio do IP
    mesmo, e aí marcar como ruim é o comportamento certo — não pode ir junto."""
    settings.CORTEX_FALLBACK_ENABLED = False
    e = _montar(_RespFake(403, {}, '<html>Forbidden</html>'))
    with pytest.raises(requests.HTTPError):
        e._request_with_rotation('GET', e.LIST_URL)
    assert len(e.pool.marcados) == e.MAX_PROXY_ROTATIONS


def test_detector_nao_confunde_pagina_boa_que_cita_o_waf():
    """200 com conteúdo real não vira desafio só por conter a palavra."""
    boa = _RespFake(200, {}, '<html><input name="javax.faces.ViewState" '
                             'value="x"/><script src="/awswaf/x.js"></script></html>')
    assert _detectar_desafio_waf(boa) is False
    assert _detectar_desafio_waf(DESAFIO_HEADER) is True
    assert _detectar_desafio_waf(DESAFIO_CORPO) is True
