from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0004_showcaseanalise_tem_cessao'),
    ]

    operations = [
        migrations.AddField(
            model_name='showcaseanalise',
            name='arquivo_path',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
    ]
