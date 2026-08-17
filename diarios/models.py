"""Watermark do backfill de diários — o `_dia_coberto` das fontes novas.

Por que um model em vez de reusar só `IngestionRun`: no DJEN a unidade de
cobertura é (tribunal, dia), e `IngestionRun.janela_inicio/fim` dá conta. Aqui
a unidade é EDIÇÃO-CADERNO — um dia do DJE/TJSP são 9 cadernos independentes,
um deles com 2.001 páginas; um dia do DEJT são 25 cadernos, um por tribunal.
Sem um registro por unidade, "retomar de onde parou" num backfill de 86 mil
cadernos vira uma varredura de IngestionRun por data, e um caderno que falhou
obriga a re-baixar o dia inteiro.

Este model é o catálogo E o watermark: catalogar é barato (o índice completo
das duas fontes grandes sai em UMA requisição), coletar é caro. Separar os dois
permite medir o acervo ANTES de baixar centenas de GB.
"""

from django.db import models
from django.db.models import UniqueConstraint
from django.utils import timezone


class EdicaoDiario(models.Model):
    """Uma unidade de coleta (edição/caderno/dia) de uma fonte de diário."""

    PENDENTE = 'pendente'
    OK = 'ok'
    VAZIA = 'vazia'                 # baixou e validou, mas não havia publicação
    INEXISTENTE = 'inexistente'     # feriado forense/recesso: NUNCA mais tentar
    FALHA = 'falha'
    FORA_DA_JANELA = 'fora_janela'  # período coberto por outra porta (DJEN)
    #: HAVIA publicação e NENHUMA é aproveitável (era pré-CNJ do TJSP: 16.952
    #: blocos reais, zero com número CNJ). Terminal como o `inexistente`, mas
    #: separado dele e do `vazia` de propósito — sem esse status, uma edição
    #: inteira descartada aparecia como "não havia nada", que é falso e
    #: invisível. Ver `diarios.base.UnidadeSemDadoAproveitavel`.
    SEM_APROVEITAMENTO = 'sem_aproveit'
    STATUS_CHOICES = [
        (PENDENTE, 'Pendente'), (OK, 'Coletada'), (VAZIA, 'Vazia'),
        (INEXISTENTE, 'Inexistente'), (FALHA, 'Falha'), (FORA_DA_JANELA, 'Fora da janela'),
        (SEM_APROVEITAMENTO, 'Sem dado aproveitável'),
    ]

    fonte = models.CharField(max_length=16)      # slug do coletor ('tjsp-dje', 'dejt', ...)
    chave = models.CharField(max_length=120)     # id determinístico da unidade NA fonte
    data = models.DateField()
    tribunal = models.ForeignKey(
        'tribunals.Tribunal', on_delete=models.PROTECT, null=True, blank=True,
        related_name='edicoes_diario',
    )
    rotulo = models.CharField(max_length=200, blank=True)
    #: o que o coletor precisa pra baixar esta unidade meses depois, sem
    #: re-catalogar a fonte (cdCaderno, nuDiario, edição, ids do POST...).
    meta = models.JSONField(default=dict, blank=True)

    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=PENDENTE)
    #: gabarito declarado pela PRÓPRIA fonte, quando existe (o DEJT declara
    #: "1 até 20 de 16.717"). É o alvo mecânico do segmentador.
    itens_esperados = models.IntegerField(null=True, blank=True)
    #: quantas linhas desta unidade estão no banco depois da última coleta
    #: (novas + as que já lá estavam). NÃO é "quantas nasceram nesta execução":
    #: essa semântica fazia o número ZERAR ao reprocessar uma edição — a
    #: dashboard mostrava `itens_gravados=0` para uma edição com 31 mil linhas.
    itens_gravados = models.IntegerField(default=0)
    itens_duplicados = models.IntegerField(default=0)
    bytes_baixados = models.BigIntegerField(default=0)
    tentativas = models.PositiveIntegerField(default=0)
    ultimo_erro = models.TextField(blank=True)
    coletado_em = models.DateTimeField(null=True, blank=True)
    ingestion_run = models.ForeignKey(
        'tribunals.IngestionRun', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='edicoes',
    )
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=['fonte', 'chave'], name='uniq_edicao_fonte_chave'),
        ]
        indexes = [
            models.Index(fields=['fonte', 'status', 'data']),
            models.Index(fields=['fonte', '-data']),
            models.Index(fields=['tribunal', '-data']),
        ]
        ordering = ['-data', 'chave']

    def __str__(self):
        return f'{self.fonte}/{self.chave}'

    def como_unidade(self):
        """Reconstrói a `UnidadeColeta` — é o que permite reprocessar uma
        unidade sem re-catalogar a fonte inteira."""
        from .base import UnidadeColeta
        return UnidadeColeta(
            chave=self.chave, data=self.data, tribunal_sigla=self.tribunal_id,
            rotulo=self.rotulo, meta=self.meta or {},
        )

    def marcar(self, status: str, *, itens_gravados: int = 0, itens_duplicados: int = 0,
               itens_esperados: int | None = None, erro: str = '',
               ingestion_run=None, contar_tentativa: bool = True) -> None:
        """Fecha (ou reabre) o watermark desta unidade num único UPDATE."""
        campos = ['status', 'ultimo_erro']
        self.status = status
        self.ultimo_erro = erro or ''
        if contar_tentativa:
            self.tentativas = (self.tentativas or 0) + 1
            campos.append('tentativas')
        if status in (self.OK, self.VAZIA):
            self.itens_gravados = itens_gravados
            self.itens_duplicados = itens_duplicados
            self.coletado_em = timezone.now()
            campos += ['itens_gravados', 'itens_duplicados', 'coletado_em']
        if itens_esperados is not None:
            self.itens_esperados = itens_esperados
            campos.append('itens_esperados')
        if ingestion_run is not None:
            self.ingestion_run = ingestion_run
            campos.append('ingestion_run')
        self.save(update_fields=campos)
