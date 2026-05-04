"""Renomeia índices customizados (0020) para nomes auto-gerados pelo Django
e reconcilia verbose_name='ID' nos BigAutoField das models do 0020.

SQL real: 3 ALTER INDEX RENAME (instantâneo). Os AlterField são no-op (sem SQL).
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tribunals', '0021_process_list_indexes'),
    ]

    operations = [
        migrations.RenameIndex(
            model_name='classificacaolog',
            new_name='tribunals_c_process_c253b0_idx',
            old_name='classif_log_proc_dt_idx',
        ),
        migrations.RenameIndex(
            model_name='leadconsumption',
            new_name='tribunals_l_cliente_186de8_idx',
            old_name='leadcons_cli_dt_idx',
        ),
        migrations.RenameIndex(
            model_name='leadconsumption',
            new_name='tribunals_l_cliente_cd6f14_idx',
            old_name='leadcons_cli_proc_idx',
        ),
        migrations.AlterField(
            model_name='apiclient',
            name='id',
            field=models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID'),
        ),
        migrations.AlterField(
            model_name='classificacaolog',
            name='id',
            field=models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID'),
        ),
        migrations.AlterField(
            model_name='classificadorversao',
            name='id',
            field=models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID'),
        ),
        migrations.AlterField(
            model_name='leadconsumption',
            name='id',
            field=models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID'),
        ),
    ]
