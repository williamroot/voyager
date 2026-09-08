# >>> CLAUDE-MARKITDOWN >>>
# Camada local de pré-processamento — funções sob demanda
export PATH="$HOME/.local/bin:$PATH"
export WHISPER_MODEL="$HOME/.local/share/whisper/ggml-small.bin"

# md <arquivo> [saida.md] — documento → Markdown
md() {
  emulate -L zsh
  [ -z "$1" ] && { echo "uso: md <arquivo> [saida.md]"; return 1; }
  local out="${2:-${1:r}.md}" src="$1" tmp=""
  # .doc (Word binário antigo) não é suportado pelo MarkItDown → normaliza p/ .docx via textutil (nativo do macOS)
  if [ "${1:e:l}" = "doc" ]; then
    tmp="${TMPDIR:-/tmp}/md_$$.docx"
    textutil -convert docx "$1" -output "$tmp" >/dev/null 2>&1 && src="$tmp"
  # Ebooks não-EPUB (Kindle e afins) → normaliza p/ .epub via Calibre; o EPUB é nativo no MarkItDown 0.1.x
  elif [[ " mobi azw azw3 fb2 fbz lit pdb lrf tcr pml snb kepub " == *" ${1:e:l} "* ]]; then
    tmp="${TMPDIR:-/tmp}/md_$$.epub"
    ebook-convert "$1" "$tmp" >/dev/null 2>&1 && src="$tmp"
  fi
  markitdown "$src" -o "$out" && echo "✓ Markdown: $out"
  local rc=$?
  [ -n "$tmp" ] && rm -f "$tmp"
  return $rc
}

# ocr <arquivo.pdf> [saida.pdf] — OCR (por+eng) → PDF pesquisável + .md
ocr() {
  emulate -L zsh
  [ -z "$1" ] && { echo "uso: ocr <arquivo.pdf> [saida.pdf]"; return 1; }
  local outpdf="${2:-${1:r}.ocr.pdf}"
  ocrmypdf -l por+eng --skip-text "$1" "$outpdf" \
    || ocrmypdf -l por+eng --force-ocr "$1" "$outpdf" || return 1
  markitdown "$outpdf" -o "${1:r}.md" \
    && echo "✓ PDF pesquisável: $outpdf" && echo "✓ Markdown: ${1:r}.md"
}

# transcrever <audio|video> [saida.md] — extrai áudio, transcreve (pt) → .md
transcrever() {
  emulate -L zsh
  [ -z "$1" ] && { echo "uso: transcrever <audio_ou_video> [saida.md]"; return 1; }
  local wav="${TMPDIR:-/tmp}/transcrever_$$.wav"
  local out="${2:-${1:r}.md}"
  ffmpeg -y -loglevel error -i "$1" -ar 16000 -ac 1 -c:a pcm_s16le "$wav" || return 1
  whisper-cli -m "$WHISPER_MODEL" -l pt -f "$wav" -otxt -of "${out:r}" >/dev/null 2>&1
  rm -f "$wav"
  [ -f "${out:r}.txt" ] && mv "${out:r}.txt" "$out" && echo "✓ Transcrição: $out"
}

# imgredux <imagem> [saida] — reduz o lado maior para 1568 px
imgredux() {
  emulate -L zsh
  [ -z "$1" ] && { echo "uso: imgredux <imagem> [saida]"; return 1; }
  local out="${2:-${1:r}_1568.${1:e}}"
  ffmpeg -y -loglevel error -i "$1" \
    -vf "scale=w='min(1568,iw)':h='min(1568,ih)':force_original_aspect_ratio=decrease" "$out" \
    && echo "✓ Imagem: $out"
}

# provmd <arquivo.md> [--tipo digital|ocr|ocr-fraco|midia|particao] [--origem …] [--fallback …] [--paginas A-B]
# Insere o cabeçalho de proveniência (OBRIGATÓRIO) no topo de um .md. Idempotente.
provmd() { emulate -L zsh; "$HOME/.claude/bin/provenance.sh" "$@"; }
# <<< CLAUDE-MARKITDOWN <<<
