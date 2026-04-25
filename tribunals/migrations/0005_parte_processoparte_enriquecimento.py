import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [('tribunals', '0004_process_aggregates_trigger')]

    operations = [
        # Process — campos de enriquecimento
        migrations.AddField('process', 'classe_codigo', models.CharField(blank=True, max_length=20)),
        migrations.AddField('process', 'classe_nome', models.CharField(blank=True, max_length=255)),
        migrations.AddField('process', 'assunto_codigo', models.CharField(blank=True, max_length=20)),
        migrations.AddField('process', 'assunto_nome', models.CharField(blank=True, max_length=255)),
        migrations.AddField('process', 'data_autuacao', models.DateField(blank=True, null=True)),
        migrations.AddField('process', 'valor_causa', models.DecimalField(blank=True, decimal_places=2, max_digits=18, null=True)),
        migrations.AddField('process', 'orgao_julgador_codigo', models.CharField(blank=True, max_length=20)),
        migrations.AddField('process', 'orgao_julgador_nome', models.CharField(blank=True, max_length=255)),
        migrations.AddField('process', 'juizo', models.CharField(blank=True, max_length=255)),
        migrations.AddField('process', 'segredo_justica', models.BooleanField(default=False)),
        migrations.AddField('process', 'enriquecido_em', models.DateTimeField(blank=True, null=True)),
        migrations.AddIndex(
            model_name='process',
            index=models.Index(fields=['enriquecido_em'], name='proc_enriqu_idx'),
        ),
        migrations.AddIndex(
            model_name='process',
            index=models.Index(fields=['classe_codigo'], name='proc_classe_idx'),
        ),
        migrations.AddIndex(
            model_name='process',
            index=models.Index(fields=['orgao_julgador_codigo'], name='proc_orgao_idx'),
        ),

        # Parte
        migrations.CreateModel(
            name='Parte',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('nome', models.CharField(max_length=255)),
                ('documento', models.CharField(blank=True, max_length=20)),
                ('tipo_documento', models.CharField(blank=True, max_length=10)),
                ('oab', models.CharField(blank=True, max_length=20)),
                ('tipo', models.CharField(
                    choices=[('pf', 'Pessoa Física'), ('pj', 'Pessoa Jurídica'),
                             ('advogado', 'Advogado'), ('desconhecido', 'Desconhecido')],
                    default='desconhecido', max_length=20,
                )),
                ('primeira_aparicao_em', models.DateTimeField(auto_now_add=True)),
                ('ultima_aparicao_em', models.DateTimeField(auto_now=True)),
                ('total_processos', models.PositiveIntegerField(default=0)),
            ],
        ),
        migrations.AddConstraint(
            model_name='parte',
            constraint=models.UniqueConstraint(
                condition=models.Q(('documento', ''), _negated=True),
                fields=('documento',), name='uniq_parte_documento',
            ),
        ),
        migrations.AddConstraint(
            model_name='parte',
            constraint=models.UniqueConstraint(
                condition=models.Q(('oab', ''), _negated=True),
                fields=('oab',), name='uniq_parte_oab',
            ),
        ),
        migrations.AddIndex(model_name='parte', index=models.Index(fields=['nome'], name='parte_nome_idx')),
        migrations.AddIndex(model_name='parte', index=models.Index(fields=['documento'], name='parte_doc_idx')),
        migrations.AddIndex(model_name='parte', index=models.Index(fields=['oab'], name='parte_oab_idx')),
        migrations.AddIndex(model_name='parte', index=models.Index(fields=['tipo'], name='parte_tipo_idx')),

        # ProcessoParte
        migrations.CreateModel(
            name='ProcessoParte',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('polo', models.CharField(
                    choices=[('ativo', 'Polo ativo'), ('passivo', 'Polo passivo'), ('outros', 'Outros')],
                    max_length=10,
                )),
                ('papel', models.CharField(blank=True, max_length=120)),
                ('inserido_em', models.DateTimeField(auto_now_add=True)),
                ('parte', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                            related_name='participacoes', to='tribunals.parte')),
                ('processo', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                               related_name='participacoes', to='tribunals.process')),
                ('representa', models.ForeignKey(blank=True, null=True,
                                                  on_delete=django.db.models.deletion.SET_NULL,
                                                  related_name='representado_por', to='tribunals.processoparte')),
            ],
        ),
        migrations.AddConstraint(
            model_name='processoparte',
            constraint=models.UniqueConstraint(
                fields=('processo', 'parte', 'polo', 'papel'),
                name='uniq_processo_parte_polo_papel',
            ),
        ),
        migrations.AddIndex(model_name='processoparte',
                            index=models.Index(fields=['parte', 'polo'], name='pp_parte_polo_idx')),
        migrations.AddIndex(model_name='processoparte',
                            index=models.Index(fields=['processo', 'polo'], name='pp_proc_polo_idx')),
        migrations.AddIndex(model_name='processoparte',
                            index=models.Index(fields=['papel'], name='pp_papel_idx')),

        # Trigger pra manter Parte.total_processos sincronizado
        migrations.RunSQL(
            sql="""
                CREATE OR REPLACE FUNCTION update_parte_total_processos()
                RETURNS trigger AS $$
                BEGIN
                  UPDATE tribunals_parte p SET total_processos = (
                    SELECT COUNT(DISTINCT processo_id)
                    FROM tribunals_processoparte
                    WHERE parte_id = p.id
                  )
                  WHERE p.id IN (
                    SELECT DISTINCT parte_id FROM new_table
                    UNION
                    SELECT DISTINCT parte_id FROM old_table
                  );
                  RETURN NULL;
                END
                $$ LANGUAGE plpgsql;

                CREATE OR REPLACE FUNCTION update_parte_total_processos_ins()
                RETURNS trigger AS $$
                BEGIN
                  UPDATE tribunals_parte p SET total_processos = (
                    SELECT COUNT(DISTINCT processo_id)
                    FROM tribunals_processoparte
                    WHERE parte_id = p.id
                  )
                  WHERE p.id IN (SELECT DISTINCT parte_id FROM new_table);
                  RETURN NULL;
                END
                $$ LANGUAGE plpgsql;

                CREATE OR REPLACE FUNCTION update_parte_total_processos_del()
                RETURNS trigger AS $$
                BEGIN
                  UPDATE tribunals_parte p SET total_processos = (
                    SELECT COUNT(DISTINCT processo_id)
                    FROM tribunals_processoparte
                    WHERE parte_id = p.id
                  )
                  WHERE p.id IN (SELECT DISTINCT parte_id FROM old_table);
                  RETURN NULL;
                END
                $$ LANGUAGE plpgsql;

                DROP TRIGGER IF EXISTS pp_total_ins ON tribunals_processoparte;
                CREATE TRIGGER pp_total_ins
                AFTER INSERT ON tribunals_processoparte
                REFERENCING NEW TABLE AS new_table
                FOR EACH STATEMENT EXECUTE FUNCTION update_parte_total_processos_ins();

                DROP TRIGGER IF EXISTS pp_total_del ON tribunals_processoparte;
                CREATE TRIGGER pp_total_del
                AFTER DELETE ON tribunals_processoparte
                REFERENCING OLD TABLE AS old_table
                FOR EACH STATEMENT EXECUTE FUNCTION update_parte_total_processos_del();
            """,
            reverse_sql="""
                DROP TRIGGER IF EXISTS pp_total_ins ON tribunals_processoparte;
                DROP TRIGGER IF EXISTS pp_total_del ON tribunals_processoparte;
                DROP FUNCTION IF EXISTS update_parte_total_processos_ins();
                DROP FUNCTION IF EXISTS update_parte_total_processos_del();
                DROP FUNCTION IF EXISTS update_parte_total_processos();
            """,
        ),
    ]
