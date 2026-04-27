"""Adiciona UniqueConstraint partial pra Partes sem doc nem OAB.

Separada de 0012 (dedup) porque Postgres não cria índice partial na
mesma transação com trigger events pendentes (gerados pelo DELETE/UPDATE
em ProcessoParte). Roda como migration própria (transação nova).
"""
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):

    dependencies = [('tribunals', '0012_dedup_partes_sem_doc')]

    operations = [
        migrations.AddConstraint(
            model_name='parte',
            constraint=models.UniqueConstraint(
                fields=['nome', 'tipo'],
                condition=Q(documento='') & Q(oab=''),
                name='uniq_parte_sem_doc_nem_oab',
            ),
        ),
    ]
