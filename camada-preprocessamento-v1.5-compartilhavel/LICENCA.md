# Licença e componentes de terceiros

## Licença deste pacote

Os arquivos deste pacote (manual, scripts do hook, funções de shell, script de proveniência,
instaladores, kit de testes) são distribuídos sob a licença **MIT**.

```
Copyright (c) 2026 Diego Machado

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

Em português, o essencial: use, adapte e redistribua à vontade, mantendo este aviso — e
**sem garantia de qualquer espécie**.

## Componentes de terceiros

Este pacote **não redistribui** nenhuma das ferramentas abaixo: os scripts apenas as chamam,
e cada uma é baixada da fonte oficial pelo instalador, sob a própria licença.

| Componente | Papel na camada | Licença (referência) |
|---|---|---|
| Claude Code (Anthropic) | executa a camada; hospeda o hook | termos da Anthropic |
| MarkItDown (Microsoft) | conversor principal → Markdown | MIT |
| OCRmyPDF | OCR de PDF escaneado | MPL 2.0 |
| Tesseract OCR | motor de OCR | Apache 2.0 |
| Poppler | `pdfinfo`, `pdftoppm`, `pdftotext` | GPL 2.0 |
| ffmpeg | áudio/vídeo e redimensionamento | LGPL 2.1+ / GPL (depende do build) |
| whisper.cpp / openai-whisper | transcrição de mídia | MIT |
| Modelo Whisper `ggml-small.bin` | modelo de transcrição | MIT (OpenAI Whisper) |
| Calibre (`ebook-convert`) | ebooks não-EPUB → EPUB | **GPL 3.0** |
| Ghostscript | usado pelo OCRmyPDF (Windows) | AGPL 3.0 / comercial |
| qpdf | partição de PDF gigante | Apache 2.0 |
| Homebrew / winget | gerenciadores de pacote | BSD 2-Clause / MIT |

As licenças acima são referência para orientar a conferência, não uma opinião jurídica.
Se o uso for comercial ou embutido em produto, confirme os termos de cada componente na
fonte — em especial **Calibre (GPL 3.0)** e **Ghostscript (AGPL 3.0 / comercial)**.

## Dados

O pacote não contém material convertido, documento de origem, credencial nem configuração de
máquina. As amostras usadas nos testes são **geradas na hora** por `testes/amostras.py`.
