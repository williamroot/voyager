"""Backfill do `proc_digits` em `voyager-movimentacoes` — 1,16 bi de docs.

    manage.py es_backfill_proc_digits --medir          # dry-run: mede, não escreve
    manage.py es_backfill_proc_digits --rodar          # executa, throttled
    manage.py es_backfill_proc_digits --parar          # KILL SWITCH (efeito em segundos)
    manage.py es_backfill_proc_digits --religar
    manage.py es_backfill_proc_digits --estado         # cursor + faltantes agora

O QUE ESTÁ QUEBRADO
===================
Medido em 31/08/2026, no índice inteiro:

    voyager-movimentacoes ......... 1.553.929.162 docs
    SEM proc_digits ............... 1.160.006.468   (74,65%)
    controle `proc` ausente ....... 0
    controle `processo_id` ausente  0
    proc_digits == "" ............. 0

O campo está no mapping E no doc builder, então **parece** chave. Agregar por
ele numa fatia de TJMG/março-2024 devolveu 16.160 processos distintos onde
`proc` e `processo_id` (que concordam a 0,07%) devolvem ~894 mil: erro de 55×,
sem erro, sem warning. Ver o bloco 🚨 de `.ia/SEARCH_SCHEMA.md`.

Causa: `es_movs_v2 --copiar` faz `_reindex` server-side do v1 pro v2, e o
`_reindex` copia o `_source` verbatim. O v1 é anterior ao commit que criou o
campo (ae91d77, 11/08/2026).

POR QUE `_update_by_query` E NÃO REINDEXAR DO POSTGRES
======================================================
O dado necessário já está no próprio documento: `proc_digits` é literalmente
`proc` sem os separadores. O script Painless deriva no servidor — não sai do
ES, não toca no Postgres (que está contido) e não depende de o processo ainda
existir em `tribunals_process`.

A FATIA É `detected_at`, NÃO `id` NEM `publish_date`
====================================================
Conferido dos dois lados em 31/08/2026:

- **100%** dos faltantes têm `detected_at` em `[2026-04-01, 2026-08-01)`; **0**
  ficam fora e **0** estão sem o campo. 122 fatias diárias.
- `publish_date` **não** particiona: todo ano é mistura (2026 tem 172,4 M sem
  contra 125,6 M com).
- `id` **não** particiona: as faixas se sobrepõem (id 422 M … 1.273 M aparece
  nos dois conjuntos) porque o enriquecimento reescreve movimentações antigas.

CUSTO MEDIDO (31/08/2026, nó `voyager-es-01`, 16 shards)
========================================================
| modo                                  | docs/s | ETA 1,16 bi | busca do site |
|---------------------------------------|-------:|------------:|---------------|
| sem slices                            |  1.493 | 215 h (9 d) | —             |
| slices=4                              |  9.497 |      33,9 h | —             |
| slices=8, nó ocioso                   | 18.818 |      17,1 h | —             |
| slices=8, com busca concorrente       |  8.021 |      40,0 h | 2,4 s a 35 s  |
| slices=8, requests_per_second=4000    |  2.985 | 107,5 h (4,5 d) | 0,06 s a 6,9 s |

Baseline da mesma busca sem carga: 1,9–2,1 s morna, 12–19 s fria. O teto do
site é `ES_QUERY_TIMEOUT` = 12 s ⇒ **sem throttle a busca não fica lenta, ela
FALHA**. Por isso o default aqui é `--rps 4000`.

Disco NÃO é o gargalo, e isso foi medido: numa passada de 3.000.000 de docs o
espaço livre do nó *subiu* 4,1 GB (o merge recupera os deletes mais rápido do
que a reescrita cria segmento).

GARANTIAS
=========
- **Retomável por construção.** A consulta de cada fatia é `must_not exists`:
  refazer uma fatia já feita custa um `_count` e escreve zero. O cursor no
  cache é conveniência, não correção.
- **Kill switch** lido ANTES de cada fatia (`--parar`, efeito em segundos, sem
  deploy). Parar no meio de uma fatia não corrompe nada — os docs já escritos
  ficam corretos e a fatia recomeça só com o que faltou.
- **Teto é ERRO, nunca corte mudo** (regra nº 2 do CLAUDE.md): `--max-docs`,
  `--max-horas` e `--timeout-fatia-h` atingidos levantam `CommandError` com o
  número real (e, no último, com o id da tarefa do ES para inspeção/cancelamento).
- **Guardas de aborto**, checadas antes de cada fatia: cluster `red`, disco
  livre abaixo de `--disco-min-gb` (default 200 GB). Violação = `CommandError`.
- **Gate por fatia**, conferido dos dois lados: `must_not exists` = 0 **e** o
  GANHO de `exists` na fatia = número de faltantes que havia antes **e**
  `proc_digits == ""` = 0 **e** amostra de 200 docs com 100% de `len == 20`
  batendo com os dígitos do `proc`. Qualquer um falhando é `CommandError` — não
  se passa pra fatia seguinte com o buraco.

  O gate compara o **delta**, não o total, e isso não é detalhe: uma fatia de
  `detected_at` mistura os dois estados (julho/2026 tem 96,9 M sem o campo e
  63,8 M com, porque o enriquecimento reescreve movimentação antiga com o doc
  builder novo). A primeira versão deste comando comparava `exists` da fatia
  com o número de faltantes e abortaria todas as fatias de julho num backfill
  perfeitamente correto — foi um teste por MUTAÇÃO que pegou.
"""
import datetime
import json
import time

from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError

from search.client import get_es, index_name

INDICE = 'movimentacoes'

#: janela que contém 100% dos faltantes (conferido: 0 fora, 0 sem o campo).
#: Não é chute nem folga: é o resultado da medição, e sair dela é sinal de que
#: a premissa mudou — daí o aviso no `--medir`.
JANELA_INICIO = datetime.date(2026, 4, 1)
JANELA_FIM = datetime.date(2026, 8, 1)          # exclusivo

PAUSA_KEY = 'es_backfill_proc_digits:parar'
CURSOR_KEY = 'es_backfill_proc_digits:cursor'

#: `requests_per_second` do `_update_by_query`. 4000 foi o valor em que a busca
#: do site ficou dentro do teto de 12 s durante a reescrita (medido).
RPS_PADRAO = 4000
SLICES_PADRAO = 8
LOTE_PADRAO = 1000                              # scroll_size

#: piso de disco livre. O pré-voo do #92 usa o mesmo número.
DISCO_MIN_GB = 200

#: teto de espera por fatia. A maior fatia (2026-05-03, 112 M docs) leva ~10,4 h
#: a 3.000 d/s; 24 h é folga, não expectativa. Estourar é ERRO, não espera eterna.
TIMEOUT_FATIA_H = 24.0

#: tetos de espera. Nada no ES sem eles (regra nº 7).
T_CURTO = 60
T_CONTA = 300
T_LONGO = 600

#: quantos docs a amostra do gate confere pelo CONTEÚDO. `exists` não mente em
#: `keyword` ausente, mas mente com string vazia — e "gravou alguma coisa" não
#: é o mesmo que "gravou os 20 dígitos certos".
AMOSTRA_GATE = 200

#: deriva os dígitos do `proc` do próprio `_source`. Sem regex de propósito:
#: `script.painless.regex.enabled` é desligado por default e ligar exige
#: restart do cluster.
SCRIPT = (
    "String p = ctx._source.proc; "
    "if (p == null) { ctx.op = 'noop'; return; } "
    "StringBuilder sb = new StringBuilder(); "
    "for (int i = 0; i < p.length(); i++) { "
    "  char c = p.charAt(i); "
    "  if (c >= (char)48 && c <= (char)57) { sb.append(c); } "
    "} "
    "if (sb.length() == 0) { ctx.op = 'noop'; return; } "
    "ctx._source.proc_digits = sb.toString();"
)


def parado() -> bool:
    try:
        return bool(cache.get(PAUSA_KEY))
    except Exception:
        # Redis fora do ar durante um backfill de 1,16 bi: parar é o lado
        # seguro do erro. Kill switch que falha aberto não é kill switch.
        return True


def _dias(inicio: datetime.date, fim: datetime.date):
    d = inicio
    while d < fim:
        yield d
        d += datetime.timedelta(days=1)


def _faixa(dia: datetime.date) -> dict:
    return {'range': {'detected_at': {'gte': dia.isoformat(),
                                      'lt': (dia + datetime.timedelta(days=1)).isoformat()}}}


def _q_faltantes(dia: datetime.date | None = None) -> dict:
    b: dict = {'must_not': [{'exists': {'field': 'proc_digits'}}]}
    if dia is not None:
        b['filter'] = [_faixa(dia)]
    return {'bool': b}


class Command(BaseCommand):
    help = 'Backfill de proc_digits em voyager-movimentacoes (throttled, retomável)'

    def add_arguments(self, p):
        p.add_argument('--medir', action='store_true',
                       help='dry-run: mede por fatia e estima, NÃO escreve')
        p.add_argument('--rodar', action='store_true', help='executa o backfill')
        p.add_argument('--parar', action='store_true', help='KILL SWITCH')
        p.add_argument('--religar', action='store_true')
        p.add_argument('--estado', action='store_true')
        p.add_argument('--de', type=str, default=JANELA_INICIO.isoformat())
        p.add_argument('--ate', type=str, default=JANELA_FIM.isoformat(),
                       help='exclusivo')
        p.add_argument('--rps', type=int, default=RPS_PADRAO,
                       help=f'requests_per_second do UBQ (0 = sem throttle). '
                            f'default {RPS_PADRAO}')
        p.add_argument('--slices', type=int, default=SLICES_PADRAO)
        p.add_argument('--lote', type=int, default=LOTE_PADRAO)
        p.add_argument('--max-docs', type=int, default=None,
                       help='teto de docs na execução. Atingir é ERRO, não parada limpa.')
        p.add_argument('--max-horas', type=float, default=None,
                       help='teto de relógio. Atingir é ERRO, não parada limpa.')
        p.add_argument('--disco-min-gb', type=int, default=DISCO_MIN_GB)
        p.add_argument('--timeout-fatia-h', type=float, default=TIMEOUT_FATIA_H,
                       help='teto de espera por fatia. Atingir é ERRO com o id da tarefa.')

    def handle(self, *a, **o):
        self.es = get_es()
        self.idx = index_name(INDICE)

        if o['parar']:
            cache.set(PAUSA_KEY, 1, timeout=None)
            self.stdout.write(self.style.WARNING('PARADO. O backfill sai na próxima fatia.'))
            return
        if o['religar']:
            cache.delete(PAUSA_KEY)
            self.stdout.write(self.style.SUCCESS('religado'))
            return
        if o['estado']:
            self._estado()
            return

        de = datetime.date.fromisoformat(o['de'])
        ate = datetime.date.fromisoformat(o['ate'])
        if de >= ate:
            raise CommandError(f'--de {de} não é anterior a --ate {ate}')

        if o['medir']:
            self._medir(de, ate)
            return
        if o['rodar']:
            self._rodar(de, ate, o)
            return
        raise CommandError('escolha --medir, --rodar, --parar, --religar ou --estado')

    # -- leitura ----------------------------------------------------------- #

    def _conta(self, query: dict, timeout: int = T_CONTA) -> int:
        return int(self.es.count(index=self.idx, query=query,
                                 request_timeout=timeout)['count'])

    def _faltam_global(self) -> int:
        return self._conta(_q_faltantes())

    def _estado(self):
        faltam = self._faltam_global()
        total = self._conta({'match_all': {}}, timeout=T_CURTO)
        self.stdout.write(json.dumps({
            'indice': self.idx,
            'docs': total,
            'faltam_proc_digits': faltam,
            'pct_faltando': round(100.0 * faltam / total, 2) if total else None,
            'parado': parado(),
            'cursor': cache.get(CURSOR_KEY),
        }, ensure_ascii=False, indent=2))

    # -- dry-run ----------------------------------------------------------- #

    def _medir(self, de, ate):
        """Mede sem escrever. Publica o resíduo FORA da janela — se não for 0, a
        premissa da fatia caiu e o backfill por dia deixaria buraco."""
        total = self._conta({'match_all': {}}, timeout=T_CURTO)
        faltam = self._faltam_global()
        fora = self._conta({'bool': {'must_not': [
            {'exists': {'field': 'proc_digits'}},
            {'range': {'detected_at': {'gte': de.isoformat(), 'lt': ate.isoformat()}}},
        ]}})
        sem_data = self._conta({'bool': {'must_not': [
            {'exists': {'field': 'proc_digits'}}, {'exists': {'field': 'detected_at'}}]}})
        # controle: TEM que dar 0 nos dois. Régua sem controle não se publica.
        sem_proc = self._conta({'bool': {'must_not': [{'exists': {'field': 'proc'}}]}})
        sem_pid = self._conta({'bool': {'must_not': [{'exists': {'field': 'processo_id'}}]}})

        self.stdout.write(f'índice ................. {self.idx}')
        self.stdout.write(f'docs ................... {total:,}')
        self.stdout.write(f'faltam proc_digits ..... {faltam:,}  '
                          f'({100.0 * faltam / total:.2f}%)' if total else '')
        self.stdout.write(f'controle sem `proc` .... {sem_proc:,}   (tem que ser 0)')
        self.stdout.write(f'controle sem `proc_id` . {sem_pid:,}   (tem que ser 0)')
        self.stdout.write(f'faltantes FORA da janela {fora:,}   (tem que ser 0)')
        self.stdout.write(f'faltantes sem detected_at {sem_data:,}   (tem que ser 0)')
        if sem_proc or sem_pid:
            self.stderr.write(self.style.ERROR(
                'o campo de CONTROLE não deu 0 — a medição inteira é lixo, não use'))
        if fora or sem_data:
            self.stderr.write(self.style.ERROR(
                f'{fora + sem_data:,} faltantes fora do alcance de --de/--ate: '
                f'a fatia por detected_at deixaria buraco. Ajuste a janela.'))

        self.stdout.write('\nfatia (detected_at)  faltam')
        soma = 0
        for dia in _dias(de, ate):
            n = self._conta(_q_faltantes(dia), timeout=T_CURTO)
            soma += n
            if n:
                self.stdout.write(f'  {dia}        {n:>12,}')
        self.stdout.write(f'\nsoma das fatias ........ {soma:,}')
        if soma != faltam:
            self.stderr.write(self.style.ERROR(
                f'soma das fatias ({soma:,}) != total faltando ({faltam:,}): '
                f'diferença de {faltam - soma:,} FORA da janela'))

        self.stdout.write('\nETA (taxas medidas em 31/08/2026):')
        for rotulo, taxa in (('slices=8, rps=4000 (busca protegida)', 2985),
                             ('slices=8, sem throttle, com busca', 8021),
                             ('slices=8, sem throttle, nó ocioso', 18818),
                             ('sem slices', 1493)):
            self.stdout.write(f'  {rotulo:<40} {soma / taxa / 3600:6.1f} h')

    # -- execução ---------------------------------------------------------- #

    def _guardas(self, disco_min_gb: int):
        h = self.es.cluster.health(request_timeout=T_CURTO)
        if h.get('status') == 'red':
            raise CommandError('cluster ES em RED — abortado (nada de escrever por cima)')
        st = self.es.nodes.stats(metric='fs', request_timeout=T_CURTO)
        livre = min(n['fs']['total']['available_in_bytes'] for n in st['nodes'].values())
        if livre < disco_min_gb * 1e9:
            raise CommandError(
                f'disco livre {livre / 1e9:.0f} GB abaixo do piso de {disco_min_gb} GB — abortado')
        return livre

    def _ubq(self, dia, o) -> dict:
        params = {
            'conflicts': 'proceed',
            'scroll_size': o['lote'],
            'slices': o['slices'],
            'wait_for_completion': False,
        }
        if o['rps'] and o['rps'] > 0:
            params['requests_per_second'] = float(o['rps'])
        r = self.es.update_by_query(
            index=self.idx, request_timeout=T_CURTO,
            query=_q_faltantes(dia), script={'lang': 'painless', 'source': SCRIPT},
            **params)
        tarefa = r['task']
        # Espera com TETO. Uma tarefa que não volta é o jeito mais silencioso de
        # este comando falhar: ele ficaria "rodando" para sempre e ninguém teria
        # como distinguir de progresso lento. Estourar é ERRO com o id da tarefa
        # (que dá pra inspecionar e cancelar no ES) e o que ela já tinha feito.
        limite = time.time() + o['timeout_fatia_h'] * 3600
        while True:
            time.sleep(15)
            t = self.es.tasks.get(task_id=tarefa, request_timeout=T_CURTO)
            if t.get('completed'):
                return dict(t['task']['status'])
            if time.time() > limite:
                st = (t.get('task') or {}).get('status') or {}
                raise CommandError(
                    f'TETO de espera de {o["timeout_fatia_h"]} h na fatia {dia}: a tarefa '
                    f'{tarefa} não terminou (updated={st.get("updated", 0):,} de '
                    f'{st.get("total", 0):,}). Inspecione com GET _tasks/{tarefa} e '
                    f'cancele com POST _tasks/{tarefa}/_cancel.')

    def _tem(self, dia) -> int:
        return self._conta({'bool': {'filter': [{'exists': {'field': 'proc_digits'}},
                                                _faixa(dia)]}}, timeout=T_CURTO)

    def _gate(self, dia, alvo: int, tem_antes: int):
        """Confere a fatia dos DOIS lados + amostra pelo conteúdo.

        ⚠️ O delta é `tem_depois - tem_antes == alvo`, NÃO `tem_depois == alvo`.
        Uma fatia de `detected_at` mistura os dois estados — julho/2026 tem 96,9 M
        sem o campo e 63,8 M com, porque o enriquecimento reescreve movimentação
        antiga com o doc builder novo. Comparar o total da fatia com o número de
        faltantes abortaria TODAS as fatias de julho num backfill correto.
        """
        self.es.indices.refresh(index=self.idx, request_timeout=T_LONGO)
        faltam = self._conta(_q_faltantes(dia), timeout=T_CURTO)
        tem = self._tem(dia)
        vazio = self._conta({'bool': {'filter': [{'term': {'proc_digits': ''}},
                                                 _faixa(dia)]}}, timeout=T_CURTO)
        erros = []
        if faltam:
            erros.append(f'{faltam:,} ainda sem proc_digits')
        if tem - tem_antes != alvo:
            erros.append(f'ganho de {tem - tem_antes:,} com proc_digits, esperado {alvo:,} '
                         f'(antes {tem_antes:,}, agora {tem:,})')
        if vazio:
            erros.append(f'{vazio:,} com proc_digits vazio (o `exists` contaria como presente)')

        r = self.es.search(index=self.idx, size=AMOSTRA_GATE, request_timeout=T_CONTA,
                           source_includes=['proc', 'proc_digits'],
                           query={'function_score': {
                               'query': {'bool': {'filter': [_faixa(dia)]}},
                               'random_score': {'seed': 106, 'field': '_seq_no'}}})
        hits = r['hits']['hits']
        ruins = 0
        for h in hits:
            s = h['_source']
            # `.get(...) is not None`: `'campo' in _source` MENTE — doc antigo
            # tem a chave com valor null e passaria.
            d = s.get('proc_digits')
            esperado_d = ''.join(c for c in (s.get('proc') or '') if c.isdigit())
            if d is None or len(d) != 20 or d != esperado_d:
                ruins += 1
        if ruins:
            erros.append(f'amostra: {ruins}/{len(hits)} com proc_digits errado ou != 20 dígitos')
        if erros:
            raise CommandError(f'GATE FALHOU na fatia {dia}: ' + '; '.join(erros))
        return tem, len(hits)

    def _rodar(self, de, ate, o):
        if parado():
            raise CommandError('kill switch LIGADO. `--religar` antes de rodar.')
        antes = self._faltam_global()
        self.stdout.write(f'antes: {antes:,} docs sem proc_digits em {self.idx}')
        self.stdout.write(f'rps={o["rps"] or "SEM THROTTLE"} slices={o["slices"]} '
                          f'lote={o["lote"]} janela={de}..{ate}')
        if not o['rps']:
            self.stderr.write(self.style.WARNING(
                'SEM THROTTLE: medido em 31/08/2026, a busca do site foi de 2 s para 35 s '
                '— acima do ES_QUERY_TIMEOUT de 12 s, ou seja, ERRO na tela.'))

        t0 = time.time()
        feitos = 0
        for dia in _dias(de, ate):
            if parado():
                self.stdout.write(self.style.WARNING(
                    f'kill switch: parando limpo em {dia} — {feitos:,} docs feitos'))
                break
            self._guardas(o['disco_min_gb'])

            alvo = self._conta(_q_faltantes(dia), timeout=T_CURTO)
            if not alvo:
                continue

            # Teto ANTES de começar a fatia: estourar no meio deixaria a fatia
            # pela metade, que é o estado que o Princípio nº 1 mais condena.
            if o['max_docs'] is not None and feitos + alvo > o['max_docs']:
                raise CommandError(
                    f'TETO de --max-docs={o["max_docs"]:,} atingido: {feitos:,} feitos, '
                    f'a fatia {dia} pede mais {alvo:,} e {self._faltam_global():,} '
                    f'ainda faltam no índice. Nada foi cortado em silêncio.')
            if o['max_horas'] is not None and (time.time() - t0) / 3600 >= o['max_horas']:
                raise CommandError(
                    f'TETO de --max-horas={o["max_horas"]} atingido em {dia}: '
                    f'{feitos:,} feitos, {self._faltam_global():,} ainda faltam.')

            tem_antes = self._tem(dia)
            ti = time.time()
            st = self._ubq(dia, o)
            dt = max(time.time() - ti, 1e-9)
            tem, amostra = self._gate(dia, alvo, tem_antes)
            feitos += st.get('updated', 0)
            cache.set(CURSOR_KEY, dia.isoformat(), timeout=None)
            self.stdout.write(
                f'{dia}  alvo={alvo:>10,}  upd={st.get("updated", 0):>10,}  '
                f'noop={st.get("noops", 0):>7,}  conflitos={st.get("version_conflicts", 0):>6,}  '
                f'{dt:6.0f}s  {st.get("updated", 0) / dt:8,.0f} d/s  '
                f'gate OK (fatia={tem:,}, amostra={amostra})')

        depois = self._faltam_global()
        self.stdout.write(self.style.SUCCESS(
            f'\nantes {antes:,} → depois {depois:,}  '
            f'(escritos {feitos:,} em {(time.time() - t0) / 3600:.1f} h)'))
