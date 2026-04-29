"""Adiciona timestamps por fonte de enriquecimento + backfill via SQL.

Campos novos (nullable):
- data_enriquecimento_tribunal: PJe consulta pública (drainer.apply_event STATUS_OK)
- data_enriquecimento_djen: ingestão DJEN per-processo (ingest_processo)
- data_enriquecimento_datajud: sync API Datajud (sync_processo)

Backfill chunked (5k rows por batch) baseado em sinais existentes:
- tribunal: enriquecido_em quando enriquecimento_status='ok'
- djen: ultima_sinc_djen_em (campo antigo que já existia)
- datajud: inserido_em quando o processo já tem Movimentacao com meio='datajud'
  (o sync de hoje setou meio='datajud' nas movs).
"""
from django.db import migrations, models


def forward(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    with schema_editor.connection.cursor() as cur:
        # 1) tribunal: copia de enriquecido_em quando status=ok
        cur.execute("""
            UPDATE tribunals_process
               SET data_enriquecimento_tribunal = enriquecido_em
             WHERE enriquecido_em IS NOT NULL
               AND enriquecimento_status = 'ok'
               AND data_enriquecimento_tribunal IS NULL
        """)
        print(f'  tribunal backfilled: {cur.rowcount}')

        # 2) djen: copia de ultima_sinc_djen_em
        cur.execute("""
            UPDATE tribunals_process
               SET data_enriquecimento_djen = ultima_sinc_djen_em
             WHERE ultima_sinc_djen_em IS NOT NULL
               AND data_enriquecimento_djen IS NULL
        """)
        print(f'  djen backfilled: {cur.rowcount}')

        # 3) datajud: usa MAX(inserido_em) das movs com meio='datajud' por processo.
        # Backfill aproximado — só pra processos que já tem movs Datajud.
        cur.execute("""
            UPDATE tribunals_process p
               SET data_enriquecimento_datajud = sub.last_dt
              FROM (
                   SELECT processo_id, MAX(inserido_em) AS last_dt
                     FROM tribunals_movimentacao
                    WHERE meio = 'datajud'
                    GROUP BY processo_id
                   ) sub
             WHERE p.id = sub.processo_id
               AND p.data_enriquecimento_datajud IS NULL
        """)
        print(f'  datajud backfilled: {cur.rowcount}')


def reverse(apps, schema_editor):
    pass  # campos serão removidos via AlterField/RemoveField nas próximas migrations


class Migration(migrations.Migration):
    atomic = False  # backfill em raw SQL, não precisa wrapper transacional

    dependencies = [
        ('tribunals', '0017_restore_missing_indexes'),
    ]

    operations = [
        migrations.AddField(
            model_name='process',
            name='data_enriquecimento_tribunal',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='process',
            name='data_enriquecimento_djen',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='process',
            name='data_enriquecimento_datajud',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(forward, reverse, elidable=False),
    ]
