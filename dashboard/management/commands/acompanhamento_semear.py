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
    {
        'titulo': 'A coleta caía por falta de descritor, não por proxy ruim',
        'tipo': N.TIPO_DESCOBERTA, 'impacto': N.IMPACTO_ALTO,
        'area': 'ingestão', 'data_evento': D(2026, 8, 17),
        'resumo': 'Um cache de proxy que nunca encolhe estourava o limite de arquivos '
                  'abertos do worker — e a partir daí TODO request falhava, inclusive '
                  'os que iriam pra proxy saudável.',
        'corpo': (
            'O sintoma era ruído comum: 403, rotação de proxy, "proxy ruim". Debaixo '
            'dele, `[Errno 24] Too many open files` — e 2.981 IngestionRun marcados '
            '`failed` pelo watchdog em 7 dias. Sem OOM e com RAM sobrando: não era '
            'memória, era descritor.\n\n'
            'Causa: `session.get(proxies=...)` faz a requests guardar um pool de '
            'conexões POR URL de proxy, num dicionário que nunca encolhe. Giramos '
            'sobre centenas de IPs; cada IP queimado deixava as conexões dele '
            'penduradas. Com nofile=1024 (o default do Docker) o processo esgotava e '
            'passava a falhar tudo. Na coleta por UF são 8 fetchers dividindo a MESMA '
            'sessão — 8× mais rápido pra estourar.\n\n'
            'O que faz isso ser grave: a falha some dentro do retry, o run vira '
            '`failed`, e o dia fica coletado pela metade sem ninguém olhar. É perda '
            'de cobertura silenciosa — o defeito nº 1 da lista.\n\n'
            'Cura em duas metades: sessão com cache de proxy limitado (LRU de 32, '
            'fechando o pool mais antigo) em todos os clientes que giram proxy, e '
            'nofile=65536 nos 20 workers que fazem HTTP externo. Só um serviço tinha '
            'o limite alto — alguém já havia batido nisto e consertado um só.'
        ),
        'numeros': [
            {'rotulo': 'runs derrubados pelo watchdog', 'valor': '2.981',
             'unidade': 'em 7 dias'},
            {'rotulo': 'limite de arquivos abertos', 'antes': '1.024', 'depois': '65.536',
             'nota': 'em 20 dos 24 serviços de worker'},
            {'rotulo': 'Errno 24 depois do deploy', 'valor': '0', 'unidade': 'em 40 min'},
        ],
        'referencias': ['djen/proxies.py', 'tests/test_proxy_fd_leak.py',
                        '.ia/OPS.md', 'commit e3edba0'],
    },
    {
        'titulo': 'A extração de entidades não convergia porque recomeçava do zero',
        'tipo': N.TIPO_DESCOBERTA, 'impacto': N.IMPACTO_ALTO,
        'area': 'busca', 'data_evento': D(2026, 8, 17),
        'resumo': 'Sem carimbo de "já processei", cada morte do processo jogava fora o '
                  'trabalho: 17,4M de 126M documentos depois de várias tentativas.',
        'corpo': (
            'A passada que extrai OAB, CPF/CNPJ e CNJ do texto das publicações guardava '
            'o cursor só na memória do processo. Quando o Elasticsearch abria o '
            'circuit-breaker e matava a passada, o relançamento recomeçava a faixa '
            'inteira. Com 126M documentos no alvo, isso nunca termina.\n\n'
            'O caso que ninguém pensa: o documento que NÃO rende entidade nenhuma. Ele '
            'não ganhava campo algum, então voltava pra fila em toda passada, pra '
            'sempre. Agora todo documento lido recebe `ents_v` — inclusive esse — e '
            'quem tem o carimbo sai do alvo.\n\n'
            'Conferido contra o índice real: a faixa 0 tinha 2.739.487 pendentes, rodei '
            '20.000, sobraram 2.716.764, e a chamada seguinte não repetiu nenhum.'
        ),
        'numeros': [
            {'rotulo': 'alvo da extração', 'valor': '126,0M', 'unidade': 'docs',
             'nota': 'os que o índice diz citar OAB/CPF/CNPJ/R$'},
            {'rotulo': 'já extraído', 'valor': '17,4M', 'unidade': 'docs (13,8%)'},
            {'rotulo': 'retrabalho por relançamento', 'antes': 'faixa inteira',
             'depois': 'zero'},
        ],
        'referencias': ['search/management/commands/es_movs_v2.py',
                        'search/mappings.py', 'commit 9ed0204'],
    },
    {
        'titulo': '212 milhões de publicações recuperáveis — medido nos 59 tribunais',
        'tipo': N.TIPO_DESCOBERTA, 'impacto': N.IMPACTO_ALTO,
        'area': 'ingestão', 'data_evento': D(2026, 8, 18),
        'resumo': 'Sondando a API de cada tribunal até esgotar e comparando com o que '
                  'temos: 41 de 59 sangram, e o buraco vale +16% do acervo.',
        'corpo': (
            'A pergunta era simples: quanto o teto de páginas por UF custou em cada '
            'tribunal? A resposta exigiu paginar a API na força bruta, tribunal a '
            'tribunal, e comparar com a nossa base pela régua certa.\n\n'
            'TJSP perde 84,7% de cada dia útil (64,9 milhões recuperáveis). TJPR 73,8% '
            '(38,8M). TJMG 70,1% (20,1M). Dezoito tribunais foram descartados COM '
            'PROVA — e o padrão dos descartes é estrutural, não sorte: STJ e TST são '
            'nacionais, com o volume espalhado pelas 27 seccionais; os TRTs pequenos '
            'nunca cruzaram 10.000 num dia.\n\n'
            'Dois avisos que valem mais que o ranking. Primeiro, a régua canônica é o '
            'Postgres filtrado ao DJEN: o Elasticsearch infla (mistura movimento do '
            'Datajud — 4,7× no TJMG) e desinfla (lag do reindexador), e medir por ele '
            'sem filtro de tipo erra em até 15×. Segundo, existe um buraco SEPARADO e '
            'provavelmente maior: dias que nunca foram ingeridos. O TRF1 tem 710 de '
            '1.480 dias úteis com menos de 100 publicações — num deles a API entrega '
            '53.919 e nós temos 9.'
        ),
        'numeros': [
            {'rotulo': 'recuperável', 'valor': '212.504.437', 'unidade': 'publicações',
             'nota': '+16% sobre 1,344 bilhão'},
            {'rotulo': 'tribunais medidos', 'antes': '1', 'depois': '59'},
            {'rotulo': 'sangram', 'valor': '41', 'unidade': 'de 59'},
            {'rotulo': 'dias-tribunal a refazer', 'valor': '12.934'},
        ],
        'referencias': ['.ia/ACERVO_CNJ.md', 'scripts/backfill_dias_capados.py'],
    },
    {
        'titulo': 'A coleta fatiada era cega a 15,7% de cada dia',
        'tipo': N.TIPO_DESCOBERTA, 'impacto': N.IMPACTO_ALTO,
        'area': 'ingestão', 'data_evento': D(2026, 8, 18),
        'resumo': 'O canário leu um dia inteiro do TJSP direto da API: 261.076 de '
                  '261.076. Dessas, 41.057 o caminho antigo nunca teve como ver.',
        'corpo': (
            'Fatiar o dia em 27 requisições por estado da OAB respondia a uma crença: '
            '"a API capa em 10.000". A crença caiu — o 10.000 é o limite de janela do '
            'Elasticsearch por baixo, ou seja um PISO, e a paginação vai até o fim '
            '(262 páginas, todos os ids distintos).\n\n'
            'Pior: fatiar por OAB só enxerga publicação que cita advogado com OAB. '
            'Isso foi provado na unidade em cinco tribunais antes do canário — no TJPE '
            'as 2.853 publicações sem OAB de um dia eram exatamente as 2.853 que '
            'faltavam. O canário mediu num dia completo do maior tribunal do país: '
            '15,7%, bem acima dos 2-10% estimados.\n\n'
            'O primeiro canário REPROVOU, e reprovou provando outro defeito ao vivo: '
            'rodou com o código antigo, a fatia de SP levou 403 após 51 rotações de '
            'proxy, e o run gravou "sucesso" com 30,6% do dia. Fui atrás do saldo '
            'disso no histórico: 1.232 runs verdes escondendo fatia perdida, cobrindo '
            '1.165 dias que o sistema considerava cobertos para sempre.'
        ),
        'numeros': [
            {'rotulo': 'canário', 'valor': '261.076 de 261.076', 'unidade': '(100,0%)'},
            {'rotulo': 'cegueira do caminho antigo', 'valor': '15,7', 'unidade': '% do dia',
             'nota': '41.057 publicações num dia só'},
            {'rotulo': 'runs verdes que mentiam', 'valor': '1.232',
             'unidade': 'cobrindo 1.165 dias'},
            {'rotulo': 'requisições à API por dia', 'antes': '27×', 'depois': '1×'},
        ],
        'referencias': ['djen/ingestion.py', 'djen/client.py',
                        'tests/test_djen_coleta_flat.py', 'commit cfe3084'],
    },
    {
        'titulo': 'O gargalo não era hardware: era 1,25 PB de disco lido por 4 queries',
        'tipo': N.TIPO_DESCOBERTA, 'impacto': N.IMPACTO_ALTO,
        'area': 'banco', 'data_evento': D(2026, 8, 18),
        'resumo': 'Sem RAM pra comprar, medimos ONDE a leitura estava. Quatro '
                  'queries respondiam por 1,25 petabyte — e três eram falta de índice.',
        'corpo': (
            'A coleta tinha parado: zero dia recuperado por hora. O diagnóstico '
            'começou pelo óbvio — o banco tem 1,7 TB e só ~22 GB de cache, ou seja '
            'um conjunto de trabalho 14 vezes maior que a memória. O cache hit '
            'estava em 82% (o saudável é acima de 99%) e 41 das 44 consultas ativas '
            'esperavam disco.\n\n'
            'A conclusão fácil seria "falta RAM". Mas o disco é NVMe e estava '
            'entregando 2 GB/s — não era lentidão, era saturação. Então a pergunta '
            'certa virou outra: quem está lendo tanto?\n\n'
            'O `pg_stat_statements` respondeu, e a resposta não era a ingestão nem a '
            'reindexação:\n\n'
            '  436.191 GB em 6.476 chamadas — a fila do reclassificador\n'
            '  393.605 GB em 33.420 chamadas — o contador do dashboard\n'
            '  247.732 GB em 15.735 chamadas — o gráfico diário do dashboard\n'
            '  179.779 GB em 664.017 chamadas — a fila do enriquecimento\n\n'
            'A do reclassificador lia 67 GB POR CHAMADA. O motivo é sutil: ela '
            'compara duas colunas da mesma linha (a classificação está mais velha '
            'que a última movimentação?), e nenhum índice comum cobre isso — então '
            'ela varria os 108 GB da tabela de processos e ordenava. Toda vez.\n\n'
            'A cura foi um índice PARCIAL. A condição de um índice parcial aceita '
            'comparação entre colunas, então dá pra indexar exatamente "as linhas '
            'que ainda precisam ser classificadas" — que são uma fração minúscula. '
            'Ele ocupa 56 MB. Mais um índice composto para as duas consultas do '
            'dashboard, e o descarte de 19 GB de índice que tinha ZERO leituras na '
            'vida inteira do banco e mesmo assim era pago em cada gravação.\n\n'
            'A operação tinha um risco conhecido: criar índice numa tabela quente '
            'espera TODAS as transações abertas terminarem, e o reclassificador '
            'segura transações de 37 minutos. Foi exatamente isso que derrubou o '
            'site por 50 minutos em 10/08. Então a ordem foi: parar quem segura '
            'transação, cancelar as consultas longas, conferir que chegou a zero, '
            'criar, religar. O site respondeu 200 em todas as amostras.'
        ),
        'numeros': [
            {'rotulo': 'leitura de disco do banco', 'antes': '2.007 MB/s', 'depois': '33 MB/s',
             'nota': 'e com MAIS carga rodando em cima'},
            {'rotulo': 'consultas esperando disco', 'antes': '41 de 44', 'depois': '2 de 8'},
            {'rotulo': 'fila do reclassificador', 'antes': '67 GB', 'depois': '3,8 MB',
             'nota': 'de 2.260 s para 144 ms'},
            {'rotulo': 'contador do dashboard', 'antes': '11,8 GB', 'depois': '32 KB',
             'unidade': 'por chamada'},
            {'rotulo': 'índice que resolveu', 'valor': '56', 'unidade': 'MB',
             'nota': 'parcial: guarda só as linhas pendentes'},
        ],
        'referencias': ['tribunals/migrations/0050_indices_io.py', '.ia/OPS.md',
                        'commit 9fff9c3'],
    },
    {
        'titulo': 'Fase 2 da recuperação: 9 tribunais, 3.693 dias na fila',
        'tipo': N.TIPO_FEATURE, 'impacto': N.IMPACTO_ALTO,
        'area': 'ingestão', 'data_evento': D(2026, 8, 19),
        'resumo': 'Com o banco respirando, a recuperação saiu do TJSP sozinho para '
                  'os nove tribunais onde o crédito contra a Fazenda nasce e é pago.',
        'corpo': (
            'A ordem não é por volume — é por densidade creditória dividida pelo '
            'custo de fila. O TRF3 vale "só" 4 milhões de publicações e entra em '
            'segundo lugar porque é a Justiça Federal de São Paulo: precatório e '
            'RPV federais contra a União e o INSS valem mais, por publicação, que '
            'intimação cível de qualquer outro lugar.\n\n'
            'Dito com todas as letras: o TJPR é o segundo do país em volume bruto '
            '(38,8 milhões recuperáveis) e ficou de fora desta onda por decisão de '
            'negócio, não por medição. O mercado de precatório do Paraná é uma '
            'fração do de SP, MG e RJ, e os 1.209 dias dele seriam quatro semanas '
            'da fila que entregam TJSP e TRF3 inteiros. Se a prioridade comercial '
            'mudar, ele sobe sem discussão — a medição dele é sólida.\n\n'
            'Como os dias são escolhidos: pela ASSINATURA do caminho antigo. A '
            'coleta fatiada gravava em lotes de 500, e cada uma das 27 fatias '
            'terminava numa página parcial — então a média cai para ~490-500 itens '
            'por página. O caminho novo dá ~990-1000. Qualquer dia com mais de '
            '9.000 publicações e razão abaixo de 700 foi coletado pelo caminho '
            'capado. O identificador do job é determinístico, então re-disparar não '
            'duplica, e o script guarda o run mais recente de cada dia — dia já '
            'refeito não volta para a fila.\n\n'
            'A ingestão foi de 8 para 14 workers no mesmo movimento: com os índices '
            'de I/O no lugar, o gargalo deixou de ser o banco e passou a ser a '
            'coleta.'
        ),
        'numeros': [
            {'rotulo': 'tribunais na fila', 'antes': '1', 'depois': '9'},
            {'rotulo': 'dias-tribunal enfileirados', 'valor': '3.693',
             'nota': '325 do TJSP + 3.368 dos outros oito'},
            {'rotulo': 'já capturado nesses dias', 'valor': '71,9M', 'unidade': 'publicações',
             'nota': 'o recuperável é múltiplo disso'},
            {'rotulo': 'workers de ingestão', 'antes': '8', 'depois': '14'},
            {'rotulo': 'ritmo medido', 'valor': '7,0M', 'unidade': 'publicações novas / 6h',
             'nota': 'só o TJSP, antes de escalar'},
        ],
        'referencias': ['scripts/backfill_dias_capados.py', '.ia/ACERVO_CNJ.md'],
    },
    {
        'titulo': 'Balanço de 12h: 14,4 milhões de publicações e o buraco do índice fechando',
        'tipo': N.TIPO_MEDICAO, 'impacto': N.IMPACTO_ALTO,
        'area': 'ingestão', 'data_evento': D(2026, 8, 19),
        'resumo': 'Primeira noite com a Fase 2 nos nove tribunais e a reindexação em '
                  'paralelo. 760 dias recuperados e +184 milhões de documentos que '
                  'saíram da invisibilidade.',
        'corpo': (
            'COLETA — 12 horas de recuperação nacional:\n\n'
            '  760 dias-tribunal concluídos\n'
            '  14.382.811 publicações novas (1,20 milhão por hora)\n\n'
            'O ritmo cai ao longo da janela (287 mil/h na última hora) porque os '
            'tribunais de dias grandes drenaram primeiro — TJGO fechou 309 dias, '
            'TJMG 142 — e sobrou o que ainda não começou. Continuam na fila 3.436 '
            'dias, liderados por TRF3 (969), TJMG (452), TJRJ (390), TJRS (300).\n\n'
            'ÍNDICE DE BUSCA — o número que mais importa aqui:\n\n'
            '  1.170.836.739  →  1.355.067.329 documentos\n\n'
            'São +184 milhões de publicações que já estavam no banco e simplesmente '
            'não eram alcançáveis pela busca. Quatro dos seis shards de reindexação '
            'já terminaram; os outros fecham em algumas horas.\n\n'
            'ENTIDADES — e uma correção de método. A primeira leitura deu 205% de '
            'cobertura, o que é impossível: eu estava dividindo pelo alvo medido '
            'ANTES de a recuperação e a reindexação trazerem publicação nova. '
            'Remedido contra o índice de hoje, o alvo são 178.672.695 documentos '
            '(os que citam OAB, CPF, CNPJ ou valor) e faltam 60.972.152 — 65,9% '
            'coberto. O alvo CRESCEU de 126 para 178,7 milhões justamente porque '
            'estamos coletando mais; é a métrica acompanhando o acervo maior, não '
            'uma regressão.\n\n'
            'O QUE NÃO ESTÁ BOM: 153 falhas em 12 horas, e 70 delas são deadlock no '
            'insert em lote. O conserto de ontem (ordenar as chaves antes de '
            'inserir) reduziu mas não eliminou — com 14 workers no mesmo tribunal a '
            'chance de colisão voltou a subir. Nenhum dia se perde (o watchdog novo '
            'devolve, e devolveu 36 na primeira execução), mas cada deadlock queima '
            'um dia inteiro de coleta. Falta ordenar também o catálogo de classes e '
            'o update de resumo dos processos, que tocam as mesmas linhas.'
        ),
        'numeros': [
            {'rotulo': 'publicações recuperadas', 'valor': '14.382.811', 'unidade': 'em 12h',
             'nota': '760 dias-tribunal, 1,20M/h'},
            {'rotulo': 'documentos no índice de busca',
             'antes': '1,171 bi', 'depois': '1,355 bi',
             'nota': '+184 milhões que estavam fora de alcance'},
            {'rotulo': 'entidades extraídas', 'valor': '117,7M de 178,7M',
             'unidade': '(65,9%)', 'nota': 'alvo remedido; o antigo dava 205%'},
            {'rotulo': 'dias ainda na fila', 'valor': '3.436'},
            {'rotulo': 'falhas por deadlock', 'valor': '70', 'unidade': 'em 12h',
             'nota': 'não perde dado, mas queima o dia'},
        ],
        'referencias': ['djen/jobs.py', 'search/management/commands/es_movs_v2.py'],
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
