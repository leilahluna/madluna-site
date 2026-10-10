#!/usr/bin/env python3
"""
Wedding guest photo pages + QR sticker sheet for madluna.ca/w/<slug>/.

  python build.py slugs     fill in missing slugs and passwords in households.csv and make photo folders
  python build.py build     build the guest pages into dist/ (thumbnails + originals)
  python build.py labels    make qr-stickers.pdf, a printable sheet of QR stickers with passwords
  python build.py deploy    upload dist/ to Cloudflare so the pages are live (add --dry-run to only check)
  python build.py uploads   download photos guests sent us into uploads/ (add --clear to free Cloudflare storage)

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
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image, ImageOps, ImageSequence

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pillow_heif = None

HERE = Path(__file__).resolve().parent

# ===== settings =====
SITE = 'https://madluna.ca'
BATCH_SIZE = 20          # photos per "save" button; tune after testing on our phones
THUMB_EDGE = 600         # longest side of grid thumbnails, in pixels
THUMB_QUALITY = 75
VIEW_EDGE = 1800         # longest side of the copy shown when a photo is tapped (saving always gets the original)
VIEW_QUALITY = 82
GIF_THUMB_EDGE = 480     # animated thumbnails keep every frame, so they're kept smaller
FILE_PREFIX = 'merrick-leilah-wedding'   # what saved photos are called, e.g. merrick-leilah-wedding-001.jpg
HEADLINE = 'Hi, {name}!'
DEFAULT_MESSAGE = (
    'Thank you so much for celebrating with us. '
    'Here are your photos from our day. Tap any photo to see it bigger, '
    'or save them all straight to your phone.'
)
SIGNOFF = 'Love, Merrick & Leilah'
STICKER_LINE = 'Scan for your photos from our day'
FULL_MAX_MB = 24         # Cloudflare won't serve files over 25 MiB; bigger photos get re-saved (same size in pixels)
MAX_UPLOAD_MB = 95       # biggest single file a guest can send (Cloudflare's free plan stops at 100 MB)
UPLOAD_BUCKET = 'madluna-wedding-uploads'   # must match r2_buckets in wrangler.jsonc

# passwords are one of these words plus 3 digits, e.g. "maple 482": easy to read off a card and type
PASSWORD_WORDS = """
acorn amber apple aspen autumn bagel bamboo banjo basil beach berry birch biscuit bloom blossom
breeze brook bubble butter button cabin cactus candle canoe canyon cedar cherry cider cinnamon clover
cobalt cocoa comet coral cosmos cotton cozy crane cricket crystal daisy dandelion dawn dazzle dolphin
dream ember falcon fern fiddle firefly flamingo forest fossil fox garden ginger glacier glow gumdrop
harbor harvest hazel heather hello honey horizon island ivory jasmine jelly jolly juniper kettle kiwi
koala lagoon lantern lark lemon lilac lily linen lotus lucky lullaby magnolia mango maple marble
meadow melody mint misty mitten moonbeam mossy muffin nectar nutmeg oasis ocean olive orbit orchid
otter paddle pancake panda papaya parade peach pebble pepper petal piano pickle pine pistachio plum
poppy pretzel puffin pumpkin quartz quill rainbow raven ribbon river robin rocket rosy ruby saffron
sage sailor sapphire seashell sequoia shadow silver sky snowflake sparrow spruce starlight sugar
summit sunny sunset swan tango teacup thistle thunder tiger toffee topaz tulip tundra turtle twinkle
valley velvet violet waffle walnut willow winter wren yellow zephyr zinnia
""".split()

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.heic', '.heif', '.webp', '.gif'}
SLUG_RE = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
SLUG_CHARS = 'abcdefghjkmnpqrstuvwxyz23456789'   # no 0/o, 1/l/i lookalikes


def paths(data_dir):
    d = Path(data_dir)
    return {
        'csv': d / 'households.csv',
        'photos': d / 'photos',
        'dist': d / 'dist',
        'pdf': d / 'qr-stickers.pdf',
        'secrets': d / '.secrets.json',
        'generated': d / 'generated' / 'config.js',
        'uploads': d / 'uploads',
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
        for k in ('slug', 'name', 'message', 'password', 'folder'):
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


def make_password():
    return f'{secrets.choice(PASSWORD_WORDS)} {secrets.randbelow(900) + 100}'


def normalize_password(pw):
    # must match normalize() in worker.js: "Maple 482", "maple-482" and "maple482" are the same
    return re.sub(r'[^a-z0-9]', '', pw.lower())


def photo_folder(p, r):
    # the "folder" column lets a household's photos live in a folder with any name, e.g. photos/bella&chloe
    return p['photos'] / (r['folder'] or r['slug'])


def cmd_slugs(p, args):
    rows = read_households(p['csv'])
    taken = {r['slug'] for r in rows if r['slug']}
    added = pw_added = 0
    for r in rows:
        if not r['slug']:
            r['slug'] = make_slug(r['name'], taken)
            taken.add(r['slug'])
            added += 1
        if not normalize_password(r['password']):
            r['password'] = make_password()
            pw_added += 1
    if added or pw_added:
        write_households(p['csv'], rows)
    made = 0
    for r in rows:
        folder = photo_folder(p, r)
        if not folder.exists():
            folder.mkdir(parents=True)
            made += 1
    print(f'{added} new slug(s) and {pw_added} new password(s) written to {p["csv"].name}, '
          f'{made} new photo folder(s) in photos/.')
    for r in rows:
        print(f'  {r["slug"]:<32} {r["password"]:<16} {r["name"]}')


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


def shrink_full(src, dst):
    """Re-saves a photo that's too big for Cloudflare as a JPEG with the same pixel size,
    keeping its camera info and colour profile. Our original file isn't touched."""
    limit = FULL_MAX_MB * 1024 * 1024
    dst.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        extra = {k: im.info[k] for k in ('exif', 'icc_profile') if im.info.get(k)}
        if im.mode not in ('RGB', 'L'):
            im = im.convert('RGB')
        for quality in (92, 88, 84, 80, 75):
            im.save(dst, 'JPEG', quality=quality, optimize=True, progressive=True, **extra)
            if dst.stat().st_size <= limit:
                break
    st = src.stat()
    os.utime(dst, (st.st_atime, st.st_mtime))   # matching times = "already done" on the next build


def make_gif_thumb(src, dst):
    """Smaller copy of an animated GIF (e.g. from the photobooth) that still animates."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        if getattr(im, 'n_frames', 1) == 1 and max(im.size) <= GIF_THUMB_EDGE:
            shutil.copyfile(src, dst)
            return
        frames, durations = [], []
        for frame in ImageSequence.Iterator(im):
            f = frame.convert('RGB')
            f.thumbnail((GIF_THUMB_EDGE, GIF_THUMB_EDGE), Image.LANCZOS)
            frames.append(f)
            durations.append(frame.info.get('duration', im.info.get('duration', 100)))
        frames[0].save(dst, 'GIF', save_all=True, append_images=frames[1:], duration=durations,
                       loop=im.info.get('loop', 0), optimize=True, disposal=2)
    if dst.stat().st_size > src.stat().st_size:   # shrinking made it bigger: just use the original
        shutil.copyfile(src, dst)


def make_thumb(src, dst, edge=THUMB_EDGE, quality=THUMB_QUALITY):
    if src.suffix.lower() == '.gif':
        return make_gif_thumb(src, dst)
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        icc = im.info.get('icc_profile')
        if im.mode not in ('RGB', 'L'):
            im = im.convert('RGB')
        im.thumbnail((edge, edge), Image.LANCZOS)
        dst.parent.mkdir(parents=True, exist_ok=True)
        im.save(dst, 'JPEG', quality=quality, optimize=True, progressive=True, **({'icc_profile': icc} if icc else {}))


def build_household(row, photos, out_dir, template):
    """Writes one household's page. Returns the set of files it owns."""
    owned = set()
    items = []
    for i, src in enumerate(photos, 1):
        ext = src.suffix.lower().replace('.jpeg', '.jpg')
        too_big = ext != '.gif' and src.stat().st_size > FULL_MAX_MB * 1024 * 1024
        if too_big:
            ext = '.jpg'
        name = f'{FILE_PREFIX}-{i:03d}{ext}'

        full = out_dir / 'full' / name
        if too_big:
            if not (full.exists() and int(full.stat().st_mtime) == int(src.stat().st_mtime)
                    and full.stat().st_size <= FULL_MAX_MB * 1024 * 1024):
                shrink_full(src, full)
        elif not same_file(src, full):
            full.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, full)
        owned.add(full)

        # thumbnail name comes from the source, so reordering photos reuses old thumbnails
        st = src.stat()
        key = hashlib.sha1(f'{src.name}|{st.st_size}|{int(st.st_mtime)}|{THUMB_EDGE}'.encode()).hexdigest()[:12]
        thumb = out_dir / 'thumbs' / f'{key}{".gif" if ext == ".gif" else ".jpg"}'
        if not thumb.exists():
            make_thumb(src, thumb)
        owned.add(thumb)
        with Image.open(thumb) as t:
            w, h = t.size

        # mid-size copy for the full-screen view; GIFs just use the original so they animate at full size
        view_url = f'full/{name}'
        if ext != '.gif':
            vkey = hashlib.sha1(f'{src.name}|{st.st_size}|{int(st.st_mtime)}|{VIEW_EDGE}'.encode()).hexdigest()[:12]
            view = out_dir / 'view' / f'{vkey}.jpg'
            if not view.exists():
                make_thumb(src, view, VIEW_EDGE, VIEW_QUALITY)
            owned.add(view)
            view_url = f'view/{view.name}'

        items.append({'thumb': f'thumbs/{thumb.name}', 'view': view_url, 'full': f'full/{name}', 'name': name, 'w': w, 'h': h})

    message = row['message'] or DEFAULT_MESSAGE
    page = (template
            .replace('{{HEADLINE}}', html.escape(HEADLINE.format(name=row['name'])))
            .replace('{{MESSAGE}}', html.escape(message.replace('\\n', '\n')))
            .replace('{{SIGNOFF}}', html.escape(SIGNOFF))
            .replace('{{BATCH_SIZE}}', str(BATCH_SIZE))
            .replace('{{MAX_UPLOAD_MB}}', str(MAX_UPLOAD_MB))
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

def load_secrets(path):
    """Random keys for the Worker, made once and kept in .secrets.json (never on GitHub).

    secret signs the "already entered the password" cookie and salts the password hashes;
    admin_token lets "python build.py uploads" fetch what guests sent.
    """
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    s = {'secret': secrets.token_hex(32), 'admin_token': secrets.token_urlsafe(32)}
    path.write_text(json.dumps(s, indent=2), encoding='utf-8')
    return s


def write_worker_config(p, published):
    """generated/config.js: bundled into the Worker at deploy, so it stays private."""
    s = load_secrets(p['secrets'])
    households = {}
    for r in published:
        pw = normalize_password(r['password'])
        digest = hashlib.sha256(f'{s["secret"]}:{r["slug"]}:{pw}'.encode()).hexdigest()
        households[r['slug']] = {'name': r['name'], 'hash': digest}
    config = {
        'secret': s['secret'],
        'adminToken': s['admin_token'],
        'loginHtml': (HERE / 'login.html').read_text(encoding='utf-8'),
        'households': households,
    }
    p['generated'].parent.mkdir(parents=True, exist_ok=True)
    p['generated'].write_text('// made by build.py, do not edit\nexport default ' + json.dumps(config, indent=1) + ';\n',
                              encoding='utf-8', newline='\n')


def cmd_build(p, args):
    rows = read_households(p['csv'])
    if any(not r['slug'] or not normalize_password(r['password']) for r in rows):
        sys.exit('some households have no slug or password yet. run "python build.py slugs" first.')
    if pillow_heif is None:
        print('note: pillow-heif is not installed, so .heic photos will fail. run: python -m pip install pillow-heif')

    template = (HERE / 'template.html').read_text(encoding='utf-8')
    out_w = p['dist'] / 'w'
    out_w.mkdir(parents=True, exist_ok=True)
    keep = set()
    published, empty = [], []

    for r in rows:
        photos = list_photos(photo_folder(p, r))
        if not photos:
            empty.append(r)
            continue
        print(f'  {r["slug"]:<32} {len(photos):>4} photo{"" if len(photos) == 1 else "s"}   {r["name"]}')
        keep |= build_household(r, photos, out_w / r['slug'], template)
        published.append(r)

    (out_w / '404.html').write_text(NOT_FOUND, encoding='utf-8', newline='\n')
    keep.add(out_w / '404.html')
    write_worker_config(p, published)

    # remove anything left over from households or photos that were taken out
    removed = 0
    for f in sorted(p['dist'].rglob('*'), reverse=True):
        if f.is_file() and f not in keep:
            f.unlink()
            removed += 1
        elif f.is_dir() and not any(f.iterdir()):
            f.rmdir()

    print(f'\nbuilt {len(published)} page(s) in {p["dist"]}' + (f', removed {removed} old file(s)' if removed else ''))
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


def sticker_lines(name, password, show_name, show_pw, split_tagline=False):
    """(text, font, size, gray) for each line under or beside the code."""
    if split_tagline:
        words = STICKER_LINE.split()
        mid = len(words) // 2 + 1
        lines = [(' '.join(words[:mid]), 'Times-Italic', 9, 0.15), (' '.join(words[mid:]), 'Times-Italic', 9, 0.15)]
    else:
        lines = [(STICKER_LINE, 'Times-Italic', 9, 0.15)]
    if show_pw:
        lines.append((f'Password: {password}', 'Helvetica-Bold', 9, 0.05))
    if show_name:
        lines.append((name, 'Helvetica', 6.5, 0.45))
    return lines


def block_height(lines):
    return sum(size * 1.25 for _, _, size, _ in lines)


def draw_lines(c, lines, x, top, max_w, centred):
    y = top
    for text, font, size, gray in lines:
        size = fit_font(c, text, font, size, max_w)
        y -= size * 1.25
        c.setFont(font, size)
        c.setFillGray(gray)
        (c.drawCentredString if centred else c.drawString)(x, y + size * 0.2, text)


def draw_label(c, sheet, x, y, url, r, show_name, show_pw, outline):
    from reportlab.lib.units import inch
    w, h = sheet['w'] * inch, sheet['h'] * inch
    if outline:
        c.setStrokeGray(0.6)
        c.setLineWidth(0.5)
        if sheet['layout'] == 'round':
            c.circle(x + w / 2, y + h / 2, w / 2)
        else:
            c.roundRect(x, y, w, h, 6)

    if sheet['layout'] == 'stack':
        pad = 0.12 * inch
        lines = sticker_lines(r['name'], r['password'], show_name, show_pw)
        qr = min(w - 2 * pad, h - 2 * pad - block_height(lines) - 2)
        qy = y + h - pad - qr
        draw_qr(c, url, x + (w - qr) / 2, qy, qr)
        draw_lines(c, lines, x + w / 2, qy - 2, w - 2 * pad, centred=True)
    elif sheet['layout'] == 'round':
        lines = sticker_lines(r['name'], r['password'], show_name, show_pw)
        qr = 1.05 * inch
        qy = y + h - 0.16 * inch - qr
        draw_qr(c, url, x + (w - qr) / 2, qy, qr)
        draw_lines(c, lines, x + w / 2, qy - 2, 1.4 * inch, centred=True)
    else:  # side: code on the left, words on the right
        pad = 0.06 * inch
        qr = h - 2 * pad
        draw_qr(c, url, x + pad, y + pad, qr)
        tx = x + pad + qr + 0.06 * inch
        lines = sticker_lines(r['name'], r['password'], show_name, show_pw, split_tagline=True)
        draw_lines(c, lines, tx, y + h / 2 + block_height(lines) / 2, x + w - 0.1 * inch - tx, centred=False)


def cmd_labels(p, args):
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas

    sheet = SHEETS[args.sheet]
    rows = read_households(p['csv'])
    if any(not r['slug'] or not normalize_password(r['password']) for r in rows):
        sys.exit('some households have no slug or password yet. run "python build.py slugs" first.')
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
        draw_label(c, sheet, x, y, f'{SITE}/w/{r["slug"]}', r, not args.no_names, not args.no_passwords, args.outline)
    c.save()

    pages = (args.skip + len(rows) + per_page - 1) // per_page
    print(f'wrote {p["pdf"].name}: {len(rows)} sticker(s) on {pages} page(s), {sheet["desc"]}.')
    print('print at 100% / "actual size" (not "fit to page"), and test-scan a few before printing the whole batch.')
    missing = [r for r in rows if not list_photos(photo_folder(p, r))]
    if missing:
        print(f'heads up: {len(missing)} of these have no photos yet, so their code would show "not found".')


def wrangler(*args, capture=False):
    # npx can't start inside a network-drive folder (\\server\share), so run it from the home
    # folder; paths in wrangler.jsonc (./dist, worker.js) are resolved next to the config file.
    cmd = ['npx.cmd' if os.name == 'nt' else 'npx', '--yes', 'wrangler@4', *args]
    return subprocess.run(cmd, cwd=Path.home(), capture_output=capture, text=capture)


def cmd_deploy(p, args):
    if not (p['dist'] / 'w').is_dir():
        sys.exit('nothing built yet. run "python build.py build" first.')
    if p['dist'].resolve() != (HERE / 'dist').resolve():
        sys.exit('deploy only works with the default --data folder (wrangler.jsonc points at ./dist).')
    if not p['generated'].exists():
        sys.exit('nothing built yet. run "python build.py build" first.')
    if not args.dry_run:
        out = wrangler('r2', 'bucket', 'create', UPLOAD_BUCKET, capture=True)
        if out.returncode != 0 and 'already exists' not in (out.stdout + out.stderr).lower():
            print(out.stdout + out.stderr)
            sys.exit('could not set up upload storage. if it mentions R2 or a payment method, turn on R2 in the '
                     'Cloudflare dashboard (R2 Object Storage) first. if it says "Not logged in", run: npx wrangler login')
    cmd = ['deploy', '--config', str(HERE / 'wrangler.jsonc')]
    if args.dry_run:
        cmd.append('--dry-run')
    result = wrangler(*cmd)
    if result.returncode != 0:
        sys.exit('deploy failed. if it says "Not logged in", run: npx wrangler login')
    if not args.dry_run:
        print(f'\nlive. each household is at {SITE}/w/<slug>')


def admin_request(base, token, path, method='GET'):
    req = urllib.request.Request(base.rstrip('/') + '/w/_admin' + path, method=method,
                                 headers={'Authorization': f'Bearer {token}', 'User-Agent': 'madluna-build'})
    return urllib.request.urlopen(req, timeout=120)


def cmd_uploads(p, args):
    """Downloads what guests sent into uploads/<slug>/. With --clear, removes each one from
    Cloudflare once its copy here is saved and the size matches, so storage stays near zero."""
    if not p['secrets'].exists():
        sys.exit('no .secrets.json here, so there is nothing deployed from this folder yet.')
    token = load_secrets(p['secrets'])['admin_token']
    base = args.base or SITE
    with admin_request(base, token, '/uploads') as res:
        items = json.load(res)
    if not items:
        print('no guest uploads waiting.')
        return
    got = skipped = cleared = 0
    for it in items:
        _, slug, fname = it['key'].split('/', 2)
        dest = p['uploads'] / slug / fname
        if dest.exists() and dest.stat().st_size == it['size']:
            skipped += 1
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            part = dest.with_name(dest.name + '.part')
            with admin_request(base, token, '/file?key=' + urllib.parse.quote(it['key'])) as res, open(part, 'wb') as f:
                shutil.copyfileobj(res, f)
            if part.stat().st_size != it['size']:
                part.unlink()
                print(f'  ! {it["key"]} did not download completely; try again later')
                continue
            part.replace(dest)
            got += 1
            print(f'  {slug:<32} {fname}')
        if args.clear:
            admin_request(base, token, '/file?key=' + urllib.parse.quote(it['key']), method='DELETE').close()
            cleared += 1
    print(f'\n{got} new, {skipped} already here, in {p["uploads"]}' +
          (f'. cleared {cleared} from Cloudflare.' if args.clear else '. add --clear to remove them from Cloudflare.'))


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
    lp.add_argument('--no-passwords', action='store_true', help='leave passwords off (if writing them on the cards by hand)')
    lp.add_argument('--only', nargs='+', metavar='SLUG', help='only these households (e.g. to reprint one)')
    lp.add_argument('--skip', type=int, default=0, help='skip this many labels at the start (for a part-used sheet)')
    lp.add_argument('--copies', type=int, default=1, help='stickers per household')
    dp = sub.add_parser('deploy', help='upload the built pages to Cloudflare')
    dp.add_argument('--dry-run', action='store_true', help='check everything but upload nothing')
    up = sub.add_parser('uploads', help='download photos guests sent us')
    up.add_argument('--clear', action='store_true', help='remove each one from Cloudflare once it is saved here')
    up.add_argument('--base', help=argparse.SUPPRESS)   # for testing against a local server
    args = ap.parse_args()

    p = paths(args.data)
    {'slugs': cmd_slugs, 'build': cmd_build, 'labels': cmd_labels, 'deploy': cmd_deploy,
     'uploads': cmd_uploads}[args.cmd](p, args)


if __name__ == '__main__':
    main()
