"""Querido Diário — diários oficiais MUNICIPAIS (~5.570 municípios mapeados).

O ACHADO QUE JUSTIFICA A FONTE (16/08/2026)
===========================================
A frase "câmara de conciliação de precatórios" devolve 64 gazetas em 2,5 anos.
A primeira é Maceió/AL de 30/04/2026 (edição 7397, 482.437 chars de texto
nativo), e dentro dela está isto, verbatim:

    PROCURADORIA GERAL DO MUNICÍPIO - PGM
    CÂMARA DE CONCILIAÇÃO DE PRECATÓRIOS - CCP CONVOCAÇÃO DOS HABILITADOS ...
    PARTE                          PRECATÓRIO N°              HORÁRIO   SALA
    Maicon dos Santos Freitas      0501276-27.2026.8.02.9003  09:00     1
    Janny Karla de Mendonça Silva  0501769-38.2025.8.02.9003  09:10     1

Nome do credor + CNJ do precatório + data da sessão de acordo, em tabela. É
lead puro, e é o tipo de coisa que NÃO existe no DJEN: é o ente devedor
chamando o credor para receber.

E O QUE ELA NÃO É
-----------------
Não é porta de acervo, e o volume é ínfimo: 3,6% das 7.819 gazetas de
julho/2026 casam "precatório", e dessas só 2 em 12 amostradas traziam CNJ — o
resto é linha de RREO/RGF ("31.5- RECEITA DE PRECATÓRIOS - FUNDEF E FUNDEB").
Estimativa honesta: 2-3 gazetas/dia no país com precatório + CNJ, contra ~3.000
movimentações/MINUTO do DJEN. Alta densidade de valor por documento, volume
ínfimo.

RISCOS QUE O CÓDIGO PRECISA TRATAR (todos medidos)
--------------------------------------------------
1. HOST ERRADO É CASCA: `queridodiario.ok.org.br/api/...` devolve HTTP 200 com
   20.943 bytes da SPA Angular. A API é `api.queridodiario.ok.org.br`. Daí
   `exigir_json` em toda resposta.
2. TETO DE PAGINAÇÃO: `offset >= 9999` → HTTP 500 e `total_gazettes` satura em
   10000 (max_result_window do OpenSearch). Coletar por DIA mantém a página
   longe do teto (máximo medido: 407 gazetas num dia), e o guard ainda existe.
3. ASPA É O OPERADOR DE FRASE — E SÓ DE FRASE. Conferido ao vivo: a FRASE
   'câmara de conciliação de precatórios' dá 64 gazetas com aspas e 10.000 (o
   cap) sem elas, porque vira OR e pega o país. Mas o TERMO SOLTO é o inverso:
   com aspas o coletor via 14 gazetas em 12 dias, sem aspas 41 (-66%), e as 8
   diferenças auditadas continham 'precatóri' verbatim. Regra em
   `_buscar`: aspas só quando a consulta tem espaço. (Corrigido em 16/08/2026;
   antes disso a fonte perdia dois terços do sinal em silêncio.)
4. COBERTURA É ESTAGNADA E O CAMPO `level` MENTE: Fortaleza tem `level="1"` e
   ZERO gazetas; São Paulo capital — maior estoque de precatório municipal do
   país — tem 20 gazetas no acervo inteiro, a última de 24/03/2025. Só 9 dos 28
   maiores municípios estavam em dia em 16/08/2026. É raspador de ONG (Open
   Knowledge Brasil), sem SLA. Por isso `frescor_por_municipio()` existe: o
   selo tem que vir de MEDIÇÃO (data da última gazeta), nunca do `level`.
"""

import logging
from datetime import date, timedelta

from diarios.base import (
    RespostaInvalida,
    UnidadeColeta,
    UnidadeInexistente,
    external_id_de,
    registrar,
)

from ..coletor import (
    FRASES_PRECATORIO,
    TERMOS_PRECATORIO,
    ColetorEnte,
    ItemEnte,
    dobrar,
    dobrar_liso,
    enriquecer_item,
    exigir_json,
    exigir_texto,
    janela_de_texto,
)
from ..models import ESFERA_MUNICIPAL

logger = logging.getLogger('voyager.diarios_entes.qd')

API = 'https://api.queridodiario.ok.org.br'
#: página da busca; a API aceita até 100 por vez com folga (o dia mais cheio
#: medido tem 407 gazetas no país inteiro).
TAMANHO_PAGINA = 100
#: `offset >= 9999` devolve HTTP 500 (max_result_window do OpenSearch). Parar
#: ANTES e avisar é melhor que capar em silêncio — o coletor ingênuo perderia
#: dado achando que terminou.
TETO_OFFSET = 9000

#: Âncoras usadas para recortar a janela de texto e para confirmar que o corpo
#: baixado é mesmo o que a busca prometeu. Já vêm DOBRADAS (minúsculas, sem
#: acento) porque é assim que `dobrar()` compara, e são RADICAIS, não as frases
#: inteiras: a busca do OpenSearch faz stemming, então a consulta "acordo direto
#: de precatórios" casa um documento que escreve "precatório" no singular, e
#: ancorar pela frase literal não acharia nada no texto.
#:
#: `conciliacao` sozinho NÃO entra: em diário municipal ele casa PROCON, câmara
#: de conciliação trabalhista e mediação de consumo, e a janela de recorte sai
#: cheia de tabela de reclamação (conferido na gazeta de Maceió de 30/04/2026).
ANCORAS = ('precatori', 'requisitori', 'ordem cronologica',
           'camara de conciliacao de precatorio')


class BuscaSemGazettes(RespostaInvalida):
    """Payload de busca sem a chave `gazettes`.

    A API do Querido Diário não é contratada nem versionada (é projeto de ONG);
    o único aviso de que o contrato mudou é a chave sumir. Falhar alto aqui é o
    que impede um backfill de meses gravar zero e reportar sucesso — o mesmo
    papel que o `SchemaDriftAlert` faz no DJEN (que não serve aqui: a tabela
    dele tem `tribunal` NOT NULL e esta fonte não tem tribunal).
    """

    def __init__(self, payload: dict, consulta: str, dia: date):
        super().__init__(
            f'QD {dia} {consulta!r}: payload sem a chave "gazettes" '
            f'(chaves: {sorted(payload)[:8]}) — provável mudança de contrato'
        )


@registrar
class QueridoDiarioColetor(ColetorEnte):
    """Coletor diário das gazetas municipais que falam de precatório."""

    slug = 'qd-municipal'
    nome = 'Diários oficiais municipais (Querido Diário)'
    esfera = ESFERA_MUNICIPAL

    #: cobertura medida ao vivo: a gazeta mais antiga do acervo é Cuiabá/MT de
    #: 02/01/1990 (`sort_by=ascending_date`). Sem `janela_fim`: é fonte corrente.
    janela_inicio = date(1990, 1, 2)

    #: 1 req/s auto-imposto. Não há rate limit do outro lado (≈80 requests na
    #: sonda, zero bloqueio) — o teto é higiene nossa, ainda mais porque cada
    #: gazeta baixada é da ordem de 100 kB a 6,6 MB de texto.
    rps = 1.0

    # ── catálogo ─────────────────────────────────────────────────────────────
    def catalogar(self, data_inicio: date, data_fim: date):
        """Uma unidade por DIA, sem tocar na rede.

        Por que dia e não município: a consulta é nacional por frase, e o que
        interessa é "o que o país publicou hoje sobre precatório". Catalogar por
        município exigiria enumerar 5.570 territórios (e 4.615 deles nunca
        tiveram uma gazeta sequer). Dia sem gazeta nenhuma vira `inexistente` na
        coleta — sem retentar para sempre.
        """
        d = data_inicio
        while d <= data_fim:
            yield UnidadeColeta(
                chave=d.isoformat(), data=d,
                rotulo=f'Gazetas municipais de {d:%d/%m/%Y}',
                meta={'consultas': list(FRASES_PRECATORIO) + list(TERMOS_PRECATORIO)},
            )
            d += timedelta(days=1)

    # ── coleta ───────────────────────────────────────────────────────────────
    def coletar(self, unidade: UnidadeColeta):
        dia = unidade.data
        consultas = unidade.meta.get('consultas') or (
            list(FRASES_PRECATORIO) + list(TERMOS_PRECATORIO))

        # 1. busca: uma passada por consulta, unindo por gazeta.
        achadas: dict[str, dict] = {}
        for consulta in consultas:
            for gazeta in self._buscar(consulta, dia):
                chave = gazeta.get('txt_url') or gazeta.get('url') or ''
                if not chave:
                    continue
                alvo = achadas.setdefault(chave, {'gazeta': gazeta, 'consultas': []})
                alvo['consultas'].append(consulta)

        if not achadas:
            # Zero casamentos NÃO é o mesmo que "não houve diário". Distinguir
            # custa 1 request e evita que o watermark trate fim de semana como
            # lacuna a retentar (a lição do `_dia_coberto` do DJEN).
            if self._total_do_dia(dia) == 0:
                raise UnidadeInexistente(
                    f'nenhuma gazeta municipal publicada em {dia} (fim de semana/feriado)')
            return

        # 2. corpo: só agora se paga o download (100 kB a 6,6 MB por gazeta).
        for dados in achadas.values():
            item = self._montar(dados['gazeta'], dados['consultas'])
            if item is not None:
                yield item

    # ── rede ─────────────────────────────────────────────────────────────────
    def _buscar(self, consulta: str, dia: date) -> list[dict]:
        """Busca de UM dia. A aspa é o operador de FRASE — e só de frase.

        CORREÇÃO DE 2026-08-16 (a aspa custava 66% do recall)
        -----------------------------------------------------
        Antes, TODA consulta ia entre aspas. A justificativa estava medida numa
        FRASE ('câmara de conciliação de precatórios': 64 resultados com aspas
        contra 10.000 sem, porque sem elas o OpenSearch vira OR e casa o país
        inteiro) e foi generalizada indevidamente para TERMO SOLTO, onde ela é
        pura perda. Medido na API viva em 12 dias corridos (03→14/08/2026):

            com aspas (o que o coletor enxergava) ......... 14 gazetas
            sem aspas ..................................... 41 gazetas   (-66%)

        E não é ruído: as 8 gazetas que só apareciam sem aspas nos dias 11, 13 e
        14/08 foram baixadas uma a uma e 8/8 contêm 'precatóri' verbatim
        (Curitiba/PR  contra 6, Rio de Janeiro/RJ  contra 2, Goiânia/GO, Londrina/PR,
        Jaguaquara/BA, Ipiranga/PR) — todas passariam pelo filtro de `ANCORAS` e
        seriam gravadas. O dia 12/08 chegou a fechar como `vazia` no watermark
        tendo 2 gazetas com precatório na fonte: perda ANTES de o coletor ver o
        documento, portanto sem log, sem alerta e sem contador — mentira por
        omissão, que é o que este projeto existe para não fazer.

        Corolário medido: a variante sem acento ('precatorio') era NO-OP
        ABSOLUTO no QD com aspas (0 resultado em 12/12 dias) e devolve 5-7/dia
        sem elas. A justificativa dela em `diarios_entes/coletor.py` foi medida
        no DOE-SP, onde de fato é outro conjunto; aqui não era.

        Contraprova de que a frase continua precisando de aspas (13/08/2026):
        'ofício requisitório' com aspas = 0 · sem aspas = 69 (vira OR e casa
        qualquer diário que fale 'ofício'). Por isso a regra é a presença de
        espaço, não uma lista.
        """
        # Frase (tem espaço) ⇒ aspas. Termo solto ⇒ nu.
        termo = f'"{consulta}"' if ' ' in consulta.strip() else consulta.strip()
        gazetas: list[dict] = []
        offset = 0
        while True:
            resp = self.sessao.get(f'{API}/gazettes', params={
                'querystring': termo,
                'published_since': dia.isoformat(),
                'published_until': dia.isoformat(),
                'size': TAMANHO_PAGINA,
                'offset': offset,
                'sort_by': 'descending_date',
                'excerpt_size': 500,
                'number_of_excerpts': 3,
            })
            payload = exigir_json(resp, contexto=f'QD busca {consulta!r} {dia}')
            if 'gazettes' not in payload:
                raise BuscaSemGazettes(payload, consulta, dia)
            pagina = payload.get('gazettes') or []
            gazetas.extend(pagina)
            total = int(payload.get('total_gazettes') or 0)
            offset += len(pagina)
            if len(pagina) < TAMANHO_PAGINA or offset >= total:
                break
            if offset >= TETO_OFFSET:
                # Só acontece se alguém alargar a janela para além de um dia.
                logger.error('QD: teto de paginação atingido em %s/%s (total=%d) — '
                             'fatie por município ou por mês', consulta, dia, total)
                break
        return gazetas

    def _total_do_dia(self, dia: date) -> int:
        """Quantas gazetas o país publicou no dia (sem filtro de termo)."""
        resp = self.sessao.get(f'{API}/gazettes', params={
            'published_since': dia.isoformat(), 'published_until': dia.isoformat(), 'size': 1,
        })
        return int(exigir_json(resp, contexto=f'QD total {dia}').get('total_gazettes') or 0)

    # ── item ─────────────────────────────────────────────────────────────────
    def _montar(self, gazeta: dict, consultas: list[str]) -> ItemEnte | None:
        txt_url = gazeta.get('txt_url') or ''
        if not txt_url:
            # Gazeta sem texto extraído (só PDF): sem OCR aqui, e OCR não é
            # decisão deste coletor. Abster e registrar > baixar 6 MB de PDF
            # que ninguém vai ler.
            logger.info('QD: gazeta sem txt_url, ignorada', extra={
                'territory_id': gazeta.get('territory_id'), 'data': gazeta.get('date')})
            return None

        resp = self.sessao.get(txt_url)
        texto_integral = exigir_texto(resp.content, contexto=f'QD txt {txt_url}')

        # Assertiva de CONTEÚDO, não de status: se nenhuma âncora aparece no
        # texto, o que veio não é o documento que a busca prometeu (arquivo
        # trocado no bucket, stemming levando a outra coisa). Não grava.
        dobrado = dobrar(texto_integral)
        if not any(a in dobrado for a in ANCORAS):
            logger.warning('QD: texto sem âncora de precatório, ignorado', extra={
                'txt_url': txt_url, 'consultas': consultas, 'chars': len(texto_integral)})
            return None

        trecho, inteiro = janela_de_texto(texto_integral, list(ANCORAS))
        data_pub = date.fromisoformat(gazeta['date'])
        # A URL do arquivo é content-addressed (sha1 do PDF). Isso é ótimo como
        # id: gazeta reprocessada com o MESMO conteúdo mantém o id, e conteúdo
        # diferente é documento diferente mesmo.
        sha = txt_url.rsplit('/', 1)[-1].split('.')[0][:12]
        item = ItemEnte(
            external_id=external_id_de(self.slug, gazeta.get('territory_id') or '0000000', sha),
            esfera=ESFERA_MUNICIPAL,
            ente=gazeta.get('territory_name') or '',
            uf=gazeta.get('state_code') or '',
            territory_id=gazeta.get('territory_id') or '',
            data_publicacao=data_pub,
            # Rótulo montado dos campos reais (a fonte não tem título por ato:
            # a unidade dela é a gazeta inteira). Não é dado inventado, é label.
            titulo=f'Diário Oficial de {gazeta.get("territory_name")} — edição '
                   f'{gazeta.get("edition") or "s/n"}'[:300],
            edicao=str(gazeta.get('edition') or ''),
            link=gazeta.get('url') or '',
            link_texto=txt_url,
            texto=trecho,
            texto_integral_chars=len(texto_integral),
            texto_completo=inteiro,
            consultas=sorted(set(consultas)),
            # CONFIANÇA SE CONFERE NO CORPO, não na busca (corrigido 16/08/2026).
            # `confianca_de` promete "ALTA = casou FRASE de alta precisão", mas
            # o que casou foi o OpenSearch — com stemming e stopword, ele dá a
            # frase por casada em texto que não a contém (reproduzido: gazeta sem
            # sequer 'concilia' no corpo saía com confianca='alta'). Como
            # `confianca` é a chave de priorização da triagem, ela só pode subir
            # com evidência verbatim. O DOE-SP já fazia assim; aqui era a
            # assimetria. As `consultas` continuam sendo TODAS as que a busca
            # casou — isso é fato sobre a busca, e é honesto registrar.
            confianca=self.confianca_de(
                [c for c in set(consultas) if dobrar_liso(c) in dobrar_liso(texto_integral)]),
        )
        return enriquecer_item(item)

    # ── observabilidade: o selo de frescor que o `level` não dá ──────────────
    def frescor_por_municipio(self, territory_ids: list[str]) -> dict[str, str | None]:
        """Data da ÚLTIMA gazeta de cada município — o selo que a dashboard usa.

        Existe porque o campo `level` da API é aspiracional e engana: Fortaleza
        tem `level="1"` e `total_gazettes=0`. A única verdade é a data da última
        gazeta, e ela só sai perguntando.
        """
        out: dict[str, str | None] = {}
        for tid in territory_ids:
            resp = self.sessao.get(f'{API}/gazettes', params={
                'territory_ids': tid, 'size': 1, 'sort_by': 'descending_date'})
            payload = exigir_json(resp, contexto=f'QD frescor {tid}')
            gazetas = payload.get('gazettes') or []
            out[tid] = gazetas[0].get('date') if gazetas else None
        return out
