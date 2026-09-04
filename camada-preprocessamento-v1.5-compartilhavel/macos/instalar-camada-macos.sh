#!/bin/zsh
# instalar-camada-macos.sh — instala a Camada Local de Pré-processamento (v1.5) no macOS.
#
# O que a camada faz: normaliza entradas pesadas (PDF, Office, ebook, escaneado, áudio,
# vídeo, imagem grande) para a forma mais barata em tokens (Markdown ou imagem ≤1568 px)
# ANTES de o Claude Code ler o arquivo. Duas frentes: um hook PreToolUse(Read) automático
# e funções de terminal sob demanda (md, ocr, transcrever, imgredux, provmd).
#
# FILOSOFIA: idempotente e defensivo. Faz backup com timestamp antes de tocar em qualquer
# arquivo existente, nunca sobrescreve às cegas, e reexecutar é seguro.
#
# USO
#   zsh instalar-camada-macos.sh                  # instala as peças + hook + funções (NÃO instala dependências)
#   zsh instalar-camada-macos.sh --com-deps       # também instala as dependências via Homebrew/pipx
#   zsh instalar-camada-macos.sh --com-deps --com-modelo-whisper   # idem + baixa o modelo Whisper (~481 MB)
#   zsh instalar-camada-macos.sh --checar         # só diagnostica o ambiente e sai
#
# PRÉ-REQUISITO MANUAL: Homebrew (o instalador dele pede senha de administrador de forma
# interativa). Veja INSTALAR-macOS.md, seção 1.
#
# NÃO cria o bundle das Quick Actions do Finder: o macOS rejeita bundles .workflow criados
# fora do Automator (erro −10811). O script imprime o passo a passo ao final.
emulate -L zsh
set -u
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

HERE="${0:A:h}"
PAY="$HERE/payload"
STAMP="$(date +%Y%m%d%H%M%S 2>/dev/null || echo backup)"
COM_DEPS=0; COM_WHISPER=0; SO_CHECAR=0
typeset -i ERROS=0

for a in "$@"; do
  case "$a" in
    --com-deps)            COM_DEPS=1 ;;
    --com-modelo-whisper)  COM_WHISPER=1 ;;
    --checar)              SO_CHECAR=1 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) print -u2 -- "opção desconhecida: $a (use --help)"; exit 2 ;;
  esac
done

say(){  print -- "• $*"; }
ok(){   print -- "  ✓ $*"; }
warn(){ print -u2 -- "  ⚠︎ $*"; ERROS+=1; }
titulo(){ print -- ""; print -- "── $* ──"; }

[ "$(uname -s)" = "Darwin" ] || { print -u2 -- "Este instalador é do macOS. No Windows use ../windows/instalar-camada-windows.ps1"; exit 1; }
[ -d "$PAY" ] || { print -u2 -- "pasta payload/ não encontrada ao lado do script. Rode de dentro da pasta macos/ do pacote."; exit 1; }

# ─────────────────────────────────────────────────────────────────────────────
titulo "1 · Diagnóstico do ambiente"
# ─────────────────────────────────────────────────────────────────────────────
FALTANDO=()
checar(){  # $1 = binário  $2 = para que serve
  if command -v "$1" >/dev/null 2>&1; then printf '  %-14s %s\n' "$1" "$(command -v "$1")"
  else printf '  %-14s FALTANDO — %s\n' "$1" "$2"; FALTANDO+=("$1"); fi
}
checar claude       "Claude Code — é ele que executa a camada"
checar brew         "Homebrew — pré-requisito manual (pede senha)"
checar python3      "Python 3.10+ (usado pelo hook e pelo pipx)"
checar pipx         "isola o MarkItDown em venv próprio"
checar markitdown   "conversor principal (documentos → Markdown)"
checar ocrmypdf     "OCR de PDF escaneado"
checar tesseract    "motor de OCR"
checar pdfinfo      "poppler — conta páginas do PDF"
checar ffmpeg       "áudio/vídeo e redimensionamento de imagem"
checar ffprobe      "poppler/ffmpeg — dimensões da imagem"
checar whisper-cli  "whisper-cpp — transcrição de mídia"
checar gtimeout     "coreutils — trava de tempo do OCR no hook"
checar qpdf         "partição de PDF gigante"
checar ebook-convert "Calibre — ebooks não-EPUB → EPUB"

PYV="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo "?")"
say "python3 em uso: ${PYV}"
[ "$PYV" != "?" ] && { python3 -c 'import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)' || warn "Python < 3.10 no PATH — instale o python@3.12 (brew) antes de seguir." ; }

if (( ${#FALTANDO} )); then
  say "faltando: ${FALTANDO[*]}"
  (( COM_DEPS )) || say "rode com --com-deps para instalar as dependências, ou instale à mão (INSTALAR-macOS.md, seção 2)."
else
  ok "todas as dependências presentes."
fi

# Instalação já existente sob quarentena do Gatekeeper: o hook não executa e, por ser
# fail-open, não reclama. É a única falha da camada que não dá sintoma nenhum — então o
# diagnóstico tem de apontá-la. Reexecutar o instalador (sem --checar) resolve.
QUARENTENADOS=()
for f in "$HOME/.claude/hooks/markitdown-read.sh" "$HOME/.claude/bin/provenance.sh" \
         "$HOME/.claude/bin/converter-md.sh" "$HOME/.claude/bin/converter-md-ocr.sh"; do
  [ -f "$f" ] && xattr "$f" 2>/dev/null | grep -q com.apple.quarantine && QUARENTENADOS+=("$f")
done
if (( ${#QUARENTENADOS} )); then
  warn "${#QUARENTENADOS} arquivo(s) instalado(s) sob quarentena do macOS — o Gatekeeper bloqueia a execução e o hook falha em silêncio:"
  for f in "${QUARENTENADOS[@]}"; do print -u2 -- "      $f"; done
  print -u2 -- "      Conserto: rode este instalador sem --checar (ele desmarca), ou à mão: xattr -d com.apple.quarantine <arquivo>"
fi

(( SO_CHECAR )) && { print -- ""; print -- "Só diagnóstico (--checar). Nada foi alterado."; exit 0; }

# ─────────────────────────────────────────────────────────────────────────────
if (( COM_DEPS )); then
titulo "2 · Dependências (Homebrew + pipx)"
  if ! command -v brew >/dev/null 2>&1; then
    warn "Homebrew ausente — é PRÉ-REQUISITO MANUAL (pede senha de administrador)."
    warn "Instale primeiro conforme INSTALAR-macOS.md §1 e rode este script de novo."
  else
    say "brew install poppler ocrmypdf tesseract ffmpeg pipx whisper-cpp coreutils qpdf"
    brew install poppler ocrmypdf tesseract ffmpeg pipx whisper-cpp coreutils qpdf || warn "falha em algum pacote Homebrew — veja a saída acima."
    if command -v ebook-convert >/dev/null 2>&1; then ok "ebook-convert já presente (Calibre)"
    else say "brew install --cask calibre (download grande)"; brew install --cask calibre || warn "falha ao instalar o Calibre — ebooks não-EPUB não converterão."; fi

    # MarkItDown: extras alvo. NÃO use [all] — xlrd e youtube-transcript-api quebram no Python 3.14.
    if command -v pipx >/dev/null 2>&1; then
      say "pipx install 'markitdown[pdf,docx,pptx,xlsx,outlook]>=0.1.7'"
      pipx install --force 'markitdown[pdf,docx,pptx,xlsx,outlook]>=0.1.7' || warn "falha ao instalar o MarkItDown via pipx."
      pipx ensurepath >/dev/null 2>&1
      MIV="$(markitdown --version 2>/dev/null | awk '{print $2}')"
      case "$MIV" in 0.0.*|"") warn "MarkItDown em versão '${MIV:-desconhecida}' — o EPUB nativo exige >= 0.1." ;; *) ok "MarkItDown $MIV" ;; esac
    else
      warn "pipx ausente — instale-o (brew install pipx) e reexecute."
    fi

    # Idiomas do Tesseract (por+eng são o padrão da camada; spa disponível).
    # ATENÇÃO: 'brew upgrade tesseract' APAGA estes arquivos — rebaixe depois de cada upgrade.
    TESSDATA="$(brew --prefix 2>/dev/null)/share/tessdata"
    if [ -d "$TESSDATA" ]; then
      for L in por spa; do
        if [ -f "$TESSDATA/$L.traineddata" ]; then ok "tessdata $L presente"
        else say "baixando tessdata $L…"
          curl -fsSL -o "$TESSDATA/$L.traineddata" "https://github.com/tesseract-ocr/tessdata_fast/raw/main/$L.traineddata" || warn "falha ao baixar $L.traineddata"
        fi
      done
      say "idiomas do Tesseract: $(tesseract --list-langs 2>&1 | tail -n +2 | tr '\n' ' ')"
    else
      warn "pasta tessdata não encontrada — instale o tesseract antes."
    fi

    # Modelo do Whisper: download grande, só com o flag explícito.
    WM="$HOME/.local/share/whisper/ggml-small.bin"
    if [ -f "$WM" ]; then ok "modelo Whisper já presente ($WM)"
    elif (( COM_WHISPER )); then
      say "baixando o modelo Whisper small (~481 MB)…"
      mkdir -p "${WM:h}"
      curl -fL --progress-bar -o "$WM" https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin || warn "falha no download do modelo Whisper."
    else
      say "modelo Whisper NÃO baixado (~481 MB). Para baixar: --com-deps --com-modelo-whisper"
      say "  (sem ele, 'transcrever' não funciona; o resto da camada funciona normalmente)"
    fi
  fi
fi

# ─────────────────────────────────────────────────────────────────────────────
titulo "3 · Peças da camada (~/.claude)"
# ─────────────────────────────────────────────────────────────────────────────
mkdir -p "$HOME/.claude/bin" "$HOME/.claude/hooks"
# Remove a quarentena do Gatekeeper do arquivo INSTALADO. Necessário porque o pacote
# costuma chegar por download/mensageiro (WhatsApp, Telegram, navegador), o que marca
# com.apple.quarantine nos arquivos — e o `cp` preserva o xattr. Com a marca, o macOS
# recusa executar o hook e os scripts de ~/.claude/bin ("operation not permitted"), e
# como o hook é fail-open a camada fica instalada e MUDA: nenhum erro aparece, só o
# custo de token de volta ao normal. Roda também no caminho "já atualizado" — reexecutar
# o instalador é justamente como se conserta uma instalação nessa situação.
desquarentenar(){  # $1 = arquivo instalado
  local dst="$1"
  xattr -d com.apple.quarantine "$dst" 2>/dev/null
  if xattr "$dst" 2>/dev/null | grep -q com.apple.quarantine; then
    warn "quarentena do macOS persiste em $dst — o Gatekeeper vai bloquear a execução. Rode: xattr -d com.apple.quarantine \"$dst\""
    return 1
  fi
  return 0
}
instalar_um(){  # $1 = origem no payload ; $2 = destino
  local src="$1" dst="$2"
  [ -f "$src" ] || { warn "payload ausente: $src"; return 1; }
  if [ -f "$dst" ] && cmp -s "$src" "$dst"; then desquarentenar "$dst"; ok "já atualizado: $dst"; return 0; fi
  if [ -f "$dst" ]; then cp "$dst" "$dst.bak.$STAMP" && say "backup: $dst.bak.$STAMP"; fi
  cp "$src" "$dst" && chmod +x "$dst" && desquarentenar "$dst" && ok "instalado: $dst"
}
instalar_um "$PAY/hooks/markitdown-read.sh" "$HOME/.claude/hooks/markitdown-read.sh"
instalar_um "$PAY/bin/provenance.sh"        "$HOME/.claude/bin/provenance.sh"
instalar_um "$PAY/bin/converter-md.sh"      "$HOME/.claude/bin/converter-md.sh"
instalar_um "$PAY/bin/converter-md-ocr.sh"  "$HOME/.claude/bin/converter-md-ocr.sh"

# ─────────────────────────────────────────────────────────────────────────────
titulo "4 · Registro do hook em ~/.claude/settings.json (merge)"
# ─────────────────────────────────────────────────────────────────────────────
SET="$HOME/.claude/settings.json"
python3 - "$SET" "$STAMP" <<'PYMERGE'
import json,os,shutil,sys
p,stamp=sys.argv[1],sys.argv[2]
cmd="$HOME/.claude/hooks/markitdown-read.sh"
try:
    with open(p,encoding='utf-8') as f: cfg=json.load(f)
    if not isinstance(cfg,dict): raise ValueError('settings.json não é um objeto')
except FileNotFoundError:
    cfg={}
except Exception as e:
    print(f"  ⚠︎ settings.json ilegível ({e}). NÃO foi alterado — registre o hook à mão.")
    sys.exit(0)

hooks=cfg.setdefault('hooks',{})
pre=hooks.setdefault('PreToolUse',[])
alvo=None
for e in pre:
    if isinstance(e,dict) and e.get('matcher')=='Read': alvo=e; break
if alvo is None:
    alvo={'matcher':'Read','hooks':[]}; pre.append(alvo)
lista=alvo.setdefault('hooks',[])
if any(isinstance(h,dict) and h.get('command')==cmd for h in lista):
    print("  ✓ hook já registrado (nada a fazer)")
else:
    lista.append({'type':'command','command':cmd,'timeout':90})
    os.makedirs(os.path.dirname(p),exist_ok=True)
    if os.path.exists(p):   # backup só quando há mudança de fato
        shutil.copy2(p,f"{p}.bak.{stamp}"); print(f"• backup: {p}.bak.{stamp}")
    with open(p,'w',encoding='utf-8') as f:
        json.dump(cfg,f,ensure_ascii=False,indent=2); f.write('\n')
    print("  ✓ hook PreToolUse(Read) registrado em "+p)
PYMERGE

# ─────────────────────────────────────────────────────────────────────────────
titulo "5 · Funções de terminal no ~/.zshrc"
# ─────────────────────────────────────────────────────────────────────────────
ZRC="$HOME/.zshrc"; BLK="$PAY/zshrc-CLAUDE-MARKITDOWN.zsh"
if [ ! -f "$BLK" ]; then
  warn "payload ausente: $BLK"
elif [ -f "$ZRC" ] && grep -qF '# >>> CLAUDE-MARKITDOWN >>>' "$ZRC"; then
  # já existe: substitui o conteúdo entre os marcadores pelo canônico (com backup)
  if diff -q <(awk '/# >>> CLAUDE-MARKITDOWN >>>/,/# <<< CLAUDE-MARKITDOWN <<</' "$ZRC") "$BLK" >/dev/null 2>&1; then
    ok "bloco CLAUDE-MARKITDOWN já é o canônico (nada a fazer)"
  else
    cp "$ZRC" "$ZRC.bak.$STAMP" && say "backup: $ZRC.bak.$STAMP"
    awk -v bf="$BLK" '
      /# >>> CLAUDE-MARKITDOWN >>>/ {while((getline l < bf) > 0) print l; pulando=1; next}
      /# <<< CLAUDE-MARKITDOWN <<</ {pulando=0; next}
      pulando {next}
      {print}
    ' "$ZRC" > "$ZRC.tmp.$$" && mv -f "$ZRC.tmp.$$" "$ZRC" \
      && ok "bloco CLAUDE-MARKITDOWN atualizado no ~/.zshrc" \
      || warn "falha ao atualizar o bloco — restaure de $ZRC.bak.$STAMP se preciso."
  fi
else
  [ -f "$ZRC" ] && { cp "$ZRC" "$ZRC.bak.$STAMP" && say "backup: $ZRC.bak.$STAMP"; }
  { print -- ""; cat "$BLK" } >> "$ZRC" && ok "bloco CLAUDE-MARKITDOWN acrescentado ao ~/.zshrc"
fi

# ─────────────────────────────────────────────────────────────────────────────
titulo "6 · Próximos passos"
# ─────────────────────────────────────────────────────────────────────────────
cat <<'FIM'
  1) Recarregue o shell:            source ~/.zshrc
  2) Teste as funções:              md algum.pdf   ·   ocr escaneado.pdf   ·   imgredux foto.jpg
  3) Kit de teste do pacote:        zsh ../testes/gerar-amostras.sh  (e siga testes/TESTES-VERIFICACAO.md)
  4) Hook em ação: abra o Claude Code numa pasta com um PDF e pergunte algo sobre ele —
     a leitura passa pelo Markdown convertido, não pelas imagens das páginas.

  QUICK ACTIONS DO FINDER (etapa manual — o macOS não permite automatizar)
  Os dois scripts de lógica já estão instalados em ~/.claude/bin/. Para o clique-direito
  no Finder, crie os dois invólucros no Automator (bundles .workflow criados por script são
  rejeitados pelo LaunchServices — erro −10811):

    a) Automator → Novo → "Ação Rápida"
    b) "O fluxo de trabalho recebe": arquivos e pastas · em: Finder
    c) Arraste "Executar Script do Shell" · Shell: /bin/zsh · Passar entrada: como argumentos
    d) Conteúdo (uma linha):     "$HOME/.claude/bin/converter-md.sh" "$@"
    e) Salvar como:              Converter para Markdown
    f) Repita para o OCR com:    "$HOME/.claude/bin/converter-md-ocr.sh" "$@"
       Salvar como:              Converter para Markdown (OCR)

  Na primeira execução o macOS pode pedir permissão de acesso a pastas (TCC) — aceite.
FIM

print -- ""
if (( ERROS )); then
  print -- "Concluído com $ERROS aviso(s). Releia os itens marcados com ⚠︎ acima."
else
  print -- "Concluído sem avisos."
fi
print -- "Manual completo: ../manual/camada-preprocessamento-claude-code.html"
