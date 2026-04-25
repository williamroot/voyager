# Code Guidelines — Voyager

Padrões obrigatórios de código no projeto. Toda PR deve aderir.
Quando houver conflito entre regras, **a ordem é**:
1. **PEP 8** (regra do Python)
2. **Zen of Python** (filosofia)
3. **Convenções deste projeto** (abaixo)

## 1. PEP 8 — sempre

- **Imports no topo do arquivo**, nunca dentro de funções/métodos. Exceção única: imports opcionais protegidos por `try/except ImportError` para features que podem não estar instaladas (ex: Sentry, ProxyScrape SDK).
- **3 grupos de imports** separados por linha em branco, em ordem:
  1. stdlib (`os`, `json`, `datetime`)
  2. terceiros (`django`, `rest_framework`, `requests`)
  3. locais (`from tribunals.models import ...`, `from . import ...`)
- Dentro de cada grupo, **ordem alfabética**.
- **Máx. 120 caracteres por linha** (relaxado vs PEP 8 padrão de 79; alinha com `ruff` e Django convention).
- **Nomenclatura**:
  - `snake_case` para funções e variáveis
  - `PascalCase` para classes
  - `UPPER_CASE` para constantes
  - `_prefixo` para privados/módulo-internos
  - **NUNCA** abreviar nomes de domínio (ex: `processo`, não `proc`; `movimentacao`, não `mov` — exceto em loops curtos)
- **2 linhas em branco** entre defs de top-level; **1 linha em branco** dentro de classes.
- **Espaço ao redor de operadores**, **nunca** dentro de parênteses.
- **Trailing comma** em literais multi-linha (compatível com Black/Ruff).

## 2. Zen of Python — guia para decisões

> import this

Citações que mais aplicamos aqui:
- **Beautiful is better than ugly.** Código bonito é menor e mais legível.
- **Explicit is better than implicit.** Não esconda comportamento em magic.
- **Simple is better than complex.** Não invente abstrações sem necessidade.
- **Readability counts.** Otimize pra leitura, não pra escrita.
- **Errors should never pass silently.** `except: pass` é proibido. Sempre logar ou re-raise.
- **In the face of ambiguity, refuse the temptation to guess.** Não chute nomes de campos — leia a fonte.
- **There should be one — and preferably only one — obvious way to do it.** Não duplique helpers.
- **Now is better than never. Although never is often better than *right* now.** Não atrase deploy por refactoring opcional, nem aceite débito que vire bola de neve.

## 3. Princípios fundamentais (CLAUDE.md global)

- **NUNCA supor nomes de campos** entre camadas (banco, API, função). Sempre ler a fonte de dados.
- **Verificar antes de alterar**. Quando consumir dados externos, ler a função/SP/modelo que retorna primeiro.
- **Early return** para reduzir aninhamento. Trate erros no topo da função e retorne cedo.
- **Funções fazem uma coisa**. Se você precisa de um `# Step 2:` no meio, provavelmente são duas funções.
- **Nunca duplicar lógica real**. Extraia pra helper SÓ quando houver ≥3 ocorrências reais (não preventivamente).
- **Sem magic numbers/strings**. Constantes nomeadas no topo do módulo.

## 4. Formatação automatizada — Ruff

O projeto usa `ruff` (formatter + linter, substitui Black + Flake8 + isort).
Configuração em `ruff.toml` na raiz.

```bash
ruff format .       # equivalente a black, formata in-place
ruff check . --fix  # equivalente a flake8/isort/pylint, aplica fixes seguros
```

Em CI: `ruff format --check . && ruff check .` deve passar.

Settings principais (consulte `ruff.toml`):
- `line-length = 120`
- `target-version = "py312"`
- isort: força grupos stdlib / third-party / first-party (`tribunals`, `djen`, `api`, `dashboard`, `core`)
- regras ativas: E, F, I, B, C4, UP, N, SIM, RET, PL

## 5. Padrões específicos do projeto

### 5.1 Django ORM
- **`select_related` / `prefetch_related`** sempre que houver acesso a FK em loops/serializers.
- **`bulk_create(ignore_conflicts=True)`** para idempotência em ingestão; nunca `for x: x.save()`.
- **Constraints e indexes nas Meta**, nunca via SQL ad-hoc (exceto extensões como triggers tsvector).
- **`update_fields`** em todo `.save()` que toca poucos campos — reduz IO e evita race em campos não relacionados.

### 5.2 DRF
- **Serializers separados em List vs Detail**. List traz só campos enxutos.
- **ViewSet escolhe via `get_serializer_class()`**.
- **Filtros via django-filter `FilterSet`**, nunca lógica de query no `get_queryset` quando puder ser declarativa.

### 5.3 Workers RQ
- **Jobs idempotentes**. Sempre re-rodar deve ser seguro.
- **Não dependa de estado em memória entre jobs**. Use Postgres/Redis.
- **Timeouts explícitos** no `@job(...)`. Default global pode ser baixo demais.

### 5.4 Logs
- **`logger = logging.getLogger('voyager.<modulo>')`** no topo do módulo.
- **Logs estruturados via `extra={...}`** sempre que carregar contexto (`tribunal`, `run_id`, `pagina`, etc).
- **Nunca f-string com PII**. Use `extra` que pode ser scrubbed.

### 5.5 Comentários
- **Default: nenhum comentário**. Código autoexplicativo via nomes.
- **Comentário só pra "porquê não-óbvio"**: invariante oculto, workaround de bug específico, comportamento que surpreenderia o leitor.
- **Não comentar o "o quê"**. Identificadores bons já fazem isso.
- **Não comentar referências temporais** ("usado por X", "adicionado pra Y") — isso vai pra commit message ou PR description.

### 5.6 Migrations
- Geradas por `makemigrations`, nunca editadas exceto pra data migrations.
- **Nunca dropar coluna em uma deploy só**:
  - Etapa 1: `null=True` + parar de escrever
  - Etapa 2: drop em deploy posterior
- **Data migrations idempotentes** (`update_or_create`, não `create`).

### 5.7 Tests
- `pytest` + `pytest-django`. Sem `unittest.TestCase`.
- **Camadas**:
  - **unit**: lógica pura, sem DB, sem rede.
  - **integration**: DB real, mocka rede com `responses`.
  - **api**: endpoints DRF com `APIClient`.
- **Cobertura mínima** 80% global, 95% em `djen/` e modelos críticos.
- **Naming**: `test_<comportamento_esperado>_quando_<condição>()`.

### 5.8 Templates Django
- **Indentação 2 espaços** em HTML.
- **Tags Django sempre em uma linha** quando possível.
- **Custom template tags** em `<app>/templatetags/<app>_extras.py`.

## 6. Git / commits

- **Conventional Commits** (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`).
- Linha 1: ≤72 chars, imperativo, presente, em pt-BR.
- Corpo (opcional): explica **por quê**, não **o quê**.
- **Sem `--no-verify`** salvo emergência aprovada.
- **PR por feature**. Revertível, focado.

## 7. Segurança

- **Secrets só em `.env`**, nunca em código nem em git.
- **Sem credentials hardcoded** em testes (use `.env.test` ou fixtures).
- **Validar input em boundaries** (views, deserializers); confiar internamente.
- **Sem `assert` em prod-path** (`python -O` retira assertions).
- **Sem `eval/exec`** sobre input externo.

## 8. Performance

- **N+1 são bug**. Cheque em todo serializer e template loop.
- **Agregações em SQL**, nunca em Python sobre QuerySet.
- **Indexes pra queries quentes**. EXPLAIN ANALYZE quando duvidar.
- **Materialized views** pra dashboard se a query passar de 200ms.

## 9. Antes de abrir PR — checklist

- [ ] `ruff format --check .` passa
- [ ] `ruff check .` passa
- [ ] `pytest` passa
- [ ] Migrations geradas e revisadas
- [ ] Não há imports inline (exceto `try/except ImportError` justificado)
- [ ] Não há comentários redundantes
- [ ] Não há código morto / unused vars
- [ ] Commit messages no formato Conventional
