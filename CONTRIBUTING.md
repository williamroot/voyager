# Contribuindo

## Setup local sem Docker

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
# DATABASE_URL apontando pro seu Postgres local com pg_trgm + unaccent
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

## Estilo
- `ruff format .` antes de commitar.
- `ruff check .` deve passar.
- Type hints quando útil; sem zelo dogmático.
- Sem comentários óbvios — código autoexplicativo via nomes claros.

## Migrations
- Toda mudança de schema → migration própria.
- Nunca dropar coluna em uma deploy só:
  - Etapa 1: marcar nullable + remover escrita.
  - Etapa 2: drop em deploy posterior.

## Testes
```bash
pytest
```

## Schema drift
Se um `SchemaDriftAlert` aparecer em produção, a resolução é uma PR:
1. Atualizar `djen/parser.py` (mapeamento + `EXPECTED_KEYS`).
2. Migration nova se adicionar campo.
3. Marcar alerta como resolvido após deploy.
