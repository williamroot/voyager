# Runbook — Schema Drift

## O que é
A DJEN pode adicionar/remover campos no payload sem aviso. O parser compara cada item com `EXPECTED_KEYS` e cria um `SchemaDriftAlert` quando há divergência.

Tipos:
- `extra_keys` — campo novo apareceu (não estamos guardando).
- `missing_keys` — campo esperado sumiu.
- `type_mismatch` — campo presente mas com tipo diferente do esperado.

## Quando o alerta dispara
- Página da DJEN parseada com chave fora do esperado.
- Slack envia notificação se `SLACK_NOTIFY_DRIFT=true`.
- Aparece como card vermelho em `/dashboard/ingestao/`.
- Aparece em `python manage.py djen_status`.

## Resolver

1. Abrir o alerta (admin Django ou dashboard).
2. Olhar o campo `exemplo` (item DJEN que disparou — `texto` truncado a 500 chars).
3. Atualizar `djen/parser.py`:
   - Se for `extra_keys` e queremos guardar: adicionar campo ao model `Movimentacao`, gerar migration, mapear em `parse_item`, adicionar à `EXPECTED_KEYS`.
   - Se for `extra_keys` e podemos ignorar: só adicionar à `EXPECTED_KEYS` (silencia o alerta).
   - Se for `missing_keys`: ajustar o parser pra tolerar a ausência (já usamos `.get(...)` por padrão), e remover de `EXPECTED_KEYS` se sumiu de vez.
4. Marcar alerta como `resolvido=True` no admin (ou via action em massa).
5. O constraint parcial `uniq_alerta_aberto_tribunal_tipo_chaves` reabre automaticamente se a divergência voltar.
