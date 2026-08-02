from django.db import migrations, models

import pdf_storage.models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('tribunals', '0044_seed_fonte_diario'),
    ]

    operations = [
        migrations.CreateModel(
            name='PdfArquivo',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('arquivo', models.FileField(storage='pdfs', upload_to='movimentacoes/%Y/%m/')),
                ('tamanho_bytes', models.PositiveBigIntegerField(default=0)),
                ('hash_sha256', models.CharField(blank=True, max_length=64)),
                ('baixado_em', models.DateTimeField(auto_now_add=True)),
                ('status', models.CharField(
                    choices=[('pendente', 'Pendente'), ('ok', 'OK'), ('erro', 'Erro')],
                    default='pendente', max_length=10)),
                ('erro', models.TextField(blank=True)),
                ('tentativas', models.PositiveSmallIntegerField(default=0)),
                ('movimentacao', models.OneToOneField(
                    on_delete=models.CASCADE,
                    related_name='pdf',
                    to='tribunals.movimentacao')),
            ],
            options={
                'ordering': ['-baixado_em'],
                'indexes': [models.Index(fields=['-baixado_em'], name='pdf_storage_baixado_135db5_idx')],
            },
        ),
    ]