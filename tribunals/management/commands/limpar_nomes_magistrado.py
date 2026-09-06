"""Conserta nome de magistrado contaminado por cabeçalho, cargo ou pronome.

O extrator já não produz mais estes nomes (`_RUIDO` ganhou `PODER`,
`JUDICIARIO`, os cargos de servidor e os pronomes de fórmula em 06/09/2026).
Este comando trata o que **já está gravado** — e ele existe como comando, e não
como script de uma vez, porque o mesmo defeito vai reaparecer em outro
tribunal com outro cabeçalho, e aí a régua tem de ser a MESMA.

## Três casos, três tratamentos

A tentação é apagar tudo. Errado: `'MONICA JACQUELINE SIFUENTES PODER
JUDICIARIO'` é uma desembargadora de verdade, com atuações provadas em
publicações reais — só a FATIA do nome saiu errada. Apagar perde a pessoa;
manter fabrica um homônimo dela mesma.

1. **lixo** — o que sobra depois de tirar o ruído não é nome de gente
   (`'FORUM JUDICIARIO'` → `''`, `'JUDICIARIA TERESINA'` → `'TERESINA'`).
   Nunca foi pessoa: APAGA, com as atuações.
2. **duplicata** — a versão limpa JÁ existe no mesmo (tribunal, órgão).
   As atuações migram para ela e a linha suja some. Nada se perde.
3. **nome torto** — a versão limpa não existe. RENOMEIA no lugar: as atuações
   são boas, só o nome estava errado.
4. **duvidoso** — o resto tem duas palavras mas não convence
   (`'CONFORME ASSEVEROU O'` → `'ASSEVEROU O'`). NÃO MEXE, e conta. Renomear
   trocaria um erro VISÍVEL por um plausível, que é o defeito que o princípio
   nº 1 chama de pior que zero; apagar poderia levar junto um nome com inicial
   (`'MARIA J SILVA'`). Abster > chutar (regra nº 6).

`--dry-run` é o padrão de leitura desta casa: mede e imprime, não escreve.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from tribunals.models import Magistrado, MagistradoAtuacao
from tribunals.services.magistrados import (
    MIN_TOKENS_NOME, _RUIDO, _sem_acento, marca_nao_pessoa,
    normalizar_nome_magistrado)

#: Só o ruído que aparece DENTRO de um nome já gravado. `_RUIDO` inteiro tem
#: token que nunca chegaria a virar nome (`'INTIME'`, `'PUBLIQUE'`), e varrer
#: por eles seria procurar o que não existe.
NO_NOME = frozenset({
    'PODER', 'JUDICIARIO', 'JUDICIARIA', 'FORUM', 'ESCREVENTE', 'ESCRIVAO',
    'DIRETOR', 'DIRETORA', 'CHEFE', 'ANALISTA', 'TECNICO', 'TECNICA',
    'OFICIAL', 'ASSESSOR', 'ASSESSORA', 'ESTAGIARIO', 'ESTAGIARIA',
    'SERVIDOR', 'SERVIDORA', 'PERITO', 'PERITA', 'ADVOGADO', 'ADVOGADA',
    'PROMOTOR', 'PROMOTORA', 'PROCURADOR', 'PROCURADORA', 'DEFENSOR',
    'DEFENSORA', 'CONCILIADOR', 'CONCILIADORA', 'MEDIADOR', 'MEDIADORA',
    'EU', 'NOS', 'CONFORME', 'DOU', 'SERVE',
}) & _RUIDO

#: Ruído de ABERTURA: a fórmula do expediente vem ANTES do nome, então tirá-lo
#: do começo devolve a pessoa (`'Eu, Rafaela Caldeira Gonçalves'`).
ABERTURA = frozenset({'EU', 'NOS', 'CONFORME', 'DOU', 'SERVE'}) & NO_NOME

#: Ruído de CABEÇALHO/CARGO. No FIM do nome ele é sujeira colada e o que sobra
#: é a pessoa (`'JOAO BATISTA GOMES MOREIRA PODER JUDICIARIO'`). No COMEÇO é
#: outra coisa: quer dizer que a fatia começou DENTRO do cabeçalho, e o que
#: sobra tende a ser a continuação dele, não gente —
#: `'JUDICIARIA MINAS GERAIS'` → `'MINAS GERAIS'`, que passaria em qualquer
#: régua de forma e não é pessoa nenhuma.
CABECALHO = NO_NOME - ABERTURA


def _limpar_exibicao(nome: str) -> str:
    """Tira do nome de EXIBIÇÃO os mesmos tokens, preservando acento e caixa.

    Não dá para reconstruir o nome a partir da chave: ela é maiúscula, sem
    acento e sem conectivo. `'Mônica Jacqueline Sifuentes'` viraria
    `'MONICA JACQUELINE SIFUENTES'` — número certo, nome errado.
    """
    return ' '.join(t for t in (nome or '').split()
                    if _sem_acento(t.strip('.,;:()')).upper() not in NO_NOME)


class Command(BaseCommand):
    help = 'Limpa nome de magistrado contaminado por cabeçalho/cargo/pronome.'

    def add_arguments(self, parser):
        parser.add_argument('--aplicar', action='store_true',
                            help='ESCREVE. Sem isto só mede e imprime.')
        parser.add_argument('--lote', type=int, default=500)

    def handle(self, *args, **o):
        alvos = []
        for pk, trib, orgao_chave, nome, chave in Magistrado.objects.values_list(
                'id', 'tribunal_id', 'orgao_chave', 'nome', 'nome_chave'
        ).iterator(chunk_size=5000):
            toks = (chave or '').split()
            if not any(t in NO_NOME for t in toks):
                continue
            limpa = ' '.join(t for t in toks if t not in NO_NOME)
            alvos.append((pk, trib, orgao_chave, nome, chave, limpa))

        lixo, dup, torto, duvida = [], [], [], []
        for a in alvos:
            pk, trib, orgao_chave, nome, chave, limpa = a
            toks_limpa = limpa.split()
            if len(toks_limpa) < MIN_TOKENS_NOME or marca_nao_pessoa(limpa):
                lixo.append(a)
            elif toks[0] in CABECALHO:
                # a fatia começou dentro do cabeçalho: o resto é a continuação
                # dele, não um nome. Renomear daria erro plausível.
                duvida.append(a)
            elif any(len(t) == 1 for t in toks_limpa):
                # pode ser prosa (`'ASSEVEROU O'`) ou inicial de nome real
                # (`'MARIA J SILVA'`). Não dá para provar qual: não mexe.
                duvida.append(a)
            elif Magistrado.objects.filter(tribunal_id=trib,
                                           orgao_chave=orgao_chave,
                                           nome_chave=limpa).exclude(pk=pk).exists():
                dup.append(a)
            else:
                torto.append(a)

        self.stdout.write(
            f'linhas contaminadas: {len(alvos):,}\n'
            f'  lixo (apagar) ...........  {len(lixo):,}\n'
            f'  duplicata (fundir) ......  {len(dup):,}\n'
            f'  nome torto (renomear) ...  {len(torto):,}\n'
            f'  duvidoso (NÃO MEXE) .....  {len(duvida):,}')
        for rot, grupo in (('lixo', lixo), ('duplicata', dup), ('torto', torto),
                           ('duvidoso', duvida)):
            for a in grupo[:3]:
                self.stdout.write(f'    [{rot}] {a[1]} {a[4][:46]!r} -> {a[5][:40]!r}')

        if not o['aplicar']:
            self.stdout.write(self.style.WARNING(
                '\n[DRY-RUN: nada escrito. Use --aplicar.]'))
            return

        apagadas = fundidas = renomeadas = 0
        for i in range(0, len(lixo), o['lote']):
            pks = [a[0] for a in lixo[i:i + o['lote']]]
            with transaction.atomic():
                MagistradoAtuacao.objects.filter(magistrado_id__in=pks).delete()
                apagadas += Magistrado.objects.filter(id__in=pks).delete()[0]

        for pk, trib, orgao_chave, nome, chave, limpa in dup:
            with transaction.atomic():
                bom = Magistrado.objects.filter(
                    tribunal_id=trib, orgao_chave=orgao_chave,
                    nome_chave=limpa).exclude(pk=pk).first()
                if bom is None:            # sumiu entre a medição e a escrita
                    continue
                # a unique é (magistrado, movimentacao_id): mover em massa pode
                # colidir com atuação que a linha boa já tem. `ignore_conflicts`
                # não serve num UPDATE, então move o que não colide e apaga o resto.
                jah = set(MagistradoAtuacao.objects.filter(magistrado=bom)
                          .values_list('movimentacao_id', flat=True))
                mover = MagistradoAtuacao.objects.filter(magistrado_id=pk)
                if jah:
                    mover.filter(movimentacao_id__in=jah).delete()
                    mover = MagistradoAtuacao.objects.filter(magistrado_id=pk)
                mover.update(magistrado=bom)
                Magistrado.objects.filter(id=pk).delete()
                fundidas += 1

        for pk, trib, orgao_chave, nome, chave, limpa in torto:
            with transaction.atomic():
                exibicao = _limpar_exibicao(nome)
                if normalizar_nome_magistrado(exibicao) != limpa:
                    # a chave limpa e o nome limpo têm de contar a MESMA coisa;
                    # divergir aqui é sinal de que a normalização mudou
                    continue
                Magistrado.objects.filter(id=pk).update(
                    nome=exibicao, nome_chave=limpa)
                renomeadas += 1

        self.stdout.write(self.style.SUCCESS(
            f'apagadas {apagadas:,} · fundidas {fundidas:,} · '
            f'renomeadas {renomeadas:,} · intocadas {len(duvida):,}'))
