# Roteiro de verificação — Camada Local de Pré-processamento v1.5

Roteiro para confirmar que a camada está funcionando de ponta a ponta, com arquivos de
amostra gerados na hora (nada de material real). São os mesmos testes **V1–V6** usados na
validação original no macOS, mais os itens de ebook da v1.5.

## 0 · Gerar as amostras

**macOS**
```bash
cd testes
zsh gerar-amostras.sh          # cria ./amostras com 6 arquivos
```

**Windows (PowerShell)**
```powershell
cd testes
.\gerar-amostras.ps1           # cria .\amostras com 6 arquivos
```

| Amostra | O que é | Rota que exercita |
|---|---|---|
| `01-pdf-digital.pdf` | PDF com camada de texto nativa | MarkItDown |
| `02-pdf-escaneado.pdf` | PDF só-imagem, sem texto | OCR (ocrmypdf + Tesseract) |
| `03-ebook.epub` | EPUB mínimo válido | ebook nativo (MarkItDown 0.1.x) |
| `04-planilha.xlsx` | XLSX mínimo válido | Office |
| `05-imagem-grande.png` | 3000×2000 px | redimensionamento para 1568 px |
| `06-audio.m4a` / `.wav` | fala sintetizada | ffmpeg + Whisper |

Todas as amostras textuais contêm a **frase-chave `PIPOCA-VERDE-1568`** — é ela que você
procura nas saídas para saber se a conversão trouxe o conteúdo certo.

---

## V1 · Binários no PATH

**macOS**
```bash
for b in markitdown ocrmypdf tesseract pdfinfo ffmpeg ffprobe whisper-cli gtimeout qpdf ebook-convert; do
  printf '%-16s ' "$b"; command -v "$b" || echo "FALTANDO"
done
tesseract --list-langs      # esperado incluir: eng, por, spa
```

**Windows**
```powershell
foreach ($b in 'markitdown','ocrmypdf','tesseract','pdfinfo','ffmpeg','ffprobe','whisper','ebook-convert') {
  $c = Get-Command $b -ErrorAction SilentlyContinue
  "{0,-16} {1}" -f $b, ($(if($c){$c.Source}else{'FALTANDO'}))
}
tesseract --list-langs
```

✅ **Passa se:** todos respondem com um caminho e `por` aparece nos idiomas.
Sem `por`, o OCR em português volta vazio.

---

## V2 · Documento digital → Markdown (função `md`)

```bash
md amostras/01-pdf-digital.pdf                 # macOS e Windows (mesma sintaxe)
grep -c PIPOCA-VERDE-1568 amostras/01-pdf-digital.md
```
```powershell
md amostras\01-pdf-digital.pdf
Select-String PIPOCA-VERDE-1568 amostras\01-pdf-digital.md
```

✅ **Passa se:** o `.md` é criado e contém a frase-chave.

---

## V3 · PDF escaneado → OCR (função `ocr`)

```bash
ocr amostras/02-pdf-escaneado.pdf
grep -c PIPOCA-VERDE-1568 amostras/02-pdf-escaneado.md
```

✅ **Passa se:** saem dois arquivos — `02-pdf-escaneado.ocr.pdf` (PDF pesquisável) e
`02-pdf-escaneado.md` — e o `.md` contém a frase-chave (o OCR pode inserir espaços a mais
em outras palavras; isso é normal).

⚠︎ **Contraprova útil:** `markitdown amostras/02-pdf-escaneado.pdf` (sem OCR) deve voltar
**vazio**. É exatamente esse vazio que faz o hook decidir pelo OCR.

📌 **Regra do protocolo:** o `.ocr.pdf` **não se descarta** — é o fallback pesquisável.
Arquive-o em `MD/_PDF-pesquisavel/` e referencie no cabeçalho de proveniência.

---

## V4 · Mídia → transcrição (função `transcrever`)

```bash
transcrever amostras/06-audio.m4a
cat amostras/06-audio.md
```
```powershell
transcrever amostras\06-audio.wav
Get-Content amostras\06-audio.md
```

✅ **Passa se:** sai um `.md` com a fala transcrita (a frase-chave aparece em palavras:
"pipoca verde 1568" — o Whisper escreve números por extenso ou em dígitos, tanto faz).
Sem transcrição, confira o caminho do modelo: `echo $WHISPER_MODEL` (macOS).

---

## V5 · Imagem grande → reduzida (função `imgredux`)

```bash
imgredux amostras/05-imagem-grande.png
ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=p=0:s=x amostras/05-imagem-grande_1568.png
```

✅ **Passa se:** o lado maior da saída é **1568** (aqui: `1568x1045`).

---

## V6 · Ebook → Markdown (v1.5)

```bash
md amostras/03-ebook.epub && head -12 amostras/03-ebook.md
markitdown --version        # precisa ser >= 0.1 (a 0.0.x não converte EPUB)
```

✅ **Passa se:** o `.md` traz os metadados (título/autor/idioma) e o texto do capítulo.

**Ebook não-EPUB** (opcional, exige Calibre): converta um `.mobi`/`.azw3` seu com
`md livro.mobi`. Por baixo, o `ebook-convert` normaliza para `.epub` antes do MarkItDown.
Ebook com **DRM não converte** — é esperado, não é falha da camada.

---

## V7 · Hook automático em ação (o teste que mais importa)

O hook é o que economiza tokens sem você pedir nada.

1. Copie `amostras/01-pdf-digital.pdf` para uma pasta qualquer de trabalho.
2. Abra o Claude Code nessa pasta: `claude`
3. Pergunte: `qual a frase-chave de verificação do arquivo 01-pdf-digital.pdf?`

✅ **Passa se:** ele responde `PIPOCA-VERDE-1568` **e** a leitura aparece apontando para um
arquivo `.md` em `~/.cache/claude-markitdown/` (não para o PDF original). Em vez das
páginas renderizadas como imagem, ele leu o Markdown convertido.

**Confira o cache** (prova material da conversão):
```bash
ls -lt ~/.cache/claude-markitdown | head        # macOS
```
```powershell
Get-ChildItem "$env:USERPROFILE\.cache\claude-markitdown" | Sort-Object LastWriteTime -Desc | Select-Object -First 5
```

**Teste da trava de OCR:** repita com `02-pdf-escaneado.pdf` (1 página, ≤ 10) — o hook faz
OCR sozinho e responde a frase-chave. Um escaneado com **mais de 10 páginas** não é
OCRado automaticamente: o hook mantém o original e avisa para rodar a função `ocr` no
terminal. É proteção contra travar a sessão.

**Fail-open:** se qualquer etapa falhar, o hook não emite nada e a leitura segue normal.
Nenhuma resposta é perdida por causa da camada.

---

## V8 · Cabeçalho de proveniência (obrigatório no protocolo)

Todo `.md` entregue precisa começar pelo cabeçalho.

```bash
provmd amostras/01-pdf-digital.md --tipo digital --origem "01-pdf-digital.pdf"
provmd amostras/02-pdf-escaneado.md --tipo ocr --origem "02-pdf-escaneado.pdf" \
       --fallback "_PDF-pesquisavel/02-pdf-escaneado.ocr.pdf"
head -10 amostras/02-pdf-escaneado.md
```
```powershell
provmd -File amostras\01-pdf-digital.md -Tipo digital -Origem "01-pdf-digital.pdf"
```

✅ **Passa se:** a primeira linha é `<!-- PROVENIENCIA-CAMADA v1 -->`, seguida do
blockquote com Origem/Método/Ressalva/Verificação. Rodar de novo **não duplica** o
cabeçalho (é idempotente) — confirme rodando duas vezes.

Tipos: `digital`, `ocr`, `ocr-fraco`, `midia`, `particao`.

---

## V9 · Quick Actions / menu de contexto

**macOS** (depois de criar as duas ações no Automator — ver `macos/INSTALAR-macOS.md` §6):
```bash
QA_SILENT=1 ~/.claude/bin/converter-md-ocr.sh amostras/02-pdf-escaneado.pdf
tail -n 20 ~/Library/Logs/converter-markdown-ocr.log
```
Depois teste pelo Finder: clique-direito no PDF → Ações Rápidas → *Converter para Markdown*.

**Windows:** clique-direito no PDF → Enviar para → `converter-md`. O `.md` sai ao lado do
original.

✅ **Passa se:** o log mostra `ok=1 fail=0` e o `.md` aparece ao lado do original.

---

## Registro do resultado

| Teste | macOS | Windows | Observação |
|---|---|---|---|
| V1 binários | | | |
| V2 digital → MD | | | |
| V3 OCR | | | |
| V4 transcrição | | | |
| V5 imagem | | | |
| V6 ebook | | | |
| V7 hook | | | |
| V8 proveniência | | | |
| V9 Quick Action | | | |

A trilha **macOS já foi validada** (V1–V6 na instalação original). A trilha **Windows é
referência a validar** — se você rodar, anote o que precisou ajustar: é a informação mais
valiosa que este pacote pode receber de volta.

## Se algo falhar

Consulte a seção *Solução de problemas* do manual
(`manual/camada-preprocessamento-claude-code.html`). Os erros mais comuns:

- **Hook não converte nada** → PATH: hooks rodam com ambiente enxuto; confirme os binários.
- **OCR trava** → falta o `gtimeout` (macOS: `brew install coreutils`).
- **Escaneado volta vazio** → idiomas do Tesseract (`por`) ausentes.
- **`brew upgrade tesseract` apagou o `por`** → é esperado; rebaixe os `.traineddata`.
- **Sem transcrição** → caminho do modelo (`WHISPER_MODEL`) ou modelo não baixado.
- **Quick Action desapareceu** → erro −10811: recrie o bundle no Automator.

## Limpeza

As amostras e as saídas ficam todas dentro de `testes/amostras/` — apague a pasta quando
terminar. Para limpar o cache do hook:
```bash
rm -rf ~/.cache/claude-markitdown        # macOS
```
```powershell
Remove-Item "$env:USERPROFILE\.cache\claude-markitdown" -Recurse -Force
```
