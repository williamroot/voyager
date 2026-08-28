"""O portão da ingestão, como FUNÇÃO — para o comando e o vigia usarem o mesmo código.

Duas réguas diferentes para a mesma pergunta é como se produz discordância
honesta e cara: em 27/08/2026 duas implementações independentes olharam o dia
25/08, concordaram na contagem crua do TJPR (6.875 nos dois) e discordaram no
tamanho do buraco (43.190 contra 81.721) só porque montavam a mediana de jeitos
diferentes. Régua única resolve isso.

`conferir_dia` (o comando) e `vigiar` (o job do scheduler) chamam ESTA função.
"""
import datetime
import logging

from django.db import connection, transaction

logger = logging.getLogger('voyager.tribunals.portao')

#: dias úteis vizinhos usados na mediana (antes e depois do dia conferido).
VIZINHOS = 5

#: abaixo disso a mediana do tribunal é ruído — não dá para acusar de incompleto
#: quem normalmente traz pouco.
PISO_MEDIANA = 200

#: fração da mediana abaixo da qual o dia é INCOMPLETO. Não é 1,0 porque volume
#: diário oscila de verdade (recesso, pauta, feriado local); o que se caça aqui é
#: o buraco de um terço, não a variação de 10%.
FRACAO_MINIMA = 0.60

SQL_CONTAGEM = """
SELECT m.tribunal_id, m.data_disponibilizacao::date AS d, count(*)
  FROM tribunals_movimentacao m
 WHERE m.data_disponibilizacao >= %s AND m.data_disponibilizacao < %s
 GROUP BY 1, 2
"""

SQL_RUNS = """
SELECT r.tribunal_id, r.status, max(r.started_at)
  FROM tribunals_ingestionrun r
 WHERE r.janela_inicio = %s AND r.janela_fim = %s
 GROUP BY 1, 2
"""


def mediana(valores):
    v = sorted(valores)
    if not v:
        return 0
    meio = len(v) // 2
    return v[meio] if len(v) % 2 else (v[meio - 1] + v[meio]) / 2


def _contagens(ini, fim, teto='240s'):
    with transaction.atomic(), connection.cursor() as c:
        c.execute('SET LOCAL statement_timeout = %s', [teto])
        c.execute(SQL_CONTAGEM, [ini, fim])
        return {(t, d): n for t, d, n in c.fetchall()}


def _runs(dia):
    with transaction.atomic(), connection.cursor() as c:
        c.execute("SET LOCAL statement_timeout = '60s'")
        c.execute(SQL_RUNS, [dia, dia])
        fora = {}
        for trib, status, quando in c.fetchall():
            fora.setdefault(trib, {})[status] = quando
        return fora


def _tribunais(dia):
    with transaction.atomic(), connection.cursor() as c:
        c.execute("SET LOCAL statement_timeout = '30s'")
        c.execute("""SELECT sigla FROM tribunals_tribunal
                      WHERE ativo = TRUE
                        AND (data_inicio_disponivel IS NULL OR data_inicio_disponivel <= %s)
                      ORDER BY sigla""", [dia])
        return [r[0] for r in c.fetchall()]


def conferir(dia, fracao=FRACAO_MINIMA, piso=PISO_MEDIANA, leitores=None) -> dict:
    """Aplica os três critérios do portão a `dia`, tribunal por tribunal.

    `leitores` existe só para o teste trocar as três leituras de banco sem mock
    de ORM — um teste que precisa de banco para provar aritmética envelhece mal.
    """
    ler_cont, ler_runs, ler_tribs = leitores or (_contagens, _runs, _tribunais)
    cont = ler_cont(dia - datetime.timedelta(days=VIZINHOS + 2),
                    dia + datetime.timedelta(days=VIZINHOS + 3))
    runs = ler_runs(dia)
    tribunais = ler_tribs(dia)

    fechados, problemas = [], []
    for t in tribunais:
        n = cont.get((t, dia), 0)
        vizinhos = []
        for k in range(-(VIZINHOS + 2), VIZINHOS + 3):
            d = dia + datetime.timedelta(days=k)
            if d == dia or d.weekday() >= 5:      # o dia em si e o fim de semana fora
                continue
            vizinhos.append(cont.get((t, d), 0))
        med = mediana(vizinhos)

        st = runs.get(t, {})
        tem_ok = 'success' in st
        falhou_por_ultimo = ('failed' in st and
                             (not tem_ok or st['failed'] > st['success']))

        if med < piso and n < piso:
            fechados.append({'t': t, 'n': n, 'med': med, 'nota': 'sem_expediente'})
            continue

        motivos = []
        if not tem_ok:
            motivos.append('sem run success')
        if falhou_por_ultimo:
            motivos.append('failed sem success posterior')
        if med >= piso and n < med * fracao:
            motivos.append(f'{n:,} contra mediana {med:,.0f} '
                           f'({100.0 * n / med:.0f}% do normal)')
        if motivos:
            problemas.append({'t': t, 'n': n, 'med': med,
                              'falta': max(int(med) - n, 0), 'motivos': motivos})
        else:
            fechados.append({'t': t, 'n': n, 'med': med, 'nota': 'ok'})

    return {'dia': dia.isoformat(), 'tribunais': len(tribunais),
            'fechados': len(fechados), 'problemas': problemas,
            'total_dia': sum(v for (t, d), v in cont.items() if d == dia),
            'falta_estimado': sum(p['falta'] for p in problemas)}


# --------------------------------------------------------------------------
# O VIGIA — o portão rodando sozinho
# --------------------------------------------------------------------------
#: chave do último resultado, para a tela e para quem quiser conferir sem rodar.
CHAVE_CACHE = 'portao:ultimo:v1'

#: quantos dias o vigia olha a cada passada. D-1 e D-2: D-1 pode ainda estar
#: coletando quando ele roda, e D-2 já deveria estar fechado sem desculpa.
DIAS_VIGIADOS = (1, 2)


def vigiar() -> dict:
    """Job do scheduler. Confere D-1 e D-2 e GRITA com o número real.

    Por que existe: um comando que ninguém executa é o mesmo silêncio verde que
    o portão foi feito para matar. Em 25/08/2026 a ingestão do dia inteiro morreu
    e ficou 21 horas sem ninguém ver — o que denunciou foi um KPI de tela, por
    acaso, porque alguém olhou.

    NUNCA levanta: vigia que derruba o scheduler leva junto os outros jobs.
    """
    from django.core.cache import cache
    from django.utils import timezone

    saida = {'em': timezone.now().isoformat(), 'dias': []}
    for k in DIAS_VIGIADOS:
        dia = timezone.localdate() - datetime.timedelta(days=k)
        try:
            r = conferir(dia)
        except Exception:
            logger.error('portão: não consegui conferir %s', dia, exc_info=True)
            saida['dias'].append({'dia': dia.isoformat(), 'erro': True})
            continue
        saida['dias'].append(r)

        if not r['problemas']:
            logger.info('portão %s: FECHADO — %d/%d tribunais, %s publicações',
                        r['dia'], r['fechados'], r['tribunais'], f"{r['total_dia']:,}")
            continue

        # ERRO com o número REAL e os nomes. "alguns tribunais incompletos" não
        # faz ninguém agir; "TJPR com 14% do normal, faltam 43.190" faz.
        nomes = ', '.join(f"{p['t']} {p['n']:,}/{p['med']:,.0f}"
                          for p in sorted(r['problemas'], key=lambda x: -x['falta'])[:8])
        logger.error(
            'portão %s: %d TRIBUNAIS FORA — faltam ~%s publicações. %s%s',
            r['dia'], len(r['problemas']), f"{r['falta_estimado']:,}", nomes,
            '' if len(r['problemas']) <= 8 else f" (+{len(r['problemas']) - 8} outros)")

    try:
        cache.set(CHAVE_CACHE, saida, 60 * 60 * 26)
    except Exception:
        logger.warning('portão: não consegui guardar o resultado no cache', exc_info=True)
    return saida
