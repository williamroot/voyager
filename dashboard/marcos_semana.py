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
    return {'em': timezone.now().isoformat(), 'marcos': linhas,
            'nao_medidos': falhas}


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
