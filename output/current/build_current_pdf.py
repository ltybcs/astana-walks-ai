"""Render the artifact-tool slide exports as an exact 16:9 PDF companion."""
from pathlib import Path
import hashlib
import json
import sys
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from PIL import Image, ImageOps, ImageDraw
from pypdf import PdfReader

root = Path(sys.argv[1] if len(sys.argv) > 1 else '.').resolve()
out = root / 'output/current'
build = root / 'tmp/current-submission'
slides = sorted((build / 'slides').glob('slide-*.png'))
assert len(slides) == 10, 'Ten artifact-tool rendered slides are required'
pdf = out / 'astana-walks-current.pdf'
c = canvas.Canvas(str(pdf), pagesize=(960, 540))
c.setTitle('Astana Walks — текущая объединённая версия, 20.10.2026')
c.setAuthor('Astana Walks team')
for slide in slides:
    c.drawImage(ImageReader(str(slide)), 0, 0, width=960, height=540)
    c.showPage()
c.save()
assert len(PdfReader(pdf).pages) == 10
sheet = Image.new('RGB', (1280, 1800), '#ffffff')
for i, slide in enumerate(slides):
    image = Image.open(slide).convert('RGB')
    sheet.paste(ImageOps.contain(image, (640, 360)), ((i % 2) * 640, (i // 2) * 360))
sheet.save(build / 'contact-sheet.jpg', quality=92)
receipt = {
    'schema_version': 1,
    'created_for': 'current unified submission, 2026-10-20',
    'slides': 10,
    'estimated_speech_seconds': 380,
    'pdf_method': 'Exact artifact-tool slide PNG exports placed as PDF pages; PPTX retains native editable text and table.',
    'test_boundary': '386 total / 384 passed / 2 Windows skips at source 8d49; frontend82 and PWA23 included. Subsequent iframe5 separate.',
    'screenshots': [{'path': 'output/current/assets/' + name, 'sha256': hashlib.sha256((out / 'assets' / name).read_bytes()).hexdigest(), 'source': '0bb, historical UI evidence, not current pharmacy live proof'} for name in ('category-mobile-initial-390.jpg', 'category-mobile-route-390.jpg')],
    'files': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in out.iterdir() if p.is_file() and p.name != 'current-submission-check.json'},
    'youtube_url': None,
    'owner_permanent_url_status': 'ltybcs.com/demo planned, not confirmed deployed',
    'qa': {'all_slide_renders_created': True, 'pdf_pages': 10, 'visual_review': 'pending'}
}
(out / 'current-submission-check.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps({'pdf': str(pdf), 'pages': 10, 'contact_sheet': str(build / 'contact-sheet.jpg')}))
