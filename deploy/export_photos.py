#!/usr/bin/env python3
"""After the party: turn the approved disposable-camera photos into a gallery.

    .venv/bin/python deploy/export_photos.py                  # latest party
    .venv/bin/python deploy/export_photos.py --party ABC234 --title "Halloween 2026"
    .venv/bin/python deploy/export_photos.py --deploy-cloudflare party-photos

Writes exports/<party>-photos/ with:
    photos/      the approved shots, numbered in the order they were taken
    index.html   a self-contained gallery page (no outside requests)
    _headers     tells Cloudflare Pages to keep the gallery out of search engines
    all-photos.zip  every photo in one download

Share it either way:
    Google Drive       upload the photos/ folder (or the zip) and share the folder link.
    Cloudflare Pages   --deploy-cloudflare PROJECT runs `npx wrangler pages deploy`
                       and prints the https://PROJECT.pages.dev link.

Only photos marked approved in /host/photos are exported. Run it from the
jukebox folder on the party Mac; it reads jukebox.db and never changes it.
"""
import argparse
import html
import json
import os
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, '.env'))

import database as db  # noqa: E402
import photos  # noqa: E402


def pick_party(requested):
    parties = db.photo_parties()
    if requested:
        if not any(p['party_code'] == requested for p in parties):
            sys.exit(f'No photos from party {requested}. Parties with photos: '
                     + (', '.join(p['party_code'] for p in parties) or 'none'))
        return next(p for p in parties if p['party_code'] == requested)
    with_approved = [p for p in parties if p['approved']]
    if not with_approved:
        sys.exit('No approved photos yet. Review them at /host/photos first.')
    return with_approved[0]


def export(party, out_dir, title):
    approved = db.list_photos(party_code=party['party_code'], status='approved')
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(os.path.join(out_dir, 'photos'))

    items, missing = [], 0
    for n, p in enumerate(approved, 1):
        src = photos.path_for(p['filename'])
        if not os.path.exists(src):
            missing += 1
            continue
        name = f'photo-{n:03d}.jpg'
        shutil.copyfile(src, os.path.join(out_dir, 'photos', name))
        items.append({'src': f'photos/{name}', 'w': p['width'], 'h': p['height']})

    with zipfile.ZipFile(os.path.join(out_dir, 'all-photos.zip'), 'w') as zf:
        for item in items:
            # JPEGs are already compressed; storing them is as small and much faster.
            zf.write(os.path.join(out_dir, item['src']), item['src'].split('/')[-1],
                     compress_type=zipfile.ZIP_STORED)

    with open(os.path.join(out_dir, 'index.html'), 'w') as f:
        f.write(GALLERY.replace('__TITLE__', html.escape(title))
                       .replace('__COUNT__', str(len(items)))
                       .replace('__PHOTOS__', json.dumps(items)))
    with open(os.path.join(out_dir, '_headers'), 'w') as f:
        f.write('/*\n  X-Robots-Tag: noindex, nofollow\n  Referrer-Policy: no-referrer\n')

    print(f'Exported {len(items)} approved photo(s) from party {party["party_code"]} '
          f'({party["pending"]} still pending, {party["rejected"]} rejected).')
    if missing:
        print(f'  ! {missing} approved photo file(s) were missing on disk and skipped.')
    print(f'  → {out_dir}')
    return len(items)


def deploy_cloudflare(out_dir, project):
    """Publish to Cloudflare Pages. Creates the project the first time."""
    if not shutil.which('npx'):
        sys.exit('npx not found: install Node.js, or upload the folder to Google Drive instead.')
    print(f'\nPublishing to Cloudflare Pages project "{project}"…')
    # Fails harmlessly when the project already exists.
    subprocess.run(['npx', '--yes', 'wrangler', 'pages', 'project', 'create', project,
                    '--production-branch', 'main'], capture_output=True)
    result = subprocess.run(['npx', '--yes', 'wrangler', 'pages', 'deploy', out_dir,
                             '--project-name', project, '--branch', 'main',
                             '--commit-dirty=true'])
    if result.returncode != 0:
        sys.exit('wrangler failed. Run `npx wrangler login` once, then try again.')
    print(f'\nGallery link: https://{project}.pages.dev')


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--party', help='party code (default: the latest party with approved photos)')
    ap.add_argument('--title', default='Party Photos', help='heading on the gallery page')
    ap.add_argument('--out', help='output folder (default: exports/<party>-photos)')
    ap.add_argument('--deploy-cloudflare', metavar='PROJECT',
                    help='publish to Cloudflare Pages under this project name')
    args = ap.parse_args()

    party = pick_party(args.party)
    out_dir = os.path.abspath(args.out or os.path.join(ROOT, 'exports', f'{party["party_code"]}-photos'))
    if not export(party, out_dir, args.title):
        sys.exit('Nothing to publish.')
    if args.deploy_cloudflare:
        deploy_cloudflare(out_dir, args.deploy_cloudflare)
    else:
        print('\nNext: upload the photos/ folder (or all-photos.zip) to Google Drive and share it,\n'
              'or rerun with --deploy-cloudflare PROJECT to publish the gallery page.')


# Self-contained gallery: no fonts, scripts or images from anywhere else.
GALLERY = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow">
<title>__TITLE__</title>
<style>
  :root {
    --bg: #f5efe1; --card: #fffdf8; --text: #2b2016; --dim: #6b5d47;
    --accent: #c1573a; --shade: rgba(43, 32, 22, 0.1);
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #14110e; --card: #1f1a15; --text: #f2ece0; --dim: #a89a85;
            --accent: #e07a5a; --shade: rgba(255, 255, 255, 0.08); }
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text);
         font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  header { max-width: 1200px; margin: 0 auto; padding: 40px 16px 20px;
           display: flex; align-items: end; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
  h1 { font-family: Georgia, serif; font-size: clamp(1.8rem, 5vw, 2.8rem); font-weight: 700; }
  .sub { color: var(--dim); margin-top: 6px; }
  .dl { color: #fff; background: var(--accent); text-decoration: none; padding: 10px 16px;
        border-radius: 999px; font-weight: 600; font-size: 0.9rem; white-space: nowrap; }
  main { max-width: 1200px; margin: 0 auto; padding: 0 16px 48px; columns: 3 260px; column-gap: 12px; }
  main button { display: block; width: 100%; margin-bottom: 12px; border: none; padding: 0;
                background: var(--shade); border-radius: 10px; overflow: hidden; cursor: zoom-in;
                break-inside: avoid; }
  main img { display: block; width: 100%; height: auto; }
  footer { text-align: center; color: var(--dim); font-size: 0.8rem; padding: 0 16px 40px; }
  .lb { position: fixed; inset: 0; background: rgba(8, 6, 4, 0.96); display: none;
        flex-direction: column; z-index: 10; }
  .lb.open { display: flex; }
  .lb img { flex: 1; min-height: 0; width: 100%; object-fit: contain; padding: 16px; }
  .lb-bar { display: flex; justify-content: space-between; align-items: center; gap: 12px;
            padding: 10px 16px 16px; color: #f2ece0; font-size: 0.9rem; }
  .lb-bar a, .lb-bar button { color: #f2ece0; background: rgba(255,255,255,0.12); border: none;
            padding: 8px 14px; border-radius: 999px; font: inherit; cursor: pointer; text-decoration: none; }
  .nav { position: absolute; top: 50%; transform: translateY(-50%); width: 48px; height: 72px;
         border: none; border-radius: 8px; background: rgba(255,255,255,0.1); color: #fff;
         font-size: 2rem; cursor: pointer; }
  .prev { left: 8px; } .next { right: 8px; }
  @media (max-width: 600px) { .nav { display: none; } }
</style>
</head>
<body>
<header>
  <div>
    <h1>__TITLE__</h1>
    <p class="sub">__COUNT__ photos from the disposable camera</p>
  </div>
  <a class="dl" href="all-photos.zip" download>Download all</a>
</header>
<main id="grid"></main>
<footer>Want a photo taken down? Just ask the host.</footer>
<div class="lb" id="lb" role="dialog" aria-label="Photo">
  <img id="lb-img" alt="">
  <div class="lb-bar">
    <span id="lb-count"></span>
    <span>
      <a id="lb-dl" href="#" download>Download</a>
      <button id="lb-close" type="button">Close</button>
    </span>
  </div>
  <button class="nav prev" id="lb-prev" type="button" aria-label="Previous">‹</button>
  <button class="nav next" id="lb-next" type="button" aria-label="Next">›</button>
</div>
<script>
  const photos = __PHOTOS__;
  const grid = document.getElementById('grid');
  const lb = document.getElementById('lb');
  let cur = -1;
  photos.forEach((p, i) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.setAttribute('aria-label', 'Photo ' + (i + 1));
    const img = document.createElement('img');
    img.src = p.src; img.loading = 'lazy'; img.alt = '';
    if (p.w && p.h) { img.width = p.w; img.height = p.h; }
    b.appendChild(img);
    b.onclick = () => show(i);
    grid.appendChild(b);
  });
  function show(i) {
    cur = (i + photos.length) % photos.length;
    document.getElementById('lb-img').src = photos[cur].src;
    document.getElementById('lb-dl').href = photos[cur].src;
    document.getElementById('lb-count').textContent = (cur + 1) + ' / ' + photos.length;
    lb.classList.add('open');
  }
  function hide() { lb.classList.remove('open'); cur = -1; }
  document.getElementById('lb-close').onclick = hide;
  document.getElementById('lb-prev').onclick = () => show(cur - 1);
  document.getElementById('lb-next').onclick = () => show(cur + 1);
  document.addEventListener('keydown', e => {
    if (cur < 0) return;
    if (e.key === 'Escape') hide();
    if (e.key === 'ArrowLeft') show(cur - 1);
    if (e.key === 'ArrowRight') show(cur + 1);
  });
  let x0 = null;
  lb.addEventListener('touchstart', e => { x0 = e.touches[0].clientX; }, { passive: true });
  lb.addEventListener('touchend', e => {
    if (x0 === null) return;
    const dx = e.changedTouches[0].clientX - x0;
    if (Math.abs(dx) > 50) show(cur + (dx < 0 ? 1 : -1));
    x0 = null;
  });
</script>
</body>
</html>
'''

if __name__ == '__main__':
    main()
