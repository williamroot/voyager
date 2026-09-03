"""Jurimetria de magistrado — o contrato de honestidade da tela.

Estes testes não protegem CRUD: protegem as regras que impedem a ficha de
virar outra coisa. Cada uma saiu de uma medição de 03/09/2026, no caso que
originou o módulo (Dra. Rafaela Caldeira Gonçalves, TJSP, 132 processos).
"""
import pytest

from dashboard import dossie_magistrado as D
from dashboard import magistrado_views as V


# ── identidade ───────────────────────────────────────────────────────────────
def test_busca_sem_tribunal_e_recusada():
    """Medido: das 195 publicações que casam com o nome, 56 eram de OUTROS
    tribunais — homônimos. Sem o recorte a ficha mistura pessoas e parece
    correta, que é o pior tipo de erro."""
    assert V._valida('Rafaela Caldeira Gonçalves', 'TJXX') is not None
    assert V._valida('Rafaela Caldeira Gonçalves', 'TJSP') is None


def test_nome_curto_ou_sem_sobrenome_e_recusado():
    """A busca é `match_phrase`: um termo só casa com meio acervo."""
    assert V._valida('Ana', 'TJSP') is not None
    assert V._valida('Rafaela', 'TJSP') is not None


def test_chave_de_cache_separa_tribunais():
    """Mesmo nome em tribunais diferentes NÃO pode compartilhar cache — seria
    o homônimo entrando pela porta dos fundos."""
    assert V._chave('Fulano de Tal', 'TJSP') != V._chave('Fulano de Tal', 'TJRJ')
    # e é estável entre variações irrelevantes de digitação
    assert V._chave(' Fulano de Tal ', 'tjsp') == V._chave('fulano de tal', 'TJSP')


# ── estatística ──────────────────────────────────────────────────────────────
def _procs(marcadores_por_caso):
    return {'processos': {f'p{i}': {'data': '2026-01-0%d' % (i % 9 + 1),
                                    'classe': 'X', 'orgao': 'Y',
                                    'marcadores': set(m), 'pubs': 1}
                          for i, m in enumerate(marcadores_por_caso)},
            'nome': 'N', 'tribunal': 'TJSP', 'publicacoes': len(marcadores_por_caso),
            'publicacoes_no_indice': len(marcadores_por_caso), 'teto_batido': False}


def test_phi_devolve_a_tabela_2x2_inteira():
    """φ sozinho esconde que uma célula tem 1 caso. A tabela sai junto de
    propósito — é o que permite ao leitor desconfiar."""
    bruto = _procs([{'condenacao', 'protetiva'}, {'condenacao'}, {'protetiva'}, set()])
    a = D.analisar(bruto)
    c = next(x for x in a['correlacoes'] if x['chave'] == 'protetiva')
    assert c['tabela'] == (1, 1, 1, 1)
    assert c['phi'] == 0.0  # independentes


def test_marcador_que_nunca_aparece_sozinho_e_sinalizado():
    """Medido: extinção da punibilidade tem 28 casos com condenação e ZERO
    sem — não é arquivamento autônomo, é extinção dentro de uma condenação."""
    bruto = _procs([{'condenacao', 'extincao'}, {'condenacao', 'extincao'},
                    {'condenacao'}, set()])
    a = D.analisar(bruto)
    c = next(x for x in a['correlacoes'] if x['chave'] == 'extincao')
    assert c['nunca_sozinho'] is True


def test_celula_pequena_marca_a_linha_como_nao_confiavel():
    """Amostra pequena EXAGERA, não atenua — foi a lição do gate dos
    especialistas. A linha sai marcada em vez de sair bonita."""
    bruto = _procs([{'condenacao', 'preventiva'}] + [{'condenacao'}] * 20)
    a = D.analisar(bruto)
    c = next(x for x in a['correlacoes'] if x['chave'] == 'preventiva')
    assert c['confiavel'] is False


def test_a_unidade_e_o_processo_nao_a_publicacao():
    """Um caso com 4 intimações contaria 4× numa contagem por documento.
    Medido no caso real: 141 publicações = 132 processos."""
    bruto = _procs([{'condenacao'}, {'condenacao'}])
    a = D.analisar(bruto)
    freq = {f['chave']: f for f in a['frequencia']}
    assert freq['condenacao']['n'] == 2
    assert a['n_processos'] == 2


def test_condenacao_e_absolvicao_podem_coexistir():
    """Absolvição parcial é rotina: "condeno pelo art. X … absolvo do art. Y".
    Se algum dia alguém tornar as duas excludentes, este teste cai."""
    bruto = _procs([{'condenacao', 'absolvicao'}])
    a = D.analisar(bruto)
    freq = {f['chave']: f['pct'] for f in a['frequencia']}
    assert freq['condenacao'] == 100.0 and freq['absolvicao'] == 100.0


# ── o que a tela recusa dizer ────────────────────────────────────────────────
def test_o_html_avisa_antes_do_primeiro_numero():
    """Um dossiê destes circula. Quem lê a segunda página sem a primeira não
    pode sair achando que tem um score de juiz."""
    a = D.analisar(_procs([{'condenacao'}]))
    h = D.render_html(a)
    pos_aviso = h.index('não mede')
    pos_tabela = h.index('Atos processuais')
    assert pos_aviso < pos_tabela, 'o aviso tem que vir ANTES dos números'
    for termo in ('ranking', 'inteiro teor', 'não somam 100%'):
        assert termo in h, f'o dossiê tem que dizer explicitamente: {termo}'


def test_o_html_nao_usa_cor_fora_do_config():
    """`warn` não existe — o token é `warning`. Classe inexistente vira NADA
    e o elemento fica invisível, sem o Tailwind avisar."""
    from pathlib import Path
    tpl = (Path(__file__).resolve().parents[1]
           / 'dashboard/templates/dashboard/magistrado.html').read_text(encoding='utf-8')
    for proibida in ('text-warn-fg', 'bg-warn/', 'border-warn/',
                     'text-success-fg', 'intcomma'):
        assert proibida not in tpl, f'`{proibida}` não existe nesta base'
