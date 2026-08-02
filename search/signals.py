"""Signals de indexação Elasticsearch (write-through)."""
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

import django_rq

from tribunals.models import Movimentacao, Process

# Campos que, se mudaram, justificam reindexar.
MOV_TRACKED = frozenset({
    'texto', 'tipo_comunicacao', 'nome_orgao', 'nome_classe', 'codigo_classe',
    'link', 'ativo', 'assunto_norm', 'data_disponibilizacao',
})


@receiver(post_save, sender=Movimentacao)
def mov_post_save(sender, instance, created, update_fields, **kwargs):
    if not _should_index_mov(instance, update_fields):
        return
    q = django_rq.get_queue('es_index')
    q.enqueue('search.jobs.indexar_movimentacao', instance.pk)


@receiver(post_delete, sender=Movimentacao)
def mov_post_delete(sender, instance, **kwargs):
    q = django_rq.get_queue('es_index')
    q.enqueue('search.jobs.desindexar_movimentacao', instance.id)


@receiver(post_save, sender=Process)
def proc_post_save(sender, instance, created, update_fields, **kwargs):
    q = django_rq.get_queue('es_index')
    q.enqueue('search.jobs.indexar_processo', instance.pk)


@receiver(post_delete, sender=Process)
def proc_post_delete(sender, instance, **kwargs):
    q = django_rq.get_queue('es_index')
    q.enqueue('search.jobs.desindexar_processo', instance.id)


def _should_index_mov(instance, update_fields) -> bool:
    """No bulk_create, update_fields=None → indexar. No save com update_fields,
    indexar só se algum campo tracked mudou."""
    if update_fields is None:
        return True
    return bool(MOV_TRACKED & set(update_fields))