"""Gera um AmostraValidacao a partir de uma estratégia de sampling (T9).

Uso:
    python manage.py gerar_lote_validacao \\
        --estrategia <choice> [--tribunal SIGLA] [--tamanho 300] \\
        [--seed N] [--usuario USERNAME] [--csv-input PATH] [--dry-run] \\
        [--faixa-borderline 0.30,0.70] [--allow-system-user]

Estratégias (= AmostraValidacao.ESTRATEGIA_CHOICES, sem `shadow_disagree`):

  borderline         random na banda de score (faixa configurável).
  fn_candidatos      candidatos a falso negativo do mining (CSV).
  top_score          PRECATORIO score alto (controle de qualidade).
  low_score          NAO_LEAD score alto (controle negativo).
  falsos_consumidos  CSV de FPs validados pelo Juriscope.
  recuperados        CSV de processos recuperados pós-análise.
  on_demand          random puro estratificado em 1 tribunal (exige --tribunal).

Cron sugerido (T9.5, integração com djen/scheduler.py é futura):
    # Diário 03:30 — top_score+borderline pra cada tribunal ativo
    30 3 * * * cd /app && python manage.py gerar_lote_validacao \\
        --estrategia top_score --tribunal TRF1 --tamanho 100

Comportamento:
- `--dry-run`: imprime count + primeiros 5 CNJs, não persiste.
- `--usuario`: default 'system'; cria com `is_active=False` se não existir
  E `--allow-system-user` foi passado.
- Estratégia inválida ou usuário inexistente sem flag → CommandError.
"""
from __future__ import annotations

import logging

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from tribunals import sampling
from tribunals.models import AmostraValidacao, Tribunal

logger = logging.getLogger('voyager.tribunals.gerar_lote_validacao')

ESTRATEGIAS_VALIDAS = {
    'borderline',
    'fn_candidatos',
    'top_score',
    'low_score',
    'falsos_consumidos',
    'recuperados',
    'on_demand',
}


class Command(BaseCommand):
    help = 'Gera um lote AmostraValidacao a partir de uma estratégia.'

    def add_arguments(self, parser):
        parser.add_argument('--estrategia', required=True, choices=sorted(ESTRATEGIAS_VALIDAS))
        parser.add_argument('--tribunal', default=None, help='Sigla. Omitir = multi-tribunal.')
        parser.add_argument('--tamanho', type=int, default=300)
        parser.add_argument('--seed', type=int, default=None)
        parser.add_argument(
            '--usuario', default='system',
            help='Username de criada_por. Default "system".',
        )
        parser.add_argument(
            '--csv-input', default=None,
            help='Path do CSV (fn_candidatos / falsos_consumidos / recuperados).',
        )
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument(
            '--faixa-borderline', default='0.30,0.70',
            help='Faixa para estratégia borderline. Formato "lo,hi". Default 0.30,0.70.',
        )
        parser.add_argument(
            '--allow-system-user', action='store_true',
            help='Permite criar usuário "system" automaticamente se não existir.',
        )

    def _dispatch_sampling(self, estrategia, *, tribunal, tamanho, seed,
                           user, csv_path, faixa):
        """Dispatch da estratégia → QuerySet."""
        common = {'tribunal': tribunal, 'limit': tamanho, 'usuario': user}
        dispatchers = {
            'borderline': lambda: sampling.sample_borderline(
                faixa=faixa, seed=seed, **common,
            ),
            'fn_candidatos': lambda: sampling.sample_fn_candidatos(
                csv_path=csv_path, **common,
            ),
            'top_score': lambda: sampling.sample_n1_alto(seed=seed, **common),
            'low_score': lambda: sampling.sample_nao_lead_top(seed=seed, **common),
            'falsos_consumidos': lambda: sampling.sample_falsos_consumidos(
                csv_path=csv_path or 'leads_trf1_falsos_consumidos_1327.csv',
                **common,
            ),
            'recuperados': lambda: sampling.sample_recuperados(
                csv_path=csv_path or 'leads_trf1_recuperados_1327.csv',
                **common,
            ),
        }
        if estrategia == 'on_demand':
            if tribunal is None:
                raise CommandError('--estrategia on_demand exige --tribunal')
            return sampling.sample_random_tribunal(seed=seed, **common)
        fn = dispatchers.get(estrategia)
        if fn is None:
            raise CommandError(f'estratégia não implementada: {estrategia}')
        return fn()

    def handle(self, *args, **opts):
        estrategia = opts['estrategia']
        if estrategia not in ESTRATEGIAS_VALIDAS:
            raise CommandError(f'estratégia inválida: {estrategia}')

        # 1. Resolve usuário
        User = get_user_model()
        username = opts['usuario']
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            if opts['allow_system_user'] and username == 'system':
                user = User.objects.create(
                    username='system', is_active=False,
                )
                self.stdout.write(self.style.WARNING(
                    'usuário "system" criado (is_active=False)'
                ))
            else:
                raise CommandError(
                    f'usuário "{username}" não existe '
                    f'(use --allow-system-user pra criar "system" automaticamente)'
                ) from None

        # 2. Resolve tribunal
        tribunal = None
        if opts['tribunal']:
            try:
                tribunal = Tribunal.objects.get(pk=opts['tribunal'])
            except Tribunal.DoesNotExist:
                raise CommandError(
                    f'tribunal "{opts["tribunal"]}" não existe'
                ) from None

        # 3. Parse faixa-borderline
        faixa = (0.30, 0.70)
        if opts['faixa_borderline']:
            try:
                lo, hi = opts['faixa_borderline'].split(',')
                faixa = (float(lo), float(hi))
            except (ValueError, TypeError):
                raise CommandError(
                    '--faixa-borderline formato inválido (esperado "lo,hi")'
                ) from None

        tamanho = int(opts['tamanho'])
        seed = opts['seed']
        csv_path = opts.get('csv_input')

        # 4. Despacha
        try:
            qs = self._dispatch_sampling(
                estrategia,
                tribunal=tribunal, tamanho=tamanho, seed=seed,
                user=user, csv_path=csv_path, faixa=faixa,
            )
        except FileNotFoundError as exc:
            raise CommandError(f'CSV não encontrado: {exc}') from exc

        # 5. Dry-run
        if opts['dry_run']:
            cnjs = list(qs.values_list('numero_cnj', flat=True)[:5])
            count = qs.count()
            self.stdout.write(self.style.SUCCESS(
                f'[dry-run] {count} processos · primeiros 5 CNJs: {cnjs}'
            ))
            return

        # 6. Persiste
        parametros = {
            'tamanho_solicitado': tamanho,
            'tribunal': tribunal.pk if tribunal else None,
        }
        if estrategia == 'borderline':
            parametros['faixa'] = list(faixa)
        if csv_path:
            parametros['csv_path'] = csv_path

        lote = sampling.criar_lote(
            estrategia=estrategia,
            queryset=qs,
            criada_por=user,
            tribunal=tribunal,
            tamanho_alvo=tamanho,
            parametros=parametros,
            seed=seed,
        )
        _ = AmostraValidacao  # silenced lint
        count_real = lote.itens.count()
        trib_label = tribunal.pk if tribunal else 'multi'
        self.stdout.write(self.style.SUCCESS(
            f'Lote {lote.pk} criado · {estrategia} · {trib_label} · {count_real} processos'
        ))
