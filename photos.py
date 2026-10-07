"""Disposable camera: turn a guest's upload into a clean JPEG on disk.

Every photo is decoded and re-encoded rather than stored as sent. That proves
it's really an image, caps its size, and drops all metadata (EXIF, including
the GPS location phones embed) before anything is kept.
"""
import io
import os
import time
import uuid

from PIL import (Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont,
                 ImageOps, UnidentifiedImageError)

PHOTOS_DIR = os.environ.get('JUKEBOX_PHOTOS_DIR') or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'photos')

# Longest edge kept. Plenty for a phone screen or a print, ~0.5 MB a shot.
MAX_EDGE = 2048
JPEG_QUALITY = 85
# Refuse anything that would decode to more than this many pixels (about a
# 12k x 8k image) before Pillow allocates memory for it.
MAX_PIXELS = 100_000_000


class PhotoError(ValueError):
    """The upload isn't a usable image. The message is safe to show the guest."""


def process(data):
    """Decode, orient, shrink and re-encode an upload. Returns (jpeg_bytes, w, h)."""
    if not data:
        raise PhotoError('The photo was empty.')
    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.width * img.height > MAX_PIXELS:
                raise PhotoError('That photo is too large.')
            img = ImageOps.exif_transpose(img)
            img = img.convert('RGB')
            img.thumbnail((MAX_EDGE, MAX_EDGE))
            out = io.BytesIO()
            # No exif= argument: the saved file carries no metadata at all.
            img.save(out, 'JPEG', quality=JPEG_QUALITY, optimize=True)
            return out.getvalue(), img.width, img.height
    except PhotoError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError):
        raise PhotoError("That doesn't look like a photo.")


# ---------------------------------------------------------------------------
# Disposable camera look
# ---------------------------------------------------------------------------
# Stamp the date in the corner in orange, like a cheap film camera does.
DATE_STAMP = True


def _curve(lift, gain, gamma):
    """A 256-entry tone curve: raise the blacks to `lift`, cap the whites at
    `gain`, and bend the midtones by `gamma`."""
    return [round(lift + (gain - lift) * (i / 255) ** gamma) for i in range(256)]


# Faded blacks, soft highlights, warm reds and yellows, and a cool green tint
# in the shadows: the colours of cheap drugstore film.
_CURVES = (_curve(22, 252, 0.92) + _curve(18, 242, 0.97) + _curve(26, 222, 1.08))


def _vignette(size):
    """A mask that is white in the middle and darkens toward the corners."""
    small = Image.radial_gradient('L').resize((128, 128))  # 0 at centre, 255 at edge
    small = small.point(lambda v: 255 - max(0, v - 100) * 255 // 155 * 40 // 100)
    return small.resize(size, Image.BILINEAR)


def _date_stamp(img):
    w, h = img.size
    text = time.strftime("%m %d '%y")
    font = ImageFont.load_default(size=max(14, h // 26))
    layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    pos = (w - (right - left) - w // 18, h - (bottom - top) - h // 16)
    draw.text(pos, text, font=font, fill=(255, 140, 40, 235))
    # A soft glow, the way the stamp bleeds into the film.
    glow = layer.filter(ImageFilter.GaussianBlur(max(2, h // 300)))
    img = Image.alpha_composite(img.convert('RGBA'), glow)
    return Image.alpha_composite(img, layer).convert('RGB')


def disposable(img):
    """Make a photo look like a disposable camera print. Keeps the size."""
    img = img.convert('RGB')
    w, h = img.size
    img = img.point(_CURVES)
    img = ImageEnhance.Color(img).enhance(1.15)
    img = ImageEnhance.Contrast(img).enhance(1.06)
    # Plastic lens: a touch soft.
    img = img.filter(ImageFilter.GaussianBlur(max(0.6, max(w, h) / 1600)))
    img = ImageChops.multiply(img, Image.merge('RGB', (_vignette(img.size),) * 3))
    # Film grain.
    noise = Image.effect_noise(img.size, 40).convert('RGB')
    img = Image.blend(img, ImageChops.overlay(img, noise), 0.18)
    if DATE_STAMP:
        img = _date_stamp(img)
    return img


def _jpeg(img):
    out = io.BytesIO()
    img.save(out, 'JPEG', quality=JPEG_QUALITY, optimize=True)
    return out.getvalue()


def process_shot(data):
    """A camera shot: like process(), plus the disposable look.
    Returns (filtered_jpeg, original_jpeg, w, h)."""
    original, w, h = process(data)
    with Image.open(io.BytesIO(original)) as img:
        return _jpeg(disposable(img)), original, w, h


# Costume contest photos are square and only ever shown at phone/TV size.
SQUARE_EDGE = 1024


def square(data, edge=SQUARE_EDGE):
    """Like process(), but centre-cropped to a square. Returns jpeg_bytes."""
    jpeg, _, _ = process(data)
    with Image.open(io.BytesIO(jpeg)) as img:
        img = ImageOps.fit(img, (min(edge, *img.size),) * 2, Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, 'JPEG', quality=JPEG_QUALITY, optimize=True)
        return out.getvalue()


def party_dir(party_code):
    return os.path.join(PHOTOS_DIR, party_code or 'unsorted')


def save(jpeg_bytes, party_code, subdir='', name=None):
    """Write the JPEG under the party's folder (or a subfolder of it).
    Returns its path relative to PHOTOS_DIR."""
    rel = os.path.join(party_code or 'unsorted', subdir) if subdir else (party_code or 'unsorted')
    folder = os.path.join(PHOTOS_DIR, rel)
    os.makedirs(folder, exist_ok=True)
    name = name or f'{uuid.uuid4().hex}.jpg'
    tmp = os.path.join(folder, f'.{name}.tmp')
    with open(tmp, 'wb') as f:
        f.write(jpeg_bytes)
    # Rename so a crash mid-write never leaves a half photo with a real name.
    os.replace(tmp, os.path.join(folder, name))
    return os.path.join(rel, name)


def path_for(filename):
    """Absolute path of a stored photo, refusing anything that escapes PHOTOS_DIR."""
    root = os.path.realpath(PHOTOS_DIR)
    full = os.path.realpath(os.path.join(root, filename))
    if not full.startswith(root + os.sep):
        raise PhotoError('Bad photo path.')
    return full


def save_shot(filtered_jpeg, original_jpeg, party_code):
    """Store a camera shot: the filtered print where review and export look,
    and the untouched original beside it in originals/. Returns the print's path."""
    filename = save(filtered_jpeg, party_code)
    save(original_jpeg, party_code, subdir='originals', name=os.path.basename(filename))
    return filename


def original_for(filename):
    """Relative path of a camera shot's unfiltered original."""
    folder, name = os.path.split(filename)
    return os.path.join(folder, 'originals', name)


def delete(filename):
    for rel in (filename, original_for(filename)):
        try:
            os.remove(path_for(rel))
        except (OSError, PhotoError):
            pass
