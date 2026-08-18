"""Semeia o Acompanhamento com as descobertas MEDIDAS de agosto/2026.

Não é dado de exemplo: é o histórico real da semana em que a completude do
acervo virou o princípio nº 1 do projeto. Cada nota carrega o número que a
sustenta, e as que não têm número dizem isso na cara.

Idempotente por `titulo` + `data_evento` — rodar de novo atualiza, não duplica.
"""
import datetime

from django.core.management.base import BaseCommand

from dashboard.models import NotaAcompanhamento as N

D = datetime.date

NOTAS = [
    {
        'titulo': 'Só tínhamos 13% do acervo nacional',
        'tipo': N.TIPO_DESCOBERTA, 'impacto': N.IMPACTO_ALTO,
        'area': 'ingestão', 'data_evento': D(2026, 8, 14),
        'resumo': 'A ingestão sempre foi DJEN-only, e o DJEN é veículo de comunicação, '
                  'não cadastro: processo sem publicação em diário nunca entrou.',
        'corpo': (
            'Um CNJ real do TJMG (5229078-89.2022.8.13.0024) não aparecia na busca. '
            'Ele existe no Datajud com 10 movimentos e tem ZERO publicações no DJEN — '
            'e como a nossa única porta de entrada é o DJEN, ele nunca teve como entrar.\n\n'
            'A medição que veio disso: amostra aleatória de 300 CNJs por tribunal, '
            'conferida um a um contra a nossa base. Falta de 81% (TRF1) a 96% (TJSP). '
            'Somando os 60 tribunais, o CNJ declara 343.235.554 processos.\n\n'
            'Perfil de quem falta: processo físico (93-100%), parado desde 2021 (98%), '
            'criminal e execução fiscal (~100%) — onde a intimação é pessoal ou por '
            'portal e nunca vira publicação em diário.'
        ),
        'numeros': [
            {'rotulo': 'acervo conhecido', 'antes': '71,4M', 'depois': '343,2M',
             'unidade': 'processos', 'nota': 'declarado ao CNJ vs o que tínhamos'},
            {'rotulo': 'cobertura', 'valor': '13', 'unidade': '%'},
            {'rotulo': 'nicho (cumprimento c/ Fazenda) no TJSP', 'valor': '5,2', 'unidade': '%'},
        ],
        'referencias': ['.ia/ACERVO_CNJ.md', 'ADR-029 em .ia/DECISIONS.md', 'commit ed4ae25'],
    },
    {
        'titulo': 'Varredura do Datajud: segunda porta de entrada',
        'tipo': N.TIPO_FEATURE, 'impacto': N.IMPACTO_ALTO,
        'area': 'ingestão', 'data_evento': D(2026, 8, 15),
        'resumo': 'Puxador que traz o esqueleto nacional do acervo declarado ao CNJ — '
                  '2,8× mais processos conhecidos, em 6h de requisição.',
        'corpo': (
            'O Datajud não substitui o DJEN: ele não traz parte, advogado nem valor. '
            'O que dá é o esqueleto — CNJ, classe, assunto, órgão, datas — e isso basta '
            'pra saber que o processo existe e pra ESCOLHER quem mandar pro enricher.\n\n'
            'A parte não-óbvia é a paginação: o índice do Datajud só ordena por '
            '@timestamp, que empata. `search_after` puro PULA documento em silêncio. '
            'Por isso paginamos por `range gte` relendo a cauda de propósito — o _id do '
            'próprio Datajud torna a reescrita idempotente.\n\n'
            'Gate executado: TRT20 varrido inteiro deu 235.758 contra 235.754 declarados. '
            'Os 4 a mais entraram durante os 162s de execução.'
        ),
        'numeros': [
            {'rotulo': 'CNJs conhecidos', 'antes': '71,4M', 'depois': '199,8M',
             'unidade': 'processos', 'nota': '75,3% dos varridos eram inéditos'},
            {'rotulo': 'varredura nacional', 'valor': '282,8M', 'unidade': 'docs'},
            {'rotulo': 'tribunais completos', 'valor': '55 de 59'},
        ],
        'referencias': ['datajud/varredura.py', '.ia/ACERVO_CNJ.md'],
    },
    {
        'titulo': 'A OAB sempre esteve no texto — a busca alcançava 0,26%',
        'tipo': N.TIPO_DESCOBERTA, 'impacto': N.IMPACTO_ALTO,
        'area': 'busca', 'data_evento': D(2026, 8, 15),
        'resumo': 'Buscar por OAB via campo estruturado enxergava 183 mil de 71 milhões. '
                  'O dado estava escrito no corpo das publicações o tempo todo.',
        'corpo': (
            'A OAB só existia onde o enricher passou — e enricher é o gargalo caro '
            '(112.820 processos/dia, 16 dos 60 tribunais). Só que ela está escrita nas '
            'publicações que já ingerimos: "ADVOGADO(A): FULANO (OAB RJ112211)".\n\n'
            'Buscar o texto cru não resolve: classificando 1.200 ocorrências reais, a OAB '
            'aparece em três formas (53,1% "OAB: UF123456", 45,8% "OAB 123456/UF", 0,9% '
            '"OAB/UF 123456"). Quem digita de um jeito perderia metade do acervo. Por isso '
            'a normalização acontece na ingestão, numa forma canônica.\n\n'
            'CPF, CNPJ e CNJ passam por dígito verificador antes de virar entidade: num '
            'texto de intimação há guia, protocolo e conta bancária que parecem documento.'
        ),
        'numeros': [
            {'rotulo': 'cobertura de OAB', 'antes': '0,26', 'depois': '22', 'unidade': '%',
             'nota': '85× — processos com publicação citando OAB'},
            {'rotulo': 'publicações que citam sentença', 'valor': '61.747.787'},
            {'rotulo': 'entidades extraídas até agora', 'valor': '11,8M', 'unidade': 'OABs'},
        ],
        'referencias': ['search/entidades_texto.py', 'tests/test_entidades_texto.py'],
    },
    {
        'titulo': 'A tela dizia que partes e advogados cobriam 100% da base',
        'tipo': N.TIPO_INCIDENTE, 'impacto': N.IMPACTO_MEDIO,
        'area': 'busca', 'data_evento': D(2026, 8, 15),
        'resumo': 'O `exists` do Elasticsearch conta string vazia como valor presente. '
                  'Medido por amostra: 20,4% e 18,6%.',
        'corpo': (
            'A busca carregava um envelope de honestidade que informava a cobertura de '
            'cada campo — e afirmava que `partes` e `advs` valiam para a base inteira. '
            'A medição usava `exists`, que no ES conta "" como valor.\n\n'
            'O código já sabia disso em outro lugar: havia um helper `_nao_vazio` '
            'corrigindo exatamente esse erro para `orgao_julgador`. Só que o truque '
            '(`must_not term ""`) não funciona em campo `text` — num campo analisado, "" '
            'não casa nada e o must_not não remove ninguém.\n\n'
            'Agora campo de texto é medido por AMOSTRA de 1.000 documentos, marcado como '
            'estimativa no payload. Dois testes que afirmavam o comportamento errado '
            'foram corrigidos, e entrou um que proíbe campo texto de se declarar total.'
        ),
        'numeros': [
            {'rotulo': 'cobertura de partes', 'antes': '100', 'depois': '20,4', 'unidade': '%'},
            {'rotulo': 'cobertura de advogados', 'antes': '100', 'depois': '18,6', 'unidade': '%'},
        ],
        'referencias': ['search/busca_ui.py', 'tests/test_busca_processos.py'],
    },
    {
        'titulo': 'Busca no texto das publicações',
        'tipo': N.TIPO_FEATURE, 'impacto': N.IMPACTO_ALTO,
        'area': 'busca', 'data_evento': D(2026, 8, 16),
        'resumo': '94 milhões de publicações estavam indexadas e fora do alcance da tela: '
                  'a busca só olhava o índice de processos.',
        'corpo': (
            'O texto sempre esteve indexado e servido pela API externa. Nenhuma tela o '
            'consultava.\n\n'
            'Entrou um seletor de escopo explícito (Processos × No texto), e não um "modo '
            'esperto" que adivinha: são índices diferentes respondendo perguntas '
            'diferentes, e a unidade do resultado muda junto — no texto, o resultado é a '
            'PUBLICAÇÃO com trecho destacado.\n\n'
            'A resposta carrega a composição do acervo porque ela desmente a leitura '
            'fácil: 82,9% do que temos são rótulos curtos de andamento ("Conclusão · para '
            'despacho"), 13% é publicação de verdade e 3,6% é peça longa.'
        ),
        'numeros': [
            {'rotulo': 'publicações com "poder judiciário"', 'valor': '94.065.570'},
            {'rotulo': 'com "precatório"', 'valor': '3.086.016'},
        ],
        'referencias': ['dashboard/busca_views.py', 'tests/test_busca_conteudo.py'],
    },
    {
        'titulo': 'Índice de movimentações migrado: 1 shard de 685 GB → 16 shards',
        'tipo': N.TIPO_FEATURE, 'impacto': N.IMPACTO_ALTO,
        'area': 'elasticsearch', 'data_evento': D(2026, 8, 17),
        'resumo': 'O shard único estava em 55% do teto rígido do Lucene (2,147 bilhões de '
                  'docs). Quando estoura, o índice para de aceitar escrita.',
        'corpo': (
            'Migração feita com espelho de escrita ligado ANTES da cópia — sem ele, as '
            'horas de publicação da janela se perderiam no cutover, e um catch-up por id '
            'no fim não cobriria UPDATE em doc antigo (que é o que o enriquecimento faz o '
            'tempo todo).\n\n'
            'Gate antes de apagar o v1: contagem idêntica e 500 documentos sorteados '
            'comparados campo a campo. E uma recópia da cauda achou 757 documentos que '
            'existiam SÓ no índice velho — a janela em voo era real.\n\n'
            'A migração também trouxe os campos de entidade extraída do texto.'
        ),
        'numeros': [
            {'rotulo': 'shards', 'antes': '1', 'depois': '16'},
            {'rotulo': 'documentos', 'valor': '1.165.338.513'},
            {'rotulo': 'amostra do gate', 'valor': '500 de 500', 'unidade': 'idênticos'},
            {'rotulo': 'recuperados da cauda', 'valor': '757', 'unidade': 'docs'},
        ],
        'referencias': ['search/management/commands/es_movs_v2.py'],
    },
    {
        'titulo': 'Uma linha decapitava 43,6% do TJSP — todo dia, por 17 meses',
        'tipo': N.TIPO_DESCOBERTA, 'impacto': N.IMPACTO_ALTO,
        'area': 'ingestão', 'data_evento': D(2026, 8, 17),
        'resumo': '`for pagina in range(1, 11)` — teto de 10 páginas por fatia de UF, com '
                  'o comentário "nenhum UF chega perto". A premissa era falsa.',
        'corpo': (
            'A cadeia tinha três camadas. O campo `count` da API devolve 10.000 fixo em '
            'página pequena (é o max_result_window do Elasticsearch por baixo — um PISO '
            'disfarçado de total). A ingestão lê isso, conclui "capou" e desvia pro '
            'fatiamento por ufOab. Só que no TJSP a fatia de SP é do tamanho do bolo '
            'inteiro. E aí o teto de 10 páginas corta cada fatia.\n\n'
            'Provei que o teto NÃO existe na API: paginei até a página 2000 (item ~199.901) '
            'e continuava vindo dado novo. O limite era nosso.\n\n'
            'Descartei lag de indexação antes de cravar: o ES tinha 117.161 e o Postgres '
            '117.215 para o mesmo dia — praticamente idênticos. A ingestão não coletou '
            'mesmo.\n\n'
            'O conserto teve um segundo ato: tirar o teto sem mudar o resto fez a função '
            'acumular 208 mil publicações em memória e os workers morreram com signal 9. '
            'Agora é fluxo — fila limitada, generator, pico de memória do tamanho da fila '
            'e não do dia. Virou o princípio "itere, não acumule" no CLAUDE.md.'
        ),
        'numeros': [
            {'rotulo': 'TJSP em 14/08/2026', 'antes': '40.427', 'depois': '257.832',
             'unidade': 'publicações', 'nota': '6,4× — tínhamos 15,7% daquele dia'},
            {'rotulo': 'processos distintos no dia', 'valor': '238.928'},
            {'rotulo': 'dias a reprocessar', 'valor': '594', 'nota': 'desde 14/03/2025'},
        ],
        'referencias': ['djen/ingestion.py', 'tests/test_djen_uf_sem_teto.py', 'CLAUDE.md'],
    },
    {
        'titulo': 'O DJE do TJSP é arquivo fechado desde 22/07/2025',
        'tipo': N.TIPO_MEDICAO, 'impacto': N.IMPACTO_MEDIO,
        'area': 'diários', 'data_evento': D(2026, 8, 17),
        'resumo': 'O TJSP entrou no DJEN em 14/03/2025 e desligou o diário próprio quatro '
                  'meses depois. O valor do DJE ficou sendo só o histórico.',
        'corpo': (
            'Eu tinha justificado o coletor do DJE dizendo que "o TJSP publica no diário '
            'próprio, por isso 61,8% do caderno de julho/2025 é novo". Estava errado: os '
            '61,8% eram o NOSSO teto de 10 páginas.\n\n'
            'Medido: 22/07/2025 devolve PDF de 4,1 MB; 23/07/2025 em diante devolve 851 '
            'bytes de página de erro.\n\n'
            'O que sobra de valor real: 2007-10-01 → 2025-03-13. Dezoito anos em que o '
            'DJEN não cobria o TJSP de forma alguma — e nisso o DJE continua sendo a '
            'única porta.'
        ),
        'numeros': [
            {'rotulo': 'última edição do DJE', 'valor': '22/07/2025'},
            {'rotulo': 'edições sem cobertura DJEN', 'valor': '4.077 de 4.162'},
        ],
        'referencias': ['.ia/DIARIOS.md'],
    },
    {
        'titulo': 'Derrubei o site por 50 minutos',
        'tipo': N.TIPO_INCIDENTE, 'impacto': N.IMPACTO_ALTO,
        'area': 'deploy', 'data_evento': D(2026, 8, 17),
        'resumo': 'git pull em produção trouxe migrations que não deviam subir; o CREATE '
                  'INDEX CONCURRENTLY ficou esperando o reclassificador.',
        'corpo': (
            'A cadeia: subi para prod um trabalho explicitamente marcado como "não ligar '
            'em produção". O web roda `migrate` no boot, e a migration cria índice '
            'CONCURRENTLY, que espera TODAS as transações abertas — havia 31 conexões do '
            'reclassificador varrendo há 3h30.\n\n'
            'Cancelei as varreduras antigas (SELECT read-only, o job re-tenta), a migration '
            'destravou, aplicou coluna e índice, e morreu antes de se registrar. No retry: '
            '"column already exists". Conferi no banco que coluna e índice existiam e eram '
            'válidos, registrei com --fake e apliquei o resto de um container saudável.\n\n'
            'Havia um segundo defeito meu no meio: a amostragem de cobertura que eu tinha '
            'escrito rodava no caminho da requisição sem teto de espera. Com o ES em '
            'forcemerge, passou do timeout do gunicorn e matou o worker em loop.\n\n'
            'Duas lições viraram regra: conferir migrations pendentes ANTES de reiniciar o '
            'web, e nada no caminho da requisição sem request_timeout.'
        ),
        'numeros': [
            {'rotulo': 'indisponibilidade', 'valor': '~50', 'unidade': 'min'},
            {'rotulo': 'conexões travando a migration', 'valor': '31'},
        ],
        'referencias': ['.ia/OPS.md', 'search/busca_ui.py'],
    },
]


class Command(BaseCommand):
    help = 'Semeia o Acompanhamento com as descobertas medidas de agosto/2026'

    def handle(self, *a, **o):
        criadas = atualizadas = 0
        for n in NOTAS:
            _, novo = N.objects.update_or_create(
                titulo=n['titulo'], data_evento=n['data_evento'],
                defaults={k: v for k, v in n.items()
                          if k not in ('titulo', 'data_evento')},
            )
            criadas += bool(novo)
            atualizadas += (not novo)
        self.stdout.write(self.style.SUCCESS(
            f'acompanhamento: {criadas} notas criadas, {atualizadas} atualizadas '
            f'({N.objects.count()} no total)'))
