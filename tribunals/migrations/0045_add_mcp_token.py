from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tribunals', '0044_seed_fonte_diario'),
    ]

    operations = [
        migrations.AddField(
            model_name='apiclient',
            name='mcp_token',
            field=models.UUIDField(blank=True, editable=False, null=True, unique=True),
        ),
    ]