"""O que a FONTE declara — medições externas, com data e procedência.

Por que isto é um arquivo e não uma query: **contagem própria não prova nada**
(regra nº 5 do CLAUDE.md). Saber que temos 1,39 bilhão de publicações não diz se
temos o acervo; só a comparação com o que a fonte declara diz. E esses números
não estão em lugar nenhum do nosso banco — vieram de sondagem à API do CNJ, de
paginação na força bruta do DJEN e do catálogo do e-SAJ.

Cada número carrega **quando foi medido e como**. Número declarado sem data
envelhece e passa a mentir em silêncio — e uma tela de completude que mente é
pior que não ter tela.

Quando remedir, ATUALIZE a data junto com o valor. A tela mostra a idade da
medição justamente para que uma medição velha seja visível, não invisível.
"""
import datetime

D = datetime.date


#: As três portas de entrada do acervo. `declarado` é o lado da FONTE.
PORTAS = [
    {
        'slug': 'djen',
        'nome': 'DJEN — Comunica API do CNJ',
        'papel': 'veículo de comunicação: só o que virou publicação em diário',
        'declarado': None,          # não há total declarado; o gabarito é por dia
        'recuperavel': 212_504_437,
        'medido_em': D(2026, 8, 18),
        'como': 'sondagem dos 59 tribunais paginando a API até esgotar, comparando '
                'com o nosso acervo dia a dia. 41 tribunais sangram, 18 descartados '
                'com prova.',
        'ref': '.ia/ACERVO_CNJ.md',
    },
    {
        'slug': 'datajud',
        'nome': 'Datajud — esqueleto nacional',
        'papel': 'cadastro: existe o processo? (não traz parte, advogado nem valor)',
        'declarado': 343_235_554,
        'recuperavel': None,
        'medido_em': D(2026, 8, 14),
        'como': 'total declarado ao CNJ pelos 60 tribunais, somado da própria API.',
        'ref': '.ia/ACERVO_CNJ.md',
    },
    {
        'slug': 'diarios',
        'nome': 'Diários próprios — a terceira porta',
        'papel': 'o que existe antes do DJEN e o que ele nunca publicou',
        'declarado': None,
        'recuperavel': None,
        'medido_em': D(2026, 8, 20),
        'como': 'DJE/TJSP cobre 2007-10-01 → 2025-03-13 (o TJSP só entrou no DJEN '
                'em 14/03/2025). São ~4.162 edições × 9 cadernos. Um caderno '
                'medido: 4.229 páginas, 32.366 CNJs distintos.',
        'ref': '.ia/DIARIOS.md',
    },
]

#: Recuperável por tribunal (o teto por UF que decapitava o dia). Só os que
#: sangram; os 18 descartados estão no .ia/ACERVO_CNJ.md com a prova de cada um.
RECUPERAVEL_POR_TRIBUNAL = {
    'TJSP': 64_895_691, 'TJPR': 38_807_963, 'TJMG': 20_054_027, 'TRT2': 14_599_568,
    'TJRS': 11_709_666, 'TJGO': 11_055_268, 'TJRJ': 9_779_900, 'TJBA': 8_157_297,
    'TJSC': 7_627_068, 'TRT15': 5_491_200, 'TRF3': 4_013_425, 'TRT1': 3_462_830,
    'TRT3': 2_955_367, 'TRT4': 2_667_080, 'TJAM': 1_269_615, 'TJMT': 863_224,
    'TJPE': 812_345, 'TRT9': 734_000, 'TJRR': 620_000, 'TRF4': 606_811,
    'TJCE': 459_862, 'TRF6': 372_220, 'TJAP': 310_000, 'TJMA': 270_628,
    'TJSE': 216_095, 'TJMS': 210_786, 'TRF2': 183_233, 'TJTO': 103_000,
}

#: Ordem da Fase 2 — onde o crédito contra a Fazenda nasce e é pago. Não é
#: volume puro: o TJPR é o 2º do país e está FORA por decisão comercial.
FASE_2 = ['TJSP', 'TRF3', 'TJMG', 'TJRS', 'TJGO', 'TJRJ', 'TRF4', 'TRF6', 'TRF2']

#: Um dia grande coletado pelo caminho FATIADO tem razão itens/página ~490-500
#: (as 27 fatias terminam cada uma numa página parcial); o caminho flat dá
#: ~990-1000. ⚠️ A régua tem FALSO POSITIVO: o downshift de 5xx reduz o page
#: size pra 200/100 e derruba a razão de um dia que saiu flat. Por isso a tela
#: mostra as DUAS leituras (razão e data do run) em vez de fingir uma só.
RAZAO_CAMINHO_FLAT = 700
MIN_ITENS_DIA_GRANDE = 9_000

#: quando o caminho flat entrou em produção (worker_ingestion reiniciado com
#: ESTRATEGIA_UF=False). Run posterior a isto saiu pelo caminho novo.
CORTE_FLAT = datetime.datetime(2026, 8, 18, 9, 48)
