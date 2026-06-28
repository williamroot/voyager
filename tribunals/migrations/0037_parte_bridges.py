import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Tabelas-ponte denormalizadas parte↔tribunal e parte↔papel pra filtro
    rápido na lista de Partes. Filtrar via EXISTS sobre tribunals_processoparte
    (bilhões de linhas) custa ~43s; estas pontes resolvem. Populadas off-band
    por `rebuild_parte_bridges` (a migration cria só as tabelas vazias)."""

    dependencies = [
        ('tribunals', '0036_seed_tjal'),
    ]

    operations = [
        migrations.CreateModel(
            name='ParteTribunal',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('total_processos', models.PositiveIntegerField(default=0)),
                ('parte', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='tribunais_ponte', to='tribunals.parte')),
                ('tribunal', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to='tribunals.tribunal')),
            ],
        ),
        migrations.CreateModel(
            name='PartePapel',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('papel', models.CharField(max_length=120)),
                ('total_processos', models.PositiveIntegerField(default=0)),
                ('parte', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='papeis_ponte', to='tribunals.parte')),
            ],
        ),
        migrations.AddConstraint(
            model_name='partetribunal',
            constraint=models.UniqueConstraint(fields=('parte', 'tribunal'), name='uniq_parte_tribunal'),
        ),
        migrations.AddIndex(
            model_name='partetribunal',
            index=models.Index(fields=['tribunal', '-total_processos'], name='idx_pt_trib_total'),
        ),
        migrations.AddConstraint(
            model_name='partepapel',
            constraint=models.UniqueConstraint(fields=('parte', 'papel'), name='uniq_parte_papel'),
        ),
        migrations.AddIndex(
            model_name='partepapel',
            index=models.Index(fields=['papel', '-total_processos'], name='idx_pp_papel_total'),
        ),
    ]
