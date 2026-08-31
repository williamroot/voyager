"""Fase 0 — marca Process.tem_sinal_precatorio lendo o texto DJEN das movimentações.

Sinal precatório computável ANTES de enriquecer, com ZERO requisição ao CNJ: cada
linha usa EXISTS com o índice (processo) sobre a movimentação — NÃO varre a tabela
de 1,55 bi (não há índice de texto). ~10 ms/proc medido no TJSP em 31/08/2026.

Resumível (só toca tem_sinal_precatorio IS NULL), throttlável (--sleep) e paralelizável:
o pick usa `FOR UPDATE SKIP LOCKED` dentro de uma transação, então dá pra rodar N cópias
NO MESMO tribunal que cada uma pega um lote disjunto (sem contention de row-lock). Também
shardável por --min-id/--max-id.

    python manage.py backfill_sinal_precatorio --tribunais TJSP
    python manage.py backfill_sinal_precatorio --tribunais TJPR --min-id 0 --max-id 5000000
    python manage.py backfill_sinal_precatorio --tribunais TODOS --dry-run   # censo

## 🔴 O recorte que media o próprio buraco (medido em 31/08/2026)

Este comando tinha DOIS cortes mudos empilhados, e o de baixo anulava o conserto
do de cima:

1. `--tribunais` nascia com `DATAJUD_ALVO = ['TJPR','TRF4','TRF6','TRF2']`. O TJSP
   ficava de fora por OMISSÃO, não por medição (`.ia/ACERVO_CNJ.md`).
2. o pick trazia `AND data_enriquecimento_datajud IS NULL`.

O corte 2 é o que importa. Medido no TJSP em 31/08/2026:

    tem_datajud | tem_sinal | processos
    ------------+-----------+-----------
    f           | f         | 14.504.845
    f           | t         |    763.477
    t           | (NULL)    |  1.513.486   ← 100% dos NULL restantes

Ou seja: **todo** processo do TJSP que ainda não tem sinal já passou pelo Datajud.
Acrescentar `'TJSP'` em `DATAJUD_ALVO` sem tirar o corte 2 selecionaria ZERO linhas
e terminaria `SUCCESS: 0 processados` — run verde, log limpo, número redondo, e o
1,5 M continuaria invisível. É o `for pagina in range(1, 11)` do CLAUDE.md com
outra roupa.

O corte 2 existia porque, na Fase 0, o sinal servia só para PRIORIZAR a fila do
refill datajud (quem já foi ao Datajud não precisa de prioridade). Mas
`tem_sinal_precatorio` virou campo de PRODUTO — está no doc do ES, no filtro da
busca e no "potencial" do mapa (`search/agg_overview.py`) — e aí "já enriquecido"
não é motivo nenhum para o campo ficar vazio. O filtro virou a flag
`--so-sem-datajud`, desligada por padrão.

## Abstenção: processo sem NENHUMA movimentação fica NULL

`EXISTS(... texto ~* padrão)` devolve `false` para quem não tem texto nenhum — e
`false` na tela quer dizer "medimos e não tem", que é confiança falsa. Regra 6/8:
abster > chutar. O UPDATE só toca quem tem ao menos uma movimentação; o resto é
contado e declarado no relatório final. (No TJSP a abstenção medida foi 0 de
3.000 na amostra de 31/08/2026, mas o custo da guarda é um probe indexado.)
"""
import logging
import time

from django.core.management.base import BaseCommand
from django.db import connection, transaction

logger = logging.getLogger('voyager.tribunals.sinal')

# Sinal de precatório no TEXTO das movimentações (mesmos termos do classificador,
# _MOVS_AGG_SQL: f11/f12/f13/f14). Precursor do crédito contra a Fazenda.
#
# NÃO ampliar para `precat[óo]ri` "para pegar o plural": `precat[óo]rio` já casa
# "precatórios" por substring, e o `o` final é o que separa PRECATÓRIO de CARTA
# PRECATÓRIA. Medido em 3.000 processos do TJSP (31/08/2026): `precat[óo]ri[ao]s?`
# leva de 92 para 316 acertos, e 196 desses 224 a mais são "carta precatória" —
# ruído puro. Ver `.ia/ACERVO_CNJ.md`.
PADRAO = r'precat[óo]rio|of[íi]cio requisit[óo]rio|\mrpv\M|requisi[çc][ãa]o de pagamento'

#: Tribunais que o refill do Datajud prioriza. É a lista do REFILL, não a lista de
#: quem merece ter o sinal — o sinal é campo de produto e vale para todo mundo.
#: `--tribunais TODOS` roda em quem tiver NULL.
DATAJUD_ALVO = ['TJPR', 'TRF4', 'TRF6', 'TRF2']

#: acima disto o comando desiste: lote que estoura o teto é sintoma, e centenas de
#: lotes queimados não são "alguns processos lentos", é algo sistêmico.
MAX_QUEIMADOS = 20_000


class Command(BaseCommand):
    help = 'Fase 0: marca tem_sinal_precatorio pelo texto DJEN (por janela de pk).'

    def add_arguments(self, p):
        p.add_argument('--tribunais', default=','.join(DATAJUD_ALVO),
                       help='CSV de siglas, ou TODOS (default: alvo datajud).')
        p.add_argument('--batch', type=int, default=2000,
                       help=('linhas por lote. Era 20.000 e virou 12,7 h numa '
                             'transação só no TJSP — o custo é por MOVIMENTAÇÃO, '
                             'não por processo, e varia 100x entre tribunais.'))
        p.add_argument('--sleep', type=float, default=0.2, help='pausa entre janelas (s)')
        p.add_argument('--min-id', type=int, default=None)
        p.add_argument('--max-id', type=int, default=None)
        p.add_argument('--dry-run', action='store_true',
                       help='não escreve: só o censo de quem está NULL, por tribunal.')
        p.add_argument('--so-sem-datajud', action='store_true',
                       help=('restaura o corte da Fase 0 (só quem NÃO passou pelo '
                             'Datajud). Era o default hardcoded e escondia 100% dos '
                             'NULL do TJSP — use só para priorizar a fila do refill.'))
        p.add_argument('--sem-censo', action='store_true',
                       help='pula o censo final de quem ficou NULL fora do recorte.')
        p.add_argument('--statement-timeout', default='120s',
                       help=('teto por LOTE. O EXISTS com regex sobre o texto das '
                             'movimentações custa proporcional ao tamanho do '
                             'processo; sem teto um lote de TJSP rodou 12,7 h e '
                             'travou outros três jobs (27/08/2026).'))
        p.add_argument('--lock-timeout', default='5s')
        p.add_argument('--censo-timeout', default='600s')

    # ------------------------------------------------------------------ censo

    def _censo(self, timeout: str) -> list[tuple[str, int]]:
        """NULL por tribunal, no acervo INTEIRO — não só no recorte.

        É o antídoto do corte mudo: o comando termina dizendo, com número real,
        quem ficou de fora. Index-only scan em `proc_trib_sinalprec_idx`;
        41 s para o total nacional em 31/08/2026, com teto próprio porque nada
        entra no caminho sem teto de espera (regra nº 7).
        """
        with transaction.atomic(), connection.cursor() as c:
            c.execute('SET LOCAL statement_timeout = %s', [timeout])
            c.execute('SELECT tribunal_id, count(*) FROM tribunals_process '
                      'WHERE tem_sinal_precatorio IS NULL GROUP BY 1 ORDER BY 2 DESC')
            return c.fetchall()

    def _relatar_fora_do_recorte(self, tribs, timeout):
        try:
            censo = self._censo(timeout)
        except Exception:
            logger.error('backfill_sinal: o censo de NULL estourou o teto de %s — o '
                         'comando NÃO pode declarar quem ficou de fora.', timeout,
                         exc_info=True)
            self.stderr.write(self.style.ERROR(
                'CENSO FALHOU — não sei dizer quem ficou fora do recorte.'))
            return
        alvo = set(tribs)
        fora = [(t, n) for t, n in censo if t not in alvo and n]
        dentro = sum(n for t, n in censo if t in alvo)
        self.stdout.write(f'NULL restante DENTRO do recorte: {dentro:,}')
        if not fora:
            self.stdout.write(self.style.SUCCESS('nada NULL fora do recorte.'))
            return
        total = sum(n for _, n in fora)
        # ERRO, não nota de rodapé: é exatamente este número que o `DATAJUD_ALVO`
        # escondia. Quem ler o log tem que ver o tamanho do que não foi medido.
        msg = ', '.join(f'{t} {n:,}' for t, n in fora[:15])
        self.stderr.write(self.style.ERROR(
            f'FORA DO RECORTE: {total:,} processos seguem com tem_sinal_precatorio '
            f'NULL em {len(fora)} tribunais que este run NÃO tocou — {msg}'
            + (' …' if len(fora) > 15 else '')))
        logger.error('backfill_sinal: %d processos NULL fora do recorte %s (%s)',
                     total, sorted(alvo), msg)

    # ----------------------------------------------------------------- handle

    def handle(self, *a, **o):
        alvo_todos = o['tribunais'].strip().upper() == 'TODOS'
        if alvo_todos:
            with transaction.atomic(), connection.cursor() as c:
                c.execute('SET LOCAL statement_timeout = %s', [o['censo_timeout']])
                c.execute('SELECT DISTINCT tribunal_id FROM tribunals_process '
                          'WHERE tem_sinal_precatorio IS NULL')
                tribs = sorted(r[0] for r in c.fetchall())
        else:
            tribs = [s.strip().upper() for s in o['tribunais'].split(',') if s.strip()]
        batch, sleep = o['batch'], o['sleep']
        self.stdout.write(f'alvo={tribs} batch={batch} sleep={sleep}s '
                          f'so_sem_datajud={o["so_sem_datajud"]}'
                          + (' DRY-RUN' if o['dry_run'] else ''), ending='\n')
        self.stdout.flush()
        if o['dry_run']:
            self._relatar_fora_do_recorte(tribs, o['censo_timeout'])
            return

        # Pega EXATAMENTE `batch` linhas ainda-NULL do alvo (usa o índice composto
        # proc_trib_sinalprec_idx) e marca só essas por pk. Batch pequeno = transação
        # curta + progresso visível + custo previsível (~batch × 10ms). Resumível.
        janela = [o['min_id'], o['max_id']]
        sql_pick = ("SELECT id FROM tribunals_process "
                    "WHERE tribunal_id = ANY(%s) AND tem_sinal_precatorio IS NULL "
                    # Corte da Fase 0, agora OPCIONAL — ver o docstring: ligado por
                    # padrão ele escondia 100% dos NULL do TJSP.
                    + ("AND data_enriquecimento_datajud IS NULL "
                       if o['so_sem_datajud'] else "")
                    + ("AND id >= %s " if janela[0] is not None else "")
                    + ("AND id < %s " if janela[1] is not None else "")
                    # lotes já queimados por teto não voltam: sem isto, engolir o
                    # timeout viraria loop infinito no mesmo lote.
                    + "AND NOT (id = ANY(%s::bigint[])) "
                    + "LIMIT %s FOR UPDATE SKIP LOCKED")
        # `atualizado_em = now()` é a CAMPAINHA: `tem_sinal_precatorio` é campo
        # do doc do ES, e SQL cru não roda o `auto_now`. Sem ele o sinal fica
        # certo no banco e o mapa comercial continua lendo o valor velho —
        # `sync_processos_atualizados` é keyset por `atualizado_em`.
        #
        # O segundo EXISTS é a ABSTENÇÃO: quem não tem movimentação nenhuma não
        # é "sem sinal", é "sem medida" — e fica NULL. Probe indexado
        # (mov_processo_data_disp_idx), custo desprezível ao lado do regex.
        sql_upd = ("UPDATE tribunals_process p SET tem_sinal_precatorio = EXISTS ("
                   "SELECT 1 FROM tribunals_movimentacao m "
                   "WHERE m.processo_id = p.id AND m.texto ~* %s), "
                   "atualizado_em = now() WHERE p.id = ANY(%s) "
                   "AND EXISTS (SELECT 1 FROM tribunals_movimentacao m2 "
                   "WHERE m2.processo_id = p.id)")
        t0 = time.monotonic()
        feitos = escritos = abstidos = lotes_queimados = 0
        queimados: list[int] = []
        while True:
            params = ([tribs] + [x for x in janela if x is not None]
                      + [queimados, batch])
            # pick+update na MESMA transação: o FOR UPDATE SKIP LOCKED segura o lote
            # até o UPDATE commitar, então cópias paralelas pegam lotes disjuntos.
            ids: list[int] = []
            n_escritos = 0
            try:
                with transaction.atomic():
                    with connection.cursor() as c:
                        # TETO DE ESPERA no lote. Sem isto, um único UPDATE ficou
                        # **12,7 HORAS** rodando em produção (27/08/2026, TJSP): o
                        # `EXISTS` com regex sobre `m.texto` custa proporcional ao
                        # número de movimentações do processo, e processo de TJSP tem
                        # muitas. Enquanto durou, ele segurou row-lock em 20.000
                        # linhas de `tribunals_process` e travou TRÊS outros jobs —
                        # o `DROP INDEX CONCURRENTLY` (12,6 h esperando), o backfill
                        # de `assunto_id` (2,6 h) e dois de `classificacao` (1,5 h
                        # cada), todos em `wait_event_type=Lock`.
                        #
                        # Com teto, o lote morre sozinho e a transação solta os
                        # locks: o comando registra o erro e segue, em vez de virar
                        # um bloqueio de meio dia que ninguém vê. Regra nº 7 —
                        # nada sem teto de espera.
                        c.execute('SET LOCAL lock_timeout = %s', [o['lock_timeout']])
                        c.execute('SET LOCAL statement_timeout = %s',
                                  [o['statement_timeout']])
                        c.execute(sql_pick, params)
                        ids = [r[0] for r in c.fetchall()]
                        if ids:
                            c.execute(sql_upd, [PADRAO, ids])
                            n_escritos = c.rowcount
            except Exception:
                # TETO É ERRO REGISTRADO COM O NÚMERO REAL, nunca `return` discreto.
                # E os ids vão para `queimados`: o pick não os re-seleciona, senão o
                # comando ficaria pra sempre no mesmo lote sem escrever nada.
                lotes_queimados += 1
                queimados.extend(ids)
                logger.error(
                    'backfill_sinal: LOTE QUEIMADO pelo teto de %s — %d processos '
                    '(ids %s..%s), lote %d, %d queimados no total. Eles ficam NULL '
                    'e NÃO voltam neste run.', o['statement_timeout'], len(ids),
                    ids[0] if ids else '-', ids[-1] if ids else '-',
                    lotes_queimados, len(queimados), exc_info=True)
                self.stderr.write(self.style.ERROR(
                    f'  LOTE QUEIMADO ({o["statement_timeout"]}): {len(ids):,} '
                    f'processos seguem NULL · {len(queimados):,} acumulados'))
                if not ids or len(queimados) > MAX_QUEIMADOS:
                    self.stderr.write(self.style.ERROR(
                        f'ABORTA: {len(queimados):,} processos queimados '
                        f'(> {MAX_QUEIMADOS:,}) — o teto não está segurando um caso '
                        f'raro, está segurando o normal.'))
                    break
                continue
            if not ids:
                break
            feitos += len(ids)
            escritos += n_escritos
            abstidos += len(ids) - n_escritos
            dt = time.monotonic() - t0
            self.stdout.write(f'  processados {feitos:,} · escritos {escritos:,} '
                              f'· abstidos {abstidos:,} · {feitos/dt:.0f}/s '
                              f'· {dt/60:.1f}min', ending='\n')
            self.stdout.flush()
            if len(ids) < batch:
                break
            if sleep:
                time.sleep(sleep)
        with connection.cursor() as c:
            c.execute("SELECT count(*) FROM tribunals_process WHERE tribunal_id = ANY(%s) "
                      "AND tem_sinal_precatorio IS TRUE", [tribs])
            com = c.fetchone()[0]
        linha = (f'FIM: {feitos:,} processados · {escritos:,} escritos · '
                 f'{abstidos:,} abstidos (sem movimentação) · {com:,} com sinal · '
                 f'{(time.monotonic()-t0)/60:.1f}min')
        if queimados:
            # run com lote queimado NÃO é run verde.
            self.stderr.write(self.style.ERROR(
                f'{linha} · {len(queimados):,} QUEIMADOS pelo teto em '
                f'{lotes_queimados} lotes'))
        else:
            self.stdout.write(self.style.SUCCESS(linha))
        if not o['sem_censo']:
            self._relatar_fora_do_recorte(tribs, o['censo_timeout'])
