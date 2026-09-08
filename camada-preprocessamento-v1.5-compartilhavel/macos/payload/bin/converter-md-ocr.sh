#!/bin/zsh
# Quick Action "Converter para Markdown (OCR)" — forca OCR por+eng.
# PDF escaneado -> PDF pesquisavel (.ocr.pdf) + .md ; imagem -> .md via tesseract.
# Chamado pelo involucro do Automator com os arquivos como argumentos ("$@").
# Modo silencioso p/ teste: QA_SILENT=1 pula as notificacoes.
export PATH="/opt/homebrew/bin:$HOME/.local/bin:/usr/bin:/bin:$PATH"
LOG="$HOME/Library/Logs/converter-markdown-ocr.log"; mkdir -p "$HOME/Library/Logs"
typeset -i ok=0 fail=0; n=$#

notify() { [ -n "$QA_SILENT" ] && return; osascript -e "display notification \"$1\" with title \"Markdown (OCR)\" $2" >/dev/null 2>&1; }

notify "OCR em $n arquivo(s)... pode demorar"
SECONDS=0
{
echo "===== $(date) ($n arquivo[s]) ====="
for f in "$@"; do
  out="${f%.*}.md"; el="${f##*.}"; el="${el:l}"
  case "$el" in
    pdf)
      tmp="${f%.*}.ocr.pdf"
      if ocrmypdf -l por+eng --force-ocr "$f" "$tmp" 2>&1 && markitdown "$tmp" -o "$out" 2>&1; then
        ok+=1; echo "OK(pdf) -> $out (+ $tmp)"
      else fail+=1; echo "FALHA(pdf) -> $f"; fi ;;
    png|jpg|jpeg|tif|tiff|bmp|webp)
      if tesseract "$f" stdout -l por+eng > "$out" 2>/dev/null && [ -s "$out" ]; then
        ok+=1; echo "OK(img) -> $out"
      else fail+=1; echo "FALHA(img) -> $f"; fi ;;
    doc)
      # .doc (Word binario antigo) nao e suportado pelo MarkItDown -> normaliza p/ .docx via textutil (nativo do macOS)
      tmp="${TMPDIR:-/tmp}/converter_ocr_$$.docx"
      if textutil -convert docx "$f" -output "$tmp" >/dev/null 2>&1 && markitdown "$tmp" -o "$out" 2>&1; then
        ok+=1; echo "OK(doc) -> $out"
      else fail+=1; echo "FALHA(doc) -> $f"; fi
      rm -f "$tmp" ;;
    mobi|azw|azw3|fb2|fbz|lit|pdb|lrf|tcr|pml|snb|kepub)
      # Ebooks nao-EPUB (Kindle e afins) -> normaliza p/ .epub via Calibre e converte (EPUB e nativo no MarkItDown 0.1.x)
      tmp="${TMPDIR:-/tmp}/converter_ocr_$$.epub"
      if ebook-convert "$f" "$tmp" >/dev/null 2>&1 && markitdown "$tmp" -o "$out" 2>&1; then
        ok+=1; echo "OK(ebook) -> $out"
      else fail+=1; echo "FALHA(ebook) -> $f"; fi
      rm -f "$tmp" ;;
    *)
      if markitdown "$f" -o "$out" 2>&1; then ok+=1; echo "OK -> $out"; else fail+=1; echo "FALHA -> $f"; fi ;;
  esac
done
echo "resumo: ok=$ok fail=$fail em ${SECONDS}s"
} >> "$LOG" 2>&1
notify "$ok pronto(s), $fail falha(s) - ${SECONDS}s" "sound name \"Glass\""
