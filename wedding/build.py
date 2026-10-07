#!/usr/bin/env python3
"""
Wedding guest photo pages + QR sticker sheet for madluna.ca/w/<slug>/.

  python build.py slugs     fill in missing slugs in households.csv and make photo folders
  python build.py build     build the guest pages into dist/ (thumbnails + originals)
  python build.py labels    make qr-stickers.pdf, a printable sheet of QR stickers
  python build.py deploy    upload dist/ to Cloudflare so the pages are live (add --dry-run to only check)

See README.md for the full workflow.
"""
import argparse
import csv
import hashlib
import html
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

from PIL import Image, ImageOps

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pillow_heif = None

HERE = Path(__file__).resolve().parent

# ===== settings =====
SITE = 'https://madluna.ca'
BATCH_SIZE = 20          # photos per "save" button; tune after testing on our phones
THUMB_EDGE = 800         # longest side of grid thumbnails, in pixels
THUMB_QUALITY = 78
FILE_PREFIX = 'merrick-leilah-wedding'   # what saved photos are called, e.g. merrick-leilah-wedding-001.jpg
HEADLINE = 'Hi, {name}!'
DEFAULT_MESSAGE = (
    'Thank you so much for celebrating with us. '
    'Here are your photos from our day. Tap any photo to see it bigger, '
    'or save them all straight to your phone.'
)
SIGNOFF = 'Love, Merrick & Leilah'
STICKER_LINE = 'Scan for your photos from our day'

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.heic', '.heif', '.webp'}
SLUG_RE = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
SLUG_CHARS = 'abcdefghjkmnpqrstuvwxyz23456789'   # no 0/o, 1/l/i lookalikes


def paths(data_dir):
    d = Path(data_dir)
    return {
        'csv': d / 'households.csv',
        'photos': d / 'photos',
        'dist': d / 'dist',
        'pdf': d / 'qr-stickers.pdf',
    }


# ===== households.csv =====

def read_households(csv_path):
    if not csv_path.exists():
        sys.exit(f'no {csv_path.name} yet. copy households.example.csv to households.csv and fill it in.')
    try:
        text = csv_path.read_text(encoding='utf-8-sig')
    except UnicodeDecodeError:
        text = csv_path.read_text(encoding='cp1252')   # Excel's plain "CSV" format on Windows
    rows = list(csv.DictReader(text.splitlines()))
    for r in rows:
        r.pop(None, None)   # stray cells past the last column
        for k in list(r):
            r[k] = (r[k] or '').strip()
        for k in ('slug', 'name', 'message'):
            r.setdefault(k, '')
    rows = [r for r in rows if r['name'] or r['slug']]
    seen = set()
    for r in rows:
        if r['slug'] and not SLUG_RE.match(r['slug']):
            sys.exit(f'bad slug "{r["slug"]}": use only lowercase letters, numbers and dashes.')
        if r['slug'] in seen and r['slug']:
            sys.exit(f'slug "{r["slug"]}" is used twice in {csv_path.name}.')
        seen.add(r['slug'])
    return rows


def write_households(csv_path, rows):
    # keep the sheet's own columns (and their order); utf-8-sig so Excel shows accents right
    fields = list(dict.fromkeys(k for r in rows for k in r))
    with open(csv_path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, '') for k in fields})


def make_slug(name, taken):
    plain = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode()   # José -> Jose
    base = re.sub(r'[^a-z0-9]+', '-', plain.lower()).strip('-')[:24].strip('-') or 'guest'
    while True:
        slug = f'{base}-{"".join(secrets.choice(SLUG_CHARS) for _ in range(4))}'
        if slug not in taken:
            return slug


def cmd_slugs(p, args):
    rows = read_households(p['csv'])
    taken = {r['slug'] for r in rows if r['slug']}
    added = 0
    for r in rows:
        if not r['slug']:
            r['slug'] = make_slug(r['name'], taken)
            taken.add(r['slug'])
            added += 1
    if added:
        write_households(p['csv'], rows)
    made = 0
    for r in rows:
        folder = p['photos'] / r['slug']
        if not folder.exists():
            folder.mkdir(parents=True)
            made += 1
    print(f'{added} new slug(s) written to {p["csv"].name}, {made} new photo folder(s) in photos/.')
    for r in rows:
        print(f'  {r["slug"]:<32} {r["name"]}')


# ===== building pages =====

def date_taken(path):
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            return exif.get_ifd(0x8769).get(36867) or exif.get(306) or ''
    except Exception:
        return ''


def file_hash(path):
    h = hashlib.sha1()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def list_photos(folder):
    """Every photo in the folder and its subfolders, minus exact duplicates.

    A household's folder can hold one subfolder per person (e.g. one Google Photos
    download each), so a photo with two of them in it shows up twice. Keep one copy.
    """
    if not folder.is_dir():
        return []
    files = sorted(f for f in folder.rglob('*') if f.is_file() and f.suffix.lower() in IMAGE_EXTS and not f.name.startswith('._'))
    by_size = {}
    for f in files:
        by_size.setdefault(f.stat().st_size, []).append(f)
    unique = []
    for same in by_size.values():
        seen = set()
        for f in same:
            key = file_hash(f) if len(same) > 1 else None
            if key not in seen:
                seen.add(key)
                unique.append(f)
    # date taken first, file name second; photos without a date go last
    return sorted(unique, key=lambda f: (date_taken(f) or '~', f.name.lower()))


def same_file(src, dst):
    if not dst.exists():
        return False
    a, b = src.stat(), dst.stat()
    return a.st_size == b.st_size and int(a.st_mtime) == int(b.st_mtime)


def make_thumb(src, dst):
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode not in ('RGB', 'L'):
            im = im.convert('RGB')
        im.thumbnail((THUMB_EDGE, THUMB_EDGE), Image.LANCZOS)
        dst.parent.mkdir(parents=True, exist_ok=True)
        im.save(dst, 'JPEG', quality=THUMB_QUALITY, optimize=True, progressive=True)


def build_household(row, photos, out_dir, template):
    """Writes one household's page. Returns the set of files it owns."""
    owned = set()
    items = []
    for i, src in enumerate(photos, 1):
        ext = src.suffix.lower().replace('.jpeg', '.jpg')
        name = f'{FILE_PREFIX}-{i:03d}{ext}'

        full = out_dir / 'full' / name
        if not same_file(src, full):
            full.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, full)
        owned.add(full)

        # thumbnail name comes from the source, so reordering photos reuses old thumbnails
        st = src.stat()
        key = hashlib.sha1(f'{src.name}|{st.st_size}|{int(st.st_mtime)}|{THUMB_EDGE}'.encode()).hexdigest()[:12]
        thumb = out_dir / 'thumbs' / f'{key}.jpg'
        if not thumb.exists():
            make_thumb(src, thumb)
        owned.add(thumb)
        with Image.open(thumb) as t:
            w, h = t.size

        items.append({'thumb': f'thumbs/{thumb.name}', 'full': f'full/{name}', 'name': name, 'w': w, 'h': h})

    message = row['message'] or DEFAULT_MESSAGE
    page = (template
            .replace('{{HEADLINE}}', html.escape(HEADLINE.format(name=row['name'])))
            .replace('{{MESSAGE}}', html.escape(message.replace('\\n', '\n')))
            .replace('{{SIGNOFF}}', html.escape(SIGNOFF))
            .replace('{{BATCH_SIZE}}', str(BATCH_SIZE))
            .replace('{{ZIP_NAME_JSON}}', json.dumps(f'{FILE_PREFIX}-photos.zip'))
            .replace('{{PHOTOS_JSON}}', json.dumps(items, separators=(',', ':')).replace('</', '<\\/')))
    index = out_dir / 'index.html'
    index.write_text(page, encoding='utf-8', newline='\n')
    owned.add(index)
    return owned


NOT_FOUND = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow"><title>Page Not Found · Merrick &amp; Leilah</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#4d3138;color:#D3D2D3;
font:16px/1.6 system-ui,sans-serif;text-align:center;padding:16px}a{color:#c9a96a}</style></head>
<body><div><h1 style="font-weight:600;font-size:1.4rem">Hmm, we couldn't find that page</h1>
<p>Try scanning the code on your card again, or <a href="/contact/">let us know</a> and we'll sort it out.</p></div></body></html>
"""

HEADERS = """/*
  X-Robots-Tag: noindex, nofollow, noimageindex
  Referrer-Policy: no-referrer
  X-Content-Type-Options: nosniff

/w/:slug/thumbs/*
  Cache-Control: public, max-age=31536000, immutable

/w/:slug/full/*
  Cache-Control: public, max-age=86400
"""


def cmd_build(p, args):
    rows = read_households(p['csv'])
    if any(not r['slug'] for r in rows):
        sys.exit('some households have no slug yet. run "python build.py slugs" first.')
    if pillow_heif is None:
        print('note: pillow-heif is not installed, so .heic photos will fail. run: python -m pip install pillow-heif')

    template = (HERE / 'template.html').read_text(encoding='utf-8')
    out_w = p['dist'] / 'w'
    out_w.mkdir(parents=True, exist_ok=True)
    keep = set()
    published, empty = 0, []

    for r in rows:
        photos = list_photos(p['photos'] / r['slug'])
        if not photos:
            empty.append(r)
            continue
        print(f'  {r["slug"]:<32} {len(photos):>4} photo{"" if len(photos) == 1 else "s"}   {r["name"]}')
        keep |= build_household(r, photos, out_w / r['slug'], template)
        published += 1

    (out_w / '404.html').write_text(NOT_FOUND, encoding='utf-8', newline='\n')
    (p['dist'] / '_headers').write_text(HEADERS, encoding='utf-8', newline='\n')
    keep |= {out_w / '404.html', p['dist'] / '_headers'}

    # remove anything left over from households or photos that were taken out
    removed = 0
    for f in sorted(p['dist'].rglob('*'), reverse=True):
        if f.is_file() and f not in keep:
            f.unlink()
            removed += 1
        elif f.is_dir() and not any(f.iterdir()):
            f.rmdir()

    print(f'\nbuilt {published} page(s) in {p["dist"]}' + (f', removed {removed} old file(s)' if removed else ''))
    if empty:
        print(f'{len(empty)} household(s) have no photos yet, so they get no page (their QR code would show "not found"):')
        for r in empty:
            print(f'  {r["slug"]:<32} {r["name"]}')
    big = [f for f in p['dist'].rglob('*') if f.is_file() and f.stat().st_size > 25 * 1024 * 1024]
    if big:
        print('\nwarning: Cloudflare will not accept files over 25 MiB. shrink these first:')
        for f in big:
            print(f'  {f.relative_to(p["dist"])}')


# ===== QR sticker sheet =====

# Avery-style sheets, all in inches on US Letter paper. Margins are best guesses:
# print with --outline on plain paper and hold it against a label sheet to check before using real labels.
SHEETS = {
    '22806': dict(desc='Avery 22806, 2" x 2" square, 12 per sheet', cols=3, rows=4,
                  w=2.0, h=2.0, left=0.625, top=0.6, hpitch=2.625, vpitch=2.6, layout='stack'),
    '22807': dict(desc='Avery 22807, 2" round, 12 per sheet', cols=3, rows=4,
                  w=2.0, h=2.0, left=0.625, top=0.6, hpitch=2.625, vpitch=2.6, layout='round'),
    '5160': dict(desc='Avery 5160, 1" x 2-5/8" address label, 30 per sheet', cols=3, rows=10,
                 w=2.625, h=1.0, left=0.1875, top=0.5, hpitch=2.75, vpitch=1.0, layout='side'),
}


def draw_qr(c, url, x, y, size):
    from reportlab.graphics import renderPDF
    from reportlab.graphics.barcode.qr import QrCodeWidget
    from reportlab.graphics.shapes import Drawing
    widget = QrCodeWidget(url, barLevel='M', barBorder=4)   # 4-module white quiet zone built in
    x1, y1, x2, y2 = widget.getBounds()
    d = Drawing(size, size, transform=[size / (x2 - x1), 0, 0, size / (y2 - y1), 0, 0])
    d.add(widget)
    renderPDF.draw(d, c, x, y)


def fit_font(c, text, font, size, max_w):
    while size > 4 and c.stringWidth(text, font, size) > max_w:
        size -= 0.25
    return size


def draw_label(c, sheet, x, y, url, name, show_name, outline):
    from reportlab.lib.units import inch
    w, h = sheet['w'] * inch, sheet['h'] * inch
    if outline:
        c.setStrokeGray(0.6)
        c.setLineWidth(0.5)
        if sheet['layout'] == 'round':
            c.circle(x + w / 2, y + h / 2, w / 2)
        else:
            c.roundRect(x, y, w, h, 6)
    c.setFillGray(0.15)
    tagline_font, name_font = 'Times-Italic', 'Helvetica'

    if sheet['layout'] in ('stack', 'round'):
        pad = 0.12 * inch if sheet['layout'] == 'stack' else 0.3 * inch
        text_h = (0.34 if show_name else 0.2) * inch
        qr = min(w - 2 * pad, h - 2 * pad - text_h) if sheet['layout'] == 'stack' else 1.2 * inch
        qx = x + (w - qr) / 2
        qy = y + h - pad - qr if sheet['layout'] == 'stack' else y + h - 0.2 * inch - qr
        draw_qr(c, url, qx, qy, qr)
        max_w = w - 2 * pad if sheet['layout'] == 'stack' else 1.5 * inch
        size = fit_font(c, STICKER_LINE, tagline_font, 9, max_w)
        ty = qy - 0.02 * inch - size
        c.setFont(tagline_font, size)
        c.drawCentredString(x + w / 2, ty, STICKER_LINE)
        if show_name:
            nsize = fit_font(c, name, name_font, 6.5, max_w * (0.8 if sheet['layout'] == 'round' else 1))
            c.setFont(name_font, nsize)
            c.setFillGray(0.45)
            c.drawCentredString(x + w / 2, ty - nsize - 3, name)
    else:  # side: code on the left, words on the right
        pad = 0.06 * inch
        qr = h - 2 * pad
        draw_qr(c, url, x + pad, y + pad, qr)
        tx = x + pad + qr + 0.06 * inch
        max_w = x + w - 0.1 * inch - tx
        words = STICKER_LINE.split()
        mid = len(words) // 2 + 1
        lines = [' '.join(words[:mid]), ' '.join(words[mid:])]
        size = min(fit_font(c, ln, tagline_font, 10, max_w) for ln in lines)
        c.setFont(tagline_font, size)
        ty = y + h / 2 + size * 0.2 + (4 if show_name else 0)
        c.drawString(tx, ty, lines[0])
        c.drawString(tx, ty - size * 1.15, lines[1])
        if show_name:
            nsize = fit_font(c, name, name_font, 6.5, max_w)
            c.setFont(name_font, nsize)
            c.setFillGray(0.45)
            c.drawString(tx, ty - size * 1.15 - nsize - 4, name)


def cmd_labels(p, args):
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas

    sheet = SHEETS[args.sheet]
    rows = read_households(p['csv'])
    if any(not r['slug'] for r in rows):
        sys.exit('some households have no slug yet. run "python build.py slugs" first.')
    if args.only:
        rows = [r for r in rows if r['slug'] in args.only]
    rows = [r for r in rows for _ in range(args.copies)]
    if not rows:
        sys.exit('no households to print.')

    c = canvas.Canvas(str(p['pdf']), pagesize=letter)
    c.setTitle('madluna wedding QR stickers')
    page_h = letter[1]
    per_page = sheet['cols'] * sheet['rows']
    slots = list(range(args.skip, args.skip + len(rows)))
    for n, (slot, r) in enumerate(zip(slots, rows)):
        if n and slot % per_page == 0:
            c.showPage()
        k = slot % per_page
        col, row = k % sheet['cols'], k // sheet['cols']
        x = (sheet['left'] + col * sheet['hpitch']) * inch
        y = page_h - (sheet['top'] + row * sheet['vpitch'] + sheet['h']) * inch
        draw_label(c, sheet, x, y, f'{SITE}/w/{r["slug"]}', r['name'], not args.no_names, args.outline)
    c.save()

    pages = (args.skip + len(rows) + per_page - 1) // per_page
    print(f'wrote {p["pdf"].name}: {len(rows)} sticker(s) on {pages} page(s), {sheet["desc"]}.')
    print('print at 100% / "actual size" (not "fit to page"), and test-scan a few before printing the whole batch.')
    folder = p['photos']
    missing = [r for r in rows if not list_photos(folder / r['slug'])]
    if missing:
        print(f'heads up: {len(missing)} of these have no photos yet, so their code would show "not found".')


def cmd_deploy(p, args):
    if not (p['dist'] / 'w').is_dir():
        sys.exit('nothing built yet. run "python build.py build" first.')
    if p['dist'].resolve() != (HERE / 'dist').resolve():
        sys.exit('deploy only works with the default --data folder (wrangler.jsonc points at ./dist).')
    # npx can't start inside a network-drive folder (\\server\share), so run it from the home
    # folder and point it at our config; the config's ./dist is resolved next to the config file.
    cmd = ['npx.cmd' if os.name == 'nt' else 'npx', '--yes', 'wrangler@4', 'deploy', '--config', str(HERE / 'wrangler.jsonc')]
    if args.dry_run:
        cmd.append('--dry-run')
    result = subprocess.run(cmd, cwd=Path.home())
    if result.returncode != 0:
        sys.exit('deploy failed. if it says "Not logged in", run: npx wrangler login')
    if not args.dry_run:
        print(f'\nlive. each household is at {SITE}/w/<slug>')


def main():
    ap = argparse.ArgumentParser(description='wedding guest photo pages for madluna.ca/w/')
    ap.add_argument('--data', default=str(HERE), help='folder holding households.csv, photos/ and dist/ (default: this folder)')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('slugs', help='fill in missing slugs and make empty photo folders')
    sub.add_parser('build', help='build the guest pages into dist/')
    lp = sub.add_parser('labels', help='make the printable QR sticker PDF')
    lp.add_argument('--sheet', choices=sorted(SHEETS), default='22806', help='label sheet type (default 22806)')
    lp.add_argument('--outline', action='store_true', help='draw label outlines, for test prints on plain paper')
    lp.add_argument('--no-names', action='store_true', help="leave the household's name off each sticker")
    lp.add_argument('--only', nargs='+', metavar='SLUG', help='only these households (e.g. to reprint one)')
    lp.add_argument('--skip', type=int, default=0, help='skip this many labels at the start (for a part-used sheet)')
    lp.add_argument('--copies', type=int, default=1, help='stickers per household')
    dp = sub.add_parser('deploy', help='upload the built pages to Cloudflare')
    dp.add_argument('--dry-run', action='store_true', help='check everything but upload nothing')
    args = ap.parse_args()

    p = paths(args.data)
    {'slugs': cmd_slugs, 'build': cmd_build, 'labels': cmd_labels, 'deploy': cmd_deploy}[args.cmd](p, args)


if __name__ == '__main__':
    main()
