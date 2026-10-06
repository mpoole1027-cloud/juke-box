"""Disposable camera: turn a guest's upload into a clean JPEG on disk.

Every photo is decoded and re-encoded rather than stored as sent. That proves
it's really an image, caps its size, and drops all metadata (EXIF, including
the GPS location phones embed) before anything is kept.
"""
import io
import os
import uuid

from PIL import Image, ImageOps, UnidentifiedImageError

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


def party_dir(party_code):
    return os.path.join(PHOTOS_DIR, party_code or 'unsorted')


def save(jpeg_bytes, party_code):
    """Write the JPEG under the party's folder. Returns its path relative to PHOTOS_DIR."""
    folder = party_dir(party_code)
    os.makedirs(folder, exist_ok=True)
    name = f'{uuid.uuid4().hex}.jpg'
    tmp = os.path.join(folder, f'.{name}.tmp')
    with open(tmp, 'wb') as f:
        f.write(jpeg_bytes)
    # Rename so a crash mid-write never leaves a half photo with a real name.
    os.replace(tmp, os.path.join(folder, name))
    return os.path.join(party_code or 'unsorted', name)


def path_for(filename):
    """Absolute path of a stored photo, refusing anything that escapes PHOTOS_DIR."""
    root = os.path.realpath(PHOTOS_DIR)
    full = os.path.realpath(os.path.join(root, filename))
    if not full.startswith(root + os.sep):
        raise PhotoError('Bad photo path.')
    return full


def delete(filename):
    try:
        os.remove(path_for(filename))
    except (OSError, PhotoError):
        pass
