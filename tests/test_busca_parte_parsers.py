"""Leitura do resultado de busca POR PARTE — os três motores, sobre HTML real.

Estes testes travam o que o recon de 04/09/2026 mediu ao vivo nos nove
tribunais (`.ia/ENRICHMENT.md` §"Busca POR PARTE"). Cada fixture aqui é uma
resposta que a fonte deu de verdade, e cada caso existe porque, sem ele, um
parser plausível devolveria "nenhum processo" para algo que não é isso:

  · e-SAJ: "refine sua busca" e "consultas simultâneas" chegam como páginas sem
    nenhum resultado — a primeira é recusa da fonte, a segunda é pacing;
  · e-SAJ: o contador trava em 1.000, e 1.000 não é total;
  · PJe: 30 é teto, e o rodapé do TRF5 anuncia 30 mostrando 1 linha;
  · TJMT: filtro que a API não conhece é ignorado, e ela responde 200 com o
    acervo inteiro.

Nada aqui toca a rede.
"""
from pathlib import Path

import pytest

from enrichers.busca import esaj_parser as E, pje_parser as P, rest_parser as R

FIXTURES = Path(__file__).parent / 'fixtures'


def ler(caminho: str) -> str:
    return (FIXTURES / caminho).read_text(encoding='utf-8')


def ler_json(caminho: str):
    import json
    return json.loads(ler(caminho))


# ── e-SAJ: os seis desfechos ─────────────────────────────────────────────────

@pytest.mark.parametrize('arquivo,esperado', [
    ('tjsp/busca_nome.html', E.DESFECHO_LISTA),
    ('tjsp/busca_oab.html', E.DESFECHO_LISTA),
    ('tjsp/busca_documento.html', E.DESFECHO_LISTA),
    ('tjsp/busca_documento_vazio.html', E.DESFECHO_VAZIO),
    ('tjsp/busca_muitos.html', E.DESFECHO_MUITOS),
    ('tjsp/busca_consultas_simultaneas.html', E.DESFECHO_SIMULTANEAS),
    ('tjal/busca_documento.html', E.DESFECHO_VAZIO),
    ('tjal/busca_nome.html', E.DESFECHO_MUITOS),
    ('tjal/busca_nome_lista.html', E.DESFECHO_LISTA),
])
def test_esaj_classifica_desfecho(arquivo, esperado):
    assert E.classificar(ler(arquivo)) == esperado


def test_esaj_detalhe_vem_da_url():
    """1 resultado só: o e-SAJ pula a lista e abre o processo."""
    assert E.classificar('<html></html>', 'https://esaj/cpopg/show.do?x=1') == E.DESFECHO_DETALHE


def test_esaj_recusa_por_amplitude_nao_e_lista_vazia():
    """A diferença entre "não tem" e "não vou responder" é o produto inteiro."""
    html = ler('tjsp/busca_muitos.html')
    assert E.classificar(html) != E.DESFECHO_VAZIO
    assert 'refine' in E.mensagem_da_fonte(html).lower()


def test_esaj_lista_traz_foro_de_cada_bloco():
    """O foro está no <h2> que agrupa, não na linha: 25 itens, 3 foros."""
    pagina = E.parse_lista(ler('tjsp/busca_oab.html'), 'TJSP', 'https://esaj.tjsp.jus.br')
    assert len(pagina.itens) == 25
    assert len({i.orgao for i in pagina.itens}) == 3
    primeiro = pagina.itens[0]
    assert primeiro.numero_cnj == '1030331-10.2016.8.26.0576'
    assert primeiro.classe == 'Embargos à Execução Fiscal'
    assert primeiro.url_fonte.startswith('https://esaj.tjsp.jus.br/cpopg/show.do')


def test_esaj_contador_de_823_e_total_de_verdade():
    pagina = E.parse_lista(ler('tjsp/busca_oab.html'), 'TJSP')
    assert pagina.total_declarado == 823
    assert pagina.total_e_teto is False
    assert pagina.tem_proxima is True


def test_esaj_contador_de_1000_e_teto_disfarcado():
    """Medido com o CNPJ do Bradesco: o e-SAJ trava em 1.000."""
    pagina = E.parse_lista(ler('tjsp/busca_documento.html'), 'TJSP')
    assert pagina.total_declarado == E.TETO_ESAJ
    assert pagina.total_e_teto is True


def test_esaj_ultima_pagina_nao_anuncia_proxima():
    pagina = E.parse_lista(ler('tjsp/busca_nome_pagina2.html'), 'TJSP', pagina=2)
    assert len(pagina.itens) == 9
    assert pagina.total_declarado == 34
    assert pagina.tem_proxima is False


def test_esaj_link_da_proxima_pagina_vem_da_fonte():
    href = E.proxima_pagina(ler('tjsp/busca_nome.html'))
    assert href and 'trocarPagina.do' in href and 'paginaConsulta=2' in href


def test_esaj_tjal_usa_o_mesmo_parser():
    pagina = E.parse_lista(ler('tjal/busca_nome_lista.html'), 'TJAL')
    assert pagina.total_declarado == len(pagina.itens) == 4
    assert all(i.tribunal == 'TJAL' for i in pagina.itens)


# ── PJe: teto de 30, zero sem número, rodapé que não é contagem ──────────────

@pytest.mark.parametrize('arquivo,tribunal,itens,total,teto', [
    ('tjmg/busca_nome.html', 'TJMG', 30, 30, True),
    ('tjmg/busca_nome_12.html', 'TJMG', 12, 12, False),
    ('tjmg/busca_nome_raro.html', 'TJMG', 0, 0, False),
    ('tjmg/busca_documento.html', 'TJMG', 30, 30, True),
    ('tjmg/busca_advogado.html', 'TJMG', 30, 30, True),
    ('tjma/busca_documento.html', 'TJMA', 6, 6, False),
    ('tjma/busca_oab.html', 'TJMA', 4, 4, False),
    ('trf1/busca_nome.html', 'TRF1', 30, 30, True),
    ('trf1/busca_documento.html', 'TRF1', 30, 30, True),
    ('trf1/busca_oab.html', 'TRF1', 30, 30, True),
    ('trf1/busca_advogado.html', 'TRF1', 30, 30, True),
    ('tjma/busca_advogado.html', 'TJMA', 30, 30, True),
])
def test_pje_conta_o_que_veio(arquivo, tribunal, itens, total, teto):
    pagina = P.parse_lista(ler(arquivo), tribunal)
    assert len(pagina.itens) == itens
    assert pagina.total_declarado == total
    assert pagina.total_e_teto is teto
    # A fonte não pagina: prometer página seguinte faria o coletor pedir uma
    # que não existe.
    assert pagina.tem_proxima is False


def test_pje_le_classe_assunto_e_partes_da_linha():
    pagina = P.parse_lista(ler('tjmg/busca_nome.html'), 'TJMG',
                           'https://pje-consulta-publica.tjmg.jus.br',
                           '/pje/ConsultaPublica/DetalheProcessoConsultaPublica')
    item = pagina.itens[0]
    assert item.numero_cnj == '3644951-61.1986.8.13.0024'
    assert item.classe == 'EXECUÇÃO DE TÍTULO EXTRAJUDICIAL CONTRA A FAZENDA PÚBLICA'
    assert item.assunto == 'Obrigação de Fazer / Não Fazer'
    assert len(item.partes_na_lista) == 2
    assert item.url_fonte.endswith('listView.seam?ca=cb8f7b64fa3255f63a18243f569e71615c28f63be3310aea')


def test_pje_zero_resultados_nao_traz_numero_no_rodape():
    """O PJe escreve "resultados encontrados", sem número, quando não achou."""
    assert P.total_declarado(ler('tjmg/busca_nome_raro.html')) == 0


def test_pje_lista_truncada_pela_fonte_mantem_o_total():
    """TRF5: a fonte CONTA 16 e renderiza uma linha.

    Medido em seis buscas (rodapés 30, 30, 16, 13, zero, 30 — sempre 1 linha):
    o rodapé varia com a busca, logo é contagem. Quem está truncada é a tabela.
    O total tem de sobreviver na resposta, com o aviso do que não veio — jogá-lo
    fora entregaria 1 processo como se fosse tudo o que existe.
    """
    pagina = P.parse_lista(ler('trf5/busca_oab.html'), 'TRF5')
    assert len(pagina.itens) == 1
    assert pagina.total_declarado == 16
    assert pagina.total_e_teto is False
    assert 'contou 16' in pagina.aviso_fonte and 'devolveu 1' in pagina.aviso_fonte


def test_pje_lista_completa_nao_gera_aviso():
    """Nos outros quatro PJe o rodapé bate com as linhas — nada a avisar."""
    for arquivo, tribunal in (('tjmg/busca_nome_12.html', 'TJMG'),
                              ('trf1/busca_oab.html', 'TRF1'),
                              ('tjma/busca_advogado.html', 'TJMA')):
        pagina = P.parse_lista(ler(arquivo), tribunal)
        assert pagina.aviso_fonte == '', arquivo
        assert pagina.total_declarado == len(pagina.itens), arquivo


def test_pje_resposta_sem_tabela_nao_e_resultado():
    """O TRF5 já serviu, na mesma URL, uma consulta pública antiga com captcha.

    Sem esta guarda, "outra página" viraria "nenhum processo".
    """
    assert P.tem_tabela(ler('trf5/busca_pagina_com_captcha.html')) is False
    assert P.tem_tabela(ler('tjmg/busca_nome.html')) is True


# ── REST: total real, e o filtro que a API ignora ────────────────────────────

def test_tjmt_le_numero_classe_e_orgao():
    bruto = ler_json('tjmt/busca_por_parte.json')['parteNome']
    pagina = R.parse_tjmt({'totalRegistros': bruto['total'], 'itens': bruto['amostra']})
    assert pagina.total_declarado == 1159
    item = pagina.itens[0]
    assert item.numero_cnj == '1045568-08.2026.8.11.0041'
    # `classe` chega como objeto nesta API; o campo tem de sair como texto.
    assert item.classe == 'AÇÃO CIVIL PÚBLICA CÍVEL'
    assert item.orgao.startswith('CEJUSC')


def test_tjmt_cpf_inexistente_e_zero_de_verdade():
    bruto = ler_json('tjmt/busca_por_parte.json')['parteCpfCnpj']
    pagina = R.parse_tjmt({'totalRegistros': bruto['total'], 'itens': bruto['amostra']})
    assert pagina.total_declarado == 0
    assert pagina.itens == []


def test_tjmt_filtro_ignorado_e_detectavel():
    """A prova de sanidade da fonte: com filtro nunca pode dar o total sem filtro."""
    assert R.parece_base_inteira(11_672_774, 11_672_774) is True
    assert R.parece_base_inteira(1159, 11_672_774) is False
    # Sem baseline conhecido, não se acusa nada.
    assert R.parece_base_inteira(1159, None) is False


def test_tjpa_achata_as_instancias_do_processo():
    corpo = ler_json('tjpa/busca_por_parte.json')['nomeparteexato']['json']
    pagina = R.parse_tjpa(corpo)
    assert pagina.total_declarado == 1
    item = pagina.itens[0]
    assert item.numero_cnj == '0065152-86.2009.8.14.0301'
    assert item.classe == 'Execução Fiscal'
    assert 'Comarca De Belém' in item.orgao and 'Execução Fiscal' in item.orgao


def test_tjpa_desambiguacao_ordena_por_quantidade():
    corpo = ler_json('tjpa/busca_por_parte.json')['nomeparte']['json']
    nomes = R.parse_tjpa_nomes(corpo)
    assert nomes[0]['nome'] == 'MARIA JOSE DOS SANTOS'
    assert nomes[0]['quantidade'] == 43
    assert [n['quantidade'] for n in nomes] == sorted(
        (n['quantidade'] for n in nomes), reverse=True)


def test_cnj_de_20_digitos_vira_mascara():
    assert R.formatar_cnj('10455680820268110041') == '1045568-08.2026.8.11.0041'
    assert R.formatar_cnj('123') == '123'
