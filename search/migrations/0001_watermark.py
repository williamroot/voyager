"""Watermark durável do sync incremental.

DDL SEGURA: cria uma tabela NOVA e pequena. Não toca `tribunals_process` nem
`tribunals_movimentacao`, logo não pega `ACCESS EXCLUSIVE` em tabela quente —
o que já enfileirou 63 sessões nesta casa.

Motivo, medido em 31/08/2026: o Redis de produção roda `save ""` +
`appendonly no`. O restart de 26/08/2026 06:59:02 UTC apagou as três
watermarks do sync, e o código lia "chave ausente" como "primeiro tique da
vida do sistema" — ancorando em `agora`. Ver `search/models.py`.
"""
from django.db import migrations, models

#: as três chaves do sync + o cursor do gate de completude.
CHAVES = ('sync_es:wm:proc_id', 'sync_es:wm:proc_ts', 'sync_es:wm:mov_id',
          'sync_es:gate:faixa_proc')


def semear_do_cache(apps, _schema_editor):
    """Copia para a tabela o que o Redis AINDA tiver, no instante do deploy.

    Sem este passo existe uma janela feia: se o Redis reiniciar entre a subida
    do código novo e o primeiro tique, cache e tabela estão os dois vazios, o
    tique lê "primeiro tique da vida do sistema" e ancora no topo — exatamente
    a amputação que esta migration existe para acabar. Com o passo, o deploy
    normal (Redis vivo, chaves lá) já nasce com o lado durável preenchido.

    Falha em silêncio de propósito: cache fora não pode impedir um `migrate`.
    O pior caso é ficar como já estava.
    """
    from datetime import datetime

    Watermark = apps.get_model('search', 'Watermark')
    try:
        from django.core.cache import cache
    except Exception:
        return
    for chave in CHAVES:
        try:
            valor = cache.get(chave)
        except Exception:
            return
        if valor is None:
            continue
        if isinstance(valor, (tuple, list)) and len(valor) == 2:
            ts, ident = valor
            bruto = {'t': 'ts_id',
                     'ts': ts.isoformat() if isinstance(ts, datetime) else ts,
                     'id': int(ident or 0)}
        else:
            try:
                bruto = {'t': 'int', 'v': int(valor)}
            except (TypeError, ValueError):
                continue
        Watermark.objects.update_or_create(chave=chave, defaults={'valor': bruto})


def desfazer(apps, _schema_editor):
    """Nada a desfazer: a tabela inteira some no `DeleteModel`."""


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='Watermark',
            fields=[
                ('chave', models.CharField(max_length=64, primary_key=True,
                                           serialize=False)),
                ('valor', models.JSONField(blank=True, null=True)),
                ('ancorada_em', models.DateTimeField(auto_now_add=True)),
                ('atualizada_em', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'watermark de sync',
                'verbose_name_plural': 'watermarks de sync',
                'db_table': 'search_watermark',
            },
        ),
        migrations.RunPython(semear_do_cache, desfazer),
    ]
