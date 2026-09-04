#!/bin/zsh
# gerar-amostras.sh — cria arquivos de amostra para testar a camada (macOS).
#
# Uso:  zsh gerar-amostras.sh [pasta-de-saida]      (padrão: ./amostras)
#
# Gera, na pasta de saída:
#   01-pdf-digital.pdf     PDF com texto nativo        → rota MarkItDown
#   02-pdf-escaneado.pdf   PDF só-imagem               → rota OCR
#   03-ebook.epub          EPUB mínimo                 → rota de ebook
#   04-planilha.xlsx       XLSX mínimo                 → rota Office
#   05-imagem-grande.png   3000×2000 px                → rota de redimensionamento
#   06-audio.m4a           fala sintetizada em pt-BR   → rota Whisper
#
# Nada aqui depende da camada instalada: são só arquivos de entrada. Se alguma
# ferramenta faltar, a amostra correspondente é pulada com aviso.
emulate -L zsh
set -u
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

HERE="${0:A:h}"
OUT="${1:-$PWD/amostras}"
mkdir -p "$OUT"
print -- "Gerando amostras em: $OUT"

# 1) PDFs, EPUB e XLSX (Python puro + poppler para o escaneado)
python3 "$HERE/amostras.py" "$OUT" || print -u2 -- "  ⚠︎ falha no amostras.py"

# 2) Imagem grande (3000×2000) — o hook deve reduzir para 1568 px
if command -v ffmpeg >/dev/null 2>&1; then
  if ffmpeg -y -loglevel error -f lavfi -i testsrc2=size=3000x2000:rate=1 -frames:v 1 "$OUT/05-imagem-grande.png"; then
    print -- "  ✓ $OUT/05-imagem-grande.png"
  else
    print -u2 -- "  ⚠︎ falha ao gerar a imagem grande"
  fi
else
  print -u2 -- "  ⚠︎ ffmpeg ausente — pulei 05-imagem-grande.png"
fi

# 3) Áudio falado em pt-BR (voz do sistema) — entrada para 'transcrever'
FRASE="Esta é uma amostra de áudio para testar a transcrição automática. Frase chave de verificação: pipoca verde mil quinhentos e sessenta e oito."
if command -v say >/dev/null 2>&1; then
  AIFF="$OUT/.06-audio.aiff"
  VOZ=""
  say -v '?' 2>/dev/null | grep -q '^Luciana' && VOZ="-v Luciana"
  if say ${=VOZ} -o "$AIFF" "$FRASE" 2>/dev/null; then
    if command -v ffmpeg >/dev/null 2>&1 && ffmpeg -y -loglevel error -i "$AIFF" -ar 16000 -ac 1 "$OUT/06-audio.m4a"; then
      rm -f "$AIFF"; print -- "  ✓ $OUT/06-audio.m4a"
    else
      mv -f "$AIFF" "$OUT/06-audio.aiff" && print -- "  ✓ $OUT/06-audio.aiff (sem ffmpeg para converter)"
    fi
  else
    print -u2 -- "  ⚠︎ 'say' não gerou áudio — pulei a amostra de mídia"
  fi
else
  print -u2 -- "  ⚠︎ 'say' ausente — pulei a amostra de mídia"
fi

print -- ""
print -- "Pronto. Siga o roteiro: TESTES-VERIFICACAO.md"
print -- "Frase-chave a procurar nas saídas: PIPOCA-VERDE-1568"
