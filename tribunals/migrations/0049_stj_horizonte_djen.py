"""STJ: registra o horizonte medido no DJEN — SEM ligar a ingestão.

O STJ não é fonte nova. Ele aderiu ao DJEN (Res. STJ/GP 19/2024) e o payload
dele bate 100% com `djen/parser.py` — 200 itens conferidos chave a chave em
16/08/2026, nenhum `SchemaDriftAlert` dispararia. Ou seja: não há coletor a
escrever, há um interruptor a apertar.

Esta migration aperta só a METADE SEGURA do interruptor: grava a data em que o
acervo do STJ começa no DJEN. Ela NÃO seta `ativo=True`, e isso é deliberado:

  1. **Semântica**: 92% das publicações do STJ trazem o CNJ da ORIGEM
     (8.26=TJSP, 8.13=TJMG, 4.01=TRF1...). Como `Process` é único por
     (tribunal, numero_cnj), ligar o STJ cria uma segunda linha de `Process`
     para processos que já temos sob o TJ de origem, e quebra a premissa
     `Process.tribunal == Movimentacao.tribunal` em toda métrica por tribunal
     (lag, cobertura, mv_tribunal_kpis). É decisão de produto, não de migration.
  2. **Volume**: o STJ sozinho publica ~12.000 movimentações por dia útil
     (medido paginando 13/08/2026 até o fim: 12.955 em 13 páginas; 12/08:
     11.560 em 12) — ~3x um TRF médio. Com `ativo=True` e este
     `data_inicio_disponivel`, o `watchdog_ingestao` dispara
     `tick_backfill_retroativo` na hora e joga ~5,3 MILHÕES de movimentações
     retroativas na fila `djen_backfill`, disputando I/O com extração e
     vetorização num Postgres que a documentação já classifica como
     disk-I/O-bound.

Ligar é `manage.py djen_ligar_stj`, que obriga a escolher a fronteira e mostra
o tamanho do estrago antes de gravar.

A DATA: 2024-11-18, e não 29/11/2024 (a da norma).
------------------------------------------------
Medido dia a dia no próprio DJEN em 16/08/2026: 2024-07-01, 2024-09-02,
2024-10-01, 2024-11-01 e 2024-11-18 devolvem `count=0`; 2024-11-25 já devolve
`count` saturado (10.000) e 2024-11-27, `count=7`. Ou seja, há publicação do STJ
ANTES da data oficial da adesão — provável janela de homologação. O piso fica no
último dia comprovadamente vazio (18/11) porque errar para trás custa 7 dias de
requisição vazia, e errar para frente custa acervo perdido em silêncio.
"""
from datetime import date

from django.db import migrations

INICIO_MEDIDO = date(2024, 11, 18)


def registrar_horizonte(apps, schema_editor):
    Tribunal = apps.get_model('tribunals', 'Tribunal')
    Tribunal.objects.filter(sigla='STJ').update(
        sigla_djen='STJ', data_inicio_disponivel=INICIO_MEDIDO,
    )


def limpar_horizonte(apps, schema_editor):
    Tribunal = apps.get_model('tribunals', 'Tribunal')
    Tribunal.objects.filter(sigla='STJ').update(data_inicio_disponivel=None)


class Migration(migrations.Migration):
    dependencies = [('tribunals', '0048_ingestionrun_fonte')]
    operations = [migrations.RunPython(registrar_horizonte, limpar_horizonte)]
