# TRF5 Enricher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adicionar suporte completo ao TRF5 (Tribunal Regional Federal da 5ª Região — AL/CE/PB/PE/RN/SE) no Voyager: ingestão DJEN diária + backfill histórico + enriquecimento via PJe consulta pública, no mesmo molde do TRF1/TRF3.

**Architecture:** TRF5 usa PJe consulta pública (sem login) em `pje1g.trf5.jus.br`. Reusamos a `BasePjeEnricher` (`enrichers/pje.py`) — só criamos uma subclasse de ~15 linhas com `BASE_URL`/`LIST_URL`/`DETALHE_PATH`/`TRIBUNAL_SIGLA`, plugamos em 4 pontos (jobs `_ENRICHERS`, ingestion `TRIBUNAIS_COM_ENRICHER`, settings `RQ_QUEUES`, compose `worker_trf5`), ativamos o `Tribunal` já seedado (inativo desde 0002), descobrimos o floor histórico via DJEN e disparamos o backfill. O `juriscope/falcon/datamodel/processors/trf5.py` serve apenas como **referência** do host PJe (ele faz login autenticado + OTP; nós usamos consulta pública, que não precisa).

**Tech Stack:** Django 5 + django-rq + Postgres + Redis + APScheduler + BeautifulSoup. PJe = JSF/Seam consulta pública. Docker Compose multi-host (`.32` web/scheduler + `.36` workers).

---

## Contexto e descobertas

Pontos de plugagem mapeados:
- `enrichers/trf1.py` (13 linhas) — template literal pra `enrichers/trf5.py`
- `enrichers/jobs.py:15-19` (`_ENRICHERS` dict)
- `djen/ingestion.py:27` (`TRIBUNAIS_COM_ENRICHER` set)
- `core/settings.py:179-180` (`RQ_QUEUES` — enrich_trf1/enrich_trf3)
- `djen/scheduler.py:66` (`EARLY` dict — slots 02:00/02:30)
- `docker-compose-workers.yml:14-30` (worker_trf1 / worker_trf3 services)
- `tribunals/migrations/0002_seed_tribunais.py:8` (TRF5 já existe com `ativo=False`)

URL prevista pra TRF5 (a confirmar via curl no Task 1):
- BASE_URL = `https://pje1g.trf5.jus.br`
- LIST_URL = `{BASE}/consultapublica/ConsultaPublica/listView.seam`
- DETALHE_PATH = `/consultapublica/ConsultaPublica/DetalheProcessoConsultaPublica`

Evidência: `juriscope/falcon/datamodel/processors/trf5.py:349` referencia
`https://pje1g.trf5.jus.br/consultapublica/ConsultaPublica/listView.seam`.

Não existem testes unitários pros enrichers TRF1/TRF3 — o padrão estabelecido é
validar live (curl manual + management command `enriquecer_processo` num CNJ
real). Este plano segue o mesmo padrão; criar tests/fixtures sintéticos pra um
subclass de 15 linhas seria contrário a YAGNI.

---

## File structure

| Caminho | Responsabilidade | Ação |
|---|---|---|
| `enrichers/trf5.py` | Subclasse PJe configurando URLs do TRF5 | **Criar** (~15 linhas) |
| `enrichers/jobs.py` | Registrar `Trf5Enricher` em `_ENRICHERS` | **Modificar** (linha 10 + 17) |
| `djen/ingestion.py` | Adicionar `'TRF5'` ao set `TRIBUNAIS_COM_ENRICHER` | **Modificar** (linha 27) |
| `core/settings.py` | Adicionar fila `enrich_trf5` ao `RQ_QUEUES` | **Modificar** (após linha 180) |
| `docker-compose-workers.yml` | Adicionar serviço `worker_trf5` (replicas baixas pra ramp-up) | **Modificar** (após `worker_trf3:` em ~30) |
| `.ia/ENRICHMENT.md` | Adicionar TRF5 na tabela "Estado atual" | **Modificar** |
| `.ia/OVERVIEW.md` | Marcar TRF5 como **Ativo** | **Modificar** (linha 33) |
| (sem migration) | TRF5 já está seedado em `0002_seed_tribunais` — só flip `ativo` em runtime | — |

---

## Pré-requisitos operacionais

- Acesso SSH a `ubuntu@192.168.1.32` (web/scheduler) e `ubuntu@192.168.1.36` (workers).
- Branch limpa em `~/projetos/voyager` (dev) e `~/voyager` (prod).
- Acesso ao admin do Voyager pra inspeção visual (`/admin/tribunals/tribunal/`).

---

## Task 1: Validar PJe consulta pública do TRF5

**Por que primeiro:** Toda a hipótese de "TRF5 cabe no `BasePjeEnricher` sem mudanças" depende da URL existir, do form `fPP` estar presente e do path do detalhe ser o que prevemos. Se 404 ou form diferente, o plano muda (vira "ramp B": custom enricher).

**Files:** Nenhum — só inspeção HTTP.

- [ ] **Step 1.1: Hit a página de busca pública pelo proxy Cortex (mesma rota que workers usam em prod)**

Run:
```bash
docker compose -f /home/will/projetos/voyager/docker-compose.yml exec -T web \
  curl -sS --max-time 30 \
  -x http://cortex-http.was.dev.br:44383 \
  'https://pje1g.trf5.jus.br/consultapublica/ConsultaPublica/listView.seam' \
  -o /tmp/trf5_list.html -w "status=%{http_code} bytes=%{size_download}\n"
```

Expected: `status=200 bytes>10000`.

- [ ] **Step 1.2: Confirmar presença do form `fPP` + ViewState + script `executarPesquisaReCaptcha`**

Run:
```bash
docker compose -f /home/will/projetos/voyager/docker-compose.yml exec -T web sh -c "
grep -c 'id=\"fPP\"' /tmp/trf5_list.html
grep -c 'javax.faces.ViewState' /tmp/trf5_list.html
grep -c 'executarPesquisaReCaptcha' /tmp/trf5_list.html
"
```

Expected: 3 contagens, todas ≥ 1.

- [ ] **Step 1.3: Confirmar que o path do detalhe é `/consultapublica/ConsultaPublica/DetalheProcessoConsultaPublica`**

Pegar um CNJ TRF5 real (qualquer um — ex: do falcon/data). Se nenhum à mão, usar o admin do PJe TRF5 manualmente no browser e copiar um número. Documentar o CNJ aqui antes de prosseguir.

`CNJ_TRF5_TESTE = <preencher>`

Rodar:
```bash
# substituir <CNJ> pelo número escolhido
docker compose -f /home/will/projetos/voyager/docker-compose.yml exec -T web \
  python -c "
import os, django; os.environ.setdefault('DJANGO_SETTINGS_MODULE','core.settings'); django.setup()
from enrichers.pje import BasePjeEnricher

class _Trf5Probe(BasePjeEnricher):
    BASE_URL = 'https://pje1g.trf5.jus.br'
    LIST_URL = f'{BASE_URL}/consultapublica/ConsultaPublica/listView.seam'
    DETALHE_PATH = '/consultapublica/ConsultaPublica/DetalheProcessoConsultaPublica'
    TRIBUNAL_SIGLA = 'TRF5'

p = _Trf5Probe()
link = p._buscar_processo('<CNJ>')
print('LINK:', link)
"
```

Expected: imprime URL começando com `https://pje1g.trf5.jus.br/consultapublica/ConsultaPublica/DetalheProcessoConsultaPublica/...`.

**Branch decisivo:** se 404, se o path for `/pje/...` (estilo TRF3), ou se o form for diferente:
- Path `/pje/ConsultaPublica/...` → ajustar `DETALHE_PATH` em Task 3 (mantém o resto)
- Form ausente / 404 → consulta pública não existe; este plano não se aplica e o trabalho passa a ser "investigar fonte alternativa" (escopo separado).

- [ ] **Step 1.4: Documentar o resultado**

Anotar neste arquivo abaixo desta linha as URLs confirmadas (caso difiram da hipótese):
```
BASE_URL confirmado: ___
LIST_URL confirmado: ___
DETALHE_PATH confirmado: ___
```

---

## Task 2: Validar que DJEN aceita siglaTribunal=TRF5

**Files:** Nenhum — inspeção HTTP.

- [ ] **Step 2.1: Hit DJEN com sigla TRF5 num intervalo curto**

Run:
```bash
docker compose -f /home/will/projetos/voyager/docker-compose.yml exec -T web \
  curl -sS --max-time 30 \
  -x http://cortex-http.was.dev.br:44383 \
  'https://comunicaapi.pje.jus.br/api/v1/comunicacao?siglaTribunal=TRF5&pagina=1&itensPorPagina=10&dataDisponibilizacaoInicio=2026-05-15&dataDisponibilizacaoFim=2026-05-19' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('count=',d.get('count'), 'items=',len(d.get('items',[])))"
```

Expected: `count` > 0 e `items` > 0. Se `count=0` para 5 dias úteis recentes, algo está errado — pode ser que a DJEN use sigla diferente (improvável; o seed já assume `sigla_djen='TRF5'`).

- [ ] **Step 2.2: Inspecionar um item pra checar drift de schema**

Run (mesmo comando, paginando o primeiro item):
```bash
docker compose -f /home/will/projetos/voyager/docker-compose.yml exec -T web \
  curl -sS --max-time 30 \
  -x http://cortex-http.was.dev.br:44383 \
  'https://comunicaapi.pje.jus.br/api/v1/comunicacao?siglaTribunal=TRF5&pagina=1&itensPorPagina=1&dataDisponibilizacaoInicio=2026-05-15&dataDisponibilizacaoFim=2026-05-19' \
  | python3 -m json.tool | head -60
```

Expected: estrutura familiar (`numeroprocessocomum`, `texto`, `dataDisponibilizacao`, `siglaTribunal='TRF5'`, etc.). Se houver chave nova vs. `djen/parser.py::EXPECTED_KEYS`, será capturado em runtime como `SchemaDriftAlert` (não-bloqueante).

---

## Task 3: Criar o enricher `enrichers/trf5.py`

**Files:**
- Create: `/home/will/projetos/voyager/enrichers/trf5.py`

- [ ] **Step 3.1: Escrever o arquivo**

```python
"""Enricher do TRF5 via PJe consulta pública (sem login).

Endpoint: https://pje1g.trf5.jus.br/consultapublica/ConsultaPublica/...

Abrangência: AL, CE, PB, PE, RN, SE.

Tribunal usa PJe padrão CNJ — toda a lógica de form/parsing/dedupe está em
`BasePjeEnricher`. Só configuramos URLs aqui.
"""
from .pje import BasePjeEnricher


class Trf5Enricher(BasePjeEnricher):
    BASE_URL = 'https://pje1g.trf5.jus.br'
    LIST_URL = f'{BASE_URL}/consultapublica/ConsultaPublica/listView.seam'
    DETALHE_PATH = '/consultapublica/ConsultaPublica/DetalheProcessoConsultaPublica'
    TRIBUNAL_SIGLA = 'TRF5'
    LOG_NAME = 'voyager.enrichers.trf5'
```

⚠️ Se o Task 1.3 ou 1.4 indicou path diferente (ex: `/pje/...` estilo TRF3),
substituir `DETALHE_PATH` e ajustar `LIST_URL` conforme o caminho real.

- [ ] **Step 3.2: Smoke-test inline (sem fila, sem DB)**

Run (escolha um CNJ TRF5 que você sabe que existe, ex: o do Task 1.3):
```bash
docker compose -f /home/will/projetos/voyager/docker-compose.yml exec -T web \
  python -c "
import os, django; os.environ.setdefault('DJANGO_SETTINGS_MODULE','core.settings'); django.setup()
from enrichers.trf5 import Trf5Enricher
e = Trf5Enricher()
link = e._buscar_processo('<CNJ_TRF5>')
print('detalhe URL:', link)
import requests
from bs4 import BeautifulSoup
soup = e._fetch_detalhe(link)
print('classe:', e._extrair_dados(soup).get('classe'))
partes = e._extrair_partes(soup)
print('partes ativo/passivo/outros:', len(partes['ativo']), len(partes['passivo']), len(partes['outros']))
"
```

Expected: imprime URL válida, classe não-vazia, e ao menos 1 parte ativa + 1 passiva.

- [ ] **Step 3.3: Commit**

```bash
cd /home/will/projetos/voyager
git add enrichers/trf5.py
git commit -m "feat(enrichers): adiciona Trf5Enricher (PJe consulta pública)"
```

---

## Task 4: Registrar TRF5 em jobs, ingestion e settings

**Files:**
- Modify: `enrichers/jobs.py`
- Modify: `djen/ingestion.py`
- Modify: `core/settings.py`

- [ ] **Step 4.1: Importar e registrar em `enrichers/jobs.py`**

Edit linha 8-19 — substituir o bloco existente:
```python
from .tjmg import TjmgEnricher
from .trf1 import Trf1Enricher
from .trf3 import Trf3Enricher

logger = logging.getLogger('voyager.enrichers.jobs')


_ENRICHERS = {
    'TRF1': Trf1Enricher,
    'TRF3': Trf3Enricher,
    'TJMG': TjmgEnricher,
}
```

Por:
```python
from .tjmg import TjmgEnricher
from .trf1 import Trf1Enricher
from .trf3 import Trf3Enricher
from .trf5 import Trf5Enricher

logger = logging.getLogger('voyager.enrichers.jobs')


_ENRICHERS = {
    'TRF1': Trf1Enricher,
    'TRF3': Trf3Enricher,
    'TRF5': Trf5Enricher,
    'TJMG': TjmgEnricher,
}
```

- [ ] **Step 4.2: Habilitar auto-enqueue em `djen/ingestion.py`**

Edit linha 27 — substituir:
```python
TRIBUNAIS_COM_ENRICHER = {'TRF1', 'TRF3', 'TJMG'}
```

Por:
```python
TRIBUNAIS_COM_ENRICHER = {'TRF1', 'TRF3', 'TRF5', 'TJMG'}
```

- [ ] **Step 4.3: Adicionar fila em `core/settings.py`**

Edit logo após linha 180 (entrada `enrich_trf3`). Localizar:
```python
    'enrich_trf1':     {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 600,   **_RQ_CONN},
    'enrich_trf3':     {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 600,   **_RQ_CONN},
```

Substituir por (acrescentando a linha do TRF5 mantendo o mesmo alinhamento):
```python
    'enrich_trf1':     {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 600,   **_RQ_CONN},
    'enrich_trf3':     {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 600,   **_RQ_CONN},
    'enrich_trf5':     {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 600,   **_RQ_CONN},
```

- [ ] **Step 4.4: Verificar que o módulo carrega (catch erro de typo)**

Run:
```bash
docker compose -f /home/will/projetos/voyager/docker-compose.yml exec -T web \
  python -c "
import os, django; os.environ.setdefault('DJANGO_SETTINGS_MODULE','core.settings'); django.setup()
from enrichers.jobs import _ENRICHERS, queue_for
from djen.ingestion import TRIBUNAIS_COM_ENRICHER
from django.conf import settings
assert 'TRF5' in _ENRICHERS, _ENRICHERS
assert 'TRF5' in TRIBUNAIS_COM_ENRICHER, TRIBUNAIS_COM_ENRICHER
assert 'enrich_trf5' in settings.RQ_QUEUES, list(settings.RQ_QUEUES)
print('OK')
"
```

Expected: `OK`.

- [ ] **Step 4.5: Commit**

```bash
cd /home/will/projetos/voyager
git add enrichers/jobs.py djen/ingestion.py core/settings.py
git commit -m "feat(enrichers): registra TRF5 em _ENRICHERS, TRIBUNAIS_COM_ENRICHER e RQ_QUEUES"
```

---

## Task 5: Adicionar `worker_trf5` ao docker-compose-workers.yml

**Files:**
- Modify: `docker-compose-workers.yml`

Volume inicial baixo (10 replicas) durante backfill DJEN — o pool só será útil
depois que a ingestão criar Process. Vamos escalar pra 60-120 quando o backfill
estiver perto de concluir (Task 8).

- [ ] **Step 5.1: Adicionar o serviço logo após `worker_trf3:`**

Localizar bloco `worker_trf3:` (perto da linha 43-54) e adicionar abaixo dele:
```yaml
  worker_trf5:
    image: voyager-web:prod
    command: python manage.py rqworker enrich_trf5
    env_file: .env
    volumes:
      - .:/app
    deploy:
      # Ramp-up: começa em 10. Escalar pra 60-120 quando o backfill DJEN
      # estiver drenando Process suficiente. Bottleneck upstream é PJe TRF5.
      replicas: 10
    restart: unless-stopped
```

- [ ] **Step 5.2: Validar sintaxe YAML**

Run:
```bash
cd /home/will/projetos/voyager
docker compose -f docker-compose-workers.yml config > /dev/null && echo OK
```

Expected: `OK` (sem erro de sintaxe).

- [ ] **Step 5.3: Commit**

```bash
git add docker-compose-workers.yml
git commit -m "feat(workers): adiciona worker_trf5 (10 replicas ramp-up)"
```

---

## Task 6: Deploy em prod (etapa 1 — código)

**Files:** Nenhum — só infra.

`web` precisa de rebuild pra carregar `enrichers/trf5.py` e o novo `RQ_QUEUES`.
`scheduler` idem (vai re-registrar daily cron na próxima criação). Workers
auxiliares no `.36` precisam puxar git + recriar pra subir `worker_trf5`.

- [ ] **Step 6.1: Push branch / merge pra `main`**

```bash
cd /home/will/projetos/voyager
git push origin <branch>
# após merge na main:
```

- [ ] **Step 6.2: Deploy no host web `.32` (web + scheduler — rebuild)**

```bash
ssh ubuntu@192.168.1.32 "cd ~/voyager && git pull --ff-only && \
  docker compose -f docker-compose-prod.yml build web && \
  docker compose -f docker-compose-prod.yml up -d --force-recreate web scheduler"
```

Acompanhar logs até `web` healthy:
```bash
ssh ubuntu@192.168.1.32 "docker compose -f ~/voyager/docker-compose-prod.yml logs -f --tail=50 web"
```

Expected: `web` sobe, `migrate` roda (sem migrations novas → no-op), `collectstatic` ok.

- [ ] **Step 6.3: Deploy no host workers `.36`**

```bash
ssh ubuntu@192.168.1.36 "cd ~/voyager && git pull --ff-only && \
  docker compose -f docker-compose-workers.yml up -d worker_trf5 \
                                                  worker_ingestion worker_default \
                                                  worker_trf1 worker_trf3 worker_tjmg \
                                                  worker_datajud worker_classificacao \
                                                  worker_djen_audit"
```

`up -d` é idempotente — só recria o que mudou de imagem/config. `worker_trf5`
é novo → será criado com 10 replicas.

Verificar:
```bash
ssh ubuntu@192.168.1.36 "docker compose -f ~/voyager/docker-compose-workers.yml ps worker_trf5"
```

Expected: 10 containers `worker_trf5-N`, todos `Up`.

---

## Task 7: Ativar TRF5 e disparar backfill

**Files:** Nenhum — comandos em prod.

- [ ] **Step 7.1: Ativar o `Tribunal(sigla='TRF5')` (já seedado, só flipa `ativo`)**

```bash
ssh ubuntu@192.168.1.32 "docker compose -f ~/voyager/docker-compose-prod.yml exec -T web \
  python manage.py shell -c \"
from tribunals.models import Tribunal
t = Tribunal.objects.get(sigla='TRF5')
print('antes:', t.ativo, t.data_inicio_disponivel, t.backfill_concluido_em)
Tribunal.objects.filter(sigla='TRF5').update(ativo=True)
print('OK, ativado')\""
```

Expected: imprime estado antes (ativo=False) e `OK`. **Não setar** `backfill_concluido_em` — deixar None pro flow normal de backfill rodar.

- [ ] **Step 7.2: Descobrir `data_inicio_disponivel`**

Esse command faz binary search na DJEN pra achar a data mais antiga em que TRF5
tem comunicações:

```bash
ssh ubuntu@192.168.1.32 "docker compose -f ~/voyager/docker-compose-prod.yml exec -T web \
  python manage.py djen_descobrir_inicio TRF5"
```

Expected: termina imprimindo algo como `TRF5: data_inicio_disponivel = 2020-XX-XX` e persiste no `Tribunal`. Pode demorar 1-3 min.

- [ ] **Step 7.3: Reiniciar scheduler pra registrar daily cron do TRF5**

`create_scheduler()` lê `Tribunal.objects.filter(ativo=True)` no startup —
precisamos refazer pra ele pegar TRF5.

```bash
ssh ubuntu@192.168.1.32 "docker compose -f ~/voyager/docker-compose-prod.yml restart scheduler"
```

Verificar no log que o cron foi agendado:
```bash
ssh ubuntu@192.168.1.32 "docker compose -f ~/voyager/docker-compose-prod.yml logs --tail=60 scheduler | grep -i 'daily_ingestion.*TRF5\\|tick_backfill.*TRF5'"
```

Expected: 2 linhas — `agendado daily_ingestion TRF5 04:XX:XX` (`idx` aloca 04:XX automaticamente, fora de EARLY) e `agendado tick_backfill TRF5 (cada 10min)`.

- [ ] **Step 7.4: Disparar backfill manual**

`tick_backfill_retroativo` rodaria de qualquer forma a cada 10min, mas vamos
adiantar — manda 1 chunk imediato pra confirmar que o pipeline funciona:

```bash
ssh ubuntu@192.168.1.32 "docker compose -f ~/voyager/docker-compose-prod.yml exec -T web \
  python manage.py djen_backfill TRF5"
```

Acompanhar:
```bash
watch -n 30 "ssh ubuntu@192.168.1.32 'docker compose -f ~/voyager/docker-compose-prod.yml exec -T web python manage.py djen_status'"
```

Expected: depois de ~5 min, `djen_status` mostra TRF5 com `IngestionRun` count crescendo, `success`/`failed` discriminados.

---

## Task 8: Validar enriquecimento end-to-end

**Files:** Nenhum — verificação operacional.

Critério de sucesso: 1 `Process` real do TRF5 enriquecido com `classe`, `assunto`,
`orgao_julgador` populados e ao menos 1 `ProcessoParte` criado.

- [ ] **Step 8.1: Esperar que o backfill tenha criado ao menos algumas centenas de Process**

```bash
ssh ubuntu@192.168.1.32 "docker compose -f ~/voyager/docker-compose-prod.yml exec -T web \
  python manage.py shell -c \"
from tribunals.models import Process
print('TRF5 processes:', Process.objects.filter(tribunal_id='TRF5').count())
print('TRF5 pendentes enriq:', Process.objects.filter(tribunal_id='TRF5', enriquecimento_status='pendente').count())\""
```

Aguardar até `processes > 100`. Como TRF5 é tribunal médio, deve levar 5-15 min
após Task 7.4 começar a drenar.

- [ ] **Step 8.2: Forçar enriquecimento manual de 1 processo (foreground, ver erros)**

```bash
ssh ubuntu@192.168.1.32 "docker compose -f ~/voyager/docker-compose-prod.yml exec -T web \
  python manage.py shell -c \"
from tribunals.models import Process
p = Process.objects.filter(tribunal_id='TRF5', enriquecimento_status='pendente').first()
print('escolhido:', p.numero_cnj, 'pk=', p.pk)\""

# pegar o CNJ acima e rodar:
ssh ubuntu@192.168.1.32 "docker compose -f ~/voyager/docker-compose-prod.yml exec -T web \
  python manage.py enriquecer_processo <CNJ_TRF5>"
```

Expected: imprime `status=ok`, `classe_raw=...`, `partes_total>=1`. Sem traceback.

- [ ] **Step 8.3: Verificar persistência no DB (precisa drainer ter consumido o stream)**

Aguardar ~1 min pro drainer drenar (rodando no `.32`):
```bash
ssh ubuntu@192.168.1.32 "docker compose -f ~/voyager/docker-compose-prod.yml exec -T web \
  python manage.py shell -c \"
from tribunals.models import Process, ProcessoParte
p = Process.objects.get(numero_cnj='<CNJ_TRF5>')
print('status:', p.enriquecimento_status, 'enriquecido_em:', p.enriquecido_em)
print('classe:', p.classe_id, 'orgao:', p.orgao_julgador, 'valor:', p.valor_causa)
print('partes:', ProcessoParte.objects.filter(processo=p).count())\""
```

Expected: `status=ok`, `enriquecido_em` recente, `classe_id` não-nulo, ao menos 1 parte.

- [ ] **Step 8.4: Drenar fila `enrich_trf5` em batch (auto-refill)**

`reabastecer_filas_enriquecimento` (cron de 2min) já deve estar enfileirando.
Verificar:
```bash
ssh ubuntu@192.168.1.32 "docker compose -f ~/voyager/docker-compose-prod.yml exec -T web \
  python manage.py shell -c \"
import django_rq
q = django_rq.get_queue('enrich_trf5')
print('enrich_trf5 pending:', len(q), 'failed:', q.failed_job_registry.count)\""
```

Expected: `pending` > 0 (refill operando), `failed` baixo (< 5% do pending).

---

## Task 9: Scale-up dos workers TRF5

**Files:**
- Modify: `docker-compose-workers.yml` (replicas)

Após confirmar que o enriquecimento está rodando sem erros sistemáticos,
escalar pra ~60 (estimativa conservadora — TRF1 está em 40, TRF3 em 160 por
ser o tribunal de maior volume; TRF5 é médio).

- [ ] **Step 9.1: Verificar RAM disponível em `.36`**

```bash
ssh ubuntu@192.168.1.36 "free -h && docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}' | head -20"
```

Expected: ao menos 8GB free. Se < 4GB, esperar — não escalar.

- [ ] **Step 9.2: Aumentar `replicas: 10` → `replicas: 60` no compose**

Edit `docker-compose-workers.yml`, no bloco `worker_trf5`:
```yaml
    deploy:
      # Ramp 10 → 60 após validar enriquecimento ok.
      replicas: 60
```

- [ ] **Step 9.3: Aplicar em prod**

```bash
ssh ubuntu@192.168.1.36 "cd ~/voyager && git pull --ff-only && \
  docker compose -f docker-compose-workers.yml up -d --scale worker_trf5=60 worker_trf5"
```

Verificar:
```bash
ssh ubuntu@192.168.1.36 "docker compose -f ~/voyager/docker-compose-workers.yml ps worker_trf5 | wc -l"
```

Expected: 61 (60 workers + header).

- [ ] **Step 9.4: Monitorar 30 min — RAM, fila drenando, sem aumento de failed**

```bash
ssh ubuntu@192.168.1.36 'free -h'
ssh ubuntu@192.168.1.32 "docker compose -f ~/voyager/docker-compose-prod.yml exec -T web \
  python manage.py shell -c \"
import django_rq
q = django_rq.get_queue('enrich_trf5')
print('pending:', len(q), 'failed:', q.failed_job_registry.count)\""
```

- [ ] **Step 9.5: Commit**

```bash
cd /home/will/projetos/voyager
git add docker-compose-workers.yml
git commit -m "ops(workers): worker_trf5 10 → 60 replicas pós-validação"
git push
```

---

## Task 10: Atualizar documentação `.ia/`

**Files:**
- Modify: `.ia/ENRICHMENT.md` (tabela "Estado atual" linha 8-14)
- Modify: `.ia/OVERVIEW.md` (linha 33 — tabela "Tribunais cobertos")
- Modify: `.ia/OPS.md` (workers do `.36` — adicionar `worker_trf5` na lista linha 60-66)

- [ ] **Step 10.1: `.ia/ENRICHMENT.md` — mover TRF5 da linha "TRF2/5/6 Não" pra "Sim"**

Localizar:
```
| TRF3 | PJe consulta pública (sem login) | **Sim** | `enrichers/trf3.py` (subclasse) |
| TRF2/5/6 | PJe (versões variadas) | Não | Mesmo motor JSF — adicionar subclasse |
```

Substituir por:
```
| TRF3 | PJe consulta pública (sem login) | **Sim** | `enrichers/trf3.py` (subclasse) |
| TRF5 | PJe consulta pública (sem login) | **Sim** | `enrichers/trf5.py` (subclasse) |
| TRF2/6 | PJe (versões variadas) | Não | Mesmo motor JSF — adicionar subclasse |
```

- [ ] **Step 10.2: `.ia/OVERVIEW.md` linha 33 — marcar TRF5 como Ativo**

Localizar:
```
| TRF2/4/5/6 | TRFs 2/4/5/6 | Cadastrados, inativos |
```

Substituir por:
```
| TRF5 | Tribunal Regional Federal da 5ª Região | **Ativo** |
| TRF2/4/6 | TRFs 2/4/6 | Cadastrados, inativos |
```

- [ ] **Step 10.3: `.ia/OPS.md` — adicionar `worker_trf5` na lista de containers do `.36`**

Localizar bloco "**`.36` (host workers consolidado)** via `docker-compose-workers.yml`":
```
worker_trf1          120   fila 'enrich_trf1'   (scale-up 2026-05-14)
worker_trf3          120   fila 'enrich_trf3'   (scale-up 2026-05-14)
worker_tjmg          120   fila 'enrich_tjmg'   (scale-up 2026-05-14)
```

Substituir por (acrescentando worker_trf5 com replicas atual):
```
worker_trf1          120   fila 'enrich_trf1'   (scale-up 2026-05-14)
worker_trf3          120   fila 'enrich_trf3'   (scale-up 2026-05-14)
worker_trf5           60   fila 'enrich_trf5'   (ramp 2026-05-20)
worker_tjmg          120   fila 'enrich_tjmg'   (scale-up 2026-05-14)
```

E atualizar a contagem total ("Total observado pós-resize+scale 2026-05-14 23:25: ~404 containers vivos.") — substituir por nota datada:
```
Total observado pós-scale TRF5 2026-05-20: ~460 containers vivos.
```

- [ ] **Step 10.4: Commit final**

```bash
cd /home/will/projetos/voyager
git add .ia/ENRICHMENT.md .ia/OVERVIEW.md .ia/OPS.md
git commit -m "docs(.ia): marca TRF5 como ativo + worker_trf5 nos runbooks"
git push
```

---

## Critério de conclusão

Plano concluído quando **todos** os abaixo forem verdadeiros:

1. `docker compose ps worker_trf5` mostra 60 containers `Up` no `.36`.
2. `djen_status` mostra TRF5 com `IngestionRun` em curso/concluído e backfill progredindo.
3. `Process.objects.filter(tribunal_id='TRF5', enriquecimento_status='ok').count() > 1000`.
4. Fila `enrich_trf5`: `failed/total < 5%` após 1h de drainage.
5. `.ia/OVERVIEW.md` mostra TRF5 como Ativo.

## Riscos identificados e mitigação

| Risco | Probabilidade | Mitigação |
|---|---|---|
| Path do detalhe é `/pje/...` (TRF3-style), não `/consultapublica/...` | Média | Task 1 valida antes de codar — só ajusta constante |
| TRF5 PJe usa captcha real (não flag desabilitado como TRF1/3) | Baixa | Task 1.2 detecta marcador captcha; mitigação: implementar bypass específico ou pular tribunal |
| DJEN devolve schema diferente pra TRF5 | Baixa | `SchemaDriftAlert` captura automaticamente; resolver no runbook OPS.md "Schema drift" |
| Backfill puxa volume inesperado (>30GB) | Média | `data_inicio_disponivel` tipicamente 2020+ → estimativa 6-10GB; monitorar disco do `.28` |
| Workers TRF5 sufocam RAM do `.36` (que já está a 90% pós-scale TRF3) | Média | Step 9.1 valida free RAM antes de escalar; se apertar, manter em 30 replicas |
| Datajud não tem index `api_publica_trf5` | Baixa | Validar com curl em `https://api-publica.datajud.cnj.jus.br/api_publica_trf5/...`; sem isso, classificação ML aplica modelo TRF1 sem ground truth TRF5 (caveat conhecido em OPS.md "Adicionar tribunal novo") |
