"""Exact PDF companion for the artifact-tool audience slide renders."""
from pathlib import Path
import sys
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from pypdf import PdfReader

root = Path(sys.argv[1] if len(sys.argv) > 1 else '.').resolve()
stage = root / 'tmp/audience-revision'
slides = sorted((stage / 'render/slides').glob('slide-*.png'))
assert len(slides) == 10
pdf = stage / 'deliverable/astana-walks-current.pdf'
c = canvas.Canvas(str(pdf), pagesize=(960, 540))
c.setTitle('Astana Walks')
c.setAuthor('Astana Walks')
for slide in slides:
    c.drawImage(ImageReader(str(slide)), 0, 0, width=960, height=540)
    c.showPage()
c.save()
assert len(PdfReader(pdf).pages) == 10
