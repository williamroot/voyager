"""Monta o dataset de sobrevivência DIREITO_CREDITÓRIO→precatório.

Roda DENTRO do container web do Voyager (tem Django/ORM p/ voyager + psycopg p/
falcon). Merge por CNJ em python puro (DBs separados, sem cross-join). Grava CSV.

Spec verificada (2 agentes, zero-erro):
- t0 = COALESCE(parse(falcon data->>'Autuação' [pt: '10 mai 2023']), voyager data_autuacao)
- evento = 1 se data_oficio OR classificacao IN (PRECATORIO,PRE_PRECATORIO)
- event_date = data_oficio, senão (positivo via classe) ultima_movimentacao_em
- censura: sem evento → dur = CORTE - t0
- is_extinto = COMPETING RISK → censura (evento=0) + flag (nunca positivo)
- features SEM vazamento: tribunal, ente_tipo, natureza, valor_acao (inicial), orgao,
  classe, assunto. NÃO usa valor_corrigido/ordem_orcamentaria (pós-evento).
- negativos vêm do universo Voyager DIREITO_CREDITORIO.
"""
import os, csv, datetime as dt
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
import django; django.setup()
from django.db import connection as vconn
import psycopg

FALCON = os.environ.get('JURISCOPE_DB_DSN')  # setado no .env do servidor (read-only)
if not FALCON:
    raise SystemExit('JURISCOPE_DB_DSN não configurado no ambiente.')
CORTE = dt.date(2026, 7, 6)
OUT = '/tmp/surv_dataset.csv'
_MES = {'jan':1,'fev':2,'mar':3,'abr':4,'mai':5,'jun':6,'jul':7,'ago':8,'set':9,'out':10,'nov':11,'dez':12}


def parse_autuacao_js(s):
    if not s: return None
    p = str(s).strip().lower().split()
    if len(p) != 3: return None
    try:
        return dt.date(int(p[2]), _MES.get(p[1][:3], 0) or 1, int(p[0]))
    except (ValueError, KeyError):
        return None


def ente_tipo(tribunal, ente_nome):
    t = (tribunal or '').upper()
    nome = (ente_nome or '').upper()
    if t.startswith('TRF') or 'TRF' in t or t.startswith('JF'):
        return 'federal'
    if 'MUNIC' in nome or 'PREFEIT' in nome:
        return 'municipal'
    if t.startswith('TJ'):
        return 'estadual'
    return 'outro'


print('Query A (falcon)...', flush=True)
falcon = {}
with psycopg.connect(FALCON, connect_timeout=20) as fc:
  with fc.cursor() as c0:
    c0.execute("SET statement_timeout='300000'")
  with fc.cursor(name='cur_a') as cur:
    cur.execute("""
        SELECT p.numero_autos, p.natureza, p.valor_acao, p.tribunal, p.data_oficio,
               p.data->>'Autuação', p.is_extinto, p.sem_expedicao, e.name
        FROM datamodel_process p LEFT JOIN datamodel_entity e ON e.id=p.entity_id
        WHERE p.numero_autos IS NOT NULL AND p.numero_autos <> ''
    """)
    for cnj, nat, val, trib, dof, autj, ext, semexp, ente in cur:
        falcon[cnj] = (nat, val, trib, dof.date() if dof else None,
                       parse_autuacao_js(autj), bool(ext), ente)
print(f'  falcon dict: {len(falcon):,}', flush=True)

print('Query B (voyager) + merge...', flush=True)
n=0; nev=0; ncens=0; next_=0; ndrop=0
with open(OUT, 'w', newline='') as f, vconn.cursor() as cur:
    w = csv.writer(f)
    w.writerow(['cnj','tribunal','ente_tipo','natureza','valor_acao','orgao','classe',
                'assunto','t0','duracao_dias','evento','is_extinto','coorte_ano'])
    cur.execute("""
        SELECT numero_cnj, data_autuacao, classificacao, orgao_julgador_codigo,
               classe_codigo, assunto_codigo, ultima_movimentacao_em, tribunal_id
        FROM tribunals_process
        WHERE classificacao IN ('DIREITO_CREDITORIO','PRE_PRECATORIO','PRECATORIO')
    """)
    for cnj, dtaut, classif, orgao, classe, assunto, ultmov, trib_v in cur:
        fx = falcon.get(cnj)
        nat = fx[0] if fx else None
        val = float(fx[1]) if fx and fx[1] is not None else None
        dof = fx[3] if fx else None
        t0_js = fx[4] if fx else None
        is_ext = fx[5] if fx else False
        ente = fx[6] if fx else None
        # tribunal: prefere falcon; senão a sigla real do Voyager (tribunal_id)
        trib = (fx[2] if fx and fx[2] else None) or trib_v or ''
        t0 = t0_js or (dtaut if dtaut else None)
        if t0 is None:
            ndrop += 1; continue
        classe_pos = classif in ('PRECATORIO', 'PRE_PRECATORIO')
        evento = 1 if (dof or classe_pos) else 0
        if is_ext and not dof:   # competing risk → censura, nunca positivo
            evento = 0
        event_date = dof or (ultmov.date() if ultmov else None)
        if evento and event_date is None:   # evento sem data mensurável → descarta
            ndrop += 1; continue
        fim = event_date if evento else CORTE
        dur = (fim - t0).days
        if dur <= 0:
            ndrop += 1; continue
        w.writerow([cnj, trib, ente_tipo(trib, ente), nat or '', val if val is not None else '',
                    orgao or '', classe or '', assunto or '', t0.isoformat(), dur, evento,
                    1 if is_ext else 0, t0.year])
        n += 1; nev += evento; ncens += (1-evento); next_ += (1 if is_ext else 0)
print(f'  linhas: {n:,} | eventos: {nev:,} ({100*nev/max(n,1):.1f}%) | censurados: {ncens:,} | is_extinto: {next_:,} | descartados(t0/dur): {ndrop:,}', flush=True)
print('OK', OUT, flush=True)
