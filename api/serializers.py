from rest_framework import serializers

from tribunals.models import IngestionRun, Movimentacao, Process, Tribunal


class TribunalSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tribunal
        fields = ('sigla', 'nome', 'sigla_djen', 'ativo', 'overlap_dias',
                  'data_inicio_disponivel', 'backfill_concluido_em')


class ProcessListSerializer(serializers.ModelSerializer):
    tribunal = serializers.CharField(source='tribunal_id')

    class Meta:
        model = Process
        fields = ('id', 'numero_cnj', 'tribunal', 'inserido_em',
                  'ultima_movimentacao_em', 'total_movimentacoes')


class ProcessDetailSerializer(ProcessListSerializer):
    class Meta(ProcessListSerializer.Meta):
        fields = ProcessListSerializer.Meta.fields + (
            'primeira_movimentacao_em', 'atualizado_em',
        )


class MovimentacaoListSerializer(serializers.ModelSerializer):
    tribunal = serializers.CharField(source='tribunal_id')
    numero_cnj = serializers.CharField(source='processo.numero_cnj', read_only=True)

    class Meta:
        model = Movimentacao
        fields = ('id', 'tribunal', 'numero_cnj', 'data_disponibilizacao', 'inserido_em',
                  'tipo_comunicacao', 'nome_orgao')


class MovimentacaoDetailSerializer(MovimentacaoListSerializer):
    class Meta(MovimentacaoListSerializer.Meta):
        fields = MovimentacaoListSerializer.Meta.fields + (
            'external_id', 'tipo_documento', 'nome_classe', 'codigo_classe',
            'link', 'destinatarios', 'texto',
        )


class IngestionRunSerializer(serializers.ModelSerializer):
    tribunal = serializers.CharField(source='tribunal_id')

    class Meta:
        model = IngestionRun
        fields = ('id', 'tribunal', 'status', 'started_at', 'finished_at',
                  'janela_inicio', 'janela_fim', 'paginas_lidas',
                  'movimentacoes_novas', 'movimentacoes_duplicadas',
                  'processos_novos', 'erros')
