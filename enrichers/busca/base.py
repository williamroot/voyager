"""Contrato da busca por parte. Sem I/O, sem Django — só o vocabulário.

Três decisões moram aqui, e todas vêm do recon de 04/09/2026
(`.ia/ENRICHMENT.md` §"Busca POR PARTE"):

1. **`total_declarado` e `teto_da_fonte` são campos separados.** O e-SAJ diz
   "1000 Processos encontrados" quando há mais de mil, e o PJe diz "30" quando
   há mais de trinta. Guardar só o número faria a resposta afirmar um total que
   é, na verdade, um piso. Quem lê precisa saber qual dos dois recebeu.

2. **Página é gerada, nunca acumulada.** `paginar()` é um generator: o
   consumidor decide quando parar e o coletor nunca segura o resultado inteiro
   na memória (regra nº 1 do CLAUDE.md).

3. **"Não achei" tem mais de um sabor, e eles não se misturam.** Lista vazia,
   "refine sua busca", "critério que esta fonte não tem" e "a fonte não
   respondeu" são quatro respostas diferentes para o usuário; unificar tudo em
   `[]` produz o "essa pessoa não tem processo" que este projeto inteiro existe
   para não dizer.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

# Critérios do catálogo. São os que a tela do JURISCOPE oferece hoje
# (`datamodel/search_methods.py`) mais o nome do advogado, que o e-SAJ e o PJe
# oferecem no mesmo formulário. O número CNJ NÃO entra: para ele já existe
# caminho próprio (`datajud.hidratacao.hidratar_cnj`).
DOCUMENTO = 'documento'
NOME = 'nome'
OAB = 'oab'
ADVOGADO = 'advogado'

CRITERIOS = (DOCUMENTO, NOME, OAB, ADVOGADO)

ROTULOS = {
    DOCUMENTO: 'CPF/CNPJ da parte',
    NOME: 'nome da parte',
    OAB: 'OAB do advogado',
    ADVOGADO: 'nome do advogado',
}


class BuscaError(Exception):
    """Raiz das falhas da busca por parte."""


class CriterioIndisponivel(BuscaError):
    """A fonte pública deste tribunal não oferece este critério.

    É resposta, não erro: a tela diz "o TRF5 não permite buscar por nome de
    advogado" em vez de mostrar zero resultados como se não houvesse processo.
    """

    def __init__(self, tribunal: str, criterio: str):
        self.tribunal, self.criterio = tribunal, criterio
        super().__init__(
            f'{tribunal} não oferece busca por {ROTULOS.get(criterio, criterio)} '
            f'na consulta pública')


class RefinarBusca(BuscaError):
    """A fonte se recusou a responder porque a busca é ampla demais.

    O e-SAJ responde isto ("Foram encontrados muitos processos ... refine sua
    busca") em vez de listar. NÃO é "nenhum resultado" — é "pergunte melhor".
    """


class FonteIndisponivel(BuscaError):
    """A fonte não respondeu, respondeu outra coisa, ou está atrás de um muro.

    Sempre RE-TENTÁVEL. Inclui WAF, captcha, 5xx, timeout e a página que não é
    o formulário que conhecemos — o TRF5 já serviu uma consulta pública antiga,
    com captcha de imagem, na mesma URL. Nada disso pode virar "não achei".
    """


@dataclass(frozen=True)
class ItemEncontrado:
    """Uma linha do resultado. É de propósito RASO.

    A busca entrega o número do processo e o pouco de contexto que a lista já
    mostra (para a tela poder exibir algo antes do enriquecimento). Partes,
    valor, movimentos e documentos são trabalho do enricher — duplicar aquele
    parser aqui seria criar um segundo lugar para o mesmo dado divergir.
    """

    numero_cnj: str
    tribunal: str
    classe: str = ''
    assunto: str = ''
    orgao: str = ''
    distribuicao: str = ''
    url_fonte: str = ''
    #: Nomes que a lista já exibe (o PJe mostra "ATIVO X PASSIVO"). Só para a
    #: tela — não vira `Parte` no banco.
    partes_na_lista: tuple[str, ...] = ()


@dataclass
class PaginaResultado:
    itens: list[ItemEncontrado] = field(default_factory=list)
    pagina: int = 1
    #: O que a FONTE declara ter. `None` = ela não diz.
    total_declarado: int | None = None
    #: `True` quando o `total_declarado` é o teto da fonte, e não o total real
    #: (e-SAJ trava em 1.000, PJe em 30). Quem monta a resposta usa isto para
    #: dizer "há mais que não são alcançáveis por este critério".
    total_e_teto: bool = False
    tem_proxima: bool = False
    #: Anomalia observada NA FONTE nesta página, em português, para subir até a
    #: resposta da API. Existe porque "o número que a fonte publica não bate com
    #: o que ela mostrou" é informação do usuário, não detalhe de parser: no
    #: TRF5 o rodapé anuncia 30 resultados e a tabela traz 1.
    aviso_fonte: str = ''


class BuscaPorParte:
    """Um motor de busca por parte (e-SAJ, PJe, REST próprio)."""

    #: Critérios que ESTA fonte aceita. O que não estiver aqui vira
    #: `CriterioIndisponivel` — recusa declarada, nunca lista vazia.
    CRITERIOS_SUPORTADOS: frozenset[str] = frozenset()

    #: Quantos resultados a fonte devolve, no máximo, por consulta. `None` =
    #: sem teto observado (os dois REST). Medido, não presumido.
    TETO_DA_FONTE: int | None = None

    #: Quantos itens cabem numa página da fonte.
    POR_PAGINA: int = 0

    TRIBUNAL: str = ''

    def suporta(self, criterio: str) -> bool:
        return criterio in self.CRITERIOS_SUPORTADOS

    def exigir_suporte(self, criterio: str) -> None:
        if not self.suporta(criterio):
            raise CriterioIndisponivel(self.TRIBUNAL, criterio)

    def paginar(self, criterio: str, valor: str,
                teto_paginas: int = 40) -> Iterator[PaginaResultado]:
        """Páginas de resultado, uma a uma, até ACABAR — ou bater o teto.

        O default é 40 porque é o que esgota a fonte mais generosa (o e-SAJ:
        25 por página, 1.000 no total). Nosso teto nunca deve ser MENOR que o
        da fonte: em 04/09/2026 o de 10 páginas colhia 250 de 823 processos e
        chamava isso de "truncado", como se o corte fosse dela.

        Atingir `teto_paginas` NÃO é o fim silencioso da iteração: quem chama
        compara o que colheu com `total_declarado` e registra o truncamento
        (regra nº 2 — teto é alerta, nunca corte mudo).
        """
        raise NotImplementedError
