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

#: SEMANAS vizinhas usadas na mediana — mesma quinta contra quinta, mesma terça
#: contra terça. **Não** dias úteis vizinhos: essa era a régua anterior e ela
#: produzia falso positivo por construção.
#:
#: MEDIDO em 28/08/2026, 3 semanas, publicações por dia da semana:
#:
#:   TJPR   Ter   6.419 ·  6.203 ·  6.875
#:          Qua  23.727 · 87.789 · 93.435
#:          Sex  44.803 · 43.151 · 237.901     ← variação de 38× na mesma semana
#:   TJSP   variação 2×      TJMG   variação 1×
#:
#: A régua antiga acusou o TJPR de 25/08 (terça) com "6.875 contra mediana
#: 50.066, 14% do normal" — e 6.875 é a MAIOR das três terças dele. A mediana
#: misturava terça com sexta. Conferido contra a fonte: a coleta estava íntegra,
#: gap 0. **Portão com falso positivo é portão que ninguém lê** — e aí ele não
#: protege nada no dia em que o buraco é real.
SEMANAS = 5

#: abaixo de tantas amostras do mesmo dia da semana, ABSTÉM em vez de acusar.
#: Mediana de duas terças não é mediana, é palpite com cara de estatística.
AMOSTRA_MINIMA = 3

#: abaixo disso a mediana do tribunal é ruído — não dá para acusar de incompleto
#: quem normalmente traz pouco.
PISO_MEDIANA = 200

#: fração da mediana abaixo da qual o dia é INCOMPLETO. Não é 1,0 porque volume
#: diário oscila de verdade (recesso, pauta, feriado local); o que se caça aqui é
#: o buraco de um terço, não a variação de 10%.
FRACAO_MINIMA = 0.60

#: Conta só os DIAS QUE INTERESSAM, não o intervalo contínuo entre eles.
#:
#: A régua compara o dia com as MESMAS terças (ou quintas) das 5 semanas
#: vizinhas — são **11 dias**, espalhados por 11 semanas. Ler o intervalo
#: contínuo custava 77 dias de `tribunals_movimentacao` para usar 11, e estourou
#: o teto de 240 s no primeiro aquecimento em produção (28/08/2026). O portão
#: saiu da lista de marcos com "sem medição" — que é o comportamento certo, mas
#: o certo mesmo é não precisar dele.
SQL_CONTAGEM = """
SELECT m.tribunal_id, m.data_disponibilizacao::date AS d, count(*)
  FROM tribunals_movimentacao m
 WHERE m.data_disponibilizacao::date = ANY(%s)
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


def _contagens(dias, _fim=None, teto='240s'):
    """`dias` é a LISTA de datas que interessam — não um intervalo.

    Aceita `_fim` só para não quebrar chamador antigo que passava (ini, fim);
    quando `dias` vier como data única e `_fim` vier preenchido, expande.
    """
    if isinstance(dias, datetime.date) and _fim is not None:
        dias = [dias + datetime.timedelta(days=k) for k in range((_fim - dias).days)]
    with transaction.atomic(), connection.cursor() as c:
        c.execute('SET LOCAL statement_timeout = %s', [teto])
        c.execute(SQL_CONTAGEM, [list(dias)])
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
    # só os 11 dias que a régua usa: o dia + as mesmas terças (ou quintas) das
    # 5 semanas de cada lado. Ler o intervalo contínuo seriam 77 dias para usar
    # 11, e foi o que estourou o teto no primeiro aquecimento.
    dias_da_regua = [dia + datetime.timedelta(weeks=k)
                     for k in range(-SEMANAS, SEMANAS + 1)]
    cont = ler_cont(dias_da_regua)
    runs = ler_runs(dia)
    tribunais = ler_tribs(dia)

    fechados, problemas = [], []
    for t in tribunais:
        n = cont.get((t, dia), 0)
        # MESMO dia da semana, mesmo tribunal. O dia em si fica fora.
        vizinhos = []
        for k in range(-SEMANAS, SEMANAS + 1):
            d = dia + datetime.timedelta(weeks=k)
            if d == dia:
                continue
            vizinhos.append(cont.get((t, d), 0))
        # dias em que o tribunal não publicou NADA saem da amostra: eles são
        # feriado/recesso, não "o normal dele é zero", e puxariam a mediana para
        # baixo escondendo buraco de verdade.
        amostra = [v for v in vizinhos if v > 0]
        med = mediana(amostra)

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
        # Sem amostra suficiente do mesmo dia da semana, o critério de VOLUME
        # não se aplica — abster > chutar. Os outros dois critérios continuam.
        if len(amostra) >= AMOSTRA_MINIMA and med >= piso and n < med * fracao:
            motivos.append(f'{n:,} contra mediana {med:,.0f} '
                           f'({100.0 * n / med:.0f}% do normal)')
        if motivos:
            problemas.append({'t': t, 'n': n, 'med': med, 'amostra': len(amostra),
                              'falta': max(int(med) - n, 0), 'motivos': motivos})
        else:
            fechados.append({'t': t, 'n': n, 'med': med, 'amostra': len(amostra),
                             'nota': 'ok' if len(amostra) >= AMOSTRA_MINIMA
                                     else 'sem_amostra'})

    # Abstenção VISÍVEL: tribunal sem amostra suficiente do mesmo dia da semana
    # não foi avaliado por volume, e o portão precisa DIZER isso. Um "fechado"
    # que na verdade é "não consegui olhar" é o silêncio verde de novo.
    sem_amostra = [f['t'] for f in fechados if f.get('nota') == 'sem_amostra']
    return {'dia': dia.isoformat(), 'tribunais': len(tribunais),
            'fechados': len(fechados), 'problemas': problemas,
            'sem_amostra': sem_amostra,
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
