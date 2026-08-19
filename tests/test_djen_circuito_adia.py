"""Circuito aberto ADIA o dia — nunca o mata.

CONTEXTO (19/08/2026, e foi o pior estrago do dia).

O circuit-breaker do DJEN existe pra proteger a API do CNJ: quando ela responde
5xx em massa, ele abre e as buscas param por um cooldown de 5 minutos. Isso é
certo e funcionou — a API estava sendo martelada por 112 requisições
simultâneas (14 workers × 8 páginas).

O que estava errado era o outro lado. Com o circuito aberto, `ingest_window`
levanta `DjenBusyError` logo na primeira página; `reprocessar_janela` deixava a
exceção subir; o RQ marcava o job como `failed` e puxava o próximo — que fazia a
mesma coisa em MILISSEGUNDOS. Resultado medido:

    9.724 dias enfileirados  →  10.325 falhas em 25 minutos
    publicações coletadas ....  ZERO
    fila ......................  vazia

Cinco minutos de proteção apagaram dias de trabalho enfileirado. E o docstring
da própria exceção já avisava: "os jobs devem tratar como 'adiar', não como erro
fatal".

O que estes testes protegem:
  1. circuito aberto reenfileira o dia COM ATRASO, e não levanta;
  2. o atraso passa do cooldown do breaker (senão volta e reabre na hora);
  3. o atraso tem folga aleatória — N dias adiados não podem voltar todos no
     mesmo segundo, que é como se reabre um circuito recém-fechado;
  4. erro de verdade continua falhando o job (adiar tudo esconderia defeito).
"""
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from djen import jobs as J
from djen.client import DjenBusyError, DjenClientError


@pytest.fixture
def tribunal(db):
    from tribunals.models import Tribunal
    t, _ = Tribunal.objects.get_or_create(
        sigla='TJSP', defaults={'nome': 'TJ São Paulo', 'sigla_djen': 'TJSP'})
    return t


@pytest.mark.django_db
def test_circuito_aberto_adia_em_vez_de_falhar(tribunal):
    fila = MagicMock()
    with patch('djen.ingestion.ingest_window', side_effect=DjenBusyError('circuito aberto')), \
         patch('django_rq.get_queue', return_value=fila):
        r = J.reprocessar_janela('TJSP', '2026-08-13', '2026-08-13')

    assert r['status'] == 'adiado'
    assert fila.enqueue_in.called, 'o dia sumiu — não foi reenfileirado'
    args = fila.enqueue_in.call_args
    assert args.args[1] == 'djen.jobs.reprocessar_janela'
    assert args.args[2:5] == ('TJSP', '2026-08-13', '2026-08-13')


@pytest.mark.django_db
def test_atraso_passa_do_cooldown_do_breaker(tribunal):
    """Voltar antes do circuito fechar é voltar pra morrer de novo."""
    from django.conf import settings
    cooldown = getattr(settings, 'DJEN_CIRCUIT_COOLDOWN', 300)
    fila = MagicMock()
    with patch('djen.ingestion.ingest_window', side_effect=DjenBusyError('x')), \
         patch('django_rq.get_queue', return_value=fila):
        J.reprocessar_janela('TJSP', '2026-08-13', '2026-08-13')

    espera = fila.enqueue_in.call_args.args[0].total_seconds()
    assert espera > cooldown, f'volta em {espera}s, antes do cooldown de {cooldown}s'


@pytest.mark.django_db
def test_atraso_tem_folga_aleatoria(tribunal):
    """Mil dias adiados voltando no mesmo segundo reabrem o circuito na hora."""
    esperas = set()
    for _ in range(25):
        fila = MagicMock()
        with patch('djen.ingestion.ingest_window', side_effect=DjenBusyError('x')), \
             patch('django_rq.get_queue', return_value=fila):
            J.reprocessar_janela('TJSP', '2026-08-13', '2026-08-13')
        esperas.add(fila.enqueue_in.call_args.args[0].total_seconds())

    assert len(esperas) > 5, f'só {len(esperas)} atrasos distintos — vão voltar em bloco'


@pytest.mark.django_db
def test_erro_de_verdade_continua_falhando(tribunal):
    """Adiar TUDO transformaria defeito em fila infinita e silenciosa."""
    with patch('djen.ingestion.ingest_window', side_effect=DjenClientError('403 do WAF')), \
         pytest.raises(DjenClientError):
        J.reprocessar_janela('TJSP', '2026-08-13', '2026-08-13')


@pytest.mark.django_db
def test_caminho_feliz_intocado(tribunal):
    run = MagicMock(pk=7, movimentacoes_novas=1234, paginas_lidas=9)
    with patch('djen.ingestion.ingest_window', return_value=run):
        r = J.reprocessar_janela('TJSP', '2026-08-13', '2026-08-13')
    assert r == {'run_id': 7, 'novas': 1234, 'pgs': 9, 'janela': '2026-08-13→2026-08-13'}


def test_data_da_janela_vira_date():
    """Regressão boba mas cara: `date.fromisoformat` some se alguém mexer."""
    assert date.fromisoformat('2026-08-13') == date(2026, 8, 13)
