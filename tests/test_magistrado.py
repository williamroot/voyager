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


# ── premium: série, SVG e matriz ─────────────────────────────────────────────
def test_serie_marca_o_mes_corrente_como_parcial():
    """Bucket incompleto desenhado como cheio sugere QUEDA onde só há mês pela
    metade. É a lição que o `buildVolumeChart` do base.html já carregava."""
    import datetime
    hoje = datetime.date.today()
    serie = D._serie_mensal([f'{hoje:%Y-%m}-01', '2026-01-15'])
    corrente = [x for x in serie if x['parcial']]
    assert len(corrente) == 1
    assert corrente[0]['mes'] == f'{hoje:%Y-%m}'


def test_serie_e_continua_zero_explicito_nao_buraco():
    """Buraco no eixo faz o olho interpolar e inventar atividade que não houve."""
    serie = D._serie_mensal(['2026-01-10', '2026-04-10'])
    assert [x['mes'] for x in serie] == ['2026-01', '2026-02', '2026-03', '2026-04']
    assert [x['n'] for x in serie] == [1, 0, 0, 1]


def test_media_mensal_ignora_o_mes_incompleto():
    """Incluir o corrente puxa a média para baixo e sugere desaceleração falsa."""
    import datetime
    hoje = datetime.date.today()
    bruto = _procs([{'condenacao'}])
    bruto['datas'] = ['2026-01-05'] * 10 + [f'{hoje:%Y-%m}-01']
    a = D.analisar(bruto)
    # 10 no mês completo; o corrente (1) não entra na conta
    assert a['destaque']['media_mes'] is not None
    completos = [x for x in a['serie'] if not x['parcial']]
    assert a['destaque']['media_mes'] == round(
        sum(x['n'] for x in completos) / len(completos), 1)


def test_svg_da_serie_e_autocontido_e_marca_o_parcial():
    """WeasyPrint NÃO roda JavaScript: se o PDF dependesse do ECharts sairia um
    retângulo vazio, e gráfico faltando parece defeito de impressão em vez de
    ausência deliberada."""
    svg = D._svg_serie([{'mes': '2026-01', 'n': 5, 'parcial': False},
                        {'mes': '2026-02', 'n': 3, 'parcial': True}])
    assert svg.startswith('<svg') and svg.endswith('</svg>')
    assert 'polyline' in svg, 'a linha dos meses completos'
    assert 'rotate(45' in svg, 'o losango do mês parcial'
    assert '<script' not in svg, 'o PDF não executa script'


def test_svg_vazio_quando_nao_ha_serie():
    assert D._svg_serie([]) == ''


def test_matriz_cobre_todos_os_pares_e_so_promove_os_confiaveis():
    """O eixo único responde 'o que anda com condenação'. A matriz responde
    'o que anda com o quê' — e os `fortes` NÃO podem incluir célula pequena,
    senão o ranking é liderado por acaso de amostra."""
    bruto = _procs([{'condenacao', 'protetiva'}] * 8
                   + [{'condenacao'}] * 8 + [{'protetiva'}] * 8
                   + [{'preventiva', 'condenacao'}] + [set()] * 8)
    a = D.analisar(bruto)
    m = a['matriz']
    assert len(m['celulas']) >= 3
    for c in m['fortes']:
        assert c['confiavel'], 'par de célula pequena não pode liderar'
