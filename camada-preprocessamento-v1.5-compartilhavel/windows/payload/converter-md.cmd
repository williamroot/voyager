@echo off
rem "Enviar para" -> converter para Markdown. Coloque este .cmd em shell:sendto
for %%f in (%*) do markitdown "%%~f" -o "%%~dpnf.md"
