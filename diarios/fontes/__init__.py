"""Uma fonte por subpacote — `diarios/fontes/<slug>/coletor.py`.

Cada implementador é DONO do seu diretório e não encosta no de outra fonte:
são quatro implementações em paralelo, e conflito de merge mata a operação.
Não existe lista central de registro de propósito — `diarios/apps.py` importa
todo subpacote daqui na subida e o `@registrar` de `diarios/base.py` faz o
resto. Acrescentar uma fonte = criar um diretório.
"""
