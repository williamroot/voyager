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

## ⚠️ Nunca subtraia um número daqui de um número vivo

Achado em 01/09/2026, no card do Datajud: `temos 344.630.543 · a fonte declara
343.235.554 · lacuna −1.394.989 · 100,4%`. Uma lacuna NEGATIVA — e nenhuma
descoberta sobre o acervo por trás dela: o nosso lado era relido a cada
aquecimento, o lado da fonte estava congelado em 14/08. Os dois cresceram; só um
foi atualizado.

**Diferença só entre números medidos no MESMO instante.** O confronto com o
Datajud virou um par `(declarado, nosso)` colhido junto, remedido em rodadas —
ver `dashboard/completude_datajud.py`. O que sobra neste arquivo é o
**retrato de fallback**, e ele é publicado com a data em letra grande.
"""
import datetime

D = datetime.date


#: As três portas de entrada do acervo. `declarado` é o lado da FONTE.
PORTAS = [
    {
        'slug': 'djen',
        'nome': 'DJEN — Comunica API do CNJ',
        'papel': 'veículo de comunicação: só o que virou publicação em diário',
        # Como esta porta se confere. NÃO existe "declarado" solto em nenhuma
        # das três: um total da fonte só significa alguma coisa ao lado do
        # NOSSO número do MESMO instante (ver a nota no topo do arquivo).
        'gabarito': 'dia_a_dia',
        # ESTIMATIVA de 18/08, congelada. O que de fato voltou é medido ao vivo
        # (`completude_warm._recuperacao`) e a tela põe os dois lado a lado com
        # a data de cada um. Ver `RECUPERAVEL_MEDIDO_EM`.
        'recuperavel': 212_504_437,
        'recuperavel_em': D(2026, 8, 18),
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
        'gabarito': 'confronto',
        'recuperavel': None,
        'medido_em': D(2026, 8, 31),
        'como': 'confronto par a par com a API do CNJ: `size:0` por tribunal, '
                'descontando as linhas sem `numeroProcesso` (o denominador do '
                'CNJ tem 5,5 milhões delas). Remedido em rodadas — a tela mostra '
                'a janela em que os pares foram colhidos.',
        'ref': '.ia/ACERVO_CNJ.md',
    },
    {
        'slug': 'diarios',
        'nome': 'Diários próprios — a terceira porta',
        'papel': 'o que existe antes do DJEN e o que ele nunca publicou',
        'gabarito': 'edicao',
        'recuperavel': None,
        'medido_em': D(2026, 8, 20),
        'como': 'DJE/TJSP cobre 2007-10-01 → 2025-03-13 (o TJSP só entrou no DJEN '
                'em 14/03/2025). São ~4.162 edições × 9 cadernos. Um caderno '
                'medido: 4.229 páginas, 32.366 CNJs distintos.',
        'ref': '.ia/DIARIOS.md',
    },
]

#: RETRATO do confronto com o Datajud — os DOIS lados colhidos na mesma
#: passada, em 01/09/2026 20:58–21:14 (`manage.py datajud_conferir_acervo
#: --json`, 59 tribunais + o STF, que não tem índice no CNJ).
#:
#: É fallback: o normal é a rodada viva de `dashboard/completude_datajud.py`.
#: O que ele prova, e por isso está aqui inteiro em vez de um número só:
#:
#:   declarado bruto ......... 350.430.801
#:   − sem `numeroProcesso` ..   5.516.266   (não são processo: CNJ null, classe -1)
#:   = denominador válido .... 344.914.535
#:   nosso, no mesmo instante  344.630.543   (= o `_count` do índice inteiro)
#:   falta ...................     283.992   (0,082%)   ·  sobra 0
#:
#: A "lacuna negativa" de 01/09 (−1.394.989) não tinha nada a ver com o acervo:
#: o declarado da tela era de 14/08 (343.235.554) e o nosso lado era relido a
#: cada 30 min. Remedido hoje, o declarado é **350.430.801** — 7,2 milhões
#: ACIMA do congelado — e o desconto das linhas vazias, que a tela nunca fazia,
#: vale outros 5,5 milhões. Os dois erros andavam no mesmo sentido: inflar a
#: nossa cobertura até passar de 100%.
CONFRONTO_DATAJUD = {
    'medido_em': D(2026, 9, 1),
    'tribunais': 59,
    'sem_fonte': ['STF'],
    'declarado': 350_430_801,
    'invalidos': 5_516_266,
    'util': 344_914_535,
    'nosso': 344_630_543,
    'falta': 283_992,
    'sobra': 0,
    'pct': 100.0 * 344_630_543 / 344_914_535,
}

#: Quando o recuperável abaixo foi ESTIMADO. Ele não se recalcula: é a sondagem
#: de 18/08 paginando a API dia a dia. Publicar sem esta data foi o defeito de
#: 01/09 — a coluna mostrava 64.895.691 no TJSP ao lado de `nunca_refeito = 0`,
#: e quem lia concluía que faltavam 64,9 milhões de publicações. Não faltavam.
RECUPERAVEL_MEDIDO_EM = D(2026, 8, 18)

#: Recuperável por tribunal (o teto por UF que decapitava o dia). Só os que
#: sangram; os 18 descartados estão no .ia/ACERVO_CNJ.md com a prova de cada um.
#:
#: ⚠️ É ESTIMATIVA, e ela erra PARA OS DOIS LADOS — medido em 02/09/2026 contra
#: o que a re-coleta de fato trouxe: TRF4 devolveu 1.224.553 contra 606.811
#: estimados (2,0×), TRF2 285.922 contra 183.233, TRF3 5.411.414 contra
#: 4.013.425; o TJSP devolveu 48.877.504 dos 64.895.691. Por isso a tela NÃO
#: publica `estimado − recuperado` como se fosse saldo: um resto negativo diria
#: "sobrou publicação", quando o que houve foi estimativa baixa. O que resta é
#: publicado em DIAS, que é o que se mede.
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

#: Tribunal que sangra e que NÃO é buraco nosso: está fora do alvo por decisão,
#: não por falha. Ele aparece na tabela — some da tela seria pior —, mas
#: marcado, e fora das somas de "o que falta recuperar". O TJPR sozinho é 43%
#: do estimado que resta na Fase 3 (38.807.963 de 89.637.928): somá-lo ao
#: buraco faria a Fase 3 parecer o dobro do problema que ela é para nós.
FORA_DO_ALVO = {'TJPR': 'fora do alvo por decisão comercial'}

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
