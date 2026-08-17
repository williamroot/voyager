import importlib
import logging
import pkgutil

from django.apps import AppConfig

logger = logging.getLogger('voyager.diarios')


class DiariosConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'diarios'
    verbose_name = 'Diários oficiais (fontes além do DJEN)'

    def ready(self):
        """Auto-descobre os coletores em `diarios/fontes/*/coletor.py`.

        Existe para que NÃO haja arquivo central de registro: quatro fontes
        sendo implementadas em paralelo não podem disputar a mesma lista — o
        conflito de merge mata a operação. Cada fonte só cria o seu diretório;
        o import é que registra (`@registrar` em `diarios/base.py`).

        Falha de import de UMA fonte não pode derrubar o processo inteiro
        (web/worker sobem juntos): loga e segue.
        """
        try:
            fontes = importlib.import_module('diarios.fontes')
        except ModuleNotFoundError:
            return
        for mod in pkgutil.iter_modules(fontes.__path__):
            if not mod.ispkg:
                continue
            nome = f'diarios.fontes.{mod.name}.coletor'
            try:
                importlib.import_module(nome)
            except Exception:
                logger.exception('falha ao carregar coletor de diário: %s', nome)
