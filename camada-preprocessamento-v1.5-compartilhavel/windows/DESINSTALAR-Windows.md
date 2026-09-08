# Remover a camada — Windows

## Desativar temporariamente (reversível)

```powershell
Rename-Item "$env:USERPROFILE\.claude\hooks\markitdown-read.ps1" 'markitdown-read.ps1.off'
# reativar: Rename-Item "$env:USERPROFILE\.claude\hooks\markitdown-read.ps1.off" 'markitdown-read.ps1'
```

Como a camada é *fail-open*, sem o arquivo no caminho apontado a leitura segue normal.

## Remover tudo

```powershell
# 1) hook e cache
Remove-Item "$env:USERPROFILE\.claude\hooks\markitdown-read.ps1" -Force
Remove-Item "$env:USERPROFILE\.cache\claude-markitdown" -Recurse -Force

# 2) registro no settings.json — restaura o backup mais recente da instalação
$set = "$env:USERPROFILE\.claude\settings.json"
$bak = Get-ChildItem "$set.bak.*" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Desc | Select-Object -First 1
if ($bak) { Copy-Item $bak.FullName $set -Force } else { notepad $set }  # sem backup: apague o bloco "hooks" à mão

# 3) bloco do perfil PowerShell — restaura o backup, ou apague à mão o trecho
#    entre "# >>> CLAUDE-MARKITDOWN >>>" e "# <<< CLAUDE-MARKITDOWN <<<"
$bakp = Get-ChildItem "$PROFILE.bak.*" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Desc | Select-Object -First 1
if ($bakp) { Copy-Item $bakp.FullName $PROFILE -Force } else { notepad $PROFILE }

# 4) item "Enviar para"
Remove-Item (Join-Path ([Environment]::GetFolderPath('SendTo')) 'converter-md.cmd') -Force

# 5) pacotes (opcional — só se não usar em mais nada)
pip uninstall -y markitdown ocrmypdf openai-whisper
winget uninstall Gyan.FFmpeg
winget uninstall UB-Mannheim.TesseractOCR
winget uninstall ArtifexSoftware.GhostScript
winget uninstall calibre.calibre
```

Os arquivos `.bak.<timestamp>` permanecem depois da remoção — apague-os quando quiser.
