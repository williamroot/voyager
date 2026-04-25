"""Constraint partial em ProcessoParte: dedupe só onde representa IS NULL.

Um advogado pode representar 2 réus distintos no mesmo processo — são 2 rows
válidos com mesma (processo, parte, polo, papel) mas representa diferentes.
Constraint full estava bloqueando esse caso.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [('tribunals', '0005_parte_processoparte_enriquecimento')]

    operations = [
        migrations.RemoveConstraint(model_name='processoparte', name='uniq_processo_parte_polo_papel'),
        migrations.AddConstraint(
            model_name='processoparte',
            constraint=models.UniqueConstraint(
                fields=('processo', 'parte', 'polo', 'papel'),
                condition=models.Q(('representa__isnull', True)),
                name='uniq_processo_parte_polo_papel_principal',
            ),
        ),
    ]
