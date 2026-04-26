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

    'tribunals',
    'djen',
    'enrichers',
    'api',
    'dashboard',
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

RQ_QUEUES = {
    'default':         {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 3600},
    'djen_ingestion':  {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 7200},
    'djen_backfill':   {'URL': REDIS_URL, 'DEFAULT_TIMEOUT': 86400},
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
PROXYSCRAPE_REFRESH_SECONDS = env.int('PROXYSCRAPE_REFRESH_SECONDS', default=900)
CORTEX_PROXY_URL = env('CORTEX_PROXY_URL', default='')
CORTEX_FALLBACK_ENABLED = env.bool('CORTEX_FALLBACK_ENABLED', default=True)
PROXY_BAD_TTL_SECONDS = env.int('PROXY_BAD_TTL_SECONDS', default=600)

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

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'console': {'format': '%(asctime)s %(levelname)s %(name)s — %(message)s'},
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
            'formatter': 'json' if (_JSON_LOG_AVAILABLE and not DEBUG) else 'console',
        },
    },
    'root': {'handlers': ['console'], 'level': 'INFO'},
    'loggers': {
        'voyager': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
        'rq.worker': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
    },
}
