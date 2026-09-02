"""SEGUNDO EIXO DO GATE: inventário do que a FONTE imprime, por marcador.

POR QUE O PRIMEIRO EIXO NÃO BASTA (e não é opinião — é medição)
---------------------------------------------------------------
O eixo que já existe mede **proporção**: dos CNJs impressos no caderno, quantos
caíram dentro de algum bloco. Ele reprova abaixo de 95%. Isso funciona, e foi
ele que pegou as doze edições de 02/09/2026 — mas ele é **estruturalmente cego
para a perda pequena**, e a cegueira foi medida:

  · a relação da DEPRE varia de tamanho. 3.833 registros derrubaram a cobertura
    a 68,4%; 2.568 a 83,1%. Uma de ~760 fecharia **acima de 95%** e passaria
    calada — com a relação inteira perdida.
  · a pauta numerada do caderno 19 passou calada em **22 de 22** edições
    verdes: 7.917 entradas, todas entre 0,60% e 4,54% dos CNJs. As seis que
    reprovaram estavam entre 7,3% e 7,4%. A separação é monotônica em 35
    edições — o que confirma a régua E confirma que ela não enxerga nada
    abaixo do próprio piso.
  · e duas edições **nunca coletadas** teriam reprovado (6,22% e 6,45%). Ou
    seja: o acaso de QUANDO coletamos decidia se veríamos ou não.

**Gate de proporção não é gate de completude.** Ele reprova a perda grande e
absolve a pequena pelo MESMO mecanismo.

O QUE ESTE EIXO MEDE
--------------------
Duas pernas, e a segunda existe porque a primeira sozinha teria a mesma
cegueira num formato que ninguém conhece ainda.

**Perna A — inventário por marcador (formato CONHECIDO, em qualquer tamanho).**
A fonte carrega a própria régua quando imprime um registro com uma linha de
abertura reconhecível. Conta-se essa linha no TEXTO EXTRAÍDO e compara-se com
quantos blocos daquele formato o segmentador produziu. A relação é `>=`, não
`==`, porque um formato de bloco pode receber registros de mais de um
marcador. Se o segmentador produziu MENOS blocos do que a fonte imprimiu
registros, é ERRO com os dois números — **independente de quanto isso
representa em CNJ**. É esta perna que teria pego a DEPRE no primeiro dia:
2.568 registros impressos contra 0 blocos.

O precedente que mostra que isso funciona: na relação de 10/03/2025, quatro
marcadores independentes do MESMO registro (`Nº de ordem cronológica`,
`Processo:`, `Processo de origem:`, `Entidade devedora:`) deram **2.568 cada**,
e o segmentador produziu 2.568 blocos. Quatro contagens que não se conversam,
o mesmo número.

**Perna B — assinatura dos órfãos (formato DESCONHECIDO).** Perna A só conhece
o que já foi declarado. Um formato novo não tem marcador — e é aí que a perna
A herdaria a cegueira. Então: os CNJs que ficaram FORA de bloco são agrupados
pela **forma da linha** em que aparecem (dígitos viram `#`), e uma forma que se
repete acima de um piso é ERRO que **nomeia o suspeito**.

Isto não é heurística inventada: é exatamente o que foi feito à mão em
02/09/2026 para descobrir os dois formatos. Agrupando as linhas dos 6.170 CNJs
órfãos de `4155-11`, **100%** caíram em duas formas (`Processo: #…` e
`Processo de origem: #…`); nos 1.212 de `4153-19`, **96,5%** numa só
(`## - #######-##.####…`). Um formato é, por definição, repetitivo — e é essa
repetição que o denuncia mesmo sem marcador.

O QUE ELE **NÃO** COBRE, escrito antes de alguém descobrir
----------------------------------------------------------
Formato desconhecido, pequeno E **cujos CNJs já caem dentro de outro bloco**.
Se as linhas de um formato novo forem engolidas por um bloco vizinho em vez de
ficarem órfãs, os CNJs contam como cobertos, não há órfão para agrupar, e a
perna B não vê nada. A perna A também não, porque não há marcador declarado.
Esse resíduo continua descoberto pelos dois eixos, e a única defesa contra ele
é a que sempre foi: alguém olhar um caderno de vez em quando.

INDEPENDÊNCIA DO SEGMENTADOR — a condição que faz isto valer
-------------------------------------------------------------
A contagem da perna A é feita sobre `Pagina.linhas` (o texto extraído do PDF) e
a comparação é contra `Bloco.formato` (a saída do segmentador). São dois
caminhos de código diferentes sobre a mesma entrada. Contar o que o parser
produziu e comparar consigo mesmo seria circular — o erro que a régua do nicho
12078 quase cometeu ao usar `codigo_classe` como prova de si mesmo.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

#: Teto de CNJs guardados para a perna B. Guardamos `cnj -> assinatura` (uma
#: string curta) enquanto as páginas passam, porque só no fim se sabe quem
#: ficou órfão. Medido: o maior caderno conhecido (`4127-19`, 7.371 páginas)
#: tem 33.141 CNJs ⇒ ~2 MB. O teto existe pela regra nº 2 da casa: atingi-lo é
#: ERRO registrado, nunca um `return` discreto — e a perna B se declara
#: PARCIAL em vez de mentir que não achou nada.
TETO_ASSINATURAS = 200_000

#: Quantos CNJs órfãos com a MESMA forma de linha bastam para chamar de
#: formato. Abaixo disso é ruído — citação de jurisprudência, CNJ partido entre
#: linhas. Medido em 02/09/2026: as famílias reais tinham 1.170 e 6.170
#: ocorrências; o resíduo legítimo tinha 10 e 7.
PISO_ASSINATURA = 30

#: `\d+` e não `\d`: RUN de dígitos vira UM `#`. Sem isso o ordinal da sessão
#: parte a família em três — `# - `, `## - `, `### - ` —, que foi exatamente o
#: que aconteceu na análise manual de `4153-19` (42 + 38 + 38 ocorrências da
#: MESMA pauta em três baldes). Formato dividido em três baldes pode cair
#: abaixo do piso e a perna B se cala.
_RE_DIGITO = re.compile(r'\d+')
_RE_ESPACO = re.compile(r'\s+')


def assinatura_de_linha(texto: str, tamanho: int = 44) -> str:
    """Forma da linha: dígitos viram `#`, espaços colapsam, corta no começo.

    O começo da linha é o que carrega o formato — é lá que ficam o rótulo
    (`Processo de origem:`) ou o ordinal da pauta (`3 - `). O corpo do despacho
    varia a cada publicação e por isso não entra.
    """
    limpo = _RE_ESPACO.sub(' ', (texto or '').strip())
    return _RE_DIGITO.sub('#', limpo)[:tamanho]


@dataclass(frozen=True)
class MarcadorRegistro:
    """Uma linha que ABRE um registro na fonte, e o formato que ela deve virar.

    `formato` é o `Bloco.formato` que o segmentador tem que produzir. Vários
    marcadores podem apontar para o mesmo formato — daí a comparação ser `>=`.
    """
    nome: str
    padrao: re.Pattern
    formato: str


@dataclass
class Divergencia:
    """Um achado do eixo. Sempre com os DOIS números, nunca um veredito solo."""
    tipo: str          # 'marcador' | 'assinatura'
    detalhe: str
    impresso: int
    segmentado: int | None = None

    def __str__(self) -> str:
        if self.tipo == 'marcador':
            return (f'a fonte imprimiu {self.impresso} registros de '
                    f'{self.detalhe} e o segmentador produziu {self.segmentado} '
                    f'blocos — {self.impresso - (self.segmentado or 0)} registros '
                    f'ficaram sem bloco')
        return (f'{self.impresso} CNJs fora de bloco com a MESMA forma de linha '
                f'{self.detalhe!r} — formato provavelmente desconhecido pelo '
                f'segmentador')


@dataclass
class Inventario:
    """O que a fonte imprimiu × o que o segmentador produziu.

    Alimentado em FLUXO: `ver_linha` enquanto as páginas passam, `ver_bloco`
    enquanto os blocos saem. Nada acumula além de contadores e da tabela de
    assinaturas (com teto).
    """
    marcadores: tuple[MarcadorRegistro, ...] = ()
    impresso: Counter = field(default_factory=Counter)
    segmentado: Counter = field(default_factory=Counter)
    #: cnj -> assinatura da linha em que ele apareceu (primeira ocorrência)
    _assinatura_por_cnj: dict = field(default_factory=dict)
    #: o teto de `TETO_ASSINATURAS` foi atingido: a perna B vira PARCIAL
    assinaturas_truncadas: bool = False

    @property
    def mede(self) -> bool:
        """Falso quando a fonte não declara marcador nenhum. Aí o eixo se
        ABSTÉM — e abstenção não é aprovação."""
        return bool(self.marcadores)

    def ver_linha(self, texto: str, cnjs) -> None:
        """Uma linha do texto EXTRAÍDO (nunca de um bloco)."""
        limpa = (texto or '').strip()
        if limpa:
            for m in self.marcadores:
                if m.padrao.match(limpa):
                    self.impresso[m.nome] += 1
                    break
        if not cnjs:
            return
        if len(self._assinatura_por_cnj) >= TETO_ASSINATURAS:
            self.assinaturas_truncadas = True
            return
        forma = assinatura_de_linha(limpa)
        for cnj in cnjs:
            self._assinatura_por_cnj.setdefault(cnj, forma)

    def ver_bloco(self, formato: str) -> None:
        self.segmentado[formato] += 1

    def total_impresso(self) -> int:
        """Gabarito declarado pela própria fonte — vai para
        `EdicaoDiario.itens_esperados`, que existe exatamente para isso."""
        return sum(self.impresso.values())

    def assinaturas_dos_orfaos(self, orfaos) -> Counter:
        """Perna B: forma de linha → quantos CNJs órfãos apareceram nela."""
        formas: Counter = Counter()
        for cnj in orfaos:
            forma = self._assinatura_por_cnj.get(cnj)
            if forma:
                formas[forma] += 1
        return formas

    def conferir(self, orfaos=(), piso_assinatura: int = PISO_ASSINATURA) -> list[Divergencia]:
        """Os dois lados, com os dois números. Lista vazia = nada a acusar.

        NÃO decide se a unidade falha — quem decide é o coletor, que é quem
        sabe distinguir "o parser não conhece o formato" de "o INSERT quebrou".
        """
        achados: list[Divergencia] = []
        for m in self.marcadores:
            impresso = self.impresso.get(m.nome, 0)
            if not impresso:
                continue
            produzido = self.segmentado.get(m.formato, 0)
            if produzido < impresso:
                achados.append(Divergencia(
                    tipo='marcador', detalhe=f'`{m.nome}` (formato `{m.formato}`)',
                    impresso=impresso, segmentado=produzido))

        for forma, n in self.assinaturas_dos_orfaos(orfaos).most_common(3):
            if n >= piso_assinatura:
                achados.append(Divergencia(tipo='assinatura', detalhe=forma, impresso=n))
        return achados

    def resumo(self) -> dict:
        """Para o log e para o `IngestionRun.erros` — número, não adjetivo."""
        return {
            'impresso': dict(self.impresso),
            'segmentado': dict(self.segmentado),
            'assinaturas_truncadas': self.assinaturas_truncadas,
        }
