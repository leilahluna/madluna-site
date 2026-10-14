#!/usr/bin/env python3
"""
Wedding guest photo pages + QR sticker sheet for madluna.ca/w/<slug>/.

  python build.py slugs     fill in missing slugs and passwords in households.csv and make photo folders
  python build.py build     build the guest pages into dist/ (thumbnails + originals)
  python build.py labels    make qr-stickers.pdf, a printable sheet of QR stickers with passwords
  python build.py deploy    upload dist/ to Cloudflare so the pages are live (add --dry-run to only check)
  python build.py uploads   download what guests sent us into uploads/ and mark it "Saved to server" on madluna.ca/w/admin

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
NOTE = ("If you don't spot yourself in a photo immediately, check in the background. "
        "We used Google Photos to detect faces, so if the person detected isn't you, we apologize!")   # under the message on every page; '' for none
STICKER_LINE = 'Scan for your photos from our day'
# Featured photos are shown big at the top of each page. photo-info.csv's "featured" column sets it per photo:
# a number is its priority (1 shows first, then 2, ...), "yes" means after the numbered ones, "no" means never.
# Add "wide" (e.g. "6 wide") to make a photo take a whole row on wide screens, like the big group photo.
# Photos by these photographers (the Artist saved in each file) count as "yes" unless the column says otherwise.
FEATURED_ARTISTS = []    # e.g. ["Verita's Eye"]
FEATURED_TITLE = 'Professional shots'
FEATURED_CREDIT = "Photos by Verita's Eye"
SHOW_TABS = False
DEFAULT_MOMENT = 'Reception'
# the usual order of the day: pages without a saved order list their regular photos this way, and the admin
# "By time of day" view starts its blocks in it (any other time of day goes after these)
MOMENT_ORDER = ['Getting ready', 'Pre-ceremony', 'Ceremony', 'Post-ceremony', 'Reception']   # time of day for any photo not covered by moments.csv or set on the admin page        # All / Photos / GIFs / Videos sorting above the photos
FULL_MAX_MB = 24         # Cloudflare won't serve files over 25 MiB; bigger photos get re-saved (same size in pixels)
MAX_UPLOAD_MB = 950000   # no real limit (about 900 GB, the most R2 can join); must match MAX_FILE in worker.js
UPLOAD_BUCKET = 'madluna-wedding-uploads'   # must match r2_buckets in wrangler.jsonc

# passwords are random codes like "KTRV47": 4 letters then 2 digits, leaving out look-alikes
# (no I, L, O or S; no 0, 1 or 5) so nobody types the wrong one off a printed sticker, and vowels
# (no A, E, U, Y) so the letters can't accidentally spell a word
PASSWORD_LETTERS = 'BCDFGHJKMNPQRTVWXZ'
PASSWORD_DIGITS = '2346789'

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
        'stickers': d / 'stickers',
        'moments': d / 'moments.csv',
        'info': d / 'photo-info.csv',
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
        for k in ('slug', 'name', 'message', 'password', 'folder', 'downloads'):
            r.setdefault(k, '')
    rows = [r for r in rows if r['name'] or r['slug']]
    # if there's a card-number column, only rows with a card number are households; the rest (notes,
    # questions) are kept as they are when the sheet is saved again, but otherwise ignored
    card_col = next((k for k in (rows[0] if rows else {}) if k.strip().lower().startswith('card')), None)
    _notes_rows.clear()
    if card_col:
        _notes_rows.extend(r for r in rows if not r[card_col].strip())
        rows = [r for r in rows if r[card_col].strip()]
    seen = set()
    for r in rows:
        if r['slug'] in ('admin', '_admin'):
            sys.exit(f'"{r["slug"]}" is reserved for the admin page; pick another slug.')
        if r['slug'] and not SLUG_RE.match(r['slug']):
            sys.exit(f'bad slug "{r["slug"]}": use only lowercase letters, numbers and dashes.')
        if r['slug'] in seen and r['slug']:
            sys.exit(f'slug "{r["slug"]}" is used twice in {csv_path.name}.')
        seen.add(r['slug'])
    return rows


_notes_rows = []   # rows without a card number, put back after the households when the sheet is saved


def write_households(csv_path, rows):
    # keep the sheet's own columns (and their order); utf-8-sig so Excel shows accents right
    rows = list(rows) + [r for r in _notes_rows if r not in rows]
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
    return ''.join(secrets.choice(PASSWORD_LETTERS) for _ in range(4)) + ''.join(secrets.choice(PASSWORD_DIGITS) for _ in range(2))


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


# moments.csv: when each part of the day started. Only used to sort photos into moments;
# times never appear on the site. Starter rows are guesses to be fixed by hand.
STARTER_MOMENTS = []     # e.g. ("2025-10-25 16:00", "Ceremony"); photos outside every range get DEFAULT_MOMENT


def read_csv(path):
    try:
        text = path.read_text(encoding='utf-8-sig')
    except UnicodeDecodeError:
        text = path.read_text(encoding='cp1252')
    return [{k: (v or '').strip() for k, v in r.items() if k} for r in csv.DictReader(text.splitlines())]


def write_csv(path, fields, rows):
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, '') for k in fields})


def load_moments(path):
    if not path.exists():
        write_csv(path, ['from', 'moment'], [{'from': a, 'moment': b} for a, b in STARTER_MOMENTS])
        print(f'made {path.name}; add rows (start time, moment) to label parts of the day by time.')
    out = []
    for r in read_csv(path):
        m = re.match(r'(\d{4})-(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?', r.get('from', ''))
        if m and r.get('moment'):
            y, mo, d, hh, mm = m.groups()
            out.append((f'{int(y):04d}:{int(mo):02d}:{int(d):02d} {int(hh or 0):02d}:{int(mm or 0):02d}:00', r['moment']))
    return sorted(out)


def moment_for(taken, moments):
    """The moment whose start is the latest one at or before the photo's time, else DEFAULT_MOMENT."""
    label = ''
    for start, name in moments:
        if taken and taken >= start:
            label = name
    return label or DEFAULT_MOMENT


def info_key(src, photos_root):
    """'bella&chloe/1.jpg': the household folder plus file name, for names used in more than one folder."""
    return f'{src.relative_to(photos_root).parts[0]}/{src.name}'


def info_for(src, info, photos_root):
    """A photo's row in photo-info.csv: by folder/name if there is one, otherwise by file name."""
    info = info or {}
    return info.get(info_key(src, photos_root)) or info.get(src.name, {})


def sync_photo_info(path, per_household, photos_root):
    """photo-info.csv has one row per photo (by file name): who's in it and, optionally, its moment.
    If the same file name is a different photo in two folders (say, two households' "1.jpg"),
    each gets its own "folder/name" row. New photos are added with the household's people filled in;
    existing rows are never changed."""
    rows = read_csv(path) if path.exists() else []
    known = {r.get('photo', '') for r in rows}
    people_for, sizes = {}, {}
    for r, photos in per_household:
        names = [n.strip() for n in re.split(r'[;,]', r.get('people', '')) if n.strip()]
        for f in photos:
            sizes.setdefault(f.name, set()).add(f.stat().st_size)
            key = info_key(f, photos_root)
            for k in (f.name, key):
                people_for.setdefault(k, [])
                people_for[k] += [n for n in names if n not in people_for[k]]
    wanted = set()
    for k in people_for:
        if '/' in k:
            name = k.split('/', 1)[1]
            if len(sizes[name]) > 1 and k not in known:
                wanted.add(k)               # same name, different photos: one row per folder
        elif len(sizes[k]) == 1 and k not in known and not any(q.endswith('/' + k) for q in known):
            wanted.add(k)
    added = [{'photo': k, 'people': ', '.join(people_for[k]), 'moment': '', 'featured': ''} for k in sorted(wanted)]
    old_header = not rows or 'featured' not in rows[0]
    if added or not path.exists() or old_header:
        write_csv(path, ['photo', 'people', 'moment', 'featured'], rows + added)
        if added:
            print(f'added {len(added)} photo(s) to {path.name}; fix names or moments there any time and rebuild.')
    return {r['photo']: r for r in rows + added}


def content_id(path):
    """Size plus a hash of the first 256 KB: tells photos apart by what they are, not what they're called."""
    with open(path, 'rb') as f:
        return f'{path.stat().st_size}-{hashlib.sha1(f.read(256 * 1024)).hexdigest()}'


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


def photographer(path):
    try:
        with Image.open(path) as im:
            return str(im.getexif().get(315, '')).strip()
    except Exception:
        return ''


def feature_rank(src, row):
    """(priority among the big photos, lower shows first; whether it takes a whole row),
    or (None, False) if the photo isn't featured."""
    choice = row.get('featured', '').strip().lower()
    wide = 'wide' in choice
    choice = choice.replace('wide', '').strip()
    if choice.isdigit() and int(choice) > 0:
        return int(choice), wide
    if choice in ('yes', 'y', 'true', 'x') or (wide and not choice):
        return 1000, wide
    if choice in ('no', 'n', 'false', '0'):
        return None, False
    return (1000, wide) if photographer(src) in FEATURED_ARTISTS else (None, False)


def greeting_name(name):
    """'Emily A.' -> 'Emily': the sheet keeps last initials to tell people apart, the page greets by first name."""
    name = re.sub(r'\s+[A-Z]\.$', '', name.strip())
    return re.sub(r'^The ', 'the ', name)   # "Hi, the Hartmanns!" reads better than "Hi, The Hartmanns!"


def detected_line(row):
    """'Every photo listed below detected either Bella or Chloe (or both!).' from the household's people."""
    first = [n.split()[0] for n in re.split(r'[;,&]| and ', row.get('people', '')) if n.strip()]
    if not first:
        return ''
    if len(first) == 1:
        who = first[0]
    elif len(first) == 2:
        who = f'either {first[0]} or {first[1]} (or both!)'
    else:
        who = f'at least one of {", ".join(first[:-1])} or {first[-1]}'
    return f'Every photo listed below detected {who}.'


def build_household(row, photos, out_dir, template, info=None, moments=(), photos_root=None):
    """Writes one household's page. Returns the set of files it owns."""
    owned = set()
    items = []
    # downloads column: "no" turns off saving for this household (e.g. a test page)
    no_downloads = row.get('downloads', '').strip().lower() in ('no', 'n', 'false', '0', 'off')
    meta_of = {src: info_for(src, info, photos_root) for src in photos}
    feat = {src: feature_rank(src, meta_of[src]) for src in photos}
    rank = {src: feat[src][0] for src in photos}
    # featured photos first, by priority; within a priority (and for the rest) still by date
    photos = sorted(photos, key=lambda s: (rank[s] is None, rank[s] or 0))
    featured = {src: rank[src] is not None for src in photos}
    for i, src in enumerate(photos, 1):
        ext = src.suffix.lower().replace('.jpeg', '.jpg')
        too_big = ext != '.gif' and src.stat().st_size > FULL_MAX_MB * 1024 * 1024
        if too_big:
            ext = '.jpg'
        name = f'{FILE_PREFIX}-{i:03d}{ext}'

        full = out_dir / 'full' / name
        if no_downloads and ext != '.gif':
            pass   # page without downloads: no full-size original needed (GIFs keep theirs, it's what they show)
        elif too_big:
            if not (full.exists() and int(full.stat().st_mtime) == int(src.stat().st_mtime)
                    and full.stat().st_size <= FULL_MAX_MB * 1024 * 1024):
                shrink_full(src, full)
        elif not same_file(src, full):
            full.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, full)
        if full.exists() and not (no_downloads and ext != '.gif'):
            owned.add(full)

        # thumbnail name comes from the source, so reordering photos reuses old thumbnails
        st = src.stat()
        # the photo's id comes from its contents, so the same photo in two folders (even renamed) is one photo
        # for tags, hiding and arranging; it also names the thumbnail, so changed photos get fresh thumbnails
        fp = content_id(src)
        key = hashlib.sha1(f'{fp}|{THUMB_EDGE}'.encode()).hexdigest()[:12]
        thumb = out_dir / 'thumbs' / f'{key}{".gif" if ext == ".gif" else ".jpg"}'
        if not thumb.exists():
            make_thumb(src, thumb)
        owned.add(thumb)
        with Image.open(thumb) as t:
            w, h = t.size

        # mid-size copy for the full-screen view; GIFs just use the original so they animate at full size
        view_url = f'full/{name}'
        if ext != '.gif':
            vkey = hashlib.sha1(f'{fp}|{VIEW_EDGE}'.encode()).hexdigest()[:12]
            view = out_dir / 'view' / f'{vkey}.jpg'
            if not view.exists():
                make_thumb(src, view, VIEW_EDGE, VIEW_QUALITY)
            owned.add(view)
            view_url = f'view/{view.name}'

        kind = 'gif' if ext == '.gif' else 'photo'   # drives the All / Photos / GIFs / Videos tabs
        # side panel: moment (from photo-info.csv, else moments.csv by time), date without time, people
        meta = meta_of[src]
        taken = date_taken(src)
        people = [n.strip() for n in re.split(r'[;,]', meta.get('people', '')) if n.strip()]
        full_url = view_url if no_downloads and ext != '.gif' else f'full/{name}'
        items.append({'id': key, 'thumb': f'thumbs/{thumb.name}', 'view': view_url, 'full': full_url, 'name': name,
                      'w': w, 'h': h, 'kind': kind,
                      'moment': meta.get('moment') or moment_for(taken, moments),
                      'date': taken[:10].replace(':', '-') if taken else '',
                      'people': people,
                      'featured': featured[src],
                      'wide': featured[src] and feat[src][1]})

    message = row['message'] or DEFAULT_MESSAGE
    page = (template
            .replace('{{HEADLINE}}', html.escape(HEADLINE.format(name=greeting_name(row['name']))))
            .replace('{{MESSAGE}}', html.escape(message.replace('\\n', '\n')))
            .replace('{{NOTE}}', html.escape(' '.join(x for x in (detected_line(row), NOTE) if x)))
            .replace('{{SIGNOFF}}', html.escape(SIGNOFF))
            .replace('{{BATCH_SIZE}}', str(BATCH_SIZE))
            .replace('{{DOWNLOADS}}', 'false' if no_downloads else 'true')
            .replace('{{FEATURED_TITLE}}', html.escape(FEATURED_TITLE))
            .replace('{{FEATURED_CREDIT}}', html.escape(FEATURED_CREDIT))
            .replace('{{SHOW_TABS}}', 'true' if SHOW_TABS else 'false')
            .replace('{{MOMENT_ORDER_JSON}}', json.dumps(MOMENT_ORDER))
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
    admin_token lets "python build.py uploads" fetch what guests sent;
    admin_password opens Merrick & Leilah's page at madluna.ca/w/admin.
    """
    s = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    made = {k: v for k, v in (('secret', secrets.token_hex(32)), ('admin_token', secrets.token_urlsafe(32)),
                              ('admin_password', make_password() + '-' + make_password())) if k not in s}
    if made:
        s.update(made)
        path.write_text(json.dumps(s, indent=2), encoding='utf-8')
        if 'admin_password' in made:
            print(f'new admin password for {SITE}/w/admin: {s["admin_password"]}  (also saved in {path.name})')
    return s


def login_page(title, lead, button, help_html):
    return ((HERE / 'login.html').read_text(encoding='utf-8')
            .replace('{{TITLE}}', title).replace('{{LEAD}}', lead)
            .replace('{{BUTTON}}', button).replace('{{HELP}}', help_html))


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
        'adminHash': hashlib.sha256(f'{s["secret"]}:admin:{normalize_password(s["admin_password"])}'.encode()).hexdigest(),
        'loginHtml': login_page('Your photos from our day',
                                'Below, enter the password written under the QR code on your thank you card.',
                                'Open my photos',
                                '<p class="help">Can\'t find it? <a href="/contact/">Let us know</a> and we\'ll sort it out.</p>'),
        'adminLoginHtml': login_page('Guest uploads', 'For Merrick &amp; Leilah only.', 'Open', ''),
        'adminHtml': (HERE / 'admin.html').read_text(encoding='utf-8').replace('{{MOMENT_ORDER_JSON}}', json.dumps(MOMENT_ORDER)),
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

    lists = [(r, list_photos(photo_folder(p, r))) for r in rows]
    moments = load_moments(p['moments'])
    info = sync_photo_info(p['info'], [(r, ph) for r, ph in lists if ph], p['photos'])
    for r, photos in lists:
        if not photos:
            empty.append(r)
            continue
        print(f'  {r["slug"]:<32} {len(photos):>4} photo{"" if len(photos) == 1 else "s"}   {r["name"]}')
        keep |= build_household(r, photos, out_w / r['slug'], template, info, moments, p['photos'])
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


# ===== single stickers for a label maker (e.g. the Katasymbol T50M Pro, printed from its phone app) =====

STICKER_PX_PER_MM = 16    # twice a 203 dpi thermal head (8 dots/mm), so the app can scale it down cleanly


# the password on label-maker stickers: the website's own Plus Jakarta Sans, in Bold (fonts/ in this folder)
STICKER_FONT = ['fonts/PlusJakartaSans.ttf:Bold', 'arialbd.ttf', 'DejaVuSans-Bold.ttf']


def sticker_font(names, size):
    """First font that loads. "file.ttf:Style" picks a weight from a variable font; plain names come from
    the system font folders, paths with a folder are relative to this script."""
    from PIL import ImageFont
    for n in names:
        name, _, style = n.partition(':')
        places = [HERE / name] if '/' in name else [f'{folder}/{name}' for folder in ('C:/Windows/Fonts', '/usr/share/fonts/truetype/dejavu', '/Library/Fonts')]
        for place in places:
            try:
                f = ImageFont.truetype(str(place), size)
            except OSError:
                continue
            if style:
                f.set_variation_by_name(style)
            return f
    return ImageFont.load_default()


def fit(draw, text, names, size, max_w):
    while size > 8:
        f = sticker_font(names, size)
        if draw.textlength(text, font=f) <= max_w:
            return f
        size -= 1
    return sticker_font(names, size)


def make_sticker_image(url, r, w_mm, h_mm, show_name, show_pw, sizes=None):
    """One black-and-white sticker: QR code on the left, words on the right. Returns (image, text sizes used);
    pass the smallest sizes across all stickers back in as `sizes` so every sticker's text matches."""
    import qrcode
    from PIL import Image, ImageDraw
    k = STICKER_PX_PER_MM
    W, H = round(w_mm * k), round(h_mm * k)
    img = Image.new('1', (W, H), 1)
    d = ImageDraw.Draw(img)
    pad = round(1.5 * k)

    # QR: whole pixels per module (sharp edges on a thermal printer), 2-module quiet zone (the label is white anyway)
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=2, box_size=1)
    qr.add_data(url)
    qr.make(fit=True)
    modules = qr.modules_count + 4
    box = (H - 2 * pad) // modules
    qimg = qr.make_image(fill_color='black', back_color='white').convert('1').resize((modules * box,) * 2, Image.NEAREST)
    qy = (H - qimg.height) // 2
    img.paste(qimg, (pad // 2, qy))

    # words, stacked and centred vertically in the space to the right of the code
    x0 = pad // 2 + qimg.width + round(0.5 * k)
    max_w = W - x0 - round(1.0 * k)
    serif_i, sans, sans_b = ['georgiai.ttf', 'DejaVuSerif-Italic.ttf'], ['arial.ttf', 'DejaVuSans.ttf'], ['arialbd.ttf', 'DejaVuSans-Bold.ttf']
    lines = [('Scan for your', serif_i, 3.4), ('photos from', serif_i, 3.4), ('our day', serif_i, 3.4)]
    if show_pw:
        lines += [(None, None, 1.2), ('Password:', sans, 2.8), (r['password'], sans_b, 4.2)]
    if show_name:
        lines += [(None, None, 1.0), (r['name'], sans, 2.6)]
    # lines in the same style share one size (the one that fits the longest), so "our day" isn't bigger
    shared = {}
    for text, font, mm in lines:
        if text:
            key = (tuple(font), mm)
            shared[key] = min(shared.get(key, 10 ** 6), fit(d, text, font, round(mm * k), max_w).size)
    if sizes:
        shared = {key: min(v, sizes.get(key, v)) for key, v in shared.items()}
    laid = []
    for text, font, mm in lines:
        if text is None:
            laid.append((None, None, round(mm * k)))
            continue
        f = sticker_font(font, shared[(tuple(font), mm)])
        top, bottom = f.getbbox('Ag')[1], f.getbbox('Ag')[3]
        laid.append((text, f, bottom - top + round(0.35 * k)))
    total = sum(h for _, _, h in laid)
    y = (H - total) // 2
    for text, f, h in laid:
        if text:
            d.text((x0, y - f.getbbox('Ag')[1]), text, font=f, fill=0)
        y += h
    return img, shared


STICKER_LOGO = 'sticker-logo.png'   # the M + L logo (black on white) put in the middle of each QR code
PRINTER_DOTS_PER_MM = 8              # 203 dpi thermal head (Katasymbol T50M Pro): stickers are drawn dot-for-dot
STICKER_MARGIN_MM = 1.0              # blank border all round; at 0.5 mm the printer clipped the code's edge
QR_QUIET_MODULES = 0                 # no extra white ring: the margin and the white label do that job
# Where this printer really lands, measured with a "--calibrate" print. Whole dots, applied by shifting only
# (never scaling, which blurs the QR squares). dx: + moves right, dy: + moves down, as the image looks on screen.
STICKER_OFFSET_MM = {'dx': 0.0, 'dy': 0.0}


def qr_for(url):
    """QR code for a page link, written in capitals: QR's alphanumeric mode packs A-Z/0-9/:/. much tighter,
    so the code has fewer, bigger squares. (The site sends capitalised links to the lowercase page.)
    Highest error correction, so the logo can cover the middle."""
    import qrcode
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_H, border=QR_QUIET_MODULES, box_size=1)
    qr.add_data(url.upper())
    qr.make(fit=True)
    return qr


def qr_image_with_logo(qr, size, logo_turn=0):
    """The code drawn `size` dots across (quiet zone included), with the logo on a white square over its middle
    ~23%. Squares are whole dots; when `size` doesn't divide evenly they mix two widths (e.g. 7 and 8 dots),
    which lets the code be any size instead of jumping a whole dot per square.
    logo_turn: degrees counter-clockwise, so the logo stands upright once the sticker is turned to read."""
    from PIL import Image, ImageDraw
    n = qr.modules_count
    q = qr.border
    modules = n + 2 * q
    edge = [round(i * size / modules) for i in range(modules + 1)]   # dot where each square starts
    qimg = Image.new('1', (edge[-1], edge[-1]), 1)
    d = ImageDraw.Draw(qimg)
    for r, row in enumerate(qr.modules):
        for c, dark in enumerate(row):
            if dark:
                d.rectangle([edge[c + q], edge[r + q], edge[c + q + 1] - 1, edge[r + q + 1] - 1], fill=0)
    logo_file = HERE / STICKER_LOGO
    if logo_file.exists():
        side = round(n * 0.23) | 1
        m0 = q + (n - side) // 2
        d.rectangle([edge[m0], edge[m0], edge[m0 + side] - 1, edge[m0 + side] - 1], fill=1)
        lg = Image.open(logo_file).convert('L')
        if logo_turn:
            lg = lg.rotate(logo_turn, expand=True, fillcolor=255)
        x0, x1 = edge[m0 + 1], edge[m0 + side - 1]
        inner = x1 - x0
        lg.thumbnail((inner, inner), Image.LANCZOS)
        lg = lg.point(lambda v: 0 if v < 140 else 255).convert('1')
        qimg.paste(lg, (x0 + (inner - lg.width) // 2, x0 + (inner - lg.height) // 2))
    return qimg


def make_password_sticker(url, r, w_mm, h_mm, pw_size=None):
    """The printed (sideways) sticker, dot-for-dot at the printer's resolution: the QR code as tall as the label
    allows on the left, the password in capitals running up the right-hand side.
    Returns (image, password text size); pass the smallest size back in as pw_size so all stickers match."""
    from PIL import Image, ImageDraw
    k = PRINTER_DOTS_PER_MM
    W, H = round(w_mm * k), round(h_mm * k)      # landscape: e.g. 320 x 240 dots for 40 x 30 mm
    margin = round(STICKER_MARGIN_MM * k)
    qr = qr_for(url)
    # the code fills the label's height inside the margin; the sticker is read upright (turned a quarter
    # clockwise), so turn the logo the other way to match the text
    qimg = qr_image_with_logo(qr, H - 2 * margin, logo_turn=90)
    # password, drawn upright then turned to read bottom-to-top beside the code
    text = re.sub(r'[^A-Za-z0-9]', '', r['password']).upper()
    room_w = W - 2 * margin - qimg.width          # space left of the code's width, across
    probe = ImageDraw.Draw(Image.new('1', (1, 1)))
    size = fit(probe, text, STICKER_FONT, round(room_w * 0.9), H - 2 * margin).size
    if pw_size:
        size = min(size, pw_size)
    f = sticker_font(STICKER_FONT, size)
    l, t_, rgt, b = f.getbbox(text)
    timg = Image.new('1', (rgt - l + 2, b - t_ + 2), 1)
    ImageDraw.Draw(timg).text((1 - l, 1 - t_), text, font=f, fill=0)
    timg = timg.rotate(90, expand=True)
    # centre the pair (code + gap + text) on the label, then apply this printer's offset
    gap = max(2, (room_w - timg.width) // 3)
    group_w = qimg.width + gap + timg.width
    x0 = (W - group_w) // 2 + round(STICKER_OFFSET_MM['dx'] * k)
    dy = round(STICKER_OFFSET_MM['dy'] * k)
    img = Image.new('1', (W, H), 1)
    img.paste(qimg, (x0, (H - qimg.height) // 2 + dy))
    img.paste(timg, (x0 + qimg.width + gap, (H - timg.height) // 2 + dy))
    return img, size


def calibration_sticker(w_mm, h_mm):
    """Boxes 1, 2 and 3 mm in from each edge, a centre cross and edge labels, to see where this printer really
    prints. Dot-for-dot, with no offset applied."""
    from PIL import Image, ImageDraw
    k = PRINTER_DOTS_PER_MM
    W, H = round(w_mm * k), round(h_mm * k)
    img = Image.new('1', (W, H), 1)
    d = ImageDraw.Draw(img)
    for mm in (1, 2, 3):
        d.rectangle([mm * k, mm * k, W - mm * k - 1, H - mm * k - 1], outline=0, width=1)
    d.line([W // 2, H // 2 - 3 * k, W // 2, H // 2 + 3 * k], fill=0, width=2)
    d.line([W // 2 - 3 * k, H // 2, W // 2 + 3 * k, H // 2], fill=0, width=2)
    f = sticker_font(STICKER_FONT, round(2.4 * k))
    for label, xy in (('TOP', (W // 2, 6 * k)), ('BOTTOM', (W // 2, H - 6 * k)), ('L', (6 * k, H // 2)), ('R', (W - 6 * k, H // 2))):
        d.text(xy, label, font=f, fill=0, anchor='mm')
    return img


def cmd_label_images(p, args):
    if args.style == 'password':
        return cmd_password_stickers(p, args)
    w_mm, h_mm = (float(v) for v in args.size.lower().split('x'))
    rows = read_households(p['csv'])
    if args.only:
        rows = [r for r in rows if r['slug'] in args.only]
    out = p['stickers']
    out.mkdir(parents=True, exist_ok=True)
    dpi = STICKER_PX_PER_MM * 25.4
    opts = (w_mm, h_mm, not args.no_names, not args.no_passwords)
    # first pass: the text size each sticker could use; then use the smallest everywhere so they all match
    sizes = {}
    for r in read_households(p['csv']):
        for key, v in make_sticker_image(f'{SITE}/w/{r["slug"]}', r, *opts)[1].items():
            sizes[key] = min(v, sizes.get(key, v))
    for r in rows:
        img, _ = make_sticker_image(f'{SITE}/w/{r["slug"]}', r, *opts, sizes=sizes)
        safe = re.sub(r'[^\w&-]+', '-', r['name']).strip('-') or r['slug']
        img.save(out / f'{safe}.png', dpi=(dpi, dpi))
        print(f'  {out.name}/{safe}.png   {r["name"]}')
    print(f'made {len(rows)} sticker image(s), {w_mm:g} x {h_mm:g} mm each, in {out}')


def cmd_password_stickers(p, args):
    """One sticker per household, drawn dot-for-dot for the label maker. "(sideways)" is the one to print on a
    40 x 30 mm label; the plain file is the same sticker turned upright, for looking at."""
    a, b = (float(v) for v in args.size.lower().split('x'))
    w_mm, h_mm = max(a, b), min(a, b)
    out = p['stickers']
    out.mkdir(parents=True, exist_ok=True)
    dpi = PRINTER_DOTS_PER_MM * 25.4
    if args.calibrate:
        calibration_sticker(w_mm, h_mm).save(out / 'calibration (sideways).png', dpi=(dpi, dpi))
        print(f'made {out.name}/calibration (sideways).png: print it like a sticker and look where the boxes land')
        return
    rows = read_households(p['csv'])
    if args.only:
        rows = [r for r in rows if r['slug'] in args.only]
    if not args.only:   # a full run starts clean (drops stickers of removed households); --only leaves the rest alone
        for old in out.glob('*.png'):
            if not old.name.startswith('calibration'):
                old.unlink()
    size = min(make_password_sticker(f'{SITE}/w/{r["slug"]}', r, w_mm, h_mm)[1] for r in read_households(p['csv']))
    for r in rows:
        img, _ = make_password_sticker(f'{SITE}/w/{r["slug"]}', r, w_mm, h_mm, size)
        safe = re.sub(r'[^\w&-]+', '-', r['name']).strip('-') or r['slug']
        # start with the card number when there is one: two households can share a name ("The Widemans")
        card = next((v for k, v in r.items() if k.strip().lower().startswith('card') and v.strip().isdigit()), '')
        if card:
            safe = f'{int(card):03d} {safe}'
        img.save(out / f'{safe} (sideways).png', dpi=(dpi, dpi))
        img.rotate(-90, expand=True).save(out / f'{safe}.png', dpi=(dpi, dpi))
        print(f'  {out.name}/{safe} (sideways).png  (+ upright copy)   {r["name"]}')
    print(f'made {len(rows)} sticker(s) for {w_mm:g} x {h_mm:g} mm labels, dot-for-dot at {PRINTER_DOTS_PER_MM} dots/mm, in {out}')


def cmd_labels(p, args):
    if args.sheet == 'labelmaker':
        return cmd_label_images(p, args)
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
    # wrangler prints emoji, so read its output as UTF-8 rather than Windows' default code page
    return subprocess.run(cmd, cwd=Path.home(), capture_output=capture,
                          encoding='utf-8' if capture else None, errors='replace' if capture else None)


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


def admin_request(base, token, path, method='GET', body=None):
    headers = {'Authorization': f'Bearer {token}', 'User-Agent': 'madluna-build'}
    if body is not None:
        body = json.dumps(body).encode()
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(base.rstrip('/') + '/w/_admin' + path, data=body, method=method, headers=headers)
    return urllib.request.urlopen(req, timeout=300)


def cmd_uploads(p, args):
    """Downloads what guests sent into uploads/<slug>/ and marks each one "Saved to server" on the
    admin page, so it's clearly safe to delete there. Nothing is deleted unless --clear is given."""
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
    newly_saved = []
    for it in items:
        _, slug, fname = it['key'].split('/', 2)
        dest = p['uploads'] / slug / fname
        if it.get('saved') or (dest.exists() and dest.stat().st_size == it['size']):
            skipped += 1   # already on the server (maybe since moved into a photos folder)
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
        if not it.get('saved'):
            newly_saved.append(it['key'])
        if args.clear:
            admin_request(base, token, '/file?key=' + urllib.parse.quote(it['key']), method='DELETE').close()
            cleared += 1
    if newly_saved and not args.clear:
        admin_request(base, token, '/saved', method='POST', body={'keys': newly_saved}).close()
    print(f'\n{got} new, {skipped} already saved, in {p["uploads"]}' +
          (f'. cleared {cleared} from Cloudflare.' if args.clear else
           f'. marked {len(newly_saved)} "Saved to server" on {SITE}/w/admin.' if newly_saved else '.'))


def main():
    ap = argparse.ArgumentParser(description='wedding guest photo pages for madluna.ca/w/')
    ap.add_argument('--data', default=str(HERE), help='folder holding households.csv, photos/ and dist/ (default: this folder)')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('slugs', help='fill in missing slugs and make empty photo folders')
    sub.add_parser('build', help='build the guest pages into dist/')
    lp = sub.add_parser('labels', help='make the printable QR sticker PDF')
    lp.add_argument('--sheet', choices=sorted(SHEETS) + ['labelmaker'], default='22806',
                    help='label sheet type (default 22806), or "labelmaker" for one image per sticker')
    lp.add_argument('--size', default='40x30', help='label size in mm for --sheet labelmaker (default 40x30)')
    lp.add_argument('--calibrate', action='store_true', help='labelmaker: make a calibration sticker instead')
    lp.add_argument('--style', choices=['password', 'full'], default='password',
                    help='labelmaker style: "password" (QR + password only, upright; default) or "full" (QR + wording)')
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
