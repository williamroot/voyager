"""Seed inicial de ClassificadorVersao(ativa=True) com pesos hardcoded v6.

Suporte ao hot reload (T17): o classificador passa a ler pesos do DB com TTL
de 60s. Antes desta migration, nenhuma row existe — em dev/staging fresh o
classificador cairia no fallback hardcoded em todas as chamadas (warning).

Idempotente: se já há uma versão ativa, não faz nada. Backfill silencioso
pra ambientes que nunca rodaram T17.
"""
from django.db import migrations

# Snapshot de HARDCODED_WEIGHTS em tribunals/classificador.py no momento da
# criação da migration. Migrations não devem importar do app pra evitar acoplar
# o schema histórico ao código atual — duplicamos o dict aqui de propósito.
_V6_WEIGHTS = {
    '_intercept_':            -2.635,
    'F1_cumprim':              1.801,
    'F10_juizado_ANTI':       -1.119,
    'F2_precat_tc':            0.129,
    'F7_envTrib_tc':           0.292,
    'F11_precat_text':         0.746,
    'F12_rpv_text':            0.357,
    'F13_reqPag_text':        -0.659,
    'F14_oficio_text':        -0.174,
    'F15_logMovs':             1.546,
    'F16_logTipos':           -3.184,
    'F17_logN1count':          0.143,
    'F18_anoZ':                0.334,
    'F19_cancelado_ANTI':      0.000,
    'F20_exp_juriscope':      -0.033,
    'F21_diasUltMovZ':         0.497,
    'F23_logPartes':           -0.606,
    'F1xF11':                 -0.131,
    'F1xF15':                  1.630,
    'F1xF20':                 -0.027,
}

_V6_METRICAS = {
    'auc': 0.9610,
    'precision_at_500': 0.986,
    'precision_at_1000': 0.993,
    'precision_at_5000': 0.991,
    'precision_at_10000': 0.982,
    'train_size': 840634,
    'test_size': 210157,
    'n_features': 19,
}


def seed_v6(apps, schema_editor):
    ClassificadorVersao = apps.get_model('tribunals', 'ClassificadorVersao')

    if ClassificadorVersao.objects.filter(ativa=True).exists():
        return

    obj, criada = ClassificadorVersao.objects.get_or_create(
        versao='v6',
        defaults={
            'pesos': _V6_WEIGHTS,
            'metricas': _V6_METRICAS,
            'ativa': True,
            'shadow': False,
            'notas': 'Seed inicial T17 — pesos hardcoded do tribunals/classificador.py',
        },
    )
    if not criada and not obj.ativa:
        # Já existia v6 mas inativa (caso esquisito). Ativa.
        obj.ativa = True
        obj.save(update_fields=['ativa'])


def unseed_v6(apps, schema_editor):
    """Reversa: remove a row criada. Não faz nada se outro modelo virou ativa
    no meio do caminho."""
    ClassificadorVersao = apps.get_model('tribunals', 'ClassificadorVersao')
    ClassificadorVersao.objects.filter(
        versao='v6',
        notas__startswith='Seed inicial T17',
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('tribunals', '0025_validacao_nits_review_t6'),
    ]

    operations = [
        migrations.RunPython(seed_v6, unseed_v6),
    ]
