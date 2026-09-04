# gerar-amostras.ps1 — cria arquivos de amostra para testar a camada (Windows).
#
# Uso:  .\gerar-amostras.ps1 [pasta-de-saida]        (padrão: .\amostras)
#
# Gera, na pasta de saída:
#   01-pdf-digital.pdf     PDF com texto nativo        → rota MarkItDown
#   02-pdf-escaneado.pdf   PDF só-imagem               → rota OCR
#   03-ebook.epub          EPUB mínimo                 → rota de ebook
#   04-planilha.xlsx       XLSX mínimo                 → rota Office
#   05-imagem-grande.png   3000×2000 px                → rota de redimensionamento
#   06-audio.wav           fala sintetizada            → rota Whisper
#
# Nada aqui depende da camada instalada: são só arquivos de entrada. Se alguma
# ferramenta faltar, a amostra correspondente é pulada com aviso.
param([string]$Saida)

$ErrorActionPreference = 'Continue'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Saida) { $Saida = Join-Path (Get-Location) 'amostras' }
New-Item -ItemType Directory -Force -Path $Saida | Out-Null
Write-Host "Gerando amostras em: $Saida"

# 1) PDFs, EPUB e XLSX (Python puro + poppler para o escaneado)
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command python3 -ErrorAction SilentlyContinue }
if ($py) {
  & $py.Source (Join-Path $here 'amostras.py') $Saida
} else {
  Write-Warning "  python ausente — pulei os PDFs, o EPUB e o XLSX"
}

# 2) Imagem grande (3000×2000) — o hook deve reduzir para 1568 px
if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
  $img = Join-Path $Saida '05-imagem-grande.png'
  & ffmpeg -y -loglevel error -f lavfi -i testsrc2=size=3000x2000:rate=1 -frames:v 1 $img
  if (Test-Path $img) { Write-Host "  ✓ $img" } else { Write-Warning "  falha ao gerar a imagem grande" }
} else {
  Write-Warning "  ffmpeg ausente — pulei 05-imagem-grande.png"
}

# 3) Áudio falado (voz do sistema) — entrada para 'transcrever'
$frase = 'Esta é uma amostra de áudio para testar a transcrição automática. Frase chave de verificação: pipoca verde mil quinhentos e sessenta e oito.'
$wav = Join-Path $Saida '06-audio.wav'
try {
  Add-Type -AssemblyName System.Speech
  $voz = New-Object System.Speech.Synthesis.SpeechSynthesizer
  # usa uma voz pt-BR se houver; senão, a padrão do sistema (o Whisper ainda transcreve)
  $ptbr = $voz.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -eq 'pt-BR' } | Select-Object -First 1
  if ($ptbr) { $voz.SelectVoice($ptbr.VoiceInfo.Name) }
  else { Write-Host "  (sem voz pt-BR instalada — usando a voz padrão do sistema)" }
  $voz.SetOutputToWaveFile($wav)
  $voz.Speak($frase)
  $voz.Dispose()
  if (Test-Path $wav) { Write-Host "  ✓ $wav" }
} catch {
  Write-Warning "  síntese de voz indisponível ($($_.Exception.Message)) — pulei a amostra de mídia"
}

Write-Host ""
Write-Host "Pronto. Siga o roteiro: TESTES-VERIFICACAO.md"
Write-Host "Frase-chave a procurar nas saídas: PIPOCA-VERDE-1568"
