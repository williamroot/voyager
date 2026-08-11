"""Recalcula `ProcessoParte.polo` das partes que caíram em 'outros' por causa do
bug de gênero no e-SAJ (fix `enrichers/esaj.py`, commit 2e3f219).

O papel processual está gravado em `tribunals_processoparte.papel`; o polo é
DERIVÁVEL dele. Então não é preciso re-enriquecer nada — recalculamos o polo no
banco usando a MESMA lógica de `EsajEnricher._polo_para_tipo` (importada daqui, pra
não divergir da fonte).

Duas frentes, porque o mesmo commit corrigiu os dois lados:
  - PASSIVO: femininos ('REQUERIDA','EXECUTADA','RÉ'...) que o `startswith` masculino
    não casava → caíam em 'outros'.
  - ATIVO:  'Exequente' por extenso, que NUNCA esteve na lista (só a abreviação
    'exeqte') → caía em 'outros' desde sempre.

Só toca linhas `polo='outros'` cujo papel passa a mapear pra ativo/passivo — mantém o
papel, muda só o polo. Respeita `uniq_processo_parte_polo_papel_principal`
(processo_id, parte_id, polo, papel) WHERE representa_id IS NULL: pula a linha se a
mesma tripla já existir no polo-destino (guarda NOT EXISTS). Batched por faixa de pk,
resumível, com --dry-run.

    python manage.py recalcular_polo_esaj --dry-run
    python manage.py recalcular_polo_esaj
"""
import time

from django.core.management.base import BaseCommand
from django.db import connection, transaction

from enrichers.esaj import BaseEsajEnricher

# Fonte única: as mesmas listas que o enricher usa pra inferir polo do texto.
_ATIVO = tuple(BaseEsajEnricher._PAPEIS_ATIVO)
_PASSIVO = tuple(BaseEsajEnricher._PAPEIS_PASSIVO)
_PASSIVO_EXATOS = tuple(sorted(BaseEsajEnricher._PAPEIS_PASSIVO_EXATOS))


def _like_any(col, prefixos):
    """(<col> LIKE 'p1%' OR <col> LIKE 'p2%' ...) — espelha o startswith do enricher."""
    termos = ' OR '.join([f"{col} LIKE %s" for _ in prefixos])
    params = [p + '%' for p in prefixos]
    return f'({termos})', params


class Command(BaseCommand):
    help = "Recalcula polo das partes e-SAJ que caíram em 'outros' por gênero (passivo) + exequent (ativo)."

    def add_arguments(self, p):
        p.add_argument('--dry-run', action='store_true')
        p.add_argument('--batch', type=int, default=50_000, help='tamanho da janela de pk')
        p.add_argument('--sleep', type=float, default=0.0)

    def handle(self, *a, **o):
        dry, batch, sleep = o['dry_run'], o['batch'], o['sleep']

        # condição de papel por polo-destino (ordem espelha _polo_para_tipo:
        # EXATOS e prefixos passivos → passivo; prefixos ativos → ativo).
        col = 'lower(btrim(papel))'
        passivo_like, passivo_params = _like_any(col, _PASSIVO)
        ativo_like, ativo_params = _like_any(col, _ATIVO)
        exatos_ph = ', '.join(['%s'] * len(_PASSIVO_EXATOS))
        cond_passivo = f'({col} IN ({exatos_ph}) OR {passivo_like})'
        cond_passivo_params = list(_PASSIVO_EXATOS) + passivo_params
        cond_ativo = ativo_like
        cond_ativo_params = ativo_params

        frentes = [
            ('passivo', cond_passivo, cond_passivo_params),
            ('ativo', cond_ativo, cond_ativo_params),
        ]

        self.stdout.write(f'{"DRY-RUN " if dry else ""}batch={batch:,} '
                          f'passivo_exatos={_PASSIVO_EXATOS} '
                          f'passivo_pref={len(_PASSIVO)} ativo_pref={len(_ATIVO)}')

        with connection.cursor() as c:
            c.execute('SELECT coalesce(min(id),0), coalesce(max(id),0) FROM tribunals_processoparte')
            lo, hi = c.fetchone()
        self.stdout.write(f'range pk: {lo:,}..{hi:,}')

        for destino, cond, cond_params in frentes:
            # Candidatas: polo='outros' + papel casa + (ainda não colide no destino).
            # A guarda de colisão só vale pras linhas representa_id IS NULL (as que o
            # índice único cobre); linhas com representa_id não colidem nunca.
            guard = (
                "AND (pp.representa_id IS NOT NULL OR NOT EXISTS ("
                "  SELECT 1 FROM tribunals_processoparte o "
                "  WHERE o.processo_id = pp.processo_id AND o.parte_id = pp.parte_id "
                "    AND o.polo = %s AND o.papel = pp.papel AND o.representa_id IS NULL "
                "    AND o.id <> pp.id))"
            )
            base_where = (f"pp.polo = 'outros' AND {cond} " + guard)
            # contagem total (candidatas sem colisão) + colisões puladas
            with connection.cursor() as c:
                c.execute(
                    f"SELECT count(*) FROM tribunals_processoparte pp "
                    f"WHERE {base_where}", cond_params + [destino])
                candidatas = c.fetchone()[0]
                c.execute(
                    f"SELECT count(*) FROM tribunals_processoparte pp "
                    f"WHERE pp.polo='outros' AND {cond} "
                    f"AND pp.representa_id IS NULL AND EXISTS ("
                    f"  SELECT 1 FROM tribunals_processoparte o "
                    f"  WHERE o.processo_id=pp.processo_id AND o.parte_id=pp.parte_id "
                    f"    AND o.polo=%s AND o.papel=pp.papel AND o.representa_id IS NULL "
                    f"    AND o.id<>pp.id)", cond_params + [destino])
                colisoes = c.fetchone()[0]
            self.stdout.write(self.style.WARNING(
                f'[{destino}] candidatas={candidatas:,} · colisões_puladas={colisoes:,}'))

            if dry:
                # amostra por papel pra conferência
                with connection.cursor() as c:
                    c.execute(
                        f"SELECT btrim(papel), count(*) FROM tribunals_processoparte pp "
                        f"WHERE {base_where} GROUP BY 1 ORDER BY 2 DESC LIMIT 20",
                        cond_params + [destino])
                    for papel, n in c.fetchall():
                        self.stdout.write(f'    {papel!r}: {n:,}')
                continue

            # UPDATE batched por faixa de pk
            feitos, t0 = 0, time.monotonic()
            lo_i = lo
            while lo_i <= hi:
                hi_i = lo_i + batch
                params = ([destino]                      # SET polo
                          + cond_params + [destino]      # WHERE cond + guard destino
                          + [lo_i, hi_i])                # faixa de pk
                with transaction.atomic():
                    with connection.cursor() as c:
                        c.execute(
                            f"UPDATE tribunals_processoparte pp SET polo = %s "
                            f"WHERE {base_where} AND pp.id >= %s AND pp.id < %s",
                            params)
                        feitos += c.rowcount
                lo_i = hi_i
                if sleep:
                    time.sleep(sleep)
            dt = time.monotonic() - t0
            self.stdout.write(self.style.SUCCESS(
                f'[{destino}] atualizadas={feitos:,} em {dt/60:.1f}min'))

        self.stdout.write(self.style.SUCCESS('FIM'))
