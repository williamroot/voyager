"""App dos diários oficiais de ENTES DEVEDORES (Executivo estadual/municipal).

App separado de `diarios/` porque tem MODEL PRÓPRIO (`PublicacaoOficial`) — a
razão está na docstring de `models.py`: publicação do Executivo não tem
tribunal, e `Movimentacao.tribunal` é FK NOT NULL.

O contrato do coletor continua sendo o de `diarios/base.py` (mesmo runner,
mesmo watermark `EdicaoDiario`, mesmo `diarios_coletar`). A auto-descoberta de
lá varre `diarios/fontes/*/coletor.py` e não alcança este app, então o import
dos coletores acontece aqui — sem lista central, pelo mesmo motivo: nenhum
implementador precisa editar arquivo de outro.
"""

import importlib
import logging
import pkgutil

from django.apps import AppConfig

logger = logging.getLogger('voyager.diarios_entes')


class DiariosEntesConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'diarios_entes'
    verbose_name = 'Diários oficiais de entes devedores'

    def ready(self):
        """Registra os coletores de `diarios_entes/fontes/*.py`.

        Falha de import de UMA fonte não pode derrubar web+worker: loga e segue
        (mesma política do `diarios/apps.py`).
        """
        try:
            fontes = importlib.import_module('diarios_entes.fontes')
        except ModuleNotFoundError:
            return
        for mod in pkgutil.iter_modules(fontes.__path__):
            nome = f'diarios_entes.fontes.{mod.name}'
            try:
                importlib.import_module(nome)
            except Exception:
                logger.exception('falha ao carregar coletor de diário de ente: %s', nome)
