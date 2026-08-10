from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    # CONCURRENTLY não roda em transação → migração não-atômica.
    atomic = False

    dependencies = [
        ('tribunals', '0045_add_mcp_token'),
    ]

    operations = [
        # BooleanField nullable sem default = metadata-only no Postgres (instantâneo,
        # sem rewrite) mesmo em 75M linhas.
        migrations.AddField(
            model_name='process',
            name='tem_sinal_precatorio',
            field=models.BooleanField(blank=True, null=True),
        ),
        # Índice composto p/ o refill priorizado; CONCURRENTLY = sem lock de escrita.
        AddIndexConcurrently(
            model_name='process',
            index=models.Index(
                fields=['tribunal', 'tem_sinal_precatorio'],
                name='proc_trib_sinalprec_idx'),
        ),
    ]
