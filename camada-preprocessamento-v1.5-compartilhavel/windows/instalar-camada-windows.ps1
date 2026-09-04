# instalar-camada-windows.ps1 — instala a Camada Local de Pré-processamento (v1.5) no Windows.
#
# STATUS: TRILHA WINDOWS É REFERÊNCIA A VALIDAR. A lógica é o porte fiel da trilha macOS
# (validada), mas ainda não foi rodada de ponta a ponta em produção. Teste com arquivos de
# amostra (..\testes\gerar-amostras.ps1) antes de usar em material sério.
#
# O que a camada faz: normaliza entradas pesadas (PDF, Office, ebook, escaneado, áudio,
# vídeo, imagem grande) para a forma mais barata em tokens (Markdown ou imagem <=1568 px)
# ANTES de o Claude Code ler o arquivo. Duas frentes: um hook PreToolUse(Read) automático
# e funções de terminal sob demanda (md, ocr, transcrever, imgredux, provmd).
#
# FILOSOFIA: idempotente e defensivo. Backup com timestamp antes de tocar em arquivo
# existente, nunca sobrescreve às cegas, e reexecutar é seguro.
#
# USO (PowerShell)
#   .\instalar-camada-windows.ps1                 # instala as peças + hook + funções (sem dependências)
#   .\instalar-camada-windows.ps1 -ComDeps        # também instala as dependências (winget + pip)
#   .\instalar-camada-windows.ps1 -Checar         # só diagnostica o ambiente e sai
#
# Se o PowerShell bloquear scripts, rode uma vez:
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

[CmdletBinding()]
param(
  [switch]$ComDeps,
  [switch]$Checar
)

$ErrorActionPreference = 'Continue'
$here  = Split-Path -Parent $MyInvocation.MyCommand.Path
$pay   = Join-Path $here 'payload'
$stamp = Get-Date -Format 'yyyyMMddHHmmss'
$avisos = 0

function Titulo($t){ Write-Host ""; Write-Host "── $t ──" }
function Diz($t){ Write-Host "• $t" }
function Ok($t){ Write-Host "  ✓ $t" }
function Aviso($t){ Write-Warning "  $t"; $script:avisos++ }

if (-not (Test-Path $pay)) { throw "pasta payload\ não encontrada ao lado do script. Rode de dentro da pasta windows\ do pacote." }

# ─────────────────────────────────────────────────────────────────────────────
Titulo "1 · Diagnóstico do ambiente"
# ─────────────────────────────────────────────────────────────────────────────
$faltando = @()
function Checar($bin,$para){
  $c = Get-Command $bin -ErrorAction SilentlyContinue
  if ($c) { Write-Host ("  {0,-16} {1}" -f $bin, $c.Source) }
  else { Write-Host ("  {0,-16} FALTANDO — {1}" -f $bin, $para); $script:faltando += $bin }
}
Checar 'claude'        'Claude Code — é ele que executa a camada'
Checar 'python'        'Python 3.10+'
Checar 'pip'           'instalador dos pacotes Python'
Checar 'markitdown'    'conversor principal (documentos → Markdown)'
Checar 'ocrmypdf'      'OCR de PDF escaneado'
Checar 'tesseract'     'motor de OCR'
Checar 'pdfinfo'       'poppler — conta páginas do PDF'
Checar 'ffmpeg'        'áudio/vídeo e redimensionamento de imagem'
Checar 'ffprobe'       'dimensões da imagem'
Checar 'whisper'       'openai-whisper — transcrição de mídia'
Checar 'ebook-convert' 'Calibre — ebooks não-EPUB → EPUB'
Checar 'qpdf'          'partição de PDF gigante (opcional)'

$pyv = (& python -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null)
if ($pyv) {
  Diz "python em uso: $pyv"
  $partes = $pyv.Split('.')
  if ([int]$partes[0] -lt 3 -or ([int]$partes[0] -eq 3 -and [int]$partes[1] -lt 10)) {
    Aviso "Python < 3.10 no PATH — instale o 3.12 (winget install Python.Python.3.12)."
  }
} else { Aviso "python não respondeu — confirme a instalação e o PATH." }

if ($faltando.Count) {
  Diz ("faltando: " + ($faltando -join ', '))
  if (-not $ComDeps) { Diz "rode com -ComDeps para instalar, ou instale à mão (INSTALAR-Windows.md, seção 2)." }
} else { Ok "todas as dependências presentes." }

if ($Checar) { Write-Host ""; Write-Host "Só diagnóstico (-Checar). Nada foi alterado."; exit 0 }

# ─────────────────────────────────────────────────────────────────────────────
if ($ComDeps) {
Titulo "2 · Dependências (winget + pip)"
  if (Get-Command winget -ErrorAction SilentlyContinue) {
    foreach ($p in 'Python.Python.3.12','Gyan.FFmpeg','UB-Mannheim.TesseractOCR','ArtifexSoftware.GhostScript','calibre.calibre') {
      Diz "winget install $p"
      winget install --accept-package-agreements --accept-source-agreements -e --id $p
      if ($LASTEXITCODE -ne 0) { Aviso "winget não concluiu $p (pode já estar instalado)." }
    }
  } else { Aviso "winget ausente — instale as dependências à mão (INSTALAR-Windows.md §2)." }

  # MarkItDown: extras alvo. NÃO use [all] — xlrd e youtube-transcript-api quebram no Python 3.14.
  Diz 'pip install "markitdown[pdf,docx,pptx,xlsx,outlook]>=0.1.7" ocrmypdf openai-whisper'
  & python -m pip install --upgrade "markitdown[pdf,docx,pptx,xlsx,outlook]>=0.1.7" ocrmypdf openai-whisper
  if ($LASTEXITCODE -ne 0) { Aviso "falha em algum pacote pip — veja a saída acima." }

  Write-Host ""
  Diz "Pendências manuais das dependências no Windows:"
  Write-Host "   · Poppler (pdfinfo): baixe os binários, extraia e adicione ...\poppler\bin ao PATH."
  Write-Host "   · Tesseract: no instalador UB-Mannheim marque Portuguese e Spanish, ou copie"
  Write-Host "     por.traineddata / spa.traineddata para C:\Program Files\Tesseract-OCR\tessdata\."
  Write-Host "   · Confirme com: tesseract --list-langs   (esperado incluir por e spa)"
}

# ─────────────────────────────────────────────────────────────────────────────
Titulo "3 · Peças da camada (%USERPROFILE%\.claude)"
# ─────────────────────────────────────────────────────────────────────────────
$claudeDir = Join-Path $env:USERPROFILE '.claude'
New-Item -ItemType Directory -Force -Path (Join-Path $claudeDir 'hooks') | Out-Null

function InstalarUm($src,$dst){
  if (-not (Test-Path -LiteralPath $src)) { Aviso "payload ausente: $src"; return }
  if (Test-Path -LiteralPath $dst) {
    $a = (Get-FileHash -LiteralPath $src).Hash; $b = (Get-FileHash -LiteralPath $dst).Hash
    if ($a -eq $b) { Ok "já atualizado: $dst"; return }
    Copy-Item -LiteralPath $dst -Destination "$dst.bak.$stamp" -Force
    Diz "backup: $dst.bak.$stamp"
  }
  Copy-Item -LiteralPath $src -Destination $dst -Force
  Ok "instalado: $dst"
}
InstalarUm (Join-Path $pay 'hooks\markitdown-read.ps1') (Join-Path $claudeDir 'hooks\markitdown-read.ps1')

# ─────────────────────────────────────────────────────────────────────────────
Titulo "4 · Registro do hook em settings.json (merge)"
# ─────────────────────────────────────────────────────────────────────────────
$set = Join-Path $claudeDir 'settings.json'
$cmd = 'powershell -NoProfile -ExecutionPolicy Bypass -File "%USERPROFILE%\.claude\hooks\markitdown-read.ps1"'
$cfg = $null
if (Test-Path -LiteralPath $set) {
  try { $cfg = Get-Content -Raw -LiteralPath $set | ConvertFrom-Json } catch { $cfg = $null }
  if ($null -eq $cfg) { Aviso "settings.json ilegível — NÃO foi alterado. Registre o hook à mão (payload\settings-hook-PreToolUse-Read.json)." }
} else {
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $set) | Out-Null
  $cfg = [pscustomobject]@{}
}

# define/atualiza uma propriedade em pscustomobject exista ela ou não
function DefinirProp($obj,$nome,$valor){
  if ($obj.PSObject.Properties[$nome]) { $obj.$nome = $valor }
  else { $obj | Add-Member -MemberType NoteProperty -Name $nome -Value $valor }
}

if ($null -ne $cfg) {
  # garante hooks.PreToolUse[] com uma entrada matcher=Read contendo o nosso comando
  if (-not $cfg.PSObject.Properties['hooks']) { DefinirProp $cfg 'hooks' ([pscustomobject]@{}) }
  if (-not $cfg.hooks.PSObject.Properties['PreToolUse']) { DefinirProp $cfg.hooks 'PreToolUse' @() }

  $pre = @($cfg.hooks.PreToolUse)
  $alvo = $pre | Where-Object { $_.matcher -eq 'Read' } | Select-Object -First 1
  if (-not $alvo) {
    $alvo = [pscustomobject]@{ matcher='Read'; hooks=@() }
    $pre += $alvo
  }
  $jaTem = @($alvo.hooks) | Where-Object { $_.command -eq $cmd }
  if ($jaTem) {
    Ok "hook já registrado (nada a fazer)"
  } else {
    DefinirProp $alvo 'hooks' (@($alvo.hooks) + [pscustomobject]@{ type='command'; command=$cmd; timeout=90 })
    DefinirProp $cfg.hooks 'PreToolUse' $pre
    if (Test-Path -LiteralPath $set) {   # backup só quando há mudança de fato
      Copy-Item -LiteralPath $set -Destination "$set.bak.$stamp" -Force
      Diz "backup: $set.bak.$stamp"
    }
    # UTF-8 SEM BOM: o BOM atrapalha leitores de JSON
    $json = ($cfg | ConvertTo-Json -Depth 12)
    [IO.File]::WriteAllText($set, $json, (New-Object System.Text.UTF8Encoding($false)))
    Ok "hook PreToolUse(Read) registrado em $set"
  }
}

# ─────────────────────────────────────────────────────────────────────────────
Titulo "5 · Funções de terminal no `$PROFILE"
# ─────────────────────────────────────────────────────────────────────────────
$blk = Join-Path $pay 'profile-CLAUDE-MARKITDOWN.ps1'
if (-not (Test-Path -LiteralPath $blk)) {
  Aviso "payload ausente: $blk"
} else {
  $bloco = Get-Content -LiteralPath $blk
  if (-not (Test-Path -LiteralPath $PROFILE)) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $PROFILE) | Out-Null
    New-Item -ItemType File -Force -Path $PROFILE | Out-Null
    Diz "perfil criado: $PROFILE"
  }
  # o .ps1 do perfil é gravado em UTF-8 COM BOM de propósito: sem o BOM, o Windows
  # PowerShell 5.1 lê o arquivo como ANSI e embaralha os acentos dos comentários.
  $atual = @(Get-Content -LiteralPath $PROFILE)
  $ini = ($atual | Select-String -SimpleMatch '# >>> CLAUDE-MARKITDOWN >>>' | Select-Object -First 1)
  if ($ini) {
    $fim = ($atual | Select-String -SimpleMatch '# <<< CLAUDE-MARKITDOWN <<<' | Select-Object -First 1)
    if (-not $fim) {
      Aviso "marcador de fim CLAUDE-MARKITDOWN ausente no `$PROFILE — ajuste à mão."
    } else {
      $a = $ini.LineNumber - 1; $b = $fim.LineNumber - 1
      $antes = if ($a -gt 0) { $atual[0..($a-1)] } else { @() }
      $depois = if ($b -lt ($atual.Count-1)) { $atual[($b+1)..($atual.Count-1)] } else { @() }
      $novo = @($antes) + @($bloco) + @($depois)
      if ((($atual -join "`n")) -eq (($novo -join "`n"))) {
        Ok "bloco CLAUDE-MARKITDOWN já é o canônico (nada a fazer)"
      } else {
        Copy-Item -LiteralPath $PROFILE -Destination "$PROFILE.bak.$stamp" -Force
        Diz "backup: $PROFILE.bak.$stamp"
        $novo | Set-Content -LiteralPath $PROFILE -Encoding UTF8
        Ok "bloco CLAUDE-MARKITDOWN atualizado no `$PROFILE"
      }
    }
  } else {
    if ($atual.Count) { Copy-Item -LiteralPath $PROFILE -Destination "$PROFILE.bak.$stamp" -Force; Diz "backup: $PROFILE.bak.$stamp" }
    (@($atual) + @('') + @($bloco)) | Set-Content -LiteralPath $PROFILE -Encoding UTF8
    Ok "bloco CLAUDE-MARKITDOWN acrescentado ao `$PROFILE"
  }
}

# ─────────────────────────────────────────────────────────────────────────────
Titulo "6 · Menu 'Enviar para' (clique-direito)"
# ─────────────────────────────────────────────────────────────────────────────
$sendTo = [Environment]::GetFolderPath('SendTo')
$cmdSrc = Join-Path $pay 'converter-md.cmd'
if (Test-Path -LiteralPath $cmdSrc) {
  InstalarUm $cmdSrc (Join-Path $sendTo 'converter-md.cmd')
  Diz "clique-direito num PDF/DOCX → Enviar para → converter-md (o .md sai ao lado do original)"
} else { Aviso "payload ausente: $cmdSrc" }

# ─────────────────────────────────────────────────────────────────────────────
Titulo "7 · Próximos passos"
# ─────────────────────────────────────────────────────────────────────────────
@'
  1) Recarregue o perfil:           . $PROFILE
  2) Teste as funções:              md algum.pdf   ·   ocr escaneado.pdf   ·   imgredux foto.jpg
  3) Kit de teste do pacote:        ..\testes\gerar-amostras.ps1  (e siga testes\TESTES-VERIFICACAO.md)
  4) Hook em ação: abra o Claude Code numa pasta com um PDF e pergunte algo sobre ele —
     a leitura passa pelo Markdown convertido, não pelas imagens das páginas.

  LEMBRE: a trilha Windows é REFERÊNCIA A VALIDAR. Anote o que precisou ajustar.
'@ | Write-Host

Write-Host ""
if ($avisos) { Write-Host "Concluído com $avisos aviso(s). Releia os itens acima." }
else { Write-Host "Concluído sem avisos." }
Write-Host "Manual completo: ..\manual\camada-preprocessamento-claude-code.html"
