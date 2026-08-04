from django.db import migrations, models


def _backfill_facetas(apps, schema_editor):
    """Preenche as facetas desnormalizadas nas análises JÁ existentes, lendo o
    ``resultado`` de cada uma. Reusa a mesma derivação do runtime
    (``dashboard.showcase_jobs._derivar_facetas``) — fonte única de verdade."""
    ShowcaseAnalise = apps.get_model('dashboard', 'ShowcaseAnalise')
    try:
        from dashboard.showcase_jobs import _derivar_facetas
    except Exception:  # pragma: no cover — se o import falhar, deixa nos defaults
        return
    qs = ShowcaseAnalise.objects.all().only(
        'id', 'resultado', 'elapsed_ms').iterator(chunk_size=500)
    lote = []
    for a in qs:
        out = a.resultado if isinstance(a.resultado, dict) else {}
        out.setdefault('elapsed_ms', a.elapsed_ms)
        fac = _derivar_facetas(out)
        a.duracao_s = fac['duracao_s']
        a.oficio_emitido = fac['oficio_emitido']
        a.calculos_homologados = fac['calculos_homologados']
        a.estagio = fac['estagio']
        a.parte_ativa = fac['parte_ativa']
        a.parte_passiva = fac['parte_passiva']
        lote.append(a)
        if len(lote) >= 500:
            ShowcaseAnalise.objects.bulk_update(lote, [
                'duracao_s', 'oficio_emitido', 'calculos_homologados',
                'estagio', 'parte_ativa', 'parte_passiva'])
            lote = []
    if lote:
        ShowcaseAnalise.objects.bulk_update(lote, [
            'duracao_s', 'oficio_emitido', 'calculos_homologados',
            'estagio', 'parte_ativa', 'parte_passiva'])


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0005_showcaseanalise_arquivo_path'),
    ]

    operations = [
        migrations.AddField(
            model_name='showcaseanalise',
            name='duracao_s',
            field=models.FloatField(default=0),
        ),
        migrations.AddField(
            model_name='showcaseanalise',
            name='oficio_emitido',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='showcaseanalise',
            name='calculos_homologados',
            field=models.BooleanField(null=True),
        ),
        migrations.AddField(
            model_name='showcaseanalise',
            name='estagio',
            field=models.CharField(blank=True, default='', max_length=32),
        ),
        migrations.AddField(
            model_name='showcaseanalise',
            name='parte_ativa',
            field=models.CharField(blank=True, default='', max_length=180),
        ),
        migrations.AddField(
            model_name='showcaseanalise',
            name='parte_passiva',
            field=models.CharField(blank=True, default='', max_length=180),
        ),
        migrations.AddIndex(
            model_name='showcaseanalise',
            index=models.Index(fields=['oficio_emitido', '-criado_em'], name='showanalise_oficio_idx'),
        ),
        migrations.AddIndex(
            model_name='showcaseanalise',
            index=models.Index(fields=['calculos_homologados', '-criado_em'], name='showanalise_homolog_idx'),
        ),
        migrations.AddIndex(
            model_name='showcaseanalise',
            index=models.Index(fields=['estagio', '-criado_em'], name='showanalise_estagio_idx'),
        ),
        migrations.RunPython(_backfill_facetas, migrations.RunPython.noop),
    ]
