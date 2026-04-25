import django.contrib.postgres.indexes
import django.contrib.postgres.search
import django.db.models.deletion
from django.contrib.postgres.operations import TrigramExtension, UnaccentExtension
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        TrigramExtension(),
        UnaccentExtension(),
        migrations.CreateModel(
            name='Tribunal',
            fields=[
                ('sigla', models.CharField(max_length=10, primary_key=True, serialize=False)),
                ('nome', models.CharField(max_length=200)),
                ('sigla_djen', models.CharField(max_length=20)),
                ('ativo', models.BooleanField(default=True)),
                ('overlap_dias', models.PositiveIntegerField(default=3)),
                ('data_inicio_disponivel', models.DateField(blank=True, null=True)),
                ('backfill_concluido_em', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={'ordering': ['sigla']},
        ),
        migrations.CreateModel(
            name='Process',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('numero_cnj', models.CharField(max_length=25)),
                ('primeira_movimentacao_em', models.DateTimeField(blank=True, null=True)),
                ('ultima_movimentacao_em', models.DateTimeField(blank=True, null=True)),
                ('total_movimentacoes', models.PositiveIntegerField(default=0)),
                ('inserido_em', models.DateTimeField(auto_now_add=True)),
                ('atualizado_em', models.DateTimeField(auto_now=True)),
                ('tribunal', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='processos', to='tribunals.tribunal')),
            ],
        ),
        migrations.AddConstraint(
            model_name='process',
            constraint=models.UniqueConstraint(fields=('tribunal', 'numero_cnj'), name='uniq_proc_tribunal_cnj'),
        ),
        migrations.AddIndex(
            model_name='process',
            index=models.Index(fields=['tribunal', 'numero_cnj'], name='tribunals_p_tribuna_cnj_idx'),
        ),
        migrations.AddIndex(
            model_name='process',
            index=models.Index(fields=['tribunal', '-ultima_movimentacao_em'], name='tribunals_p_ult_mov_idx'),
        ),
        migrations.AddIndex(
            model_name='process',
            index=models.Index(fields=['inserido_em'], name='tribunals_p_inserido_idx'),
        ),
        migrations.CreateModel(
            name='Movimentacao',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('external_id', models.CharField(max_length=64)),
                ('data_disponibilizacao', models.DateTimeField()),
                ('inserido_em', models.DateTimeField(auto_now_add=True)),
                ('tipo_comunicacao', models.CharField(blank=True, max_length=120)),
                ('tipo_documento', models.CharField(blank=True, max_length=120)),
                ('nome_orgao', models.CharField(blank=True, max_length=255)),
                ('nome_classe', models.CharField(blank=True, max_length=255)),
                ('codigo_classe', models.CharField(blank=True, max_length=20)),
                ('link', models.URLField(blank=True, max_length=500)),
                ('destinatarios', models.JSONField(default=list)),
                ('texto', models.TextField(blank=True)),
                ('search_vector', django.contrib.postgres.search.SearchVectorField(null=True)),
                ('processo', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='movimentacoes', to='tribunals.process')),
                ('tribunal', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='movimentacoes', to='tribunals.tribunal')),
            ],
        ),
        migrations.AddConstraint(
            model_name='movimentacao',
            constraint=models.UniqueConstraint(fields=('tribunal', 'external_id'), name='uniq_mov_tribunal_extid'),
        ),
        migrations.AddIndex(
            model_name='movimentacao',
            index=models.Index(fields=['processo', '-data_disponibilizacao'], name='mov_proc_dt_idx'),
        ),
        migrations.AddIndex(
            model_name='movimentacao',
            index=models.Index(fields=['tribunal', '-data_disponibilizacao'], name='mov_trib_dt_idx'),
        ),
        migrations.AddIndex(
            model_name='movimentacao',
            index=models.Index(fields=['inserido_em'], name='mov_inserido_idx'),
        ),
        migrations.AddIndex(
            model_name='movimentacao',
            index=django.contrib.postgres.indexes.GinIndex(fields=['search_vector'], name='mov_search_vector_gin'),
        ),
        migrations.AddIndex(
            model_name='movimentacao',
            index=django.contrib.postgres.indexes.GinIndex(fields=['texto'], name='mov_texto_trgm', opclasses=['gin_trgm_ops']),
        ),
        migrations.CreateModel(
            name='IngestionRun',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('started_at', models.DateTimeField(auto_now_add=True)),
                ('finished_at', models.DateTimeField(blank=True, null=True)),
                ('status', models.CharField(choices=[('running', 'Em execução'), ('success', 'Sucesso'), ('failed', 'Falha')], default='running', max_length=20)),
                ('janela_inicio', models.DateField()),
                ('janela_fim', models.DateField()),
                ('paginas_lidas', models.PositiveIntegerField(default=0)),
                ('movimentacoes_novas', models.PositiveIntegerField(default=0)),
                ('movimentacoes_duplicadas', models.PositiveIntegerField(default=0)),
                ('processos_novos', models.PositiveIntegerField(default=0)),
                ('erros', models.JSONField(default=list)),
                ('tribunal', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='runs', to='tribunals.tribunal')),
            ],
            options={'ordering': ['-started_at']},
        ),
        migrations.AddIndex(
            model_name='ingestionrun',
            index=models.Index(fields=['tribunal', '-started_at'], name='run_trib_dt_idx'),
        ),
        migrations.AddIndex(
            model_name='ingestionrun',
            index=models.Index(fields=['status', '-started_at'], name='run_status_dt_idx'),
        ),
        migrations.AddIndex(
            model_name='ingestionrun',
            index=models.Index(fields=['tribunal', 'janela_inicio', 'janela_fim'], name='run_trib_window_idx'),
        ),
        migrations.CreateModel(
            name='SchemaDriftAlert',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('detectado_em', models.DateTimeField(auto_now_add=True)),
                ('tipo', models.CharField(choices=[('extra_keys', 'Chaves extras'), ('missing_keys', 'Chaves faltantes'), ('type_mismatch', 'Tipo divergente')], max_length=20)),
                ('chaves', models.JSONField()),
                ('chaves_hash', models.CharField(db_index=True, max_length=64)),
                ('exemplo', models.JSONField()),
                ('resolvido', models.BooleanField(default=False)),
                ('resolvido_em', models.DateTimeField(blank=True, null=True)),
                ('ingestion_run', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='tribunals.ingestionrun')),
                ('tribunal', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='drift_alerts', to='tribunals.tribunal')),
            ],
        ),
        migrations.AddConstraint(
            model_name='schemadriftalert',
            constraint=models.UniqueConstraint(condition=models.Q(('resolvido', False)), fields=('tribunal', 'tipo', 'chaves_hash'), name='uniq_alerta_aberto_tribunal_tipo_chaves'),
        ),
        migrations.AddIndex(
            model_name='schemadriftalert',
            index=models.Index(fields=['resolvido', 'tribunal'], name='drift_resolv_trib_idx'),
        ),
        # tsvector trigger
        migrations.RunSQL(
            sql=[
                """
                CREATE OR REPLACE FUNCTION mov_search_vector_update() RETURNS trigger AS $$
                BEGIN
                  NEW.search_vector :=
                    setweight(to_tsvector('portuguese', unaccent(coalesce(NEW.tipo_comunicacao,''))), 'A') ||
                    setweight(to_tsvector('portuguese', unaccent(coalesce(NEW.nome_classe,''))),     'A') ||
                    setweight(to_tsvector('portuguese', unaccent(coalesce(NEW.nome_orgao,''))),      'B') ||
                    setweight(to_tsvector('portuguese', unaccent(coalesce(NEW.texto,''))),           'C');
                  RETURN NEW;
                END
                $$ LANGUAGE plpgsql;
                """,
                """
                DROP TRIGGER IF EXISTS mov_search_vector_trg ON tribunals_movimentacao;
                CREATE TRIGGER mov_search_vector_trg
                BEFORE INSERT OR UPDATE OF tipo_comunicacao, nome_classe, nome_orgao, texto
                ON tribunals_movimentacao
                FOR EACH ROW EXECUTE FUNCTION mov_search_vector_update();
                """,
            ],
            reverse_sql=[
                "DROP TRIGGER IF EXISTS mov_search_vector_trg ON tribunals_movimentacao;",
                "DROP FUNCTION IF EXISTS mov_search_vector_update();",
            ],
        ),
    ]
