"""Serializer helper: monta _source no formato Jusbrasil a partir do ORM."""
from rest_framework import serializers

from tribunals.models import Movimentacao, ProcessoParte


class DiarioDocSerializer(serializers.Serializer):
    """Serializa uma Movimentacao no formato _source do Jusbrasil/Digesto."""

    def to_representation(self, instance: Movimentacao):
        proc = instance.processo
        # Serializa advs e partes.
        pps = ProcessoParte.objects.filter(processo=proc).select_related('parte')
        advs = []
        partes = []
        for pp in pps:
            if pp.parte.tipo == 'advogado' or 'ADVOGADO' in (pp.papel or ''):
                nome = pp.parte.nome
                if pp.parte.oab:
                    nome = f'{nome} (OAB {pp.parte.oab})'
                advs.append(nome)
            partes.append(pp.parte.nome)

        from pdf_storage.cached_docurl import cached_docurl_for

        return {
            'id': instance.id,
            'recorte_id': instance.id,
            'tribunal': instance.tribunal_id,
            'source': None,  # populado pela FonteDiario se existir
            'publish_date': instance.data_disponibilizacao.isoformat() if instance.data_disponibilizacao else None,
            'available_at': instance.inserido_em.isoformat() if instance.inserido_em else None,
            'detected_at': instance.inserido_em.isoformat() if instance.inserido_em else None,
            'body': instance.texto,
            'docurl': instance.link,
            'cached_docurl': cached_docurl_for(instance),
            'proc': proc.numero_cnj,
            'proc_alt': None,
            'proc_apens': None,
            'advs': ', '.join(advs),
            'partes': ', '.join(partes),
            'assunto': proc.assunto_nome or '',
            'assunto_norm': instance.assunto_norm or [],
            'processo_id': proc.id,
            'classe_nome': instance.nome_classe or proc.classe_nome or '',
            'secao_diario': instance.nome_orgao,
            'ativo': instance.ativo,
            'tipo_comunicacao': instance.tipo_comunicacao,
            'nome_orgao': instance.nome_orgao,
        }