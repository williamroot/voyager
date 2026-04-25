import os

os.environ.setdefault('DJANGO_SECRET_KEY', 'test-secret-key-only-for-pytest')
os.environ.setdefault('DJANGO_DEBUG', 'False')
os.environ.setdefault('DATABASE_URL', 'postgres://voyager:voyager@localhost:5432/voyager_test')
os.environ.setdefault('REDIS_URL', 'redis://localhost:6379/15')

import django  # noqa: E402

django.setup()
