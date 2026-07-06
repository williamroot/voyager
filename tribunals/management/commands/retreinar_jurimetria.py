"""Re-treina o modelo de sobrevivência DIREITO_CREDITÓRIO→precatório e grava o
artefato servível (`dashboard/data/surv_strata.json`). Freshness: agendado semanal
no scheduler, mantém as previsões atualizadas sem intervenção.

SEM pandas/lifelines — Kaplan-Meier em numpy puro (roda em qualquer container do
cluster, sem novas dependências). Extração idêntica à spec verificada:
t0=autuação(jsonb pt + Voyager), evento=data_oficio∨classificação, is_extinto=censura,
negativos do Voyager. Estratos {ente_tipo × natureza} + fallback.

Uso:  python manage.py retreinar_jurimetria [--min-estrato 200]
"""
from __future__ import annotations

import datetime as dt
import json
import os

import numpy as np
import psycopg
from django.conf import settings
from django.core.management.base import BaseCommand

_MES = {'jan': 1, 'fev': 2, 'mar': 3, 'abr': 4, 'mai': 5, 'jun': 6,
        'jul': 7, 'ago': 8, 'set': 9, 'out': 10, 'nov': 11, 'dez': 12}
_HOR = {'12m': 365, '24m': 730, '36m': 1095}


def _parse_aut(s):
    if not s:
        return None
    p = str(s).strip().lower().split()
    if len(p) != 3:
        return None
    try:
        return dt.date(int(p[2]), _MES.get(p[1][:3], 0) or 1, int(p[0]))
    except (ValueError, KeyError):
        return None


def _ente_tipo(trib, nome):
    t = (trib or '').upper()
    n = (nome or '').upper()
    if t.startswith('TRF') or t.startswith('JF'):
        return 'federal'
    if 'MUNIC' in n or 'PREFEIT' in n:
        return 'municipal'
    if t.startswith('TJ'):
        return 'estadual'
    return 'outro'


class Command(BaseCommand):
    help = 'Re-treina o modelo de sobrevivência DC→precatório (KM numpy) e grava o artefato.'

    def add_arguments(self, parser):
        parser.add_argument('--min-estrato', type=int, default=200)

    def handle(self, *args, min_estrato, **opts):
        falcon_dsn = getattr(settings, 'JURISCOPE_DB_DSN', '')
        if not falcon_dsn:
            self.stderr.write('JURISCOPE_DB_DSN não configurado — abortando.')
            return
        corte = dt.date.today()

        # --- Falcon: dict por CNJ (streaming) ---
        self.stdout.write('extraindo falcon...')
        falcon = {}
        with psycopg.connect(falcon_dsn, connect_timeout=25) as fc:
            with fc.cursor() as c0:
                c0.execute("SET statement_timeout='300000'")
            with fc.cursor(name='cur_a') as cur:
                cur.itersize = 20000
                cur.execute("""
                    SELECT p.numero_autos, p.natureza, p.tribunal, p.data_oficio,
                           p.data->>'Autuação', p.is_extinto, e.name
                    FROM datamodel_process p LEFT JOIN datamodel_entity e ON e.id=p.entity_id
                    WHERE p.numero_autos IS NOT NULL AND p.numero_autos <> ''
                """)
                for cnj, nat, trib, dof, autj, ext, ente in cur:
                    falcon[cnj] = (nat, trib, dof.date() if dof else None,
                                   _parse_aut(autj), bool(ext), ente)
        self.stdout.write(f'  falcon: {len(falcon):,}')

        # --- Voyager: stream + merge → (chave_estrato, dur, evento) ---
        self.stdout.write('merge voyager...')
        rows = []  # (ente_tipo, natureza, dur, evento)
        vpg = psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=25)
        with vpg.cursor(name='cur_b') as cur:
            cur.itersize = 20000
            cur.execute("""
                SELECT numero_cnj, data_autuacao, classificacao,
                       ultima_movimentacao_em, tribunal_id
                FROM tribunals_process
                WHERE classificacao IN ('DIREITO_CREDITORIO','PRE_PRECATORIO','PRECATORIO')
            """)
            for cnj, dtaut, classif, ultmov, trib_v in cur:
                fx = falcon.get(cnj)
                nat = (fx[0] if fx else None) or 'DESCONHECIDA'
                trib = (fx[1] if fx and fx[1] else None) or trib_v or ''
                dof = fx[2] if fx else None
                t0 = (fx[3] if fx else None) or dtaut
                is_ext = fx[4] if fx else False
                ente = fx[5] if fx else None
                if t0 is None:
                    continue
                classe_pos = classif in ('PRECATORIO', 'PRE_PRECATORIO')
                evento = 1 if (dof or classe_pos) else 0
                if is_ext and not dof:
                    evento = 0
                event_date = dof or (ultmov.date() if ultmov else None)
                if evento and event_date is None:
                    continue
                fim = event_date if evento else corte
                dur = (fim - t0).days
                if dur <= 0 or dur > 366 * 25:
                    continue
                rows.append((_ente_tipo(trib, ente), str(nat).upper(), dur, evento))
        vpg.close()
        self.stdout.write(f'  linhas: {len(rows):,} | eventos: {sum(r[3] for r in rows):,}')

        # --- KM estratificado (numpy puro) ---
        arr_et = np.array([r[0] for r in rows])
        arr_nat = np.array([r[1] for r in rows])
        dur = np.array([r[2] for r in rows], dtype=float)
        ev = np.array([r[3] for r in rows], dtype=int)

        def km(mask):
            d, e = dur[mask], ev[mask]
            if len(d) == 0:
                return None
            et, deaths = np.unique(d[e == 1], return_counts=True)
            if len(et) == 0:
                o = {'n': int(len(d)), 'eventos': 0, 'tempo_mediano_dias': None}
                for h in _HOR:
                    o[f'chance_{h}'] = 0.0
                return o
            order = np.sort(d)
            atrisk = len(d) - np.searchsorted(order, et, side='left')
            surv = np.cumprod(1 - deaths / atrisk)
            o = {'n': int(len(d)), 'eventos': int(e.sum())}
            for h, dias in _HOR.items():
                k = np.searchsorted(et, dias, side='right') - 1
                s = float(surv[k]) if k >= 0 else 1.0
                o[f'chance_{h}'] = round(100 * (1 - s), 1)
            below = np.where(surv <= 0.5)[0]
            o['tempo_mediano_dias'] = int(et[below[0]]) if len(below) else None
            return o

        strata = {'_overall': km(np.ones(len(rows), dtype=bool))}
        for et_v in np.unique(arr_et):
            m = arr_et == et_v
            if m.sum() >= min_estrato:
                strata[f'{et_v}|*'] = km(m)
            for nat_v in np.unique(arr_nat[m]):
                mm = m & (arr_nat == nat_v)
                if mm.sum() >= min_estrato:
                    strata[f'{et_v}|{nat_v}'] = km(mm)

        strata['_meta'] = {
            'treinado_em': corte.isoformat(),
            'n_total': len(rows),
            'eventos': int(ev.sum()),
            'metodo': 'kaplan-meier estratificado (numpy)',
        }

        # --- grava artefato ---
        path = os.path.join(settings.BASE_DIR, 'dashboard', 'data', 'surv_strata.json')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(strata, f, ensure_ascii=False)
        os.replace(tmp, path)   # atômico
        self.stdout.write(self.style.SUCCESS(
            f'OK — {len([k for k in strata if not k.startswith("_")])} estratos gravados em {path}'))
