"""Lê o magistrado do texto das publicações e grava `Magistrado`/`MagistradoAtuacao`.

QUANTO ISSO GRAVA (medido, antes de rodar)
------------------------------------------
Duas amostras independentes de 03/09/2026 — 42.281 publicações no Postgres com
salto pseudoaleatório (**11,39%** produzem atribuição) e 600 sorteadas no ES
(**11,83%**) — projetam **~184 a 191 milhões de linhas** sobre as
1.618.133.888 de `tribunals_movimentacao`. Rodar a faixa inteira não é um
comando: é uma decisão de disco.

ENTRA POR FAIXA FECHADA DE PK — NUNCA POR `--tribunal`
------------------------------------------------------
Não é preferência. `tribunals_movimentacao` não tem índice que sirva a
"filtra por tribunal, ordena por id": em `tribunals_process` o índice com esse
nome existe com **uma coluna só**, e o `LIMIT 1` que dele dependia ficou
**1.318 s** em produção e enfileirou 63 sessões atrás de um `ALTER TABLE`
(`.ia/DATA_MODEL.md`). Faixa fechada de pk usa a PK, é contígua, é retomável e
é o que torna o trabalho paralelizável em shards sem sobreposição — o mesmo
desenho do `backfill_partes_djen`.

ITERA, NÃO ACUMULA (regra nº 1)
-------------------------------
`.iterator(chunk_size=...)` com cursor no servidor, e a gravação em lotes
fechados. O corpo de uma publicação chega a **56 KB** (medido no TJDFT), então
o que limita o lote é BYTE, não linha: 500 publicações já são ~28 MB só de
texto. Foi orçamento de página, e não de byte, que produziu 342 OOM na coleta
do DJEN.

TETO É ALERTA, NUNCA CORTE MUDO (regra nº 2)
--------------------------------------------
`--max-publicacoes` existe para a rodada exploratória. Bater nele **não** é um
`return` discreto: a saída diz `TETO ATINGIDO` com o pk onde parou, e o código
de saída é 1. Quem automatizar isso não vai confundir "acabou a faixa" com
"parei no meio".

O QUE ELE NÃO FAZ
-----------------
Não conta nada durante o backfill de faixa. `Magistrado.n_publicacoes` fica
`NULL` — porque uma contagem feita sobre uma faixa parcial seria um número
verdadeiro respondendo a outra pergunta. A contagem sai do modo `--contar`,
que recomputa TODOS os magistrados a partir de `MagistradoAtuacao` num
`GROUP BY` só e carimba `n_publicacoes_em`. Contagem sem data de medição
envelhece em silêncio.

    # medir antes: a faixa e o que ela promete
    manage.py backfill_magistrados --de 1 --ate 50000 --dry-run

    # gravar, em shards que não se sobrepõem
    manage.py backfill_magistrados --de 0          --ate 100000000
    manage.py backfill_magistrados --de 100000000  --ate 200000000

    # depois de fechar as faixas, e só depois:
    manage.py backfill_magistrados --contar
"""
from __future__ import annotations

import sys
from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Max, Min
from django.utils import timezone

from tribunals.models import Magistrado, MagistradoAtuacao, Movimentacao
from tribunals.services import magistrados as mag

#: Publicação do TJDFT mede 56 KB. 500 × 56 KB ≈ 28 MB de texto vivo — é o
#: teto de BYTES disfarçado de teto de linhas, e está aqui escrito para que
#: quem subir o número saiba o que está comprando.
CHUNK_LEITURA = 500
LOTE_ESCRITA = 2000


class Command(BaseCommand):
    help = 'Extrai magistrado do texto das publicações numa faixa de pk.'

    def add_arguments(self, parser):
        parser.add_argument('--de', type=int, help='pk inicial (inclusivo)')
        parser.add_argument('--ate', type=int, help='pk final (EXCLUSIVO)')
        parser.add_argument('--max-publicacoes', type=int, default=0,
                            help='teto da rodada. 0 = sem teto')
        parser.add_argument('--dry-run', action='store_true',
                            help='lê e conta, não grava nada')
        parser.add_argument('--contar', action='store_true',
                            help='modo contagem: recomputa n_publicacoes de '
                                 'TODOS os magistrados a partir das atuações')

    def handle(self, *args, **opts):
        if opts['contar']:
            self._contar()
            return
        if opts['de'] is None or opts['ate'] is None:
            raise CommandError('--de e --ate são obrigatórios (faixa fechada de pk)')
        if opts['ate'] <= opts['de']:
            raise CommandError('--ate tem de ser maior que --de')
        self._backfill(opts['de'], opts['ate'], opts['max_publicacoes'],
                       opts['dry_run'])

    # ------------------------------------------------------------------ #
    def _backfill(self, de: int, ate: int, teto: int, dry: bool) -> None:
        c = Counter()
        erros: Counter = Counter()
        cache: dict[tuple, int] = {}
        pendentes: list[dict] = []
        ultimo_pk = de

        qs = (Movimentacao.objects
              .filter(id__gte=de, id__lt=ate)
              .values_list('id', 'processo_id', 'tribunal_id', 'nome_orgao',
                           'data_disponibilizacao', 'texto')
              .order_by('id'))

        for pk, proc_id, trib, orgao, data, texto in qs.iterator(
                chunk_size=CHUNK_LEITURA):
            ultimo_pk = pk
            c['lidas'] += 1
            leitura = mag.ler(texto)
            erros.update(leitura.erros)
            if leitura.marcadores_vistos:
                c['com_marcador'] += 1
            if not leitura.atribuicoes:
                continue
            c['com_atribuicao'] += 1
            for a in leitura.atribuicoes:
                c['atribuicoes'] += 1
                pendentes.append({
                    'chave': (trib, mag.normalizar_orgao(orgao), a.chave_nome),
                    'nome': a.nome, 'orgao': orgao or '', 'cargo': a.cargo,
                    'formato': a.formato, 'processo_id': proc_id,
                    'movimentacao_id': pk,
                    'publicado_em': data.date() if data else None,
                })
            if len(pendentes) >= LOTE_ESCRITA:
                self._gravar(pendentes, cache, c, dry)
                pendentes.clear()
            if teto and c['lidas'] >= teto:
                self._gravar(pendentes, cache, c, dry)
                self._resumo(c, erros, de, ultimo_pk, dry)
                self.stderr.write(self.style.ERROR(
                    f'TETO ATINGIDO: {teto} publicações lidas, parei no pk '
                    f'{ultimo_pk}. A faixa [{de}, {ate}) NÃO foi concluída — '
                    f'retome com --de {ultimo_pk + 1}.'))
                sys.exit(1)

        self._gravar(pendentes, cache, c, dry)
        self._resumo(c, erros, de, ate, dry)

    def _gravar(self, pendentes, cache, c, dry) -> None:
        if not pendentes or dry:
            return
        # 1) os magistrados que a faixa ainda não conhece
        novos = {}
        for p in pendentes:
            if p['chave'] in cache or p['chave'] in novos:
                continue
            trib, orgao_chave, nome_chave = p['chave']
            novos[p['chave']] = Magistrado(
                tribunal_id=trib, nome=p['nome'], nome_chave=nome_chave,
                orgao=p['orgao'], orgao_chave=orgao_chave, cargo=p['cargo'],
                formato=p['formato'], fonte=Magistrado.FONTE_TEXTO)
        if novos:
            Magistrado.objects.bulk_create(list(novos.values()),
                                           ignore_conflicts=True)
            # `ignore_conflicts` NÃO devolve pk das linhas que colidiram — e a
            # colisão é o caso normal aqui (o mesmo magistrado assina milhares
            # de publicações). Quem dá o id é sempre uma leitura de volta,
            # nunca o objeto em memória. As três colunas entram no filtro: só
            # `tribunal × nome` seria produto cartesiano e traria linhas de
            # outros órgãos para o cache.
            achados = Magistrado.objects.filter(
                tribunal_id__in={k[0] for k in novos},
                orgao_chave__in={k[1] for k in novos},
                nome_chave__in={k[2] for k in novos},
            ).values_list('tribunal_id', 'orgao_chave', 'nome_chave', 'id')
            for trib, orgao_chave, nome_chave, mid in achados:
                cache[(trib, orgao_chave, nome_chave)] = mid
            c['magistrados_vistos'] = len(cache)

        # 2) as atuações
        linhas, sem_dono = [], 0
        for p in pendentes:
            mid = cache.get(p['chave'])
            if mid is None:
                sem_dono += 1        # não deveria acontecer; conta em vez de sumir
                continue
            linhas.append(MagistradoAtuacao(
                magistrado_id=mid, processo_id=p['processo_id'],
                movimentacao_id=p['movimentacao_id'], formato=p['formato'],
                cargo=p['cargo'], publicado_em=p['publicado_em']))
        c['atuacao_sem_dono'] += sem_dono
        if linhas:
            MagistradoAtuacao.objects.bulk_create(linhas, ignore_conflicts=True)
            c['atuacoes_gravadas'] += len(linhas)

    def _resumo(self, c, erros, de, ate, dry) -> None:
        lidas = c['lidas'] or 1
        self.stdout.write(
            f"faixa [{de}, {ate})  lidas={c['lidas']:,}  "
            f"com_marcador={c['com_marcador']:,} ({100 * c['com_marcador'] / lidas:.2f}%)  "
            f"com_atribuicao={c['com_atribuicao']:,} ({100 * c['com_atribuicao'] / lidas:.2f}%)  "
            f"abstencoes={max(0, c['com_marcador'] - c['com_atribuicao']):,}  "
            f"atribuicoes={c['atribuicoes']:,}  "
            f"magistrados={c['magistrados_vistos']:,}  "
            f"atuacoes={c['atuacoes_gravadas']:,}"
            + ('  [DRY-RUN: nada gravado]' if dry else ''))
        if erros:
            self.stdout.write('  abstenções por motivo: ' + ', '.join(
                f'{k}={v}' for k, v in erros.most_common(8)))
        if c['atuacao_sem_dono']:
            self.stderr.write(self.style.ERROR(
                f"{c['atuacao_sem_dono']} atuações sem magistrado resolvido — "
                f"isto não deveria acontecer; investigue antes de confiar na faixa."))

    # ------------------------------------------------------------------ #
    def _contar(self) -> None:
        """Recomputa `n_publicacoes`, `primeira_em` e `ultima_em` de TODOS.

        Um `GROUP BY` só. Roda depois que as faixas fecharam — contar durante
        o backfill produziria um número verdadeiro para outra pergunta.
        """
        agora = timezone.now()
        agregado = (MagistradoAtuacao.objects
                    .values('magistrado_id')
                    .annotate(n=Count('id'), pri=Min('publicado_em'),
                              ult=Max('publicado_em')))
        atualizados = 0
        lote: list[Magistrado] = []
        for linha in agregado.iterator(chunk_size=5000):
            lote.append(Magistrado(
                id=linha['magistrado_id'], n_publicacoes=linha['n'],
                n_publicacoes_em=agora, primeira_em=linha['pri'],
                ultima_em=linha['ult']))
            if len(lote) >= LOTE_ESCRITA:
                Magistrado.objects.bulk_update(
                    lote, ['n_publicacoes', 'n_publicacoes_em',
                           'primeira_em', 'ultima_em'])
                atualizados += len(lote)
                lote.clear()
        if lote:
            Magistrado.objects.bulk_update(
                lote, ['n_publicacoes', 'n_publicacoes_em',
                       'primeira_em', 'ultima_em'])
            atualizados += len(lote)
        total = Magistrado.objects.count()
        self.stdout.write(
            f'contados {atualizados:,} de {total:,} magistrados. Os '
            f'{total - atualizados:,} restantes ficam com n_publicacoes NULL — '
            f'"não contamos", que é diferente de zero.')
