# Instalar no macOS — Camada Local de Pré-processamento v1.5

Trilha **validada** (testes V1–V6 aprovados). Tempo típico: 15–30 min, quase tudo download.

Você pode seguir por dois caminhos — escolha um:

- **A · Claude Code faz** — cole o prompt de `../PROMPT-INSTALACAO-CLAUDE-CODE.md` numa
  sessão do `claude` aberta na pasta do pacote. Ele lê o manual, mostra cada comando antes
  de rodar e pede sua aprovação. É o caminho original do projeto.
- **B · Script idempotente** — `instalar-camada-macos.sh`, desta pasta. Menos interação,
  mesmo resultado. É o caminho descrito abaixo.

Em ambos, o **Homebrew é pré-requisito manual** (§1) e as **Quick Actions do Finder** são a
única etapa que não dá para automatizar (§6).

---

## 1 · Homebrew (pré-requisito manual — pede senha)

O instalador do Homebrew pede a senha de administrador de forma interativa. Nem o Claude
Code nem o script conseguem digitá-la: é você quem faz este passo.

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Depois ponha o `brew` no PATH — **Apple Silicon** (M1/M2/M3/M4):
```bash
(echo; echo 'eval "$(/opt/homebrew/bin/brew shellenv)"') >> ~/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv)"
```
**Intel:**
```bash
(echo; echo 'eval "$(/usr/local/bin/brew shellenv)"') >> ~/.zprofile
eval "$(/usr/local/bin/brew shellenv)"
```

Confirme antes de seguir:
```bash
brew --version && brew --prefix
```

> **Claude Code** também é pré-requisito (é ele que executa a camada). Se ainda não tiver:
> `curl -fsSL https://claude.ai/install.sh | bash`, depois `claude --version` e `claude doctor`.
> Requer plano Pro, Max, Team ou Enterprise.

---

## 2 · Diagnóstico (não altera nada)

```bash
cd macos
zsh instalar-camada-macos.sh --checar
```

Lista o que existe e o que falta, com o caminho de cada binário. Rode isso primeiro.

---

## 3 · Dependências

Deixe o script instalar:

```bash
zsh instalar-camada-macos.sh --com-deps
```

O que ele instala:

| Pacote | Para quê |
|---|---|
| `poppler` | `pdfinfo` (conta páginas), `pdftoppm` |
| `ocrmypdf` + `tesseract` | OCR de PDF escaneado |
| `ffmpeg` | áudio/vídeo e redimensionamento de imagem |
| `whisper-cpp` | transcrição (`whisper-cli`) |
| `pipx` | isola o MarkItDown num venv próprio |
| `coreutils` | `gtimeout` — a trava de tempo do OCR no hook |
| `qpdf` | partição de PDF gigante |
| `calibre` (cask) | `ebook-convert`: ebooks não-EPUB → EPUB |
| `markitdown` (pipx) | o conversor principal |
| `por.traineddata`, `spa.traineddata` | idiomas do OCR |

> **Nunca use `markitdown[all]`.** No Python 3.14, `xlrd` e `youtube-transcript-api` derrubam
> a instalação e o pip recua para a 0.0.2 (que não converte EPUB). O pacote usa extras alvo:
> `markitdown[pdf,docx,pptx,xlsx,outlook]>=0.1.7`.

**Modelo do Whisper (~481 MB)** não vem por padrão. Para baixar:
```bash
zsh instalar-camada-macos.sh --com-deps --com-modelo-whisper
```
Sem ele, tudo funciona menos o `transcrever`.

> **Python:** o `brew install python@3.12` entra no prefixo do Homebrew e **não toca** no
> `/usr/bin/python3` do sistema. Isolamento duplo: prefixo próprio + venv do pipx.

---

## 4 · Instalar a camada

```bash
zsh instalar-camada-macos.sh
source ~/.zshrc
```

O que isso faz:

1. **Peças em `~/.claude/`** (com backup `.bak.<timestamp>` do que já existir):
   - `hooks/markitdown-read.sh` — o hook `PreToolUse(Read)`, fail-open
   - `bin/provenance.sh` — cabeçalho de proveniência
   - `bin/converter-md.sh` e `bin/converter-md-ocr.sh` — lógica das Quick Actions
2. **Registra o hook** em `~/.claude/settings.json` por **merge** (preserva o que houver;
   backup antes; não duplica se já estiver lá).
3. **Acrescenta o bloco** `# >>> CLAUDE-MARKITDOWN >>>` … `<<<` ao `~/.zshrc` com as funções
   `md`, `ocr`, `transcrever`, `imgredux` e `provmd`. Se o bloco já existir, é substituído
   pelo canônico (com backup).

Reexecutar é seguro: só mexe no que está diferente.

---

## 5 · Verificar

```bash
cd ../testes
zsh gerar-amostras.sh
```
E siga `TESTES-VERIFICACAO.md` (V1 a V9). O teste que mais importa é o **V7** — o hook em
ação numa sessão real do Claude Code.

---

## 6 · Quick Actions do Finder (etapa manual)

Os dois scripts de lógica já estão em `~/.claude/bin/`. Falta só o invólucro que aparece no
clique-direito. **Precisa ser criado dentro do Automator**: o LaunchServices rejeita bundles
`.workflow` criados ou editados por fora (erro −10811, a ação simplesmente desaparece do
menu).

Para cada uma das duas ações:

1. Automator → **Novo** → **Ação Rápida**
2. *O fluxo de trabalho recebe*: **arquivos e pastas** · *em*: **Finder**
3. Arraste **Executar Script do Shell** · *Shell*: `/bin/zsh` · *Passar entrada*: **como argumentos**
4. Conteúdo — uma linha só:

   | Ação | Linha | Salvar como |
   |---|---|---|
   | Converter | `"$HOME/.claude/bin/converter-md.sh" "$@"` | `Converter para Markdown` |
   | Converter com OCR | `"$HOME/.claude/bin/converter-md-ocr.sh" "$@"` | `Converter para Markdown (OCR)` |

Na primeira execução o macOS pede permissão de acesso a pastas (TCC) — aceite. A lógica
fica nos scripts, então você pode editá-los à vontade sem tocar no bundle nunca mais.

Logs: `~/Library/Logs/converter-markdown.log` e `~/Library/Logs/converter-markdown-ocr.log`.

---

## 7 · Manutenção

- **⚠︎ `brew upgrade tesseract` apaga os idiomas extras.** O `tessdata` fica dentro do
  Cellar, então o upgrade remove `por.traineddata` e `spa.traineddata` — e o OCR em
  português volta vazio. É esperado. Rebaixe depois de cada upgrade:
  ```bash
  TESSDATA="$(brew --prefix)/share/tessdata"
  curl -fsSL -o "$TESSDATA/por.traineddata" https://github.com/tesseract-ocr/tessdata_fast/raw/main/por.traineddata
  curl -fsSL -o "$TESSDATA/spa.traineddata" https://github.com/tesseract-ocr/tessdata_fast/raw/main/spa.traineddata
  tesseract --list-langs   # esperado: eng, osd, por, snum, spa
  ```
- **Cache do hook:** `~/.cache/claude-markitdown/` (chave `sha1(caminho)-mtime`). Pode
  apagar à vontade; é recriado sob demanda.
- **Desativar o hook temporariamente:**
  ```bash
  mv ~/.claude/hooks/markitdown-read.sh ~/.claude/hooks/markitdown-read.sh.off
  ```
- **Remover tudo:** ver `DESINSTALAR-macOS.md`.

---

## Parâmetros (para conferência)

| Parâmetro | Valor |
|---|---|
| Limiar de tamanho do hook | 5 MB |
| OCR automático no hook | ≤ 10 páginas, timeout 60 s |
| Timeout do hook | 90 s |
| Imagem reduzida a | 1568 px (lado maior) |
| Idiomas do OCR | `por+eng` (`spa` disponível) |
| Modelo do Whisper | `ggml-small.bin` (~481 MB) |
| Cache | `~/.cache/claude-markitdown/` |
