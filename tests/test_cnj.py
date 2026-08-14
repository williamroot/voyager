"""CNJ → tribunal: o número já carrega o tribunal (Resolução CNJ 65/2008).

Validado contra 1.080 processos REAIS do índice de produção (60 por tribunal,
18 maiores): 99,44% de acerto, 0 casos de "não soube", 0 DV inválido. As 6
divergências foram todas TRF6→TRF1 — ver `test_trf6_herda_cnj_do_trf1`.
"""
from tribunals.cnj import (SEGMENTOS, descrever, dv_valido, partes,
                           sigla_do_cnj, so_digitos)


def test_deriva_tribunal_de_cnjs_reais():
    """CNJs colhidos do índice de produção, com o tribunal que o ES registra."""
    casos = [
        ('1105916-36.2026.8.26.0053', 'TJSP'),
        ('0801184-49.2025.8.19.0010', 'TJRJ'),
        ('0800334-78.2026.8.10.0149', 'TJMA'),
        ('0518368-72.2011.8.06.0001', 'TJCE'),
        ('6002434-35.2024.8.03.0008', 'TJAP'),
        ('0144101-59.2024.8.17.2001', 'TJPE'),
        ('5002816-68.2023.8.13.0051', 'TJMG'),
        ('5009780-22.2025.8.13.0079', 'TJMG'),
    ]
    for cnj, esperado in casos:
        assert sigla_do_cnj(cnj) == esperado, f'{cnj} → {sigla_do_cnj(cnj)}'


def test_aceita_com_e_sem_mascara():
    """O usuário cola como vier — do site do tribunal, de um PDF, de um email."""
    assert sigla_do_cnj('1105916-36.2026.8.26.0053') == 'TJSP'
    assert sigla_do_cnj('11059163620268260053') == 'TJSP'
    assert sigla_do_cnj(' 1105916-36.2026.8.26.0053 ') == 'TJSP'


def test_trf6_herda_cnj_do_trf1():
    """O TRF6 (2022) foi desmembrado do TRF1 e herdou processos com CNJ antigo.

    Foram as ÚNICAS 6 divergências em 1.080 processos reais. A função está
    certa pelo número; quem consome tem que preferir o tribunal OBSERVADO no
    índice quando o processo já está na base — derivação é palpite fundamentado,
    dado observado é fato.
    """
    # um CNJ com código 01 é TRF1 pelo número, mesmo que hoje corra no TRF6
    assert sigla_do_cnj('0000000-00.2020.4.01.0000') == 'TRF1'


def test_segmentos_e_superiores():
    assert sigla_do_cnj('0000000-00.2024.4.03.0000') == 'TRF3'
    assert sigla_do_cnj('0000000-00.2024.5.02.0000') == 'TRT2'
    assert sigla_do_cnj('0000000-00.2024.8.07.0000') == 'TJDFT'   # DF = TJDFT
    assert sigla_do_cnj('0000000-00.2024.3.00.0000') == 'STJ'
    assert SEGMENTOS['8'] == 'Justiça Estadual'


def test_abstem_em_vez_de_chutar():
    """Número incompleto ou código desconhecido → None, nunca um palpite.

    Mandar o usuário consultar no tribunal errado devolve "não encontrado" —
    e ele conclui que o processo não existe.
    """
    assert sigla_do_cnj('') is None
    assert sigla_do_cnj('123') is None
    assert sigla_do_cnj(None) is None
    assert sigla_do_cnj('0000000-00.2024.4.99.0000') is None   # TRF99 não existe
    assert sigla_do_cnj('0000000-00.2024.8.99.0000') is None   # UF 99 não existe


def test_dv_separa_numero_errado_de_nao_encontrado():
    """DV é aritmética: dá pra dizer "esse número está errado" sem consultar."""
    assert dv_valido('1105916-36.2026.8.26.0053') is True
    assert dv_valido('1105916-37.2026.8.26.0053') is False      # DV trocado
    d = descrever('1105916-37.2026.8.26.0053')
    assert d['valido'] is False and d['motivo'] == 'dv'
    assert d['sigla'] == 'TJSP'      # ainda sabe o tribunal


def test_descrever_nunca_levanta():
    for entrada in ('', None, 'abc', '123', '1105916-36.2026.8.26.0053'):
        d = descrever(entrada)
        assert isinstance(d, dict) and 'valido' in d


def test_partes_e_digitos():
    p = partes('1105916-36.2026.8.26.0053')
    assert p['ano'] == '2026' and p['segmento'] == '8' and p['tribunal'] == '26'
    assert so_digitos('1105916-36.2026.8.26.0053') == '11059163620268260053'
