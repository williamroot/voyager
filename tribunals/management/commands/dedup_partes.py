"""Deduplica tribunals_parte. Causado por índices únicos parciais que
ficaram INVÁLIDOS (CREATE UNIQUE INDEX CONCURRENTLY que falhou — ver
migration 0017).

Set-based em SQL: Python loop em ~80M linhas é inviável. Resumível por
grupo: cada grupo recalcula o mapa de duplicatas a partir do estado atual
da tabela, então re-rodar após interrupção refaz grupos concluídos como
no-op e continua de onde parou.

Anti-homônimo: colapso só por chave EXATA; absorção masc_to_real só com 1
candidato. Survivor = MIN(id) / sempre a Parte de doc real.

O grupo `oab_zero` (31/08/2026, pendência #96) é a ÚNICA exceção ao "chave
EXATA": ele colapsa `PE00475` em `PE475`, porque o zero à esquerda do número
da inscrição não é significativo. Ele carrega guardas próprias — ver
`_dedup_oab_zero`.
"""
import logging
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

logger = logging.getLogger('voyager.dedup_partes')

# Grupos de colapso por chave byte-idêntica: nome -> (predicado WHERE, PARTITION BY)
GRUPOS = {
    'oab': ("oab <> ''", 'oab'),
    'doc_real': (
        "documento <> '' AND documento NOT LIKE '%X%' "
        "AND documento NOT LIKE '%x%' AND documento NOT LIKE '%*%'",
        'documento',
    ),
    'doc_masc': (
        "(documento LIKE '%X%' OR documento LIKE '%x%' OR documento LIKE '%*%')",
        'nome, documento',
    ),
}
ORDEM_ALL = ['oab', 'oab_zero', 'doc_real', 'doc_masc', 'masc_to_real']


class Command(BaseCommand):
    help = 'Deduplica tribunals_parte (anti-homônimo). Ver plano dedup-partes.'

    def add_arguments(self, parser):
        parser.add_argument('--group', choices=ORDEM_ALL + ['all'], default='all')
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--batch-size', type=int, default=200_000)
        parser.add_argument(
            '--sem-normalizar-solitarias', action='store_true',
            help='oab_zero: NÃO reescreve as OAB zero-padded que não têm gêmea. '
                 'Deixa a porta de escrita canônica criando duplicata nova nelas.')

    def handle(self, *args, **opts):
        grupos = ORDEM_ALL if opts['group'] == 'all' else [opts['group']]
        for g in grupos:
            if g == 'masc_to_real':
                self._merge_masc_to_real(dry_run=opts['dry_run'], batch=opts['batch_size'])
            elif g == 'oab_zero':
                self._dedup_oab_zero(
                    dry_run=opts['dry_run'], batch=opts['batch_size'],
                    normalizar_solitarias=not opts['sem_normalizar_solitarias'])
            else:
                self._dedup_grupo(g, dry_run=opts['dry_run'], batch=opts['batch_size'])

    def _apply_dedup_map(self, *, label, dry_run, batch):
        """Consome a tabela _dedup_map(loser_id, survivor_id) já criada e
        indexada. É UNLOGGED real (não TEMP): sob pgbouncer transaction-mode
        cada transação cai num backend diferente, e uma TEMP de sessão não
        sobreviveria entre os lotes.
        Por lote (faixa de loser_id): remove ProcessoParte que ficaria
        redundante pós-repoint (mantém o de menor id por slot), nulla
        representa_id que apontaria pra PP deletada, repointa o restante e
        deleta as Partes-loser.
        """
        with connection.cursor() as cur:
            cur.execute('SELECT count(*), min(loser_id), max(loser_id) FROM _dedup_map')
            total, lo, hi = cur.fetchone()
        self.stdout.write(f'[{label}] losers a colapsar: {total or 0:,}'
                          + ('  (DRY-RUN)' if dry_run else ''))
        if dry_run or not total:
            return
        t0 = time.time()
        cursor_id = lo
        while cursor_id <= hi:
            fim = cursor_id + batch
            with transaction.atomic():
                with connection.cursor() as c2:
                    # PP-loser deste lote + a parte que terão pós-repoint.
                    c2.execute("""
                        CREATE TEMP TABLE _pp_lote ON COMMIT DROP AS
                        SELECT ppl.id AS pp_id, ppl.processo_id, ppl.polo,
                               ppl.papel, ppl.representa_id,
                               m.survivor_id AS post_parte
                        FROM tribunals_processoparte ppl
                        JOIN _dedup_map m ON m.loser_id = ppl.parte_id
                        WHERE m.loser_id >= %s AND m.loser_id < %s
                    """, [cursor_id, fim])
                    # Redundante: já há outra PP no mesmo slot cuja parte
                    # pós-repoint é igual, com id menor (survivor PP existente
                    # OU outra PP-loser de id menor). Mantém só o menor id.
                    c2.execute("""
                        CREATE TEMP TABLE _pp_del ON COMMIT DROP AS
                        SELECT l.pp_id FROM _pp_lote l
                        WHERE EXISTS (
                            SELECT 1 FROM tribunals_processoparte o
                            LEFT JOIN _dedup_map mo ON mo.loser_id = o.parte_id
                            WHERE o.processo_id = l.processo_id
                              AND o.polo = l.polo AND o.papel = l.papel
                              AND o.representa_id IS NOT DISTINCT FROM l.representa_id
                              AND COALESCE(mo.survivor_id, o.parte_id) = l.post_parte
                              AND o.id < l.pp_id
                        )
                    """)
                    # representa_id é FK self; nulla quem aponta pras PP que
                    # serão deletadas (raw DELETE não dispara on_delete=SET_NULL).
                    c2.execute("""
                        UPDATE tribunals_processoparte
                        SET representa_id = NULL
                        WHERE representa_id IN (SELECT pp_id FROM _pp_del)
                    """)
                    # Deleta as PP redundantes.
                    c2.execute("""
                        DELETE FROM tribunals_processoparte
                        WHERE id IN (SELECT pp_id FROM _pp_del)
                    """)
                    # Repointa as PP-loser restantes pro survivor.
                    c2.execute("""
                        UPDATE tribunals_processoparte ppl
                        SET parte_id = m.survivor_id
                        FROM _dedup_map m
                        WHERE ppl.parte_id = m.loser_id
                          AND m.loser_id >= %s AND m.loser_id < %s
                    """, [cursor_id, fim])
                    # Deleta as Partes-loser do lote.
                    c2.execute("""
                        DELETE FROM tribunals_parte p
                        USING _dedup_map m
                        WHERE p.id = m.loser_id
                          AND m.loser_id >= %s AND m.loser_id < %s
                    """, [cursor_id, fim])
            self.stdout.write(f'[{label}] lote {cursor_id:,}–{fim:,} ok '
                              f'({time.time() - t0:.0f}s acum.)')
            cursor_id = fim
        self.stdout.write(self.style.SUCCESS(f'[{label}] concluído'))

    def _dedup_grupo(self, grupo, *, dry_run, batch):
        where, partition = GRUPOS[grupo]
        with connection.cursor() as cur:
            cur.execute('DROP TABLE IF EXISTS _dedup_map')
            cur.execute(f"""
                CREATE UNLOGGED TABLE _dedup_map AS
                SELECT id AS loser_id,
                       min(id) OVER (PARTITION BY {partition}) AS survivor_id
                FROM tribunals_parte WHERE {where}
            """)
            cur.execute('DELETE FROM _dedup_map WHERE loser_id = survivor_id')
            cur.execute('CREATE INDEX ON _dedup_map (loser_id)')
        self._apply_dedup_map(label=grupo, dry_run=dry_run, batch=batch)
        with connection.cursor() as cur:
            cur.execute('DROP TABLE IF EXISTS _dedup_map')

    def _merge_masc_to_real(self, *, dry_run, batch):
        """Absorve Parte de doc mascarado na Parte de doc real correspondente.
        Só funde com nome byte-idêntico + máscara casando + EXATAMENTE 1
        candidato real. `translate(doc,'Xx*','___')` vira o pattern LIKE.
        Roda depois de doc_real/doc_masc (compara contra dados já colapsados).
        """
        with connection.cursor() as cur:
            cur.execute('DROP TABLE IF EXISTS _dedup_map')
            cur.execute("""
                CREATE UNLOGGED TABLE _dedup_map AS
                SELECT masc_id AS loser_id, real_id AS survivor_id FROM (
                    SELECT m.id AS masc_id, min(r.id) AS real_id, count(*) AS n
                    FROM tribunals_parte m
                    JOIN tribunals_parte r
                      ON r.nome = m.nome
                     AND r.id <> m.id
                     AND r.documento <> ''
                     AND r.documento NOT LIKE '%X%' AND r.documento NOT LIKE '%x%'
                     AND r.documento NOT LIKE '%*%'
                     AND r.documento LIKE translate(m.documento, 'Xx*', '___')
                    WHERE m.documento LIKE '%X%' OR m.documento LIKE '%x%'
                       OR m.documento LIKE '%*%'
                    GROUP BY m.id
                ) cand
                WHERE n = 1
            """)
            cur.execute('CREATE INDEX ON _dedup_map (loser_id)')
        self._apply_dedup_map(label='masc_to_real', dry_run=dry_run, batch=batch)
        with connection.cursor() as cur:
            cur.execute('DROP TABLE IF EXISTS _dedup_map')

    # ---------------------------------------------------------------- #
    # oab_zero — zero à esquerda (pendência #96)
    # ---------------------------------------------------------------- #
    #: Só a forma `UF + dígitos (+ 1 letra)` entra na régua. Tudo que não casa
    #: ABSTÉM — inclusive as 1.424 linhas `MT10079GO` do enricher do TJMT, onde
    #: o prefixo é o TRIBUNAL e a UF real está no sufixo (medido 31/08/2026:
    #: 1.427 linhas fora do padrão, 1.424 começando por `MT`).
    _SQL_CANON = """
        CREATE UNLOGGED TABLE _oab_canon AS
        SELECT id, oab,
               substring(oab from '^[A-Z]{2}([0-9]+)')       AS dig,
               substring(oab from '^([A-Z]{2})')
                 || COALESCE(NULLIF(ltrim(substring(oab from '^[A-Z]{2}([0-9]+)'), '0'), ''),
                             substring(oab from '^[A-Z]{2}([0-9]+)'))
                 || COALESCE(substring(oab from '([A-Z])$'), '')  AS canon,
               regexp_replace(upper(unaccent(nome)), '[^A-Z0-9]', '', 'g') AS nome_n,
               CASE WHEN documento <> ''
                     AND documento NOT LIKE '%X%' AND documento NOT LIKE '%x%'
                     AND documento NOT LIKE '%*%'
                    THEN regexp_replace(documento, '[^0-9]', '', 'g') END  AS doc_real
        FROM tribunals_parte
        WHERE oab ~ '^[A-Z]{2}[0-9]+[A-Z]?$'
    """

    def _dedup_oab_zero(self, *, dry_run, batch, normalizar_solitarias=True):
        """Colapsa `PE00475` em `PE475` — e SÓ onde dá pra provar a pessoa.

        Medido em produção em 31/08/2026 (943.510 `Parte` com OAB):

            linhas na régua ................ 942.086   (as 1.427 fora do
                                                        padrão ficam de fora)
            grupos em colisão ..............  19.481
            linhas a colapsar (teto) .......  19.493
            grupos SEM nenhuma forma
              zero-padded ..................       0   ⇒ o zero responde por
                                                        100% da colisão

        Três guardas, nesta ordem, e cada uma tem número:

        1. **UF na chave.** `canon` começa pela UF gravada, então `SP475` e
           `PE475` nunca caem no mesmo grupo — dois advogados com o mesmo
           número em UFs diferentes são pessoas DIFERENTES. Controle medido:
           19.482 de 19.482 grupos (100%) têm UMA UF.
        2. **Nome idêntico** (caixa/acento/pontuação normalizados). 18.490
           grupos passam; **991 abstêm**. Não é zelo: o grupo `PE475` tem
           `TANEY QUEIROZ E FARIAS` (`PE00475`) e `LUZIA HELENA DE VALOIS
           CORREIA` (`PE475`) — mesma UF, mesmo número, pessoas diferentes.
           A maioria dos 992 é troca de sobrenome (casamento) ou o sufixo
           `registrado(a) civilmente como…`, e ainda assim **abster > chutar**:
           fusão é destrutiva e o par duvidoso não tem como ser desfeito.
        3. **CPF real não divergente.** 57 grupos têm dois CPF reais
           diferentes; 56 já caíam na guarda 2, 1 só é pego aqui.

        Resultado da regra: **18.489 grupos, 18.501 linhas** colapsadas; 992
        linhas ABSTIDAS (991 por nome + 1 por CPF). Nenhum grupo tem o MESMO CPF real dos dois lados —
        ou seja o CPF nunca prova identidade aqui, ele só a NEGA.

        Survivor = a linha já canônica de menor id (para a porta de escrita
        canônica cair nela); se o grupo inteiro for zero-padded, o menor id e
        a `oab` dele é reescrita para a forma canônica.
        """
        with connection.cursor() as cur:
            cur.execute('DROP TABLE IF EXISTS _oab_canon')
            cur.execute(self._SQL_CANON)
            cur.execute('CREATE INDEX ON _oab_canon (canon)')
            cur.execute('CREATE INDEX ON _oab_canon (id)')
            cur.execute("""
                SELECT count(*), count(*) FILTER (WHERE canon IS NULL OR canon = '')
                FROM _oab_canon
            """)
            na_regua, sem_canon = cur.fetchone()
            # CONTROLE: toda linha da régua tem que produzir chave. Se não
            # produzir, a medição é cega numa fatia e não vale.
            if sem_canon:
                raise CommandError(
                    f'[oab_zero] CONTROLE FALHOU: {sem_canon:,} de {na_regua:,} '
                    'linhas sem forma canônica — régua cega, nada foi escrito.')
            self.stdout.write(f'[oab_zero] linhas na régua: {na_regua:,} '
                              f'(controle canon != NULL: 100%)')

            cur.execute("""
                DROP TABLE IF EXISTS _oab_grupos;
                CREATE UNLOGGED TABLE _oab_grupos AS
                SELECT canon,
                       count(*)                                        AS n,
                       count(DISTINCT oab)                             AS formas,
                       count(DISTINCT nome_n)                          AS nomes,
                       count(DISTINCT doc_real)
                         FILTER (WHERE doc_real IS NOT NULL)           AS docs,
                       COALESCE(min(id) FILTER (WHERE dig NOT LIKE '0%'),
                                min(id))                               AS survivor
                FROM _oab_canon GROUP BY canon HAVING count(DISTINCT oab) > 1
            """)
            cur.execute('CREATE INDEX ON _oab_grupos (canon)')
            cur.execute("""
                SELECT count(*), COALESCE(sum(n - 1), 0),
                       count(*) FILTER (WHERE nomes > 1),
                       count(*) FILTER (WHERE nomes = 1 AND docs > 1)
                FROM _oab_grupos
            """)
            grupos, teto, ab_nome, ab_doc = cur.fetchone()
        self.stdout.write(
            f'[oab_zero] TETO: {grupos:,} grupos, {teto:,} linhas em colisão · '
            f'abstém {ab_nome:,} por nome divergente e {ab_doc:,} por CPF divergente')

        with connection.cursor() as cur:
            cur.execute('DROP TABLE IF EXISTS _dedup_map')
            cur.execute("""
                CREATE UNLOGGED TABLE _dedup_map AS
                SELECT o.id AS loser_id, g.survivor AS survivor_id
                FROM _oab_canon o JOIN _oab_grupos g USING (canon)
                WHERE g.nomes = 1 AND g.docs <= 1 AND o.id <> g.survivor
            """)
            cur.execute('CREATE INDEX ON _dedup_map (loser_id)')
            # Amostra do que SERIA fundido — o dry-run tem que mostrar, não
            # só contar. Par duvidoso aqui = parar, não aplicar.
            cur.execute("""
                SELECT c.canon, m.survivor_id, s.oab, m.loser_id, c.oab, left(c.nome_n, 40)
                FROM _dedup_map m
                JOIN _oab_canon c ON c.id = m.loser_id
                JOIN _oab_canon s ON s.id = m.survivor_id
                ORDER BY c.canon LIMIT 10
            """)
            amostra = cur.fetchall()
        self.stdout.write('[oab_zero] amostra (survivor ← loser):')
        for canon, sid, soab, lid, loab, nome in amostra:
            self.stdout.write(f'    {canon:<10} {sid} {soab:<10} ← {lid} {loab:<10} {nome}')

        self._apply_dedup_map(label='oab_zero', dry_run=dry_run, batch=batch)

        if not dry_run:
            self._normalizar_survivors_zero_padded()
        if normalizar_solitarias:
            self._normalizar_oab_solitarias(dry_run=dry_run, batch=min(batch, 5_000))

        with connection.cursor() as cur:
            cur.execute('DROP TABLE IF EXISTS _dedup_map')
            cur.execute('DROP TABLE IF EXISTS _oab_grupos')
            cur.execute('DROP TABLE IF EXISTS _oab_canon')

    def _normalizar_survivors_zero_padded(self):
        """Grupo cujas DUAS formas eram zero-padded (`PE0475` + `PE00475`):
        o survivor sobrevive zero-padded. Reescreve a `oab` dele para a forma
        canônica — senão a porta de escrita canônica cria a duplicata de novo
        no primeiro enriquecimento.
        """
        with connection.cursor() as cur:
            cur.execute("""
                UPDATE tribunals_parte p
                SET oab = g.canon
                FROM _oab_grupos g
                JOIN _oab_canon c ON c.id = g.survivor
                WHERE p.id = g.survivor AND p.oab = c.oab AND p.oab <> g.canon
                  AND NOT EXISTS (SELECT 1 FROM tribunals_parte q
                                  WHERE q.oab = g.canon AND q.id <> p.id)
            """)
            self.stdout.write(f'[oab_zero] survivors reescritos p/ canônico: '
                              f'{cur.rowcount:,}')

    def _normalizar_oab_solitarias(self, *, dry_run, batch):
        """OAB zero-padded SEM gêmea canônica: reescreve a string, NÃO funde.

        Não é dedup — é a mesma linha, mesma entidade, só a grafia. Existe
        porque a porta de escrita passou a ser canônica: sem isto, cada uma
        dessas linhas vira uma duplicata NOVA no próximo enriquecimento.
        Medido em 31/08/2026: 29.979 linhas zero-padded no total ⇒ **10.485
        solitárias**.
        """
        with connection.cursor() as cur:
            cur.execute('DROP TABLE IF EXISTS _oab_rename')
            cur.execute("""
                CREATE UNLOGGED TABLE _oab_rename AS
                SELECT c.id, c.canon
                FROM _oab_canon c
                WHERE c.dig LIKE '0%' AND c.oab <> c.canon
                  AND NOT EXISTS (SELECT 1 FROM _oab_canon o
                                  WHERE o.canon = c.canon AND o.id <> c.id)
            """)
            cur.execute('CREATE INDEX ON _oab_rename (id)')
            cur.execute('SELECT count(*) FROM _oab_rename')
            (total,) = cur.fetchone()
        self.stdout.write(f'[oab_zero] solitárias a normalizar: {total:,}'
                          + ('  (DRY-RUN)' if dry_run else ''))
        if dry_run or not total:
            if dry_run:
                with connection.cursor() as cur:
                    cur.execute('DROP TABLE IF EXISTS _oab_rename')
            return
        t0, cursor_id, feitas = time.time(), -1, 0
        while True:
            with transaction.atomic():
                with connection.cursor() as cur:
                    cur.execute('SELECT max(id) FROM (SELECT id FROM _oab_rename '
                                'WHERE id > %s ORDER BY id LIMIT %s) x',
                                [cursor_id, batch])
                    (ate,) = cur.fetchone()
                    if ate is None:
                        break
                    cur.execute("""
                        UPDATE tribunals_parte p
                        SET oab = r.canon
                        FROM _oab_rename r
                        WHERE p.id = r.id AND r.id > %s AND r.id <= %s
                          AND NOT EXISTS (SELECT 1 FROM tribunals_parte q
                                          WHERE q.oab = r.canon AND q.id <> p.id)
                    """, [cursor_id, ate])
                    feitas += cur.rowcount
                    cursor_id = ate
        self.stdout.write(self.style.SUCCESS(
            f'[oab_zero] solitárias normalizadas: {feitas:,} de {total:,} '
            f'({time.time() - t0:.0f}s)'))
        with connection.cursor() as cur:
            cur.execute('DROP TABLE IF EXISTS _oab_rename')
