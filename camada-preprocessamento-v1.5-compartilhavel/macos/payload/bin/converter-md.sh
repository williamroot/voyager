#!/bin/zsh
# Quick Action "Converter para Markdown" (documento digital -> .md).
# Chamado pelo involucro do Automator com os arquivos como argumentos ("$@").
# A logica vive AQUI (editavel livremente); o bundle do Automator so chama este script.
# Modo silencioso p/ teste: QA_SILENT=1 pula as notificacoes.
export PATH="/opt/homebrew/bin:$HOME/.local/bin:/usr/bin:/bin:$PATH"
LOG="$HOME/Library/Logs/converter-markdown.log"; mkdir -p "$HOME/Library/Logs"
typeset -i ok=0 fail=0; n=$#

notify() { [ -n "$QA_SILENT" ] && return; osascript -e "display notification \"$1\" with title \"Converter para Markdown\" $2" >/dev/null 2>&1; }

notify "Convertendo $n arquivo(s)..."
SECONDS=0
{
echo "===== $(date) ($n arquivo[s]) ====="
for f in "$@"; do
  out="${f%.*}.md"; src="$f"; tmp=""
  el="${f##*.}"; el="${el:l}"
  # .doc (Word binário antigo) nao e suportado pelo MarkItDown -> normaliza p/ .docx via textutil (nativo do macOS)
  if [ "$el" = "doc" ]; then
    tmp="${TMPDIR:-/tmp}/converter_md_$$.docx"
    textutil -convert docx "$f" -output "$tmp" >/dev/null 2>&1 && src="$tmp"
  # Ebooks nao-EPUB (Kindle e afins) -> normaliza p/ .epub via Calibre; o EPUB e nativo no MarkItDown 0.1.x
  elif [[ " mobi azw azw3 fb2 fbz lit pdb lrf tcr pml snb kepub " == *" $el "* ]]; then
    tmp="${TMPDIR:-/tmp}/converter_md_$$.epub"
    ebook-convert "$f" "$tmp" >/dev/null 2>&1 && src="$tmp"
  fi
  if markitdown "$src" -o "$out" 2>&1; then ok+=1; echo "OK -> $out"; else fail+=1; echo "FALHA -> $f"; fi
  [ -n "$tmp" ] && rm -f "$tmp"
done
echo "resumo: ok=$ok fail=$fail em ${SECONDS}s"
} >> "$LOG" 2>&1
notify "$ok convertido(s), $fail falha(s) - ${SECONDS}s" "sound name \"Glass\""
