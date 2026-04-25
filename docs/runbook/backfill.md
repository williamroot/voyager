# Runbook — Backfill DJEN

## Quando rodar
- Ao subir o sistema pela primeira vez para um tribunal ativo.
- Ao reativar um tribunal que estava inativo.
- Ao re-popular após corrupção de dados.

## Passos

```bash
# 1) (Se ainda não rodado) descobrir o primeiro dia disponível na DJEN
docker compose exec web python manage.py djen_descobrir_inicio TRF1

# 2) Disparar o backfill completo (vai pra fila djen_backfill, worker dedicado)
docker compose exec web python manage.py djen_backfill TRF1

# 3) Acompanhar
docker compose exec web python manage.py djen_status
docker compose logs -f worker_ingestion
```

O backfill é particionado em janelas de 30 dias, do mais antigo pro mais novo. Cada janela = 1 `IngestionRun`. Se o job cair, basta re-disparar — janelas com `status=success` são puladas.

`Tribunal.backfill_concluido_em` só é setado **se todas as janelas estão success**. Se alguma estiver falhada, o campo permanece NULL e o cron diário continua pulando esse tribunal — proposital.

## Forçar inicio diferente

```bash
docker compose exec web python manage.py djen_backfill TRF1 --inicio 2024-01-01
```

## Rodar inline (debug)

```bash
docker compose exec web python manage.py djen_backfill TRF1 --sync
```
