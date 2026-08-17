import re, sys, time, json
from pypdf import PdfReader
p = sys.argv[1]
t0 = time.time()
r = PdfReader(p)
n = len(r.pages)
CNJ = re.compile(r'\d{7}-\d{2}\.\d{4}\.5\.\d{2}\.\d{4}')
OAB = re.compile(r'OAB:\s*([0-9A-Za-z]+/[A-Z]{2})')
cnjs, oabs, chars, vazias = set(), set(), 0, 0
for i, pg in enumerate(r.pages):
    try:
        txt = pg.extract_text() or ''
    except Exception:
        txt = ''
    if len(txt.strip()) < 30:
        vazias += 1
    chars += len(txt)
    cnjs.update(CNJ.findall(txt))
    oabs.update(OAB.findall(txt))
    if i % 2000 == 0:
        print(f'  ...pag {i}/{n} {time.time()-t0:.0f}s', flush=True)
out = dict(arquivo=p, paginas=n, chars=chars, paginas_sem_texto=vazias,
           cnjs_distintos=len(cnjs), oabs_distintos=len(oabs),
           segundos=round(time.time()-t0, 1))
print(json.dumps(out, ensure_ascii=False, indent=2))
