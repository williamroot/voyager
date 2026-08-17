"""Transporte do DEJT: postback JSF 1.2 + conversa Seam sobre JBoss 4.3.0.GA.

POR QUE ISTO NÃO É UM CLIENTE HTTP NOVO
=======================================
Backoff, rotação/fixação de proxy, circuit-breaker por fonte, kill switch e
rate limit auto-imposto já estão em `diarios/base.py::SessaoDiario` — que por
sua vez é a mecânica do `djen/client.py`, paga em incidente (2026-07-10: nós
éramos parte da sobrecarga). Aqui só mora o que é do DEJT e de mais ninguém: o
protocolo de formulário com estado.

O que a sonda de 16/08/2026 mediu e que este arquivo codifica:

  · Não existe GET para o PDF. São 3 passos: GET da tela (pega JSESSIONID +
    `javax.faces.ViewState`) → POST da busca → POST do clique no link.
  · O ViewState da busca é REUSÁVEL: os 25 cadernos de um dia (519 MB) saíram
    sequencialmente sem refazer a busca.
  · A sessão é sticky no ALB (o JSESSIONID carrega o backend:
    `...dejt-ip-10-200-16-194.production.aws`). Trocar de IP no meio do fluxo
    quebra a conversa. Se um dia for preciso proxy, tem que ser `PROXY_PRESO`
    (um IP do começo ao fim) — o padrão de rotação por request do DJEN é
    ANTI-padrão aqui. Hoje não é preciso: o DEJT respondeu 200 a um burst de 40
    buscas vindas de IP de datacenter (AS28666), sem um 403/429.
  · Na tela de matéria, postar na URL de RESPOSTA (que traz
    `?conversationPropagation=nest&conversationId=N`) abre conversa ANINHADA no
    Seam e devolve o formulário em branco com HTTP 200 — 3 tentativas perdidas
    na sonda. Por isso `URL_AVANCADA` é usada limpa no clique de item.
  · Não há `robots.txt` (404) e não há rate limit. O servidor não vai nos
    defender de nós mesmos: o teto é o `rps` do coletor, e ele é baixo de
    propósito.
"""

import logging
import re

from diarios.base import RespostaInvalida, SessaoDiario, exigir_pdf

from .catalogo import TIPO_CADERNO, conferir_eco

logger = logging.getLogger('voyager.diarios.dejt')

BASE = 'https://dejt.jt.jus.br/dejt'
URL_DIARIOCON = f'{BASE}/f/n/diariocon'
URL_AVANCADA = f'{BASE}/f/n/materiapublicadacon'

RE_VIEWSTATE = re.compile(r'name="javax\.faces\.ViewState" value="([^"]*)"')

#: Um PDF de caderno-dia grande (TRT2 de 2024) passa de 100 MB. O timeout de
#: leitura do `SessaoDiario` (180 s) cobre; o mínimo de bytes abaixo é só para
#: pegar o HTML de erro disfarçado de download.
MIN_BYTES_CADERNO = 20 * 1024

#: ASN.1 SEQUENCE — primeiro byte de um envelope PKCS#7/CMS. Ver
#: `desembrulhar_assinatura`.
_ASN1_SEQUENCE = 0x30
#: o `%PDF` do caderno de 2010 aparece no byte 68 do envelope. A folga cobre
#: variação de tamanho do cabeçalho ASN.1 sem virar "procure PDF em qualquer
#: lugar do arquivo".
_LIMITE_CABECALHO_CMS = 4096


def desembrulhar_assinatura(corpo: bytes) -> bytes:
    """Tira o envelope PKCS#7 dos cadernos antigos.

    ISTO É DERIVA DE FORMATO MEDIDA, não teoria. Baixando o caderno do TRT22 em
    seis épocas (16/08/2026):

        10/03/2010  30 83 0e 41 e4 06 09 2a ...  → CMS, %PDF no byte 68
        12/03/2012  30 83 0d 2d 82 06 09 2a ...  → CMS, %PDF no byte 68
        11/03/2014  25 50 44 46 2d 31 2e 34      → %PDF cru
        2016/2018/2020/2022/2023/2024            → %PDF cru

    Até algum ponto entre 2012 e 2014 o CSJT servia o caderno **assinado
    digitalmente**: o PDF vem embrulhado num `signedData` (OID
    1.2.840.113549.1.7.2), com o Content-Type mentindo `application/pdf` e o
    Content-Disposition dizendo `Diario_436__10_3_2010.pdf`. Ou seja: HTTP 200,
    header de PDF, e bytes que não são PDF — o mesmo padrão de falso-positivo
    que este coletor persegue em todo lugar. São ~14 mil edições de 2008-2012
    (as de título 'Jurídico' no inventário) que ficariam de fora em silêncio.

    O recorte é literal e conferível: do primeiro `%PDF` até o último `%%EOF`
    (a cauda de 1.857 bytes depois dele é a assinatura, e deixá-la atrapalha o
    `startxref` que o leitor de PDF procura a partir do fim). NÃO validamos a
    assinatura — não é o que este coletor faz, e o `/dejt/f/n/autenticacaodiariocon`
    do próprio DEJT está quebrado (HTTP 599).
    """
    if not corpo or corpo[:4] == b'%PDF':
        return corpo
    if corpo[0] != _ASN1_SEQUENCE:
        return corpo
    inicio = corpo.find(b'%PDF', 0, _LIMITE_CABECALHO_CMS)
    if inicio < 0:
        return corpo
    fim = corpo.rfind(b'%%EOF')
    miolo = corpo[inicio:fim + 5] if fim > inicio else corpo[inicio:]
    logger.info('caderno vinha assinado (PKCS#7): %d bytes de envelope removidos',
                len(corpo) - len(miolo))
    return miolo


class SessaoJSF:
    """Uma conversa com o DEJT. Descartável: se o ViewState expirar, jogue fora
    e crie outra (é 1 GET), nunca tente consertar no meio."""

    def __init__(self, http: SessaoDiario):
        self.http = http
        self.viewstate: str | None = None

    # -- passo 1 --------------------------------------------------------------
    def abrir(self, caderno: str = 'J') -> str:
        """GET da tela de cadernos: nasce o JSESSIONID e o primeiro ViewState."""
        resp = self.http.get(f'{URL_DIARIOCON}?pesquisacaderno={caderno}&evento=y')
        html_ = resp.text
        self.viewstate = self._viewstate(html_, contexto='GET diariocon')
        return html_

    def _viewstate(self, html_: str, contexto: str) -> str:
        m = RE_VIEWSTATE.search(html_ or '')
        if not m:
            raise RespostaInvalida(
                f'{contexto}: sem javax.faces.ViewState ({len(html_ or "")} bytes) — '
                'a tela não é o formulário esperado'
            )
        return m.group(1)

    def _form_cadernos(self, data_ini: str, data_fim: str, tribunal_idx: object,
                       caderno: str, source: str) -> dict:
        """Os campos que o `diariocon` exige. Chaves verbatim da sonda —
        faltando qualquer uma o JSF ignora o postback e redevolve a tela."""
        return {
            'corpo:formulario': 'corpo:formulario',
            'corpo:formulario:tipoCaderno': TIPO_CADERNO[caderno],
            'corpo:formulario:dataIni': data_ini,
            'corpo:formulario:dataFim': data_fim,
            'corpo:formulario:tribunal': '' if tribunal_idx in ('', None) else str(tribunal_idx),
            'corpo:formulario:ordenacaoPlc': '',
            'org.apache.myfaces.trinidad.faces.FORM': 'corpo:formulario',
            '_noJavaScript': 'false',
            'javax.faces.ViewState': self.viewstate or '',
            'source': source,
        }

    # -- passo 2 --------------------------------------------------------------
    def buscar(self, data_ini: str, data_fim: str, tribunal_idx: object = '',
               caderno: str = 'J') -> str:
        """POST da busca de cadernos. Datas em dd/MM/yyyy (formato do form).

        Devolve o HTML JÁ CONFERIDO pelo eco — quem chama pode confiar que a
        tabela é do período pedido.
        """
        if self.viewstate is None:
            self.abrir(caderno)
        dados = self._form_cadernos(data_ini, data_fim, tribunal_idx, caderno,
                                    'corpo:formulario:botaoAcaoPesquisar')
        resp = self.http.post(URL_DIARIOCON, data=dados,
                              headers={'Referer': URL_DIARIOCON})
        html_ = resp.text
        conferir_eco(html_, data_ini=data_ini, tribunal_idx=tribunal_idx,
                     contexto=f'busca {data_ini}→{data_fim} trib={tribunal_idx!r}')
        # O ViewState novo (quando vem) é o válido daqui pra frente; se a
        # resposta não trouxer, o anterior segue valendo — foi o que a sonda viu
        # ao baixar 25 cadernos com o mesmo ViewState.
        m = RE_VIEWSTATE.search(html_)
        if m:
            self.viewstate = m.group(1)
        return html_

    # -- passo 3 --------------------------------------------------------------
    def baixar_caderno(self, source: str, *, data_ini: str, data_fim: str,
                       tribunal_idx: object, caderno: str = 'J') -> bytes:
        """POST do clique no link da linha → `application/pdf`.

        `source` tem que vir da resposta de busca ATUAL (ele contém um `j_id`
        gerado pelo JSF, volátil entre deploys). Validamos por MAGIC BYTES, não
        por status code: caderno inexistente e view expirada devolvem 200 com
        HTML.
        """
        dados = self._form_cadernos(data_ini, data_fim, tribunal_idx, caderno, source)
        resp = self.http.post(URL_DIARIOCON, data=dados,
                              headers={'Referer': URL_DIARIOCON})
        corpo = desembrulhar_assinatura(resp.content)
        ctype = (resp.headers.get('content-type') or '').lower()
        if 'pdf' not in ctype and not corpo.lstrip()[:5].startswith(b'%PDF'):
            raise RespostaInvalida(
                f'download devolveu content-type={ctype!r} com {len(corpo)} bytes '
                '(o DEJT responde HTML quando a view expira)'
            )
        return exigir_pdf(corpo, min_bytes=MIN_BYTES_CADERNO, contexto=f'caderno {source}')

    # -- gabarito (pesquisa avançada) ----------------------------------------
    def abrir_pesquisa_avancada(self, data_ini: str, data_fim: str,
                                caderno: str = 'J'):
        """Chega em `materiapublicadacon` pelo BOTÃO da tela de cadernos.

        Entrar pela URL direta perde o `conversationId` do Seam e o JBoss
        devolve 'Erro 599'. É por isso que este passo existe em vez de um GET.
        """
        self.buscar(data_ini, data_fim, '', caderno)
        dados = self._form_cadernos(data_ini, data_fim, '', caderno,
                                    'corpo:formulario:botaoPesquisaAvancada')
        resp = self.http.post(URL_DIARIOCON, data=dados,
                              headers={'Referer': URL_DIARIOCON})
        self.viewstate = self._viewstate(resp.text, contexto='pesquisa avançada')
        return resp

    def pesquisar_materias(self, resp_form, *, data_ini: str, data_fim: str,
                           tribunal_idx: object, caderno: str = 'J') -> str:
        """Busca de matérias — usada SÓ como gabarito de contagem.

        Nunca como via de ingestão: são 20 itens por página, ou seja 836
        requisições de listagem para o TRT3 de um único dia, contra 1 download
        de PDF.

        A data é obrigatória ('Data inicial é obrigatório(a)') e o tribunal é
        obrigatório na prática.
        """
        dados = {
            'corpo:formulario': 'corpo:formulario',
            'corpo:formulario:tipoCaderno': TIPO_CADERNO[caderno],
            'corpo:formulario:dataPublicacaoINI': data_ini,
            'corpo:formulario:dataPublicacaoFIM': data_fim,
            'corpo:formulario:tribunal': str(tribunal_idx),
            'corpo:formulario:cmbUnidadePublicadora':
                'org.jboss.seam.ui.NoSelectionConverter.noSelectionValue',
            'corpo:formulario:cmbTipoMateria': '',
            'corpo:formulario:numeroProcesso': '',
            'corpo:formulario:adv': '',
            'corpo:formulario:orderByUsuario': 'disponibilizacao',
            'corpo:formulario:ordenacaoPlc': '',
            'navDe': '',
            'org.apache.myfaces.trinidad.faces.FORM': 'corpo:formulario',
            '_noJavaScript': 'false',
            'javax.faces.ViewState': self._viewstate(resp_form.text, contexto='form de matéria'),
            'source': 'corpo:formulario:botaoAcaoPesquisar',
        }
        # Aqui o POST vai para `resp_form.url` (COM conversationId) de
        # propósito: é a continuação da MESMA conversa Seam. A URL limpa só é
        # obrigatória no clique de um item — inverter os dois é o erro que a
        # sonda cometeu e que devolve 200 com formulário em branco.
        resp = self.http.post(resp_form.url, data=dados,
                              headers={'Referer': resp_form.url})
        return resp.text
