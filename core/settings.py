from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env()
environ.Env.read_env(BASE_DIR / '.env')

SECRET_KEY = env('DJANGO_SECRET_KEY', default='unsafe-dev-key-change-me')
DEBUG = env.bool('DJANGO_DEBUG', default=False)
ALLOWED_HOSTS = env.list('DJANGO_ALLOWED_HOSTS', default=['*'] if DEBUG else [])

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.postgres',

    'rest_framework',
    'rest_framework_api_key',
    'django_filters',
    'drf_spectacular',
    'django_rq',
    'django_prometheus',

    'core',
    'tribunals',
    'djen',
    'datajud',
    'enrichers',
    'api',
    'dashboard',
    'accounts',
    # Jusbrasil-compat (Fase A) — busca ES, PDFs no MinIO, monitoramento, MCP.
    'search',
    'pdf_storage',
    'monitoring',
    'mcp_server',
    # Diários oficiais além do DJEN (DJE/TJSP, DEJT, STF, DOEs de entes).
    # Contrato + runner compartilhados em `diarios/base.py`; cada fonte vive
    # em `diarios/fontes/<slug>/`.
    'diarios',
    # Diários oficiais de ENTES DEVEDORES (Executivo estadual/municipal).
    # App separado porque tem model próprio: publicação do Executivo não tem
    # tribunal, e `Movimentacao.tribunal` é FK NOT NULL.
    'diarios_entes',
]

MIDDLEWARE = [
    'django_prometheus.middleware.PrometheusBeforeMiddleware',
    'core.middleware.RequestIdMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'django_prometheus.middleware.PrometheusAfterMiddleware',
]

ROOT_URLCONF = 'core.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'dashboard' / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'core.wsgi.application'

DATABASES = {'default': env.db('DATABASE_URL', default='postgres://voyager:voyager@postgres:5432/voyager')}
DATABASES['default']['ENGINE'] = 'django_prometheus.db.backends.postgresql'
# pgbouncer transaction-mode: cursors server-side e prepared statements quebram
# (a conexão pode pular pra outro backend entre cursor.fetch). Django 4.2+
# expõe estes flags em OPTIONS — mais seguro que CONN_MAX_AGE=0 isolado.
DATABASES['default'].setdefault('OPTIONS', {}).update({
    'server_side_binding': False,
})
DATABASES['default']['DISABLE_SERVER_SIDE_CURSORS'] = True
DATABASES['default']['CONN_MAX_AGE'] = 0

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 10}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
]

LANGUAGE_CODE = 'pt-br'
TIME_ZONE = env('DJANGO_TIME_ZONE', default='America/Sao_Paulo')
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static'] if (BASE_DIR / 'static').exists() else []

# WhiteNoise: em DEBUG serve via finders (sem manifest, autoreload);
# em prod, gera staticfiles.json com hashes (cache busting estrutural).
#
# IMPORTANTE: Django 5 ignora `STATICFILES_STORAGE` legacy em favor de `STORAGES`.
# Sem essa config, `collectstatic` não gera manifest, `{% static %}` retorna
# URL sem hash, e o nginx serve com `Cache-Control: immutable max-age=30d`
# fazendo browsers nunca pegarem atualizações de CSS/JS sem bust manual.
# Ver ADR-023 em .ia/DECISIONS.md.
WHITENOISE_USE_FINDERS = DEBUG
WHITENOISE_AUTOREFRESH = DEBUG
STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': (
            'django.contrib.staticfiles.storage.StaticFilesStorage' if DEBUG
            else 'whitenoise.storage.CompressedManifestStaticFilesStorage'
        ),
    },
    'pdfs': {
        'BACKEND': 'storages.backends.s3.S3Storage',
    },
}
# Tolera entries faltando no manifest — evita ValueError em runtime se algum
# template referenciar arquivo que não foi coletado. Em vez de explodir,
# Whitenoise serve o nome original. Útil pra resolver chicken-and-egg de
# `static_url()` chamado em import-time de URLConf (core/urls.py favicon).
WHITENOISE_MANIFEST_STRICT = False

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = '/dashboard/login/'
LOGIN_REDIRECT_URL = '/dashboard/'
LOGOUT_REDIRECT_URL = '/dashboard/login/'

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [],
    'DEFAULT_PERMISSION_CLASSES': ['rest_framework_api_key.permissions.HasAPIKey'],
    'DEFAULT_PAGINATION_CLASS': 'api.pagination.DefaultPagination',
    'PAGE_SIZE': 50,
    'DEFAULT_FILTER_BACKENDS': ['django_filters.rest_framework.DjangoFilterBackend'],
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
}

SPECTACULAR_SETTINGS = {
    'TITLE': 'Voyager API',
    'DESCRIPTION': 'API para consulta de movimentações DJEN por tribunal.',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
}

REDIS_URL = env('REDIS_URL', default='redis://redis:6379/0')

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': REDIS_URL,
        'KEY_PREFIX': 'v',
        'TIMEOUT': 3600,
        'OPTIONS': {
            'socket_connect_timeout': 5,
            'socket_timeout': 30,
            'retry_on_timeout': True,
        },
    }
}

# Kwargs compartilhados por todas as filas RQ — timeouts de socket para
# workers não bloquearem forever em ops Redis que não sejam BLPOP.
_RQ_CONN = {
    'socket_connect_timeout': 2,
    'socket_timeout': 10,
    'retry_on_timeout': True,
}

RQ_QUEUES = {
    'default':         {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 3600,  **_RQ_CONN},
    'djen_ingestion':  {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 7200,  **_RQ_CONN},
    'djen_backfill':   {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 86400, **_RQ_CONN},
    'djen_audit':      {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 3600,  **_RQ_CONN},
    # Enriquecimento por tribunal — workers dedicados por sigla pra
    # paralelizar coletas no PJe consulta pública sem misturar pools.
    'enrich_trf1':     {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 600,   **_RQ_CONN},
    'enrich_trf3':     {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 600,   **_RQ_CONN},
    'enrich_trf5':     {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 600,   **_RQ_CONN},
    'enrich_tjmg':     {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 600,   **_RQ_CONN},
    'enrich_tjma':     {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 600,   **_RQ_CONN},
    'enrich_tjsp':     {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 600,   **_RQ_CONN},
    'enrich_tjal':     {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 600,   **_RQ_CONN},
    'enrich_tjdft':    {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 600,   **_RQ_CONN},
    # Adicionados no recon 2026-06-29 (consulta pública aberta, sem captcha/login).
    'enrich_tjce':     {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 600,   **_RQ_CONN},
    'enrich_tjap':     {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 600,   **_RQ_CONN},
    'enrich_tjpe':     {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 600,   **_RQ_CONN},
    'enrich_tjrj':     {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 600,   **_RQ_CONN},
    'enrich_tjro':     {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 600,   **_RQ_CONN},
    'enrich_tjac':     {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 600,   **_RQ_CONN},
    'enrich_tjmt':     {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 600,   **_RQ_CONN},
    'enrich_tjpa':     {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 600,   **_RQ_CONN},
    # Fila prioritária pra requests on-demand do dashboard (botões de
    # 'Atualizar dados públicos' / 'Sincronizar movimentações'). Workers
    # dedicados garantem latência baixa mesmo com filas de backfill cheias.
    'manual':          {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 600,   **_RQ_CONN},
    # Sincronização via API Datajud (CNJ) — 1 request por processo,
    # cobre todos os tribunais. Dedicada pra não competir com DJEN nem
    # com PJe scraping.
    'datajud':         {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 600,   **_RQ_CONN},
    # Classificação de leads (modelo LR v5). reclassificar_recentes pode
    # rodar 500k procs por hora — isolar pra não bloquear default que
    # também tem watchdogs e ticks.
    'classificacao':   {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 14400, **_RQ_CONN},
    # Consumo de leads reportado pelo Falcon — assíncrono e idempotente.
    # Isolado pra não competir com classificação/ingestão; volume em rajada
    # (catch-up de ~268k + reportes diários).
    'leads_consumo':   {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 1800,  **_RQ_CONN},
    # Indexação Elasticsearch — write-through de Movimentacao/Process.
    # Fila dedicada pra não competir com default/ingestion.
    'es_index':       {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 120,  **_RQ_CONN},
    # Download de PDFs da DJEN (Movimentacao.link → MinIO).
    'pdf_download':   {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 600,  **_RQ_CONN},
    # Monitoramento push — varredura diária + webhook delivery.
    'monitoring':     {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 600,  **_RQ_CONN},
    # Varredura do acervo declarado ao CNJ (Datajud → voyager-acervo). Fila
    # SEPARADA da `datajud` de propósito: uma varredura de tribunal grande é um
    # job de horas, e na mesma fila ela empurraria pro fim da linha as
    # sincronizações por processo, que atendem usuário. Timeout longo porque o
    # job é justamente "varre o TJSP inteiro" — retomável pelo watermark.
    'varredura':      {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 86400, **_RQ_CONN},
    # Coleta de diários próprios (DJE/TJSP, DEJT, STF, DOEs). Fila SEPARADA da
    # djen_* de propósito: a unidade aqui é um caderno de até 2.001 páginas /
    # 62 MB, e um job desses na fila do DJEN empurraria a fronteira diária pro
    # fim da linha. Timeout longo porque baixar+segmentar um caderno é minutos.
    'diarios':        {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 7200,  **_RQ_CONN},

}

# DJEN
DJEN_BASE_URL = env('DJEN_BASE_URL', default='https://comunicaapi.pje.jus.br/api/v1/comunicacao')
# Juriscope/Falcon (precatórios) — leitura read-only por CNJ pro dossiê de
# jurimetria. DSN completo (postgres://...); vazio = integração desligada.
JURISCOPE_DB_DSN = env.str('JURISCOPE_DB_DSN', default='')

DJEN_REQUEST_TIMEOUT_CONNECT = env.int('DJEN_REQUEST_TIMEOUT_CONNECT', default=10)
DJEN_REQUEST_TIMEOUT_READ = env.int('DJEN_REQUEST_TIMEOUT_READ', default=60)
DJEN_PAGE_SLEEP_SECONDS = env.float('DJEN_PAGE_SLEEP_SECONDS', default=1.0)
DJEN_MAX_RETRIES = env.int('DJEN_MAX_RETRIES', default=5)
DJEN_USER_AGENT = env('DJEN_USER_AGENT', default='voyager-ingestion/0.1')

# Escotilha do caminho antigo de coleta: fatiar o dia por `ufOab` (27 requisições).
# Padrão OFF desde 18/08/2026 — a paginação flat (`iter_pages`) esgota o dia sem
# teto, e o fatiamento é cego a publicação sem advogado com OAB (2-10% de todo
# dia grande). Ver o comentário de ESTRATEGIA_UF em djen/ingestion.py.
DJEN_ESTRATEGIA_UF = env.bool('DJEN_ESTRATEGIA_UF', default=False)

# Páginas do MESMO dia buscadas em paralelo por `iter_pages`. Serial, um dia de
# TJSP são 262 requisições em fila indiana (163 min medidos). 8 é o mesmo teto
# em voo dos fetchers de UF, e fica muito abaixo do rate-limit de 20/s do CNJ.
DJEN_PAGINAS_PARALELAS = env.int('DJEN_PAGINAS_PARALELAS', default=8)

# Proxies
PROXYSCRAPE_API_KEY = env('PROXYSCRAPE_API_KEY', default='')
# API key alternativa para workers Datajud numa máquina específica.
# Quando definida, DatajudClient usa pool isolada (Redis: voyager:proxies:datajud:*)
# sem interferir na pool padrão das outras máquinas.
DATAJUD_PROXYSCRAPE_API_KEY = env('DATAJUD_PROXYSCRAPE_API_KEY', default='')
# Liga/desliga o enfileiramento de sync Datajud (auto-enqueue na ingestão +
# reabastecer_fila_datajud). Desligado em 2026-07-02: a API pública do CNJ
# ficou com o _search pendurado e a fila explodiu (63M jobs / Redis 39GB de 48).
DATAJUD_ENQUEUE_ENABLED = env.bool('DATAJUD_ENQUEUE_ENABLED', default=True)
# Teto GLOBAL de requisições/min ao Datajud (token-bucket Redis em datajud.ratelimit).
# A APIKey pública é compartilhada e tem rate limit global; <=0 desliga o limite.
DATAJUD_RATE_LIMIT_RPM = env.int('DATAJUD_RATE_LIMIT_RPM', default=100)
# Cota SEPARADA da varredura em massa do acervo (datajud/varredura.py), pega
# ANTES da global. Dimensionamento medido: cada requisição leva ~8s, então uma
# réplica faz ~7,5 req/min — 8 réplicas ≈ 60 rpm. Somado ao consumo real do
# sync por processo (~26 rpm, média de 30 dias), dá 86 dos 100 globais: usa o
# que sobra sem estrangular quem atende usuário. Subir daqui exige subir também
# o teto global, e aí o risco passa a ser a APIKey COMPARTILHADA do CNJ, que já
# nos derrubou uma vez (incidente 2026-07-02).
DATAJUD_VARREDURA_RPM = env.int('DATAJUD_VARREDURA_RPM', default=40)

# ── DIÁRIOS PRÓPRIOS (terceira porta) ─────────────────────────────────────────
# Todas com default no código (`getattr(settings, ..., default)`), então nada
# quebra se faltarem. Ficam aqui para serem AJUSTÁVEIS POR ENV, sem deploy — que
# é o que importa quando o servidor do outro lado começa a sofrer. Ver .ia/DIARIOS.md.
#
# TETO DE CONDUTA: nenhuma dessas fontes tem rate limit, WAF ou robots.txt. O
# servidor NÃO vai nos defender de nós mesmos (o CSJT roda um JBoss de 2010); o
# teto é auto-imposto e é a diferença entre backfill e negação de serviço
# acidental. Os números são os medidos por fonte no recon de 16/08/2026.
DIARIOS_USER_AGENT = env('DIARIOS_USER_AGENT',
                         default='voyager-ops/1.0 (+https://voyager.was.dev.br)')
DIARIOS_TIMEOUT_CONNECT = env.int('DIARIOS_TIMEOUT_CONNECT', default=15)
# 180s: um caderno do DEJT tem 62 MB e um do TJSP, 15 MB.
DIARIOS_TIMEOUT_READ = env.int('DIARIOS_TIMEOUT_READ', default=180)
# Requisições/segundo POR FONTE (lidas por `SessaoDiario` via DIARIOS_RPS_<SLUG>).
DIARIOS_RPS_TJSP_DJE = env.float('DIARIOS_RPS_TJSP_DJE', default=1.0)
DIARIOS_RPS_DEJT = env.float('DIARIOS_RPS_DEJT', default=0.5)
DIARIOS_RPS_STF = env.float('DIARIOS_RPS_STF', default=1.0)
# Portal legado do STF (IIS/ASP) — sessão e breaker próprios: é ele que dita o
# ritmo real da fonte (~590 GETs por dia útil com cache frio).
DIARIOS_RPS_STF_PORTAL = env.float('DIARIOS_RPS_STF_PORTAL', default=0.8)
DIARIOS_RPS_QD_MUNICIPAL = env.float('DIARIOS_RPS_QD_MUNICIPAL', default=1.0)
DIARIOS_RPS_DOE_SP = env.float('DIARIOS_RPS_DOE_SP', default=2.0)
# Circuit-breaker por fonte (mesma mecânica do djen/client.py, que curou o
# incidente 2026-07-10 em que NÓS éramos parte da sobrecarga).
DIARIOS_CIRCUITO_LIMIAR = env.int('DIARIOS_CIRCUITO_LIMIAR', default=15)
DIARIOS_CIRCUITO_JANELA = env.int('DIARIOS_CIRCUITO_JANELA', default=120)
DIARIOS_CIRCUITO_COOLDOWN = env.int('DIARIOS_CIRCUITO_COOLDOWN', default=300)
# Piso do gate de cobertura contra o gabarito da própria fonte. Baixar isto é
# aceitar gravar meia edição — só com medição na mão.
DIARIOS_COBERTURA_MINIMA = env.float('DIARIOS_COBERTURA_MINIMA', default=0.95)
# DEJT: primeira data cujo caderno o segmentador atual sabe ler (migração ao
# PJe). Antes disso a matéria é prosa corrida numerada e a cobertura medida cai
# a 0-72% — o coletor ABSTÉM antes do download. Baixar esta data quando o parser
# da era antiga existir recataloga as ~47 mil edições pré-2018 de uma vez.
DIARIOS_DEJT_SEGMENTAVEL_DESDE = env('DIARIOS_DEJT_SEGMENTAVEL_DESDE', default='2018-01-01')
# DEJT: se um dia precisar de proxy, TEM que ser 'preso' — a sessão é sticky no
# ALB e a conversa Seam de 3 passos morre se o IP mudar no meio.
DIARIOS_DEJT_MODO_PROXY = env('DIARIOS_DEJT_MODO_PROXY', default='direto')
# STF: bundle TLS com o intermediário AlphaSSL 2025 + raiz GlobalSign R6 (a
# máquina não tem a raiz e o `verify` padrão falha). O intermediário expira em
# 21/05/2027 — nesse dia o coletor falha ALTO (SSLError), e a troca é por env,
# sem deploy. Vazio = usa o bundle embarcado em diarios/fontes/stf/ca_stf.pem.
DIARIOS_STF_CA_BUNDLE = env('DIARIOS_STF_CA_BUNDLE', default='')
# Liga o agendamento automático (tick + catalogar_fronteira) no scheduler.
# DESLIGADO por padrão de propósito: o backfill destas fontes é da ordem de
# centenas de milhões de linhas contra um Postgres já disk-I/O-bound, e quem
# decide começar é gente, não deploy. Ver runbook em .ia/DIARIOS.md.
DIARIOS_SCHEDULER_ENABLED = env.bool('DIARIOS_SCHEDULER_ENABLED', default=False)
# Fontes que o scheduler pode tocar quando ligado (vazio = todas as registradas).
# É o recorte ANTES do kill switch: serve para ligar uma fonte por vez.
DIARIOS_FONTES_AGENDADAS = env.list('DIARIOS_FONTES_AGENDADAS', default=[])
PROXYSCRAPE_REFRESH_SECONDS = env.int('PROXYSCRAPE_REFRESH_SECONDS', default=900)
CORTEX_PROXY_URL = env('CORTEX_PROXY_URL', default='')
CORTEX_FALLBACK_ENABLED = env.bool('CORTEX_FALLBACK_ENABLED', default=True)
# CapSolver — resolução de CAPTCHA p/ consultas públicas gated (Turnstile,
# imagem, reCAPTCHA, hCaptcha). Ver enrichers/captcha.py.
CAPSOLVER_API_KEY = env('CAPSOLVER_API_KEY', default='')
# IPs datacenter reciclam rápido — 10 min de quarentena queimava o pool
# inteiro durante ondas de WAF (1490/1500 bad observados). 2 min permite
# rotação saudável sem voltar imediatamente pro mesmo IP queimado.
PROXY_BAD_TTL_SECONDS = env.int('PROXY_BAD_TTL_SECONDS', default=120)
# Cooldown do Cortex residencial. Curto porque o gateway tem rotação
# interna — basta um momento pro próximo IP ser saudável.
CORTEX_BAD_TTL_SECONDS = env.int('CORTEX_BAD_TTL_SECONDS', default=15)
# Probabilidade de cada request DJEN sair via Cortex (residencial) em vez do
# pool ProxyScrape (datacenter). Diversifica IPs por request — quando o WAF
# bloqueia datacenter em onda, ainda passa metade via Cortex e vice-versa.
# Cortex só explícito (prefer_cortex) ou fallback; ProxyScrape é o primário (mais banda).
DJEN_CORTEX_RATIO = env.float('DJEN_CORTEX_RATIO', default=0.0)
# Ratio quando o pool datacenter está DEGRADADO (queimado/429): sem sentido
# apostar no datacenter morto → 100% Cortex por padrão (2026-07-06).
DJEN_CORTEX_RATIO_DEGRADED = env.float('DJEN_CORTEX_RATIO_DEGRADED', default=1.0)
# Ordem dos proxies no enriquecimento em massa. DATACENTER-FIRST (default False):
# o pool ProxyScrape é barato/abundante e resolve a maioria dos tribunais; o
# Cortex (residencial) fica como FALLBACK, acionado só quando o pool falha —
# poupa a banda/cota do Cortex e não paga o custo dele em cada job. Tribunais com
# WAF que exigem residencial caem no Cortex via _next_proxy quando o pool é
# bloqueado. True volta a Cortex-first (2026-07-12: invertido a pedido).
ENRICH_PREFER_CORTEX = env.bool('ENRICH_PREFER_CORTEX', default=False)
# Seguir incidentes no e-SAJ (cada parte tem um incidente/precatório). O DETALHE
# do incidente exige captcha (uuidCaptcha) na consulta pública → só funciona com
# captcha-solver OU sessão e-SAJ autenticada (como o Juriscope). Default OFF até
# essa decisão de infra; o código está pronto (enrichers/esaj.py) e degrada pro
# processo-pai. (2026-07-06)
ESAJ_SEGUIR_INCIDENTES = env.bool('ESAJ_SEGUIR_INCIDENTES', default=True)

# LLM (Ollama OpenAI-compat) — narrativa de jurimetria. Fail-closed: sem key, a
# narrativa fica desativada e o dossiê determinístico segue normal. Espelha o Horizon.
OLLAMA_BASE_URL = env('OLLAMA_BASE_URL', default='https://ollama.com/v1')
OLLAMA_MODEL = env('OLLAMA_MODEL', default='kimi-k2.6')
OLLAMA_API_KEY = env('OLLAMA_API_KEY', default='')
OLLAMA_REASONING_EFFORT = env('OLLAMA_REASONING_EFFORT', default='low')

# Em ondas pesadas de WAF (todas as fontes bloqueando), o cliente faz pausas
# escalonadas entre rotações pra dar tempo do WAF "abrir" — evita queimar
# 51 rotações em <30s e morrer.
DJEN_ROTATION_PAUSE_AFTER = env.int('DJEN_ROTATION_PAUSE_AFTER', default=10)
DJEN_ROTATION_PAUSE_STEP = env.float('DJEN_ROTATION_PAUSE_STEP', default=5.0)
DJEN_ROTATION_PAUSE_MAX = env.float('DJEN_ROTATION_PAUSE_MAX', default=30.0)
# Quando saudáveis ficam abaixo desse limiar, força refresh da ProxyScrape API
# pra puxar IPs novos.
DJEN_POOL_REFRESH_THRESHOLD = env.int('DJEN_POOL_REFRESH_THRESHOLD', default=20)
# Degradação do pool por TAXA DE FALHA (não por contagem de IPs). N falhas
# seguidas sem nenhum 200 pelo pool → marca degradado por
# DJEN_POOL_DEGRADED_TTL_SECONDS e o tráfego vai pro Cortex
# (DJEN_CORTEX_RATIO_DEGRADED). O TTL também é a sonda: quando expira, o pool
# é testado de novo e volta sozinho se o WAF liberou.
DJEN_POOL_FAIL_STREAK_DEGRADE = env.int('DJEN_POOL_FAIL_STREAK_DEGRADE', default=25)
DJEN_POOL_DEGRADED_TTL_SECONDS = env.int('DJEN_POOL_DEGRADED_TTL_SECONDS', default=600)

# Classificador — hot reload de pesos
# TTL do cache em memória do classificador. A cada N segundos, o classificador
# tenta recarregar a ClassificadorVersao(ativa=True) do DB. Em erro/inválido,
# mantém o cache atual (ou cai pra hardcoded fallback no boot).
CLASSIFICADOR_RELOAD_TTL = env.int('CLASSIFICADOR_RELOAD_TTL', default=60)

# Shadow mode — fração [0, 1] de classificações principais que disparam
# também a aplicação das ClassificadorVersao(shadow=True) em job async.
# 0.0 desliga (default em testes); 0.1 = 10% sample em prod.
SHADOW_SAMPLE_RATE = env.float('SHADOW_SAMPLE_RATE', default=0.1)

# Notificações
SLACK_WEBHOOK_URL = env('SLACK_WEBHOOK_URL', default='')
SLACK_NOTIFY_DRIFT = env.bool('SLACK_NOTIFY_DRIFT', default=True)
SLACK_NOTIFY_FAILED_RUN = env.bool('SLACK_NOTIFY_FAILED_RUN', default=True)

# Pipeline semanal de lotes de validação humana (T21).
# Quando True, scheduler adiciona cron domingo 02:00 que minera FN
# candidatos e cria AmostraValidacao(estrategia='fn_candidatos').
VALIDACAO_LOTES_SEMANAIS_ENABLED = env.bool(
    'VALIDACAO_LOTES_SEMANAIS_ENABLED', default=True,
)

# Sentry
SENTRY_DSN = env('SENTRY_DSN', default='')
if SENTRY_DSN and _SENTRY_AVAILABLE:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=env('SENTRY_ENVIRONMENT', default='production'),
        traces_sample_rate=env.float('SENTRY_TRACES_SAMPLE_RATE', default=0.05),
        integrations=[DjangoIntegration(), RqIntegration()],
    )

SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
CSRF_TRUSTED_ORIGINS = env.list('DJANGO_CSRF_TRUSTED_ORIGINS', default=[])

# Classificação de IP para auditoria de cadastro (Invite). Vazio = usa
# endpoint free do ip-api.com (rate limit ~45req/min, sem HTTPS).
IP_API_KEY = env('IP_API_KEY', default='')

# Zordon — serviço de busca semântica + extração de autos.
# Default aponta pro host Zordon na tailscale (localhost:8011 nunca vale dentro do
# container web); sobrescreva com ZORDON_URL no .env se o endereço mudar.
ZORDON_URL = env('ZORDON_URL', default='http://100.116.189.18:8011')
ZORDON_API_KEY = env('ZORDON_API_KEY', default='')

# Elasticsearch — cluster dedicado pra busca Jusbrasil-compat.
# Source of truth continua no Postgres; ES é índice de busca (write-through).
ELASTICSEARCH_URL = env('ELASTICSEARCH_URL', default='http://elasticsearch:9200')
ELASTICSEARCH_INDEX_PREFIX = env('ELASTICSEARCH_INDEX_PREFIX', default='voyager')
ES_TIMEOUT = env.int('ES_TIMEOUT', default=30)
# Espelho de escrita durante migração de índice: `origem:destino[,origem:destino]`.
# Ex.: 'movimentacoes:movimentacoes-v2' faz todo write-through cair nos DOIS.
# Sem isto, a janela da migração (horas de publicação + updates de
# enriquecimento) se perde no cutover. Ver search/client.py::indices_espelho.
ES_INDICE_ESPELHO = env('ES_INDICE_ESPELHO', default='')
# Sufixo do índice canônico de ENTIDADES (o autocomplete de "quem deve").
# `voyager-entidades` é um ALIAS apontando pro índice versionado
# (`voyager-entidades-v1`, 1.131.058 entidades, promovido em 14/08/2026).
# Alias e não índice direto de propósito: reindexar no futuro vira criar o v2,
# validar e mover o alias — troca atômica, zero downtime, e rollback é mover o
# alias de volta.
ENTIDADES_INDICE_SUFIXO = env('ENTIDADES_INDICE_SUFIXO', default='entidades')

# MinIO — armazenamento de PDFs das movimentações (cached_docurl).
# S3-compatível local; django-storages fala com ele via boto3.
MINIO_ENDPOINT = env('MINIO_ENDPOINT', default='minio:9000')
MINIO_ACCESS_KEY = env('MINIO_ACCESS_KEY', default='voyager')
MINIO_SECRET_KEY = env('MINIO_SECRET_KEY', default='voyager123')
MINIO_BUCKET_PDFS = env('MINIO_BUCKET_PDFS', default='voyager-pdfs')
MINIO_USE_SSL = env.bool('MINIO_USE_SSL', default=False)
# django-storages S3 backend aponta pro MinIO.
AWS_S3_ENDPOINT_URL = env('AWS_S3_ENDPOINT_URL', default=f'http{"s" if MINIO_USE_SSL else ""}://{MINIO_ENDPOINT}')
AWS_ACCESS_KEY_ID = MINIO_ACCESS_KEY
AWS_SECRET_ACCESS_KEY = MINIO_SECRET_KEY
AWS_STORAGE_BUCKET_NAME = MINIO_BUCKET_PDFS
AWS_S3_FILE_OVERWRITE = False
AWS_QUERYSTRING_EXPIRE = 604800  # 7 dias — presigned URL expira em 1 semana.

# MCP server — Model Context Protocol pra LLMs/agentes conversarem sobre processos.
# Transport SSE em /mcp/sse; auth via ApiClient.mcp_token (UUID).
MCP_RATE_LIMIT_RPM = env.int('MCP_RATE_LIMIT_RPM', default=60)
# Gate: se True, expõe a tool classificacao_lead (score de ML). Default off.
MCP_ENABLE_CLASSIFICACAO = env.bool('MCP_ENABLE_CLASSIFICACAO', default=False)

# Showcase do Extrator (/dashboard/ia/showcase/) — URLs por VERSÃO do modelo,
# cada uma a raiz de um SDK standalone (FastAPI) servido num pod (extração 100%
# on-device, sem consulta externa). Preencher quando o pod servidor subir; versão
# sem 'url' aparece como indisponível. Formato:
#   {"v21": {"url": "http://IP:PORT", "label": "v2.1 (campeão)", "cor": "#22c55e",
#            "explicavel": True}, ...}
# Via env JSON (SHOWCASE_MODELOS) ou editar o default aqui.
# Nó GPU macOS `voyager-worker-mac` (Mac mini M4, Metal) via Tailscale — ver
# `.ia/GPU_MACOS.md`. Substituiu o pod QuickPod RTX 5090 (159.48.242.22:3200x),
# que saiu do ar. Trade-off consciente: o M4 gera ~22,9 t/s contra ~150-200 da
# 5090, então a extração é ~1 ordem de grandeza mais lenta — aceitável porque o
# pod custava ~$0,50/h e estava indisponível. v22 sobe quando gatear.
_SHOWCASE_DEFAULT = {
    "v1":  {"url": "http://100.105.16.107:8001", "label": "Geração 1 (v1)",      "cor": "#71717a"},
    "v2":  {"url": "http://100.105.16.107:8002", "label": "v2 · Ficha da Parte", "cor": "#3b82f6"},
    "v21": {"url": "http://100.105.16.107:8003", "label": "v2.1 · campeão",      "cor": "#22c55e"},
    "v22": {"url": "",                           "label": "v2.2 · herdeiros",    "cor": "#a855f7"},
}
try:
    _raw = env('SHOWCASE_MODELOS', default='')
    SHOWCASE_MODELOS = __import__('json').loads(_raw) if _raw else _SHOWCASE_DEFAULT
except Exception:
    SHOWCASE_MODELOS = _SHOWCASE_DEFAULT

# Cards explicativos (specs técnicas) dos modelos no showcase — editável aqui.
# VELOCIDADE não é hardcode: cada card mostra o tempo REAL do run assim que uma
# extração roda naquela versão. Base comum: Qwen2.5-7B-Instruct, QLoRA 4-bit,
# servido em GGUF Q4_K_M ~4,68GB (~6GB VRAM).
SHOWCASE_MODELO_INFO = {
    "v1": {
        "nome": "Geração 1", "criado": "28/07/2026",
        "base": "Qwen2.5-7B-Instruct",
        "treino": "QLoRA · 1 época · ~10,4h (RTX 3090)",
        "vram": "~6 GB", "servido": "GGUF Q4_K_M · 4,68 GB",
        "gate": "TEST macro 87,9 (+9,9 vs base)",
        "novidade": "1ª geração — esquema one-shot (extração única). Esquema antigo.",
        "status": "geracao1",
    },
    "v2": {
        "nome": "Ficha da Parte", "criado": "28-29/07/2026",
        "base": "Qwen2.5-7B-Instruct",
        "treino": "QLoRA r32/α32 · 2 épocas · ~13,3h",
        "vram": "~6 GB", "servido": "GGUF Q4_K_M · 4,68 GB",
        "gate": "train loss 0,028 · core forte (ofício/decisão ~100)",
        "novidade": "Esquema por-DOCUMENTO: ficha por parte com papel + valores. "
                    "Partes/herdeiros/cessão ainda fracos.",
        "status": "padrao",
    },
    "v21": {
        "nome": "Campeão atual", "criado": "30-31/07/2026",
        "base": "Qwen2.5-7B-Instruct",
        "treino": "QLoRA r32/α32 · 2 épocas · ~11,4h",
        "vram": "~6 GB", "servido": "GGUF Q4_K_M · 4,68 GB",
        "gate": "MACRO 91,76",
        "novidade": "Janelamento de docs longos — acórdão/sentença/cessão/partes "
                    "saltaram de 16-80% para 97-100%.",
        "status": "campeao",
    },
    "v22": {
        "nome": "Herdeiros", "criado": "31/07/2026",
        "base": "Qwen2.5-7B-Instruct",
        "treino": "QLoRA r32/α32 · 2 épocas · ~11h",
        "vram": "~6 GB", "servido": "GGUF Q4_K_M · 4,68 GB",
        "gate": "pendente (treinando)",
        "novidade": "Gold de herdeiros re-rotulado com professor (DeepSeek): docs com "
                    "herdeiro 25%→49%; papéis herdeiro/inventariante/cônjuge/sucessor.",
        "status": "treino",
    },
}

# Showcase — upload em CHUNKS + extração ASSÍNCRONA (aguenta ~1GB via Cloudflare).
# Chunks gravados em streaming num diretório de montagem; job na fila 'manual'
# (worker_manual, .103 — mesmo host do web, vê os chunks montados). Ver
# dashboard/showcase_chunks.py e .ia/DASHBOARD.md. Todos overridable via .env.
SHOWCASE_UPLOAD_DIR = env('SHOWCASE_UPLOAD_DIR', default=str(BASE_DIR / 'media' / 'showcase_uploads'))
SHOWCASE_MAX_UPLOAD_BYTES = env.int('SHOWCASE_MAX_UPLOAD_BYTES', default=2 * 1024**3)   # 2 GB
SHOWCASE_MAX_CHUNKS = env.int('SHOWCASE_MAX_CHUNKS', default=4096)
SHOWCASE_JOB_TIMEOUT = env.int('SHOWCASE_JOB_TIMEOUT', default=3600)                    # pod lento em doc grande
SHOWCASE_JOB_TTL = env.int('SHOWCASE_JOB_TTL', default=3600)                            # estado do job no cache
SHOWCASE_UPLOAD_TTL = env.int('SHOWCASE_UPLOAD_TTL', default=6 * 3600)                  # janela p/ terminar o upload
SHOWCASE_QUEUE = env('SHOWCASE_QUEUE', default='manual')
# Teto de body bufferizado do Django (guarda de form-parsing). O upload em chunks
# lê o corpo via streaming (request.read), sem tocar request.body — mas o fallback
# e outros POST JSON pequenos passam por aqui. 16 MB cobre 1 chunk de 8 MB folgado.
DATA_UPLOAD_MAX_MEMORY_SIZE = env.int('DATA_UPLOAD_MAX_MEMORY_SIZE', default=16 * 1024**2)

# QuickPod — API de crédito/pods da frota GPU cloud (Command Center · card CUSTO).
# Consultada só pelo scheduler (warm_command_center), cacheada em Redis; nunca
# batida no request do browser. Sobrescreva a chave via .env em prod.
QUICKPOD_API_URL = env('QUICKPOD_API_URL', default='https://api.quickpod.org/update/api')
QUICKPOD_API_KEY = env('QUICKPOD_API_KEY', default='')  # credencial: só via .env (nunca hardcoded)

# Prometheus (Observability stack — Fase B, ver .ia/OPS.md). Alimenta o bloco
# INFRA do Command Center (cache-hit HNSW, índice vs shared_buffers, dead
# tuples, autovacuum, conexões). Consultado só pelo scheduler
# (warm_command_center), cacheado em Redis; nunca no request do browser.
PROMETHEUS_URL = env('PROMETHEUS_URL', default='http://zordon:9490')
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'SAMEORIGIN'  # a tela de extração embute o HTML rico do Zordon num iframe same-origin

# Optional dependencies — guarded with try/except por serem features opcionais
# (PEP 8 §3.1 admite imports condicionais para features opcionais).
try:
    import pythonjsonlogger.jsonlogger  # noqa: F401
    _JSON_LOG_AVAILABLE = True
except ImportError:
    _JSON_LOG_AVAILABLE = False

try:
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.rq import RqIntegration
    _SENTRY_AVAILABLE = True
except ImportError:
    _SENTRY_AVAILABLE = False

try:
    import colorlog as _colorlog
    _COLORLOG_AVAILABLE = True
except ImportError:
    _COLORLOG_AVAILABLE = False

_use_color = DEBUG and _COLORLOG_AVAILABLE

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'console': {'format': '%(asctime)s %(levelname)s %(name)s — %(message)s'},
        **({'color': {
            '()': 'colorlog.ColoredFormatter',
            'format': '%(asctime)s %(log_color)s%(levelname)-8s%(reset)s %(cyan)s%(name)s%(reset)s — %(message)s',
            'datefmt': '%H:%M:%S',
            'log_colors': {
                'DEBUG':    'white',
                'INFO':     'bold_green',
                'WARNING':  'bold_yellow',
                'ERROR':    'bold_red',
                'CRITICAL': 'bold_red,bg_white',
            },
        }} if _use_color else {}),
        **({
            'json': {
                '()': 'pythonjsonlogger.jsonlogger.JsonFormatter',
                'fmt': '%(asctime)s %(levelname)s %(name)s %(message)s',
            },
        } if _JSON_LOG_AVAILABLE else {}),
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'color' if _use_color else ('json' if (_JSON_LOG_AVAILABLE and not DEBUG) else 'console'),
        },
    },
    'root': {'handlers': ['console'], 'level': 'INFO'},
    'loggers': {
        'voyager': {'handlers': ['console'], 'level': 'DEBUG' if DEBUG else 'INFO', 'propagate': False},
        'rq.worker': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
    },
}
