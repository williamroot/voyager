"""A sigla que dizemos × a sigla que o próprio número CNJ diz.

    # a tabela inteira do índice rico (exata, ~22 min de CPU no nó de busca)
    manage.py conferir_siglas_cnj

    # um tribunal só (segundos), que é como se investiga uma suspeita
    manage.py conferir_siglas_cnj --tribunais TJDFT,TRF6

    # o lado do CNJ: o esqueleto está arquivado sob a sigla certa?
    manage.py conferir_siglas_cnj --indice acervo

    # pra colar em issue
    manage.py conferir_siglas_cnj --tribunais TJDFT --json

Por que existe: `Process.tribunal` é um RÓTULO — vem do DJEN, do enricher ou da
hidratação. O número CNJ é um FATO: `NNNNNNN-DD.AAAA.J.TR.OOOO` carrega o
segmento e o tribunal (Resolução CNJ 65/2008). Quando os dois discordam, ou o
rótulo está errado (e aí todo denominador por tribunal mente), ou a discordância
é estrutural — e as duas coisas se distinguem contando, não opinando.

MEDIDO EM 31/08/2026, os 104.003.151 documentos do `voyager-processos`:

    divergem .............. 2.479.437   (2,3840%)
      TST                   1.162.389   recurso guarda o número da ORIGEM
      STJ                     939.350   idem — 79,4% dele é número de origem
      TRF6                    377.294   herdou processo com o código antigo 4.01
      resíduo real                404   0,00039% — dado sujo do DJEN

Ou seja: **99,9996% do acervo carrega o tribunal certo pelo próprio número**, e
o TJDFT — que parecia inflado em 2,84× contra o Datajud — tem 100% de acerto.
Ver `.ia/ACERVO_CNJ.md` § "TJDFT ao contrário".

DUAS ARMADILHAS que esta medição evita, e por isso ela não é um `for` em Python:

1. **Não é amostra.** Contar divergência por amostra num campo cujo desvio é de
   1 em 1 milhão não mede nada: o intervalo de confiança engole o achado. Aqui é
   `terms` sobre um runtime field que recorta os dígitos 13:16 de `proc_digits`,
   uma agregação por tribunal — a população inteira.
2. **Campo de CONTROLE obrigatório.** `MISSING` (sem `proc_digits`) e `LEN_n`
   (comprimento ≠ 20) TÊM que dar zero, e `sum_other_doc_count` também. Se não
   derem, a régua está torta e o resultado não se publica — foi um controle em
   0,0% que pegou a régua torta do #105 em 30/08/2026.
"""
import json

from django.core.management.base import BaseCommand, CommandError

from search.client import get_es, index_name
from tribunals.cnj import sigla_do_cnj

#: recorta `J.TR` do CNJ (dígitos 13:16) sem tocar em `_source`. As guardas
#: emitem rótulo próprio em vez de estourar: doc quebrado tem que APARECER na
#: contagem, não derrubar a agregação nem sumir dela.
SCRIPT_JTR = (
    'def v = doc["proc_digits"]; '
    'if (v.size() == 0) { emit("MISSING"); return; } '
    'String s = v.value; '
    'if (s.length() != 20) { emit("LEN_" + s.length()); return; } '
    'emit(s.substring(13,16));'
)

#: Tribunais de SOBREPOSIÇÃO: o recurso mantém o número do processo de origem
#: (Res. 65/2008), então `tribunal ≠ sigla_do_cnj(numero)` é o comportamento
#: CORRETO neles — a unicidade do `Process` é (tribunal, numero_cnj). Marcá-los
#: existe para que a linha do TST em 100% não seja lida como defeito.
SOBREPOSICAO = {'STF', 'STJ', 'STM', 'TST'}

ES_TIMEOUT = 600


class Command(BaseCommand):
    help = 'Compara a sigla do tribunal com a que o próprio número CNJ declara'

    def add_arguments(self, p):
        p.add_argument('--indice', default='processos',
                       choices=['processos', 'acervo'],
                       help='`processos` = o acervo rico (default); '
                            '`acervo` = o esqueleto do Datajud')
        p.add_argument('--tribunais', default='',
                       help='lista separada por vírgula (default: todos)')
        p.add_argument('--json', action='store_true', dest='como_json')

    def handle(self, *a, **o):
        es = get_es()
        idx = index_name(o['indice'])
        siglas = [s.strip().upper() for s in o['tribunais'].split(',') if s.strip()]
        if not siglas:
            siglas = self._siglas_do_indice(es, idx)
        if not siglas:
            raise CommandError(f'nenhum tribunal em {idx}')

        linhas = []
        if not o['como_json']:
            self.stdout.write(f'{"nossa":8s}{"docs":>14s}{"bate":>14s}'
                              f'{"diverge":>12s} {"%":>9s}  para onde')
        # linha a linha: uma passada de 104 M leva ~22 min e um run que morre no
        # fim leva junto tudo o que já sabia
        for sigla in siglas:
            linha = self._medir(es, idx, sigla)
            linhas.append(linha)
            if not o['como_json']:
                self._imprimir(linha)

        if o['como_json']:
            self.stdout.write(json.dumps(
                {'indice': idx, 'tribunais': linhas,
                 'controle': self._controle(linhas)},
                ensure_ascii=False, default=str))
        else:
            self._rodape(linhas)

    # -- medição ------------------------------------------------------------ #

    def _siglas_do_indice(self, es, idx) -> list[str]:
        r = es.options(request_timeout=ES_TIMEOUT).search(
            index=idx, size=0, track_total_hits=False,
            aggs={'t': {'terms': {'field': 'tribunal', 'size': 500}}})
        b = r['aggregations']['t']['buckets']
        # menor primeiro: se a passada morrer no meio, morreu tendo respondido
        # o máximo de tribunais possível
        return [x['key'] for x in sorted(b, key=lambda x: x['doc_count'])]

    def _medir(self, es, idx, sigla) -> dict:
        r = es.options(request_timeout=ES_TIMEOUT).search(
            index=idx, size=0, track_total_hits=True,
            runtime_mappings={'jtr': {'type': 'keyword',
                                      'script': {'source': SCRIPT_JTR}}},
            query={'term': {'tribunal': sigla}},
            aggs={'jtr': {'terms': {'field': 'jtr', 'size': 500}}})
        agg = r['aggregations']['jtr']
        total = r['hits']['total']['value']
        bate, quebrados, destinos = 0, 0, {}
        for b in agg['buckets']:
            jtr, n = b['key'], b['doc_count']
            if jtr == 'MISSING' or jtr.startswith('LEN_'):
                quebrados += n
                destinos[jtr] = destinos.get(jtr, 0) + n
                continue
            derivada = sigla_do_cnj(f'0000000-00.2000.{jtr[0]}.{jtr[1:]}.0000')
            if derivada == sigla:
                bate += n
            else:
                chave = derivada or f'?{jtr}'
                destinos[chave] = destinos.get(chave, 0) + n
        return {
            'tribunal': sigla, 'docs': total, 'bate': bate,
            'diverge': total - bate, 'destinos': destinos,
            # CONTROLE: os três TÊM que ser zero
            'controle_quebrados': quebrados,
            'controle_sum_other': agg['sum_other_doc_count'],
            'sobreposicao': sigla in SOBREPOSICAO,
        }

    def _controle(self, linhas) -> dict:
        q = sum(x['controle_quebrados'] for x in linhas)
        o = sum(x['controle_sum_other'] for x in linhas)
        return {'quebrados': q, 'sum_other': o, 'ok': q == 0 and o == 0}

    # -- saída --------------------------------------------------------------- #

    def _imprimir(self, L):
        alvo = ' · '.join(
            f'{k}:{v:,}' for k, v in
            sorted(L['destinos'].items(), key=lambda kv: -kv[1])[:5])
        if len(L['destinos']) > 5:
            alvo += f' (+{len(L["destinos"]) - 5})'
        pct = 100.0 * L['diverge'] / L['docs'] if L['docs'] else 0.0
        marca = ' ⟨sobreposição⟩' if L['sobreposicao'] and L['diverge'] else ''
        estilo = (self.style.SUCCESS if not L['diverge']
                  else self.style.WARNING if L['sobreposicao'] or pct < 0.01
                  else self.style.ERROR)
        self.stdout.write(estilo(
            f"{L['tribunal']:8s}{L['docs']:14,d}{L['bate']:14,d}"
            f"{L['diverge']:12,d} {pct:8.4f}%  {alvo}{marca}"))

    def _rodape(self, linhas):
        docs = sum(x['docs'] for x in linhas)
        div = sum(x['diverge'] for x in linhas)
        sob = sum(x['diverge'] for x in linhas if x['sobreposicao'])
        zero = [x['tribunal'] for x in linhas if not x['diverge']]
        ctl = self._controle(linhas)
        self.stdout.write('')
        self.stdout.write(
            f'{len(zero)} de {len(linhas)} tribunais com ZERO divergência')
        self.stdout.write(
            f'TOTAL {docs:,} docs · divergem {div:,} '
            f'({100.0 * div / docs if docs else 0:.4f}%)')
        self.stdout.write(
            f'  descontando os tribunais de sobreposição: {div - sob:,} '
            f'({100.0 * (div - sob) / docs if docs else 0:.6f}%)')
        msg = (f"CONTROLE quebrados={ctl['quebrados']} "
               f"sum_other={ctl['sum_other']}")
        self.stdout.write(self.style.SUCCESS(msg + ' ✔') if ctl['ok']
                          else self.style.ERROR(
                              msg + ' ✖ — a régua está torta, NÃO publique'))
