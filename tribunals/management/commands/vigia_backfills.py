"""Ver, medir, parar e religar o vigia dos backfills longos (#119).

    manage.py vigia_backfills              # o retrato do CACHE (não toca no banco)
    manage.py vigia_backfills --medir      # RECALCULA dos dados, imprime, não grava
    manage.py vigia_backfills --agora      # força um tique síncrono e mostra o que fez
    manage.py vigia_backfills --teto       # remede o teto alcançável do `fase` (caro)
    manage.py vigia_backfills --parar      # kill switch (Redis)
    manage.py vigia_backfills --religar
    manage.py vigia_backfills --fk-off     # pausa só a auto-cura das FKs
    manage.py vigia_backfills --fk-on
    manage.py vigia_backfills --json

O `--medir` é a régua do antes/depois: ele lê os dados, não o cache, e não
grava nada. É com ele que se confere se o card está velho ou errado — as duas
coisas que um retrato em cache pode ser.
"""
import json

from django.core.management.base import BaseCommand
from django.core.cache import cache
from django.utils import timezone

from tribunals import vigia_backfills as V


class Command(BaseCommand):
    help = 'Estado, medição e kill switch do vigia dos backfills longos.'

    def add_arguments(self, p):
        p.add_argument('--medir', action='store_true')
        p.add_argument('--agora', action='store_true')
        p.add_argument('--teto', action='store_true')
        p.add_argument('--parar', action='store_true')
        p.add_argument('--religar', action='store_true')
        p.add_argument('--fk-off', action='store_true')
        p.add_argument('--fk-on', action='store_true')
        p.add_argument('--json', action='store_true', dest='como_json')

    def handle(self, *a, **o):
        if o['parar']:
            cache.set(V.CHAVE_OFF, 1, timeout=None)
            self.stdout.write(self.style.WARNING(
                'PARADO. O tique continua MEDINDO e o card continua mostrando o '
                'número — só a auto-cura das FKs para. Card vazio some da vista.'))
            return
        if o['religar']:
            cache.delete(V.CHAVE_OFF)
            self.stdout.write(self.style.SUCCESS('religado'))
            return
        if o['fk_off']:
            cache.set(V.CHAVE_FK_OFF, 1, timeout=None)
            self.stdout.write(self.style.WARNING('auto-cura das FKs PAUSADA'))
            return
        if o['fk_on']:
            cache.delete(V.CHAVE_FK_OFF)
            self.stdout.write(self.style.SUCCESS('auto-cura das FKs religada'))
            return

        if o['teto']:
            r = V.medir_teto_fase(forcar=True)
            self.stdout.write(json.dumps(r, ensure_ascii=False, indent=2, default=str))
            return
        if o['agora']:
            r = V.tick_vigia_backfills()
        elif o['medir']:
            r = {'medido_em': timezone.now(),
                 'proc_digits': V.medir_proc_digits(),
                 'fase': V.medir_fase(),
                 'fks': V.medir_fks(),
                 'teto_fase': V.medir_teto_fase()}
        else:
            r = V.estado()
            if not r:
                self.stdout.write(self.style.ERROR(
                    'SEM RETRATO. Ou o tique nunca rodou (o `scheduler` não subiu '
                    f'com `vigia_backfills` agendado), ou o retrato venceu o TTL de '
                    f'{V.ESTADO_TTL_S // 3600} h — e isso, sozinho, já é o alarme.'))
                return

        if o['como_json']:
            self.stdout.write(json.dumps(r, ensure_ascii=False, indent=2, default=str))
            return
        self._humano(r)

    def _humano(self, r):
        quando = r.get('medido_em')
        idade = ''
        if quando:
            try:
                idade = f'  (há {int((timezone.now() - quando).total_seconds() / 60)} min)'
            except Exception:  # noqa: BLE001
                pass
        self.stdout.write(f'medido em {quando}{idade}')
        if r.get('pausado'):
            self.stdout.write(self.style.WARNING('  KILL SWITCH LIGADO'))

        d = r.get('proc_digits')
        if d:
            self.stdout.write(
                f"\nproc_digits ({d['indice']})\n"
                f"  docs .................. {d['docs']:,}\n"
                f"  faltam ................ {d['faltam']:,}\n"
                f"  cobertura (_count) .... {d['pct_exists']}%\n"
                f"  cobertura (amostra) ... {d['pct_amostra']}%  "
                f"({d['amostra_ok']}/{d['amostra_n']} conferidos pelo CONTEÚDO)\n"
                f"  vazios / malformados .. {d['amostra_vazio']} / {d['amostra_errado']}"
                f"   (tem que ser 0/0)\n"
                f"  fora da janela ........ {d['fora_da_janela']:,}   (tem que ser 0)")
            for t in d.get('tarefas') or []:
                self.stdout.write(
                    f"  ES ESCREVENDO ......... {t['id']} {t['pct']}% "
                    f"({t['updated']:,}/{t['total']:,}) a {t['rps']} d/s há "
                    f"{t['minutos']} min — container fora do ar NÃO significa parado")
            if not (d.get('tarefas') or []):
                self.stdout.write(
                    '  ES escrevendo ......... nenhuma tarefa `*byquery` viva')
        else:
            self.stdout.write(self.style.ERROR('\nproc_digits: SEM MEDIDA'))

        f = r.get('fase')
        if f:
            teto = r.get('teto_fase') or {}
            self.stdout.write(
                f"\nfase_codigo (tribunals_process)\n"
                f"  linhas ................ {f['linhas']:,}\n"
                f"  cobertura ............. {f['pct']}% ± {f['pct_erro_pp']} pp "
                f"({f['amostra_n']:,} linhas em {f['paginas_amostradas']:,} páginas)\n"
                f"  faltam (estimado) ..... {f['faltam_estimado']:,}\n"
                f"  string vazia / NULL ... {f['string_vazia']:,} / {f['nulos']:,}"
                f"   (o `<> ''` é o que separa medida de propaganda)")
            if teto:
                self.stdout.write(
                    f"  teto alcançável ....... {teto.get('pct_alcancavel')}% dos SEM "
                    f"fase têm publicação em diário com classe "
                    f"(amostra {teto.get('amostra_sem_fase')}, medido "
                    f"{teto.get('medido_em')})")
        else:
            self.stdout.write(self.style.ERROR('\nfase_codigo: SEM MEDIDA'))

        k = r.get('fks')
        if k:
            self.stdout.write(f"\nFKs NOT VALID .......... {len(k['pendentes'])}")
            for tabela, nome in k['pendentes']:
                self.stdout.write(f'    {tabela}.{nome}')
            for pid, seg, estado_, espera in k['validando']:
                self.stdout.write(f'    validando agora: pid={pid} {seg}s '
                                  f'state={estado_} wait={espera}')
            acao = r.get('fk_acao') or {}
            if acao.get('enfileirada'):
                self.stdout.write(f"    enfileirada neste tique: {acao['enfileirada']}")
            elif acao.get('motivo'):
                self.stdout.write(f"    nada enfileirado: {acao['motivo']}")
        else:
            self.stdout.write(self.style.ERROR('\nFKs: SEM MEDIDA'))

        for p in r.get('parados') or []:
            estilo = self.style.ERROR if p['veredito'] in (
                'parado', 'janela_furada', 'exists_mente') else self.style.WARNING
            self.stdout.write(estilo(
                f"\n! {p['o_que']}: {p['veredito']}"
                + (f" — faltam {p['faltam']:,}" if p.get('faltam') else '')))
        for e in r.get('erros') or []:
            self.stdout.write(self.style.ERROR(f'! medição falhou — {e}'))
