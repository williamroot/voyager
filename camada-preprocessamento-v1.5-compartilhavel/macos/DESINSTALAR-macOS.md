# Remover a camada — macOS

## Desativar temporariamente (reversível, 1 comando)

```bash
mv ~/.claude/hooks/markitdown-read.sh ~/.claude/hooks/markitdown-read.sh.off
# reativar:
# mv ~/.claude/hooks/markitdown-read.sh.off ~/.claude/hooks/markitdown-read.sh
```

O registro em `settings.json` continua lá, mas o hook não existe mais no caminho apontado —
e como a camada é *fail-open*, a leitura simplesmente segue normal.

## Remover tudo

Rode por partes, conferindo cada bloco. Os `.bak.<timestamp>` gerados na instalação são
usados aqui para restaurar `settings.json` e `.zshrc`.

```bash
# 1) hook + registro no settings.json (restaura o backup mais recente)
rm -f ~/.claude/hooks/markitdown-read.sh
ls -t ~/.claude/settings.json.bak.* 2>/dev/null | head -1 | xargs -I{} cp {} ~/.claude/settings.json

# 2) bloco do .zshrc (restaura backup) e cache
ls -t ~/.zshrc.bak.* 2>/dev/null | head -1 | xargs -I{} cp {} ~/.zshrc
rm -rf ~/.cache/claude-markitdown

# 3) Quick Actions, scripts de lógica, logs e modelo do Whisper
rm -rf "$HOME/Library/Services/Converter para Markdown.workflow" \
       "$HOME/Library/Services/Converter para Markdown (OCR).workflow"
rm -f ~/.claude/bin/converter-md.sh ~/.claude/bin/converter-md-ocr.sh ~/.claude/bin/provenance.sh
rm -f ~/Library/Logs/converter-markdown.log ~/Library/Logs/converter-markdown-ocr.log
/System/Library/CoreServices/pbs -update
rm -f ~/.local/share/whisper/ggml-small.bin

# 4) idiomas extras do Tesseract (opcional)
rm -f "$(brew --prefix)"/share/tessdata/por.traineddata "$(brew --prefix)"/share/tessdata/spa.traineddata

# 5) pacotes Homebrew (opcional — só se não usar em mais nada)
brew uninstall ocrmypdf poppler whisper-cpp tesseract coreutils qpdf
pipx uninstall markitdown
brew uninstall --cask calibre
```

> **Se não houver backup de `.zshrc`/`settings.json`**, edite à mão: apague o trecho entre
> `# >>> CLAUDE-MARKITDOWN >>>` e `# <<< CLAUDE-MARKITDOWN <<<` no `~/.zshrc`, e a entrada
> `PreToolUse` com `matcher: "Read"` que aponta para `markitdown-read.sh` no
> `~/.claude/settings.json`.

Os arquivos `.bak.<timestamp>` permanecem depois da remoção — apague-os quando quiser.
