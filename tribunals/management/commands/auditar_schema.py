"""Inventário do que as migrations declaram e o banco não tem — e o reparo.

    # a régua (não escreve nada)
    manage.py auditar_schema
    manage.py auditar_schema --json

    # cria as FKs ausentes em DUAS etapas — etapa 1: NOT VALID (não varre)
    manage.py auditar_schema --reparar-fks --max-bytes 200000000000

    # etapa 2: VALIDATE (varre, mas NÃO bloqueia escrita)
    manage.py auditar_schema --validar-fks

Por que duas etapas
-------------------
`ALTER TABLE ... ADD CONSTRAINT ... FOREIGN KEY` normal pega **ACCESS
EXCLUSIVE** e varre a tabela inteira **segurando o lock**. Em 104 M de linhas
isso é o auto-jam de 25/08/2026: ACCESS EXCLUSIVE *enfileira*, e enquanto o
ALTER espera, todo SELECT que chega depois espera junto — 63 sessões travadas.

    ADD CONSTRAINT ... NOT VALID   ACCESS EXCLUSIVE, mas NÃO varre  (ms)
    VALIDATE CONSTRAINT            varre, mas SHARE UPDATE EXCLUSIVE:
                                   não bloqueia INSERT/UPDATE/DELETE

A partir do `NOT VALID` a FK **já vale para toda linha nova** — o que falta é
só a prova sobre o passado. Por isso a etapa 1 é a que importa, e ela é barata
se o lock vier.

`lock_timeout` curto e RETENTATIVA, nunca espera longa
------------------------------------------------------
Cada tentativa custa, no pior caso, `--lock-timeout` de fila na tabela. Com 3 s
o dano máximo é 3 s; subir esse número não "aumenta a chance", aumenta o
tamanho do estrago quando não vier. Se todas as tentativas falharem isso é
**ERRO com o número real** (regra nº 2 do CLAUDE.md: teto é alerta, nunca corte
mudo), e o caminho é janela de manutenção — parar os escritores, aplicar em
segundos, religar.

`--max-bytes` existe porque tamanho é decisão de operação
---------------------------------------------------------
`tribunals_movimentacao` tem **1,89 TB / 1,55 bilhão de linhas**. Um
`VALIDATE` ali é uma varredura de dias, e o `ADD ... NOT VALID` pega ACCESS
EXCLUSIVE na tabela mais quente do sistema. Fica FORA do default e só entra
com o teto subido de propósito — e isso é registrado, não escondido.
"""
from __future__ import annotations

import json
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.utils import OperationalError

from tribunals.schema_auditoria import inventariar

#: 200 GB: entra `tribunals_process` (131 GB) e `tribunals_processoparte`
#: (35 GB); fica de fora `tribunals_movimentacao` (1,89 TB).
MAX_BYTES_DEFAULT = 200 * 1000 ** 3

#: erros de lock que PODEM ser retentados — `55P03` é o `lock_timeout`.
TRANSITORIOS = ('55P03', '40P01')


class Command(BaseCommand):
    help = ('Compara as migrations com o banco POR COLUNA e, opcionalmente, '
            'cria as FKs ausentes em duas etapas (NOT VALID → VALIDATE).')

    def add_arguments(self, p):
        p.add_argument('--json', action='store_true', dest='como_json')
        p.add_argument('--reparar-fks', action='store_true',
                       help='cria as FKs ausentes com ADD CONSTRAINT ... NOT VALID.')
        p.add_argument('--validar-fks', action='store_true',
                       help='roda VALIDATE CONSTRAINT nas FKs ainda NOT VALID.')
        p.add_argument('--max-bytes', type=int, default=MAX_BYTES_DEFAULT,
                       help=f'tabela maior que isto fica FORA (default '
                            f'{MAX_BYTES_DEFAULT:,} = 200 GB). Ver a docstring.')
        p.add_argument('--lock-timeout', default='3s',
                       help='teto de espera por tentativa (default 3s). Subir '
                            'este número aumenta o ESTRAGO, não a chance.')
        p.add_argument('--statement-timeout', default='30s')
        p.add_argument('--tentativas', type=int, default=10)
        p.add_argument('--espera', type=float, default=4.0,
                       help='segundos entre tentativas.')
        p.add_argument('--sem-guarda-de-indice', action='store_true',
                       help='cria a FK mesmo sem índice na coluna que referencia. '
                            'NÃO use sem ler `_tem_indice_liderado_por`: sem o '
                            'índice, cada DELETE no lado referenciado varre a '
                            'tabela filha INTEIRA, uma vez por linha.')
        p.add_argument('--validate-timeout', default='3600s',
                       help='teto do VALIDATE (varre a tabela; não bloqueia escrita).')

    # ------------------------------------------------------------------ #

    def handle(self, *a, **o):
        inv = inventariar(connection)

        # A medição só se publica com o controle em 100%.
        if not inv.controle_ok:
            raise CommandError(
                f'CONTROLE FALHOU: {inv.controle_pk_encontradas} de '
                f'{inv.controle_pk_esperadas} PKs declaradas foram encontradas. '
                'PK é o objeto que certamente existe — se ela não fecha, a régua '
                'está torta e o inventário inteiro é lixo.')

        if o['reparar_fks']:
            self._reparar(inv, o)
            inv = inventariar(connection)          # o "depois", remedido
        if o['validar_fks']:
            self._validar(o)
            inv = inventariar(connection)

        if o['como_json']:
            self.stdout.write(json.dumps(inv.como_dict(), indent=1, ensure_ascii=False))
            return

        self.stdout.write(
            f'{inv.modelos} modelos declarados · {inv.tabelas_no_banco} tabelas no '
            f'banco · CONTROLE PK {inv.controle_pk_encontradas}/'
            f'{inv.controle_pk_esperadas} (100%)')
        ausentes = inv.ausentes()
        self.stdout.write(f'\nDECLARADO E AUSENTE: {len(ausentes)}')
        for tipo, n in sorted(inv.por_tipo().items(), key=lambda kv: -kv[1]):
            self.stdout.write(f'  {tipo:16s} {n}')
        for ach in ausentes:
            self.stdout.write('  ' + ach.como_linha())
        avisos = [x for x in inv.achados if x.gravidade == 'aviso']
        self.stdout.write(f'\nAVISOS: {len(avisos)}')
        for ach in avisos:
            self.stdout.write('  ' + ach.como_linha())

    # ------------------------------------------------------------------ #

    def _tamanho(self, tabela: str) -> int:
        with connection.cursor() as cur:
            cur.execute('SELECT pg_total_relation_size(%s::regclass)', [tabela])
            return cur.fetchone()[0]

    def _tem_indice_liderado_por(self, tabela: str, coluna: str) -> bool:
        """A coluna que REFERENCIA é a 1ª de algum índice válido?

        Sem isso a FK é uma bomba. O lado REFERENCIADO paga: apagar uma linha
        do pai dispara, para cada linha, `SELECT 1 FROM filho WHERE fk = $1`.
        Sem índice, isso é um Seq Scan da tabela filha **por linha apagada**.

        Medido em 01/09/2026: a FK `processoparte.representa_id -> self` foi
        criada por este command e teve que ser derrubada em 13 minutos —
        `representa_id` não tem índice, e `enrichers/drainer.py:423` apaga
        `ProcessoParte` no caminho quente (o drainer aplica ~126 mil events/h).
        Cada linha apagada teria varrido 35 GB.
        """
        with connection.cursor() as cur:
            cur.execute("""
                SELECT 1
                  FROM pg_index i JOIN pg_class t ON t.oid = i.indrelid
                  JOIN pg_attribute a
                    ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
                 WHERE t.relname = %s AND a.attname = %s
                   AND i.indisvalid AND i.indpred IS NULL
                 LIMIT 1
            """, [tabela, coluna])
            return cur.fetchone() is not None

    def _campo(self, tabela: str, coluna: str):
        """(model, field) da coluna — para o nome sair IGUAL ao do Django."""
        from django.apps import apps as registro
        for model in registro.get_models():
            if model._meta.db_table != tabela:
                continue
            for f in model._meta.local_fields:
                if f.column == coluna:
                    return model, f
        return None, None

    def _reparar(self, inv, o):
        faltando = [a for a in inv.ausentes() if a.tipo == 'fk']
        if not faltando:
            self.stdout.write('nenhuma FK ausente — nada a reparar.')
            return
        criadas, fora, perdidas, sem_indice = 0, [], [], []
        for ach in faltando:
            tam = self._tamanho(ach.tabela)
            if tam > o['max_bytes']:
                fora.append((ach, tam))
                continue
            if not (o['sem_guarda_de_indice']
                    or self._tem_indice_liderado_por(ach.tabela, ach.objeto)):
                sem_indice.append(ach)
                continue
            model, campo = self._campo(ach.tabela, ach.objeto)
            if campo is None:
                raise CommandError(
                    f'{ach.tabela}.{ach.objeto} está no estado das migrations mas '
                    'não no registro de models — não sei nomear a constraint.')
            alvo = campo.remote_field.model._meta
            para_tabela, para_coluna = alvo.db_table, campo.target_field.column
            # O MESMO nome que o Django geraria: assim uma alteração futura do
            # campo encontra a constraint e o banco para de divergir do estado.
            with connection.schema_editor(collect_sql=True, atomic=False) as se:
                nome = str(se._fk_constraint_name(
                    model, campo, '_fk_%(to_table)s_%(to_column)s')).strip('"')
            if self._add_not_valid(ach.tabela, ach.objeto, para_tabela,
                                   para_coluna, nome, o):
                criadas += 1
                self.stdout.write(self.style.SUCCESS(
                    f'  CRIADA NOT VALID {nome}  ({ach.tabela}.{ach.objeto} '
                    f'-> {para_tabela}.{para_coluna})'))
            else:
                perdidas.append(ach)

        for ach, tam in fora:
            # Teto atingido é ERRO registrado com o número real, nunca um
            # `return` discreto.
            self.stderr.write(self.style.ERROR(
                f'  FORA DO TETO {ach.tabela}.{ach.objeto}: '
                f'{tam:,} bytes > --max-bytes {o["max_bytes"]:,}. '
                'Decisão de operação, não de deploy.'))
        for ach in sem_indice:
            self.stderr.write(self.style.ERROR(
                f'  SEM ÍNDICE {ach.tabela}.{ach.objeto}: criar a FK aqui faria '
                f'cada DELETE em {ach.detalhe.split("->")[1].strip()} varrer '
                f'{ach.tabela} inteira (o lado referenciado é quem paga). '
                'Crie o índice ANTES — CONCURRENTLY — e rode de novo.'))
        for ach in perdidas:
            self.stderr.write(self.style.ERROR(
                f'  LOCK NÃO VEIO em {ach.tabela}.{ach.objeto} após '
                f'{o["tentativas"]} tentativas de {o["lock_timeout"]}. '
                'ACCESS EXCLUSIVE enfileira: NÃO suba o lock_timeout — peça a '
                'janela, pare os escritores e reaplique (segundos).'))
        self.stdout.write(f'FKs criadas: {criadas} · fora do teto: {len(fora)} '
                          f'· sem índice: {len(sem_indice)} '
                          f'· lock não veio: {len(perdidas)}')
        if perdidas or fora or sem_indice:
            raise CommandError(
                f'{len(perdidas) + len(fora) + len(sem_indice)} FKs declaradas '
                'continuam ausentes.')

    def _add_not_valid(self, tabela, coluna, para_tabela, para_coluna, nome, o) -> bool:
        sql = (f'ALTER TABLE "{tabela}" ADD CONSTRAINT "{nome}" '
               f'FOREIGN KEY ("{coluna}") REFERENCES "{para_tabela}" ("{para_coluna}") '
               f'DEFERRABLE INITIALLY DEFERRED NOT VALID')
        for tentativa in range(1, o['tentativas'] + 1):
            try:
                with transaction.atomic(), connection.cursor() as cur:
                    # `SET LOCAL` DENTRO da transação, nunca `SET` num laço:
                    # cliente que morre deixa o backend órfão server-side, e
                    # órfão não tem quem o cancele (OPS.md, 25/08/2026).
                    cur.execute(f"SET LOCAL lock_timeout = '{o['lock_timeout']}'")
                    cur.execute(
                        f"SET LOCAL statement_timeout = '{o['statement_timeout']}'")
                    cur.execute(sql)
                return True
            except OperationalError as e:
                causa = getattr(e, '__cause__', None)
                # psycopg3 expõe `sqlstate`; o `pgcode` do psycopg2 fica de
                # reserva. Sem os dois, um lock_timeout viraria `raise` e
                # mataria a corrida inteira na primeira tabela ocupada.
                codigo = (getattr(causa, 'sqlstate', None)
                          or getattr(causa, 'pgcode', None))
                if codigo not in TRANSITORIOS:
                    raise
                self.stdout.write(
                    f'    {tabela}.{coluna}: lock não veio '
                    f'(tentativa {tentativa}/{o["tentativas"]})')
                if tentativa < o['tentativas']:
                    time.sleep(o['espera'])
        return False

    def _validar(self, o):
        with connection.cursor() as cur:
            cur.execute("""
                SELECT t.relname, co.conname
                  FROM pg_constraint co JOIN pg_class t ON t.oid = co.conrelid
                  JOIN pg_namespace n ON n.oid = t.relnamespace
                 WHERE n.nspname = 'public' AND co.contype = 'f'
                   AND NOT co.convalidated ORDER BY 1, 2
            """)
            pendentes = cur.fetchall()
        if not pendentes:
            self.stdout.write('nenhuma FK NOT VALID — nada a validar.')
            return
        for tabela, nome in pendentes:
            t0 = time.monotonic()
            with transaction.atomic(), connection.cursor() as cur:
                cur.execute(
                    f"SET LOCAL statement_timeout = '{o['validate_timeout']}'")
                # VALIDATE pega SHARE UPDATE EXCLUSIVE: varre, mas NÃO
                # bloqueia INSERT/UPDATE/DELETE na tabela.
                cur.execute(f'ALTER TABLE "{tabela}" VALIDATE CONSTRAINT "{nome}"')
            self.stdout.write(self.style.SUCCESS(
                f'  VALIDADA {tabela}.{nome} em {time.monotonic() - t0:.1f}s '
                '(0 violações — o ALTER teria falhado com a linha ofensora)'))
