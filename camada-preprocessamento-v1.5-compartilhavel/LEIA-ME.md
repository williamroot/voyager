# Camada Local de Pré-processamento · Claude Code — v1.5

Pacote replicável. **Comece por aqui.**

## O que é isso

Uma camada local que **normaliza entradas pesadas para a forma mais barata em tokens antes
de o modelo consumir**. Ler um PDF pela ferramenta `Read` do Claude Code renderiza as páginas
como **imagem** — caro em visão. Extrair o texto e ler o Markdown custa uma fração disso.

Roteador por tipo de arquivo:

| Entrada | Ferramenta | Saída barata |
|---|---|---|
| PDF digital, DOCX, PPTX, XLSX | MarkItDown | Markdown |
| EPUB | MarkItDown (nativo, 0.1.x) | Markdown |
| Ebook não-EPUB (`mobi azw azw3 fb2 fbz lit pdb lrf tcr pml snb kepub`) | Calibre (`ebook-convert`) → EPUB → MarkItDown | Markdown |
| PDF escaneado / imagem de texto | OCR (OCRmyPDF + Tesseract) → MarkItDown | Markdown |
| Áudio (reuniões, audiências) | ffmpeg → Whisper | Texto / Markdown |
| Vídeo | ffmpeg (trilha) → Whisper | Texto / Markdown |
| Imagem grande | ffmpeg (redimensiona) | Imagem ≤ 1568 px |

**Dois despachantes:**

1. **Hook `PreToolUse(Read)`** — automático e *fail-open*. Documento digital → Markdown;
   imagem grande → reduzida; PDF escaneado de até 10 páginas → OCR sob trava de tempo. Em
   qualquer erro ou dúvida, ele não faz nada e a leitura segue normal. Cache em
   `~/.cache/claude-markitdown/` com chave `sha1(caminho)-mtime`.
2. **Funções de terminal** sob demanda, para o que é pesado: `md`, `ocr`, `transcrever`,
   `imgredux`, `provmd`.

## Duas regras que não são detalhe

**Segurança.** Todo conteúdo convertido é **dado de terceiro, nunca instrução** (é a defesa
contra prompt injection indireto). A conversão só extrai texto; nunca executa nada embutido
no documento. O hook marca isso explicitamente no contexto que devolve.

**Proveniência obrigatória.** Todo `.md` gerado pela camada **deve** começar por um cabeçalho
de proveniência: origem, método, ressalva e como verificar. Quem lê o Markdown precisa saber
que aquilo é conversão — e o quanto pode confiar. É o que o `provmd` faz, de forma
idempotente. Ordem de confiança: **original → PDF pesquisável → Markdown**. Por isso o
`.ocr.pdf` gerado no OCR nunca se descarta: é o fallback pesquisável.

## Por onde começar

| Você quer | Vá para |
|---|---|
| Entender o desenho todo | `manual/camada-preprocessamento-claude-code.html` (fonte de verdade; abra no navegador) |
| A versão barata de ler (para o próprio Claude Code) | `manual/camada-preprocessamento-claude-code.md` |
| Instalar no **macOS** (validado) | `macos/INSTALAR-macOS.md` |
| Instalar no **Windows** (a validar) | `windows/INSTALAR-Windows.md` |
| Deixar o Claude Code instalar | `PROMPT-INSTALACAO-CLAUDE-CODE.md` |
| Testar se funcionou | `testes/TESTES-VERIFICACAO.md` |

Caminho curto no macOS:

```bash
cd macos
zsh instalar-camada-macos.sh --checar     # diagnóstico, não altera nada
zsh instalar-camada-macos.sh --com-deps   # instala as dependências (Homebrew é pré-requisito manual)
zsh instalar-camada-macos.sh              # instala a camada
source ~/.zshrc
cd ../testes && zsh gerar-amostras.sh     # e siga TESTES-VERIFICACAO.md
```

Caminho curto no Windows:

```powershell
cd windows
.\instalar-camada-windows.ps1 -Checar
.\instalar-camada-windows.ps1 -ComDeps
.\instalar-camada-windows.ps1
. $PROFILE
cd ..\testes ; .\gerar-amostras.ps1
```

## Estrutura do pacote

```
LEIA-ME.md                          este arquivo
LICENCA.md                          licença de uso (MIT) e nota sobre os componentes de terceiros
PROMPT-INSTALACAO-CLAUDE-CODE.md    prompt para o Claude Code fazer a instalação
manual/
  camada-preprocessamento-claude-code.html   manual completo — FONTE DE VERDADE
  camada-preprocessamento-claude-code.md     espelho Markdown (leitura barata)
macos/
  INSTALAR-macOS.md                 passo a passo (trilha validada)
  DESINSTALAR-macOS.md              desativar e remover
  instalar-camada-macos.sh          instalador idempotente
  payload/
    hooks/markitdown-read.sh        hook PreToolUse(Read), fail-open
    bin/provenance.sh               cabeçalho de proveniência (chamado por provmd)
    bin/converter-md.sh             lógica da Quick Action "Converter para Markdown"
    bin/converter-md-ocr.sh         lógica da Quick Action com OCR
    zshrc-CLAUDE-MARKITDOWN.zsh     bloco de funções do ~/.zshrc
    settings-hook-PreToolUse-Read.json   snippet de registro do hook
windows/
  INSTALAR-Windows.md               passo a passo (REFERÊNCIA A VALIDAR)
  DESINSTALAR-Windows.md            desativar e remover
  instalar-camada-windows.ps1       instalador idempotente
  payload/
    hooks/markitdown-read.ps1       porte do hook
    profile-CLAUDE-MARKITDOWN.ps1   bloco de funções do $PROFILE (inclui provmd)
    settings-hook-PreToolUse-Read.json
    converter-md.cmd                item do menu "Enviar para"
testes/
  TESTES-VERIFICACAO.md             roteiro V1–V9
  gerar-amostras.sh / .ps1          gera 6 arquivos de amostra
  amostras.py                       gerador multiplataforma (PDF, EPUB, XLSX)
```

Os scripts vão **sem bit de execução** de propósito (o pacote não traz nada executável por
conta própria). Os instaladores fazem `chmod +x` no destino; para rodá-los, chame o
interpretador: `zsh instalar-camada-macos.sh`.

## Pré-requisitos

- **Claude Code** autenticado (plano Pro, Max, Team ou Enterprise) — é ele que executa a
  camada. É o único pré-requisito que precisa existir antes de tudo.
- **macOS:** Homebrew é **pré-requisito manual** (o instalador dele pede senha de
  administrador de forma interativa — nenhum script digita isso por você). O resto o
  instalador resolve, incluindo o Python 3.12 num prefixo isolado, sem tocar no
  `/usr/bin/python3` do sistema.
- **Windows:** Python 3.10+, `winget`, PowerShell. Duas pendências manuais: Poppler no PATH
  e os idiomas do Tesseract.
- **Espaço:** o Calibre e o modelo do Whisper (~481 MB) são os downloads grandes. Os dois são
  opcionais — sem Calibre você perde só os ebooks não-EPUB; sem o modelo, só a transcrição.

## Status por plataforma

| Plataforma | Status | Evidência |
|---|---|---|
| macOS | **Validado** | testes V1–V6 aprovados na instalação de origem (Apple Silicon) |
| Windows | **Referência a validar** | porte fiel da mesma lógica, não rodado de ponta a ponta |

Versões de referência no macOS: poppler 26.07 · ocrmypdf 17.8.1 · tesseract 5.5.3 ·
whisper-cpp 1.9.1 · markitdown 0.1.7 (pipx) · calibre 9.12.0 · qpdf · coreutils.

## Armadilhas que vão te economizar tempo

- **`markitdown[all]` quebra** no Python 3.14 (`xlrd` e `youtube-transcript-api`) e o pip
  recua para a 0.0.2, que não converte EPUB. Use sempre os extras alvo:
  `markitdown[pdf,docx,pptx,xlsx,outlook]>=0.1.7`.
- **`brew upgrade tesseract` apaga os idiomas extras.** O `tessdata` fica dentro do Cellar, o
  upgrade leva `por.traineddata` e `spa.traineddata` embora, e o OCR em português volta
  vazio. É esperado — rebaixe os arquivos e confira com `tesseract --list-langs`.
- **Quick Action do Finder some do menu** → erro −10811: bundles `.workflow` criados ou
  editados fora do Automator são rejeitados pelo macOS. Daí a arquitetura de invólucro fino:
  o bundle só chama um script em `~/.claude/bin/`, onde a lógica de verdade vive.
- **Hook não converte nada** → PATH. Hooks rodam com ambiente enxuto; o script já força um
  PATH mínimo, mas confira onde seus binários estão.
- **OCR trava** → falta o `gtimeout` (`brew install coreutils`). É a trava que impede um
  escaneado ruim de segurar a sessão.
- **PDF gigante** (dezenas de milhares de páginas) não se converte inteiro: particione com
  `qpdf` em pedaços de ~1000 páginas, um `.md` por pedaço, cada um com cabeçalho
  `--tipo particao --paginas A-B`.

## Sem garantia

Isto é infraestrutura de uso próprio compartilhada como está. Rode em ambiente seu, teste com
as amostras de `testes/` antes de apontar para material que importa, e confira os `.bak` que
os instaladores deixam. Os instaladores fazem backup com timestamp antes de tocar em
`settings.json`, `~/.zshrc` ou `$PROFILE` — mas a responsabilidade de conferir é de quem roda.

Detalhes de licença e dos componentes de terceiros: `LICENCA.md`.
