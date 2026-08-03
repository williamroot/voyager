from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0003_showcaseanalise'),
    ]

    operations = [
        migrations.AddField(
            model_name='showcaseanalise',
            name='tem_cessao',
            field=models.BooleanField(default=False),
        ),
        migrations.AddIndex(
            model_name='showcaseanalise',
            index=models.Index(fields=['tem_cessao', '-criado_em'], name='showanalise_cessao_idx'),
        ),
    ]
