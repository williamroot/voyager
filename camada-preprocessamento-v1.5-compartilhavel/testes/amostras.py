#!/usr/bin/env python3
"""amostras.py — gera arquivos de amostra para testar a Camada de Pré-processamento.

Multiplataforma (só a biblioteca padrão do Python). Chamado pelos wrappers
gerar-amostras.sh (macOS) e gerar-amostras.ps1 (Windows), que acrescentam as
amostras que dependem do sistema (imagem grande via ffmpeg e áudio falado).

Uso:  python3 amostras.py [pasta-de-saida]

Gera:
  01-pdf-digital.pdf     PDF com camada de texto nativa   → testa a rota MarkItDown
  02-pdf-escaneado.pdf   PDF só-imagem (sem texto)        → testa a rota OCR
  03-ebook.epub          EPUB mínimo válido               → testa a rota de ebook
  04-planilha.xlsx       XLSX mínimo válido               → testa a rota Office

O PDF escaneado é o digital rasterizado com o pdftoppm (poppler). Se o poppler não
estiver no PATH, essa amostra é pulada com aviso — as outras continuam.
"""
import os
import shutil
import struct
import subprocess
import sys
import zipfile

TEXTO = [
    "AMOSTRA DE TESTE - CAMADA DE PRE-PROCESSAMENTO",
    "",
    "Este documento existe para validar a conversao para Markdown.",
    "Frase-chave de verificacao: PIPOCA-VERDE-1568",
    "",
    "Valores para conferir no OCR: R$ 12.345,67 - clausula 4.2 - CNPJ 00.000.000/0001-91",
    "Acentuacao: coracao, atencao, pressao, judicial, licao.",
]


# ─────────────────────────────────────────── PDF com texto nativo
def pdf_digital(destino):
    """Monta um PDF 1.4 mínimo com texto Helvetica (sem dependências externas)."""
    linhas = ["BT", "/F1 12 Tf", "72 720 Td", "14 TL"]
    for l in TEXTO:
        seguro = l.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        linhas.append(f"({seguro}) Tj T*")
    linhas.append("ET")
    fluxo = "\n".join(linhas).encode("latin-1", "replace")

    objetos = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(fluxo)).encode() + b" >>\nstream\n" + fluxo + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    _escrever_pdf(destino, objetos)
    return destino


# ─────────────────────────────────────────── PDF só-imagem (escaneado)
def pdf_escaneado(destino, origem):
    """Rasteriza o PDF digital com pdftoppm e embute o JPEG num PDF sem camada de texto."""
    if not shutil.which("pdftoppm"):
        print("  ⚠︎ pdftoppm (poppler) ausente — pulei 02-pdf-escaneado.pdf")
        return None
    base = os.path.join(os.path.dirname(destino) or ".", "_raster")
    try:
        subprocess.run(
            ["pdftoppm", "-jpeg", "-r", "150", "-f", "1", "-l", "1", origem, base],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        print(f"  ⚠︎ falha ao rasterizar ({e}) — pulei 02-pdf-escaneado.pdf")
        return None

    pasta = os.path.dirname(base) or "."
    alvo = os.path.basename(base)
    jpgs = sorted(f for f in os.listdir(pasta)
                  if f.startswith(alvo) and f.lower().endswith((".jpg", ".jpeg")))
    if not jpgs:
        print("  ⚠︎ pdftoppm não gerou JPEG — pulei 02-pdf-escaneado.pdf")
        return None
    caminho_jpg = os.path.join(pasta, jpgs[0])
    with open(caminho_jpg, "rb") as f:
        jpg = f.read()
    os.remove(caminho_jpg)

    dim = _dimensoes_jpeg(jpg)
    if not dim:
        print("  ⚠︎ JPEG inesperado — pulei 02-pdf-escaneado.pdf")
        return None
    w, h = dim
    pw, ph = w * 72.0 / 150.0, h * 72.0 / 150.0  # 150 dpi → pontos
    conteudo = f"q {pw:.2f} 0 0 {ph:.2f} 0 0 cm /Im0 Do Q".encode()

    objetos = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {pw:.2f} {ph:.2f}] "
        f"/Resources << /XObject << /Im0 5 0 R >> >> /Contents 4 0 R >>".encode(),
        b"<< /Length " + str(len(conteudo)).encode() + b" >>\nstream\n" + conteudo + b"\nendstream",
        b"<< /Type /XObject /Subtype /Image /Width " + str(w).encode()
        + b" /Height " + str(h).encode()
        + b" /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length "
        + str(len(jpg)).encode() + b" >>\nstream\n" + jpg + b"\nendstream",
    ]
    _escrever_pdf(destino, objetos)
    return destino


def _dimensoes_jpeg(dados):
    i = 2
    while i < len(dados) - 9:
        if dados[i] != 0xFF:
            i += 1
            continue
        marcador = dados[i + 1]
        if marcador in (0xC0, 0xC1, 0xC2, 0xC3):
            h, w = struct.unpack(">HH", dados[i + 5:i + 9])
            return w, h
        if marcador in (0xD8, 0xD9) or 0xD0 <= marcador <= 0xD7:
            i += 2
            continue
        (tam,) = struct.unpack(">H", dados[i + 2:i + 4])
        i += 2 + tam
    return None


def _escrever_pdf(destino, objetos):
    saida = bytearray(b"%PDF-1.4\n")
    posicoes = []
    for n, corpo in enumerate(objetos, start=1):
        posicoes.append(len(saida))
        saida += f"{n} 0 obj\n".encode() + corpo + b"\nendobj\n"
    inicio_xref = len(saida)
    saida += f"xref\n0 {len(objetos)+1}\n".encode()
    saida += b"0000000000 65535 f \n"
    for p in posicoes:
        saida += f"{p:010d} 00000 n \n".encode()
    saida += (f"trailer\n<< /Size {len(objetos)+1} /Root 1 0 R >>\nstartxref\n"
              f"{inicio_xref}\n%%EOF\n").encode()
    with open(destino, "wb") as f:
        f.write(bytes(saida))


# ─────────────────────────────────────────── EPUB
def epub(destino):
    container = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""
    opf = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Amostra da Camada</dc:title><dc:creator>Kit de testes</dc:creator>
    <dc:language>pt-BR</dc:language><dc:identifier id="id">urn:uuid:amostra-camada</dc:identifier>
  </metadata>
  <manifest><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/></manifest>
  <spine><itemref idref="c1"/></spine>
</package>"""
    corpo = ("<h1>Capitulo de amostra</h1>"
             + "".join(f"<p>{l}</p>" for l in TEXTO if l))
    xhtml = f"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>c1</title></head>
<body>{corpo}</body></html>"""
    with zipfile.ZipFile(destino, "w") as z:
        # o mimetype precisa ser o primeiro item e ficar sem compressão
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                   compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/c1.xhtml", xhtml)
    return destino


# ─────────────────────────────────────────── XLSX
def xlsx(destino):
    valores = [("Item", "Valor"), ("Frase-chave", "PIPOCA-VERDE-1568"),
               ("Honorarios", "12345.67"), ("Paginas", "3")]
    linhas = []
    for i, (a, b) in enumerate(valores, start=1):
        linhas.append(
            f'<row r="{i}"><c r="A{i}" t="inlineStr"><is><t>{a}</t></is></c>'
            f'<c r="B{i}" t="inlineStr"><is><t>{b}</t></is></c></row>'
        )
    sheet = ('<?xml version="1.0" encoding="UTF-8"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             "<sheetData>" + "".join(linhas) + "</sheetData></worksheet>")
    workbook = ('<?xml version="1.0" encoding="UTF-8"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="Amostra" sheetId="1" r:id="rId1"/></sheets></workbook>')
    wb_rels = ('<?xml version="1.0" encoding="UTF-8"?>'
               '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
               '<Relationship Id="rId1" '
               'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
               'Target="worksheets/sheet1.xml"/></Relationships>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/></Relationships>')
    content_types = ('<?xml version="1.0" encoding="UTF-8"?>'
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     '<Default Extension="rels" '
                     'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                     '<Default Extension="xml" ContentType="application/xml"/>'
                     '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-'
                     'officedocument.spreadsheetml.sheet.main+xml"/>'
                     '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.'
                     'openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return destino


def main():
    pasta = sys.argv[1] if len(sys.argv) > 1 else "amostras"
    os.makedirs(pasta, exist_ok=True)
    p = lambda n: os.path.join(pasta, n)

    digital = pdf_digital(p("01-pdf-digital.pdf"))
    print(f"  ✓ {digital}")
    escaneado = pdf_escaneado(p("02-pdf-escaneado.pdf"), digital)
    if escaneado:
        print(f"  ✓ {escaneado}  (sem camada de texto — exige OCR)")
    print(f"  ✓ {epub(p('03-ebook.epub'))}")
    print(f"  ✓ {xlsx(p('04-planilha.xlsx'))}")
    print("")
    print("  Frase-chave presente em todas as amostras: PIPOCA-VERDE-1568")


if __name__ == "__main__":
    main()
