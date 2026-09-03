"""Os avanços da semana, com o número de ANTES e o de AGORA — medidos.

A tela de Acompanhamento guarda o relato; este card guarda a RÉGUA. A diferença
importa: um relato diz "consertamos a duplicação"; a régua diz **2,70 → 1,02**,
e é a régua que permite alguém conferir daqui a um mês se regrediu.

## Como os dois lados são obtidos, e por que são diferentes

**AGORA** é sempre MEDIDO, a cada aquecimento. Nenhum número aqui é digitado.

**ANTES** é uma constante com a DATA em que foi medido — e tem que ser, porque
não existe máquina do tempo: a duplicação de 2,70× de 27/08 não se recalcula
hoje, o dia já passou. Fingir que dá seria pior que assumir. Cada marco carrega
`medido_em` e a tela mostra.

Regra que este arquivo não pode quebrar: **marco cujo "agora" não deu para medir
sai da lista**, com o motivo. Meia régua é pior que régua nenhuma — ela dá
confiança sem cobertura.
"""
import datetime
import logging

from django.core.cache import cache
from django.db import connection, transaction
from django.utils import timezone

logger = logging.getLogger('voyager.dashboard.marcos')

CHAVE = 'marcos_semana:v1'
TTL = 60 * 60 * 30


def _sql_um(sql, params=None, teto='60s'):
    with transaction.atomic(), connection.cursor() as c:
        c.execute('SET LOCAL statement_timeout = %s', [teto])
        c.execute(sql, params or [])
        linha = c.fetchone()
        return linha[0] if linha else None


# --------------------------------------------------------------------------
# como medir o AGORA de cada marco
# --------------------------------------------------------------------------
#: Último dia com COLETA DE VERDADE — o mais recente em que alguma página foi
#: lida. Sábado e domingo têm run para os 59 tribunais e **zero páginas**, porque
#: não há publicação. Usar "o último dia" cru fazia o marco de páginas mostrar
#: `14.760 → 0` num sábado: tecnicamente certo, e enganoso — parece a vitória do
#: século e é só o fim de semana. Número que engana é pior que número ausente.
_SQL_DIA_UTIL = """
SELECT max(janela_inicio) FROM tribunals_ingestionrun
 WHERE started_at > now() - interval '8 days'
   AND janela_inicio IN (SELECT janela_inicio FROM tribunals_ingestionrun
                          GROUP BY janela_inicio HAVING sum(paginas_lidas) > 0)
"""


def _fator_duplicacao():
    """`runs / tribunais` do último dia COM coleta. 1,0 = cada dia lido uma vez."""
    r = _sql_um("""SELECT count(*)::float / NULLIF(count(DISTINCT tribunal_id), 0)
                     FROM tribunals_ingestionrun
                    WHERE janela_inicio = (%s)""" % _SQL_DIA_UTIL, teto='90s')
    return round(r, 2) if r else None


def _paginas_do_dia():
    return _sql_um("""SELECT coalesce(sum(paginas_lidas), 0)
                        FROM tribunals_ingestionrun
                       WHERE janela_inicio = (%s)""" % _SQL_DIA_UTIL, teto='90s')


def _idade_sync_h():
    import search.sync_incremental as S
    ts, _ = S._wm_par(cache.get(S._WM_PROC_TS))
    if not ts:
        return None
    return round((timezone.now() - ts).total_seconds() / 3600, 2)


def _partes_djen():
    return _sql_um("SELECT count(*) FROM tribunals_processoparte WHERE fonte = 'djen'",
                   teto='90s')


def _process_com_grau():
    return _sql_um("SELECT count(*) FROM tribunals_process WHERE grau <> ''", teto='120s')


def _frota_vista():
    """Quantos workers o alarme de código velho consegue ENUMERAR."""
    import django_rq
    from djen.jobs import _frota_viva
    return len(_frota_viva(django_rq.get_connection('default')))


def _cobertura_nacional():
    from dashboard import cobertura_nacional
    p = cobertura_nacional.ler()
    return p.get('cobertura') if p else None


def _dias_fechados():
    """Tribunais fechados no portão, no último dia útil conferido."""
    from tribunals import portao
    ontem = timezone.localdate() - datetime.timedelta(days=1)
    r = portao.conferir(ontem)
    return f"{r['fechados']}/{r['tribunais']}"


#: Cada marco: rótulo, o ANTES com a data em que foi medido, como medir o AGORA,
#: e se subir é bom. `nota` é o que a régua significa em uma linha.
MARCOS = [
    {'k': 'cobertura', 'rotulo': 'Cobertura do acervo nacional',
     'antes': '13%', 'medido_em': '14/08', 'sufixo': '%', 'sobe': True,
     'fn': _cobertura_nacional,
     'nota': 'quantos dos processos que existem no país nós temos'},
    {'k': 'partes', 'rotulo': 'Partes tiradas do texto da publicação',
     'antes': 0, 'medido_em': '25/08', 'sufixo': '', 'sobe': True,
     'fn': _partes_djen,
     'nota': 'sem uma requisição ao tribunal — tribunais sem enricher ganham parte'},
    {'k': 'grau', 'rotulo': 'Processos com grau preenchido',
     'antes': 0, 'medido_em': '26/08', 'sufixo': '', 'sobe': True,
     'fn': _process_com_grau,
     'nota': 'é o grau de origem que separa RPV de precatório'},
    {'k': 'sync', 'rotulo': 'Atraso da busca (escrita em lote → índice)',
     'antes': 128.26, 'medido_em': '26/08', 'sufixo': ' h', 'sobe': False,
     'fn': _idade_sync_h,
     'nota': 'estava DIVERGINDO: perdia 13 s de relógio a cada segundo'},
    {'k': 'duplicacao', 'rotulo': 'Duplicação da coleta diária',
     'antes': 2.70, 'medido_em': '27/08', 'sufixo': '×', 'sobe': False,
     'fn': _fator_duplicacao,
     'nota': '1,0 = cada tribunal-dia lido uma vez só'},
    {'k': 'paginas', 'rotulo': 'Páginas pedidas ao CNJ por dia',
     'antes': 14_760, 'medido_em': '27/08', 'sufixo': '', 'sobe': False,
     'fn': _paginas_do_dia,
     'nota': 'é essa banda que abre o circuito e adia o dia dos outros — '
             'medido no último dia COM coleta, nunca num fim de semana'},
    {'k': 'frota', 'rotulo': 'Workers que o alarme enxerga',
     'antes': 44, 'medido_em': '28/08', 'sufixo': '', 'sobe': True,
     'fn': _frota_vista,
     'nota': 'o alarme que pega worker rodando código velho'},
    {'k': 'portao', 'rotulo': 'Tribunais fechados no portão (ontem)',
     'antes': '—', 'medido_em': '—', 'sufixo': '', 'sobe': True,
     'fn': _dias_fechados,
     'nota': 'o portão não existia antes desta semana'},
]


def calcular() -> dict | None:
    linhas = []
    falhas = []
    for m in MARCOS:
        try:
            agora = m['fn']()
        except Exception:
            logger.warning('marcos: não consegui medir %s', m['k'], exc_info=True)
            agora = None
        if agora is None:
            # Meia régua é pior que régua nenhuma: sai da lista, com o motivo.
            falhas.append(m['rotulo'])
            continue
        linhas.append({'k': m['k'], 'rotulo': m['rotulo'], 'antes': m['antes'],
                       'agora': agora, 'sufixo': m['sufixo'], 'sobe': m['sobe'],
                       'medido_em': m['medido_em'], 'nota': m['nota']})
    if not linhas:
        return None
    # o resumo executivo entra no MESMO payload e no MESMO aquecimento: uma
    # medição a mais não justifica um segundo job, um segundo cache e um
    # segundo lugar onde o número pode envelhecer sozinho.
    try:
        resumo = resumo_7d()
    except Exception:
        logger.warning('marcos: resumo 7d falhou', exc_info=True)
        resumo = None
    return {'em': timezone.now().isoformat(), 'marcos': linhas,
            'nao_medidos': falhas, 'resumo': resumo}


def aquecer() -> dict | None:
    try:
        p = calcular()
    except Exception:
        logger.error('marcos: aquecimento falhou', exc_info=True)
        return None
    if p:
        cache.set(CHAVE, p, TTL)
        logger.info('marcos: %d medidos, %d sem medição',
                    len(p['marcos']), len(p['nao_medidos']))
    return p


def ler():
    """O que a TELA usa. Só cache — o portão sozinho custa segundos."""
    return cache.get(CHAVE)

# ═══ RESUMO EXECUTIVO DOS 7 DIAS ═══════════════════════════════════════════
#
# O card de `MARCOS` acima é a régua POR MÉTRICA: uma linha, um antes, um
# agora. Ele responde "isto regrediu?". Não responde "o que mudou na semana",
# porque sete réguas soltas não formam um quadro.
#
# Este resumo agrupa em duas perguntas que o dono do produto faz de verdade:
#
#   COBERTURA — o que ENTROU no acervo que antes não estava lá;
#   PESQUISA  — o que ficou ALCANÇÁVEL, que é coisa diferente. Dado coletado e
#               invisível na tela vale o mesmo que dado não coletado — foi
#               exatamente esse buraco (94 M publicações fora de alcance) que
#               originou a busca no texto.
#
# Mesma disciplina do resto do arquivo, e ela não é negociável aqui:
#
#   * o AGORA é medido a cada aquecimento, nunca digitado;
#   * o ANTES é constante COM DATA, porque o dia já passou;
#   * item que não deu para medir SAI, com o motivo — meia régua dá confiança
#     sem cobertura, que é pior que régua nenhuma;
#   * nada de denominador inventado. Onde não há total declarado pela fonte, o
#     item mostra o número absoluto e diz que não há denominador.

def _diarios_dje():
    """Edições do DJE/TJSP: `ok`, falhas e linhas gravadas."""
    from diarios.models import EdicaoDiario as E
    qs = E.objects.filter(fonte='tjsp-dje')
    ok = qs.filter(status=E.OK).count()
    falha = qs.filter(status=E.FALHA).count()
    from django.db.models import Sum
    linhas = qs.aggregate(g=Sum('itens_gravados'))['g'] or 0
    return {'ok': ok, 'falha': falha, 'linhas': linhas}


def _incidentes_esaj():
    """Incidentes do e-SAJ — a porta que não passa por CNJ.

    95,7% deles são Precatório/RPV e NÃO têm número de processo próprio: não
    entram por DJEN nem por Datajud. Antes de 02/09 não havia onde guardá-los.
    """
    n = _sql_um('SELECT count(*) FROM tribunals_incidente')
    if n is None:
        return None
    sem_cnj = _sql_um("SELECT count(*) FROM tribunals_incidente "
                      "WHERE cnj_proprio IS NULL OR cnj_proprio = ''")
    return {'total': n, 'sem_cnj': sem_cnj}


def _ente_devedor_passivo():
    """Participações de ENTE DEVEDOR no polo PASSIVO.

    É o que alimenta o "quem deve" do Overview. Ficou em ZERO por meses: os
    dois caminhos que trariam o ente estavam fechados — um por parser (a
    relação da DEPRE, descartada por ser um formato desconhecido), outro por
    código morto no e-SAJ. `papel` é indexado; polo entra no filtro.
    """
    return _sql_um("""SELECT count(*) FROM tribunals_processoparte
                       WHERE polo = 'passivo'
                         AND upper(papel) LIKE '%%ENTIDADE DEVEDORA%%'""")


def _recuperacao_fase3():
    """Dias-alvo da Fase 3 ainda por refazer — lido do retrato do tique."""
    from djen import recuperacao as R
    r = R.estado()
    return r.get('pendentes') if r else None


def _es_count(indice):
    from search.client import get_es, index_name
    return get_es().count(index=index_name(indice), request_timeout=30)['count']


def _movs_indexadas():
    return _es_count('movimentacoes')


def _acervo_nacional():
    return _es_count('acervo')


#: Cada item: rótulo, o ANTES com data, como medir o AGORA, e a nota que diz
#: o que aquilo significa para quem lê. `unidade` só existe para a tela não
#: precisar adivinhar.
RESUMO_COBERTURA = [
    {'k': 'fase3', 'rotulo': 'Dias de coleta ainda por refazer',
     'antes': '7.167 e PARADO', 'medido_em': '02/09', 'sobe': False,
     'fn': _recuperacao_fase3,
     'nota': 'a recuperação nacional era um mutirão manual que terminou em '
             '27/08 e ninguém religou; agora é tique agendado que se auto-cura'},
    {'k': 'dje', 'rotulo': 'Diários do TJSP coletados',
     'antes': '62 ok e 255 FALHAS', 'medido_em': '02/09', 'sobe': True,
     'fn': lambda: (lambda d: f"{d['ok']} ok · {d['falha']} falha"
                    if d else None)(_diarios_dje()),
     'nota': 'a terceira porta estava fechada por uma coluna NOT NULL sem '
             'default: 253 das 255 falhas eram a MESMA linha'},
    {'k': 'ente', 'rotulo': 'Entes devedores no polo passivo',
     'antes': 0, 'medido_em': '02/09', 'sobe': True,
     'fn': _ente_devedor_passivo,
     'nota': 'é o que alimenta o "quem deve" — a tela existia e não tinha '
             'de onde se alimentar'},
    {'k': 'incid', 'rotulo': 'Incidentes do e-SAJ (precatório/RPV)',
     'antes': 0, 'medido_em': '02/09', 'sobe': True,
     'fn': lambda: (lambda d: f"{d['total']} ({d['sem_cnj']} sem CNJ próprio)"
                    if d else None)(_incidentes_esaj()),
     'nota': '95,7% não têm número de processo: não entram por DJEN nem por '
             'Datajud, e até 02/09 não havia onde guardá-los'},
]

RESUMO_PESQUISA = [
    {'k': 'movs', 'rotulo': 'Publicações alcançáveis pela busca',
     'antes': '1,557 bi', 'medido_em': '02/09', 'sobe': True,
     'fn': _movs_indexadas,
     'nota': 'o que a busca de texto enxerga; coletado e não indexado vale o '
             'mesmo que não coletado'},
    {'k': 'acervo', 'rotulo': 'Esqueleto nacional (Datajud)',
     'antes': '344.630.543', 'medido_em': '01/09', 'sobe': True,
     'fn': _acervo_nacional,
     'nota': 'confrontado com o declarado ao CNJ: falta 0,082%, sobra 0 — o '
             '"100,4%" de antes somava número congelado com linha sem CNJ'},
]


def _medir(itens, onde):
    linhas, falhas = [], []
    for m in itens:
        try:
            agora = m['fn']()
        except Exception:
            logger.warning('resumo7d/%s: não consegui medir %s', onde, m['k'],
                           exc_info=True)
            agora = None
        if agora is None:
            falhas.append(m['rotulo'])
            continue
        linhas.append({'k': m['k'], 'rotulo': m['rotulo'], 'antes': m['antes'],
                       'agora': agora, 'sobe': m['sobe'],
                       'medido_em': m['medido_em'], 'nota': m['nota']})
    return linhas, falhas


def resumo_7d() -> dict | None:
    """Os dois blocos do resumo executivo. `None` se nada deu para medir."""
    cob, f1 = _medir(RESUMO_COBERTURA, 'cobertura')
    pes, f2 = _medir(RESUMO_PESQUISA, 'pesquisa')
    if not cob and not pes:
        return None
    return {'cobertura': cob, 'pesquisa': pes, 'nao_medidos': f1 + f2}
