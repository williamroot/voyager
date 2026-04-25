from datetime import datetime, timedelta, timezone

from django import template
from django.utils import timezone as djtz

register = template.Library()


@register.filter
def relative_dt(value):
    """'há 5min', 'há 3h', 'ontem', 'há 2 dias', 'há 3 meses'."""
    if not value:
        return '—'
    if isinstance(value, str):
        return value
    now = djtz.now()
    if isinstance(value, datetime) and djtz.is_naive(value):
        value = djtz.make_aware(value, timezone.utc)
    diff = now - value
    sec = diff.total_seconds()
    if sec < 60:
        return 'agora'
    if sec < 3600:
        return f'há {int(sec/60)}min'
    if sec < 86400:
        return f'há {int(sec/3600)}h'
    days = diff.days
    if days == 1:
        return 'ontem'
    if days < 30:
        return f'há {days} dias'
    if days < 365:
        return f'há {days // 30} {"mês" if days // 30 == 1 else "meses"}'
    return f'há {days // 365} {"ano" if days // 365 == 1 else "anos"}'


@register.filter
def format_int(value):
    """1234567 -> '1.234.567'."""
    if value is None or value == '':
        return '—'
    try:
        return f'{int(value):,}'.replace(',', '.')
    except (TypeError, ValueError):
        return str(value)


# Cores estáveis por tipo. Cada combo dark-aware (light + dark).
_TYPE_COLORS = {
    'Intimação':   ('bg-sky-100 text-sky-700 border-sky-200 dark:bg-sky-900/40 dark:text-sky-300 dark:border-sky-800/60', 'sky'),
    'Citação':     ('bg-amber-100 text-amber-700 border-amber-200 dark:bg-amber-900/40 dark:text-amber-300 dark:border-amber-800/60', 'amber'),
    'Decisão':     ('bg-emerald-100 text-emerald-700 border-emerald-200 dark:bg-emerald-900/40 dark:text-emerald-300 dark:border-emerald-800/60', 'emerald'),
    'Despacho':    ('bg-zinc-100 text-zinc-700 border-zinc-200 dark:bg-zinc-800/60 dark:text-zinc-300 dark:border-zinc-700/60', 'zinc'),
    'Sentença':    ('bg-violet-100 text-violet-700 border-violet-200 dark:bg-violet-900/40 dark:text-violet-300 dark:border-violet-800/60', 'violet'),
    'Acórdão':     ('bg-rose-100 text-rose-700 border-rose-200 dark:bg-rose-900/40 dark:text-rose-300 dark:border-rose-800/60', 'rose'),
    'Edital':      ('bg-fuchsia-100 text-fuchsia-700 border-fuchsia-200 dark:bg-fuchsia-900/40 dark:text-fuchsia-300 dark:border-fuchsia-800/60', 'fuchsia'),
    'Comunicação': ('bg-teal-100 text-teal-700 border-teal-200 dark:bg-teal-900/40 dark:text-teal-300 dark:border-teal-800/60', 'teal'),
    'Ofício':      ('bg-indigo-100 text-indigo-700 border-indigo-200 dark:bg-indigo-900/40 dark:text-indigo-300 dark:border-indigo-800/60', 'indigo'),
}
_OUTROS = ('bg-zinc-100 text-zinc-700 border-zinc-200 dark:bg-zinc-800/60 dark:text-zinc-300 dark:border-zinc-700/60', 'zinc')


@register.filter
def type_classes(value):
    if not value:
        return _OUTROS[0]
    for key, (cls, _) in _TYPE_COLORS.items():
        if key.lower() in value.lower():
            return cls
    return _OUTROS[0]


@register.filter
def meio_label(value):
    """Curto: D=Diário, E=Eletrônico, F=Físico, etc. mantém para outros."""
    if not value:
        return ''
    return value


@register.filter
def truncate_words_smart(value, n):
    if not value:
        return ''
    s = str(value)
    if len(s) <= n:
        return s
    cut = s[:n]
    if ' ' in cut:
        cut = cut[:cut.rfind(' ')]
    return cut + '…'


@register.filter
def slugify_id(value):
    if value is None:
        return ''
    return str(value).replace('-', '').replace('.', '').replace(' ', '')


@register.simple_tag
def query_string(request, **kwargs):
    """Helper pra construir URLs preservando query existente, sobrescrevendo só os params passados."""
    qd = request.GET.copy()
    for k, v in kwargs.items():
        if v is None or v == '':
            qd.pop(k, None)
        else:
            qd[k] = v
    return qd.urlencode()


@register.filter
def is_in_list(value, csv):
    """Para destacar chip ativo: {% if 'TRF1'|is_in_list:tribunal_filtro %}"""
    if not csv:
        return False
    return str(value) in str(csv).split(',')
