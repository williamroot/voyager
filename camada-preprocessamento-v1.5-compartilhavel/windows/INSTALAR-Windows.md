# Instalar no Windows — Camada Local de Pré-processamento v1.5

> ### ⚠︎ Status: REFERÊNCIA A VALIDAR
> A trilha **macOS é validada**; esta é o **porte fiel da mesma lógica**, ainda não rodado de
> ponta a ponta em produção. O formato do hook (o JSON em `settings.json`) é idêntico; o que
> muda é a mecânica: instalação por `winget`/`pip`, hook em PowerShell, funções no `$PROFILE`
> e menu "Enviar para" em vez das Quick Actions do Finder.
>
> **Teste com as amostras de `..\testes\` antes de usar em material sério** — e anote o que
> precisou ajustar.
>
> **O payload desta pasta está à frente do manual.** O manual descreve a trilha Windows na
> v1.4 e deixa o ramo de ebook da v1.5 como pendência ("porte o mesmo ramo"). O
> `payload/markitdown-read.ps1` e o `payload/profile-CLAUDE-MARKITDOWN.ps1` deste pacote **já
> trazem esse porte** (ebook não-EPUB → `.epub` via `ebook-convert`, mesmo padrão do macOS),
> ainda não validado. Havendo divergência com o HTML nesse ponto específico, o payload é o
> mais novo; no resto, o manual manda.

Requisitos: Windows 10 1809+ ou 11 · PowerShell · Claude Code instalado e autenticado ·
Python 3.10+ · Git for Windows (recomendado — habilita o shell Bash do Claude Code).

Se o PowerShell bloquear scripts, rode uma vez:
```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

---

## 1 · Diagnóstico (não altera nada)

```powershell
cd windows
.\instalar-camada-windows.ps1 -Checar
```

---

## 2 · Dependências

```powershell
.\instalar-camada-windows.ps1 -ComDeps
```

Instala via `winget`: Python 3.12, ffmpeg (Gyan), Tesseract (UB-Mannheim), Ghostscript e
Calibre. Depois, via `pip`:

```powershell
pip install "markitdown[pdf,docx,pptx,xlsx,outlook]>=0.1.7" ocrmypdf openai-whisper
```

> **Nunca use `markitdown[all]`**: no Python 3.14, `xlrd` e `youtube-transcript-api` derrubam
> a instalação e o pip recua para a 0.0.2 (que não converte EPUB).

> No Windows a transcrição usa **`openai-whisper`** (pip), não o `whisper.cpp` — compilar o
> whisper.cpp aqui dá muito mais trabalho. O modelo `small` é baixado sozinho no primeiro uso.

### Duas pendências manuais

1. **Poppler** (fornece o `pdfinfo`, que o hook usa para contar páginas): baixe os binários
   do Poppler para Windows, extraia e acrescente a pasta `...\poppler\bin` ao **PATH** do
   sistema. Sem `pdfinfo`, o ramo de OCR do hook não decide e cai no fail-open.
2. **Idiomas do Tesseract:** no instalador do UB-Mannheim marque **Portuguese** e
   **Spanish**. Alternativa: copiar `por.traineddata` e `spa.traineddata` para
   `C:\Program Files\Tesseract-OCR\tessdata\`. Confira com:
   ```powershell
   tesseract --list-langs
   ```
   Sem `por`, o OCR em português volta vazio.

---

## 3 · Instalar a camada

```powershell
.\instalar-camada-windows.ps1
. $PROFILE
```

O que isso faz:

1. **Hook** em `%USERPROFILE%\.claude\hooks\markitdown-read.ps1` (backup do que existir).
2. **Registra o hook** em `%USERPROFILE%\.claude\settings.json` por **merge**:
   ```json
   {
     "hooks": {
       "PreToolUse": [
         { "matcher": "Read",
           "hooks": [ { "type": "command",
                        "command": "powershell -NoProfile -ExecutionPolicy Bypass -File \"%USERPROFILE%\\.claude\\hooks\\markitdown-read.ps1\"",
                        "timeout": 90 } ] }
       ]
     }
   }
   ```
3. **Funções** no `$PROFILE`, entre `# >>> CLAUDE-MARKITDOWN >>>` e `# <<< CLAUDE-MARKITDOWN <<<`:
   `md`, `ocr`, `transcrever`, `imgredux` e `provmd`.
4. **Menu de contexto:** copia `converter-md.cmd` para a pasta *Enviar para*
   (`Win+R` → `shell:sendto`). Uso: clique-direito num PDF/DOCX → **Enviar para** →
   `converter-md`. O `.md` sai ao lado do original.

Reexecutar é seguro (idempotente, com backup antes de tocar em arquivo existente).

> Para um item de clique-direito de primeiro nível (sem passar por "Enviar para"), o
> PowerToys ou uma entrada no Registro dão um resultado mais elegante — a validar.

---

## 4 · Verificar

```powershell
cd ..\testes
.\gerar-amostras.ps1
```
E siga `TESTES-VERIFICACAO.md` (V1 a V9). O teste que mais importa é o **V7** — o hook em
ação numa sessão real do Claude Code.

---

## 5 · Diferenças conhecidas em relação ao macOS

| Item | macOS | Windows |
|---|---|---|
| Transcrição | `whisper-cpp` (`whisper-cli`) + modelo baixado à mão | `openai-whisper` (pip), modelo automático |
| Trava de tempo do OCR no hook | `gtimeout 60` (coreutils) | **sem trava** — um escaneado ruim pode demorar |
| `.doc` (Word antigo) | `textutil -convert docx` (nativo) | só se houver LibreOffice (`soffice`) no PATH |
| Menu de contexto | Quick Actions do Automator (2 ações, com OCR) | "Enviar para" (uma ação, sem OCR) |
| Proveniência | `provenance.sh` (zsh) | função `provmd` (porte PowerShell) |
| Ebooks não-EPUB | Calibre via Homebrew cask | Calibre via winget — garanta `ebook-convert.exe` no PATH |

**Pontos a conferir no primeiro deploy** (é onde o porte tende a falhar):

- `provmd`: codificação **UTF-8** do cabeçalho e escape de crase no PowerShell.
- Caminho do hook no `settings.json`: se `%USERPROFILE%` não expandir, troque por caminho
  absoluto (`C:\Users\SEU_USUARIO\.claude\hooks\markitdown-read.ps1`).
- `pdfinfo` no PATH (Poppler) — sem ele o ramo de OCR não decide.
- Ebook não-EPUB: confirme que `ebook-convert` responde no PowerShell.

---

## 6 · Remover

Ver `DESINSTALAR-Windows.md`.
