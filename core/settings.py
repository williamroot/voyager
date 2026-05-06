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

# WhiteNoise: em DEBUG serve direto via finders (sem manifest);
# em prod, exige `collectstatic` pra gerar manifest e tira benefícios de cache busting + compressão.
WHITENOISE_USE_FINDERS = DEBUG
WHITENOISE_AUTOREFRESH = DEBUG
if not DEBUG:
    STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

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
            'socket_connect_timeout': 2,
            'socket_timeout': 3,
            'retry_on_timeout': True,
            'max_connections': 20,
        },
    }
}

# Kwargs compartilhados por todas as filas RQ — limita pool de conexões por
# processo (evita crescimento ilimitado com muitos workers) e define timeouts
# de socket (workers sem timeout bloqueiam forever em ops Redis que não sejam BLPOP).
_RQ_CONN = {
    'socket_connect_timeout': 2,
    'socket_timeout': 10,
    'retry_on_timeout': True,
    'max_connections': 20,
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
    'enrich_tjmg':     {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 600,   **_RQ_CONN},
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
    # Warm de cache do dashboard (KPIs, charts, partes, estatísticas, filtros).
    # Fila dedicada pra não competir com `default` (que tem watchdogs/ticks
    # do scheduler). Worker em .30, perto do scheduler.
    'warm':            {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 900,   **_RQ_CONN},
}

# DJEN
DJEN_BASE_URL = env('DJEN_BASE_URL', default='https://comunicaapi.pje.jus.br/api/v1/comunicacao')
DJEN_REQUEST_TIMEOUT_CONNECT = env.int('DJEN_REQUEST_TIMEOUT_CONNECT', default=10)
DJEN_REQUEST_TIMEOUT_READ = env.int('DJEN_REQUEST_TIMEOUT_READ', default=60)
DJEN_PAGE_SLEEP_SECONDS = env.float('DJEN_PAGE_SLEEP_SECONDS', default=1.0)
DJEN_MAX_RETRIES = env.int('DJEN_MAX_RETRIES', default=5)
DJEN_USER_AGENT = env('DJEN_USER_AGENT', default='voyager-ingestion/0.1')

# Proxies
PROXYSCRAPE_API_KEY = env('PROXYSCRAPE_API_KEY', default='')
# API key alternativa para workers Datajud numa máquina específica.
# Quando definida, DatajudClient usa pool isolada (Redis: voyager:proxies:datajud:*)
# sem interferir na pool padrão das outras máquinas.
DATAJUD_PROXYSCRAPE_API_KEY = env('DATAJUD_PROXYSCRAPE_API_KEY', default='')
PROXYSCRAPE_REFRESH_SECONDS = env.int('PROXYSCRAPE_REFRESH_SECONDS', default=900)
CORTEX_PROXY_URL = env('CORTEX_PROXY_URL', default='')
CORTEX_FALLBACK_ENABLED = env.bool('CORTEX_FALLBACK_ENABLED', default=True)
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
DJEN_CORTEX_RATIO = env.float('DJEN_CORTEX_RATIO', default=0.5)
# Em ondas pesadas de WAF (todas as fontes bloqueando), o cliente faz pausas
# escalonadas entre rotações pra dar tempo do WAF "abrir" — evita queimar
# 51 rotações em <30s e morrer.
DJEN_ROTATION_PAUSE_AFTER = env.int('DJEN_ROTATION_PAUSE_AFTER', default=10)
DJEN_ROTATION_PAUSE_STEP = env.float('DJEN_ROTATION_PAUSE_STEP', default=5.0)
DJEN_ROTATION_PAUSE_MAX = env.float('DJEN_ROTATION_PAUSE_MAX', default=30.0)
# Quando saudáveis ficam abaixo desse limiar, força refresh da ProxyScrape API
# pra puxar IPs novos.
DJEN_POOL_REFRESH_THRESHOLD = env.int('DJEN_POOL_REFRESH_THRESHOLD', default=20)

# Notificações
SLACK_WEBHOOK_URL = env('SLACK_WEBHOOK_URL', default='')
SLACK_NOTIFY_DRIFT = env.bool('SLACK_NOTIFY_DRIFT', default=True)
SLACK_NOTIFY_FAILED_RUN = env.bool('SLACK_NOTIFY_FAILED_RUN', default=True)

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
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'

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
