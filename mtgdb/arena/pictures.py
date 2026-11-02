"""The pictures a deck sends to Proxic Arena, in the forms Arena's deck rules ask for.

Arena's deck rules say which picture formats it takes and whether it takes art
crops (``ArenaFormat.picture_formats``, ``art_crops``). When it takes JPEGs,
each card's print PNG is sent as the full-size 88% JPEG Arena itself made for
its close-ups: the same picture, about a seventh of the bytes, so a 100-card
deck is about 22 MB instead of 150. When it takes art crops, Scryfall's crop of
each picture's art goes with it, for the art window on Arena's table.

Both are cached beside the print PNGs and made once: the JPEG from the cached
PNG (again only when the PNG is newer), the crop downloaded like a PNG.
Printing keeps using the PNGs. A picture's SHA-256 is remembered by file, size
and time, so a deck sent again is not read through to be fingerprinted again.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading

from PIL import Image

import mtgdb.core.cache_names as cache_names
import mtgdb.core.net as net
from mtgdb.core.scryfall_json import card_faces
from mtgdb.printing.service import download_validated_image

# The quality Arena's close-up copy was always made at.
JPEG_QUALITY = 88
# Where a card's border colour is read for its see-through corners: a JPEG has
# none, so they are filled with it, as Arena's own copy does.
BORDER_AT = 0.02

LIGHT_DIRECTORY = "arena_jpg"
CROP_DIRECTORY = "art_crop"
FINGERPRINTS_FILE = "arena_fingerprints.json"


def light_jpeg(png_path, jpeg_dir):
    """Return the light JPEG of the print PNG at ``png_path``, made once into ``jpeg_dir``."""
    os.makedirs(jpeg_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(png_path))[0]
    target = os.path.join(jpeg_dir, f"{stem}.jpg")
    try:
        if os.path.getmtime(target) >= os.path.getmtime(png_path):
            return target
    except OSError:
        pass
    partial = f"{target}.part"
    with Image.open(png_path) as image:
        picture = image.convert("RGBA")
    width, height = picture.size
    red, green, blue, _alpha = picture.getpixel((int(width * BORDER_AT), height // 2))
    opaque = Image.new("RGB", picture.size, (red, green, blue))
    opaque.paste(picture, mask=picture.getchannel("A"))
    try:
        opaque.save(partial, "JPEG", quality=JPEG_QUALITY)
        os.replace(partial, target)
    finally:
        if os.path.exists(partial):
            os.remove(partial)
    return target


def art_crop_url(card, face_index=None):
    """Scryfall's art crop for a card's front (``face_index`` None) or one of its faces."""
    if face_index is None:
        url = (card or {}).get("image_art_crop")
        if url:
            return str(url)
        faces = card_faces(card)
        face_index = 0 if faces else None
        if face_index is None:
            return None
    faces = card_faces(card)
    if not 0 <= int(face_index) < len(faces):
        return None
    uris = faces[int(face_index)].get("image_uris")
    url = uris.get("art_crop") if isinstance(uris, dict) else None
    return str(url) if url else None


def ensure_art_crop(card, face_index, crop_dir, http=net, cancel_event=None):
    """Return a validated local art crop for a card's front or face, or None when Scryfall has none."""
    url = art_crop_url(card, face_index)
    if not url:
        return None
    path = cache_names.cache_path(card, crop_dir, ".jpg")
    if face_index:
        stem, ext = os.path.splitext(path)
        path = f"{stem}.face{int(face_index)}{ext}"
    name = str((card or {}).get("name") or "this card")
    return download_validated_image(url, path, name, http=http, cancel_event=cancel_event)


class PictureFingerprints:
    """SHA-256 of picture files, remembered by path, size and modification time."""

    def __init__(self, path):
        self.path = os.fspath(path)
        self._lock = threading.Lock()
        try:
            with open(self.path, encoding="utf-8") as handle:
                saved = json.load(handle)
            self._known = saved if isinstance(saved, dict) else {}
        except (OSError, ValueError):
            self._known = {}
        self._changed = False

    def __call__(self, file_path):
        key = os.path.abspath(os.fspath(file_path))
        stat = os.stat(key)
        stamp = [stat.st_size, stat.st_mtime_ns]
        with self._lock:
            known = self._known.get(key)
            if isinstance(known, list) and len(known) == 3 and known[:2] == stamp:
                return str(known[2])
        digest = hashlib.sha256()
        with open(key, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        sha = digest.hexdigest()
        with self._lock:
            self._known[key] = stamp + [sha]
            self._changed = True
        return sha

    def save(self):
        """Keep what was learned for the next send; a failure only costs re-reading."""
        with self._lock:
            if not self._changed:
                return
            # Forget files that are gone, so the record does not grow forever.
            known = {key: value for key, value in self._known.items() if os.path.exists(key)}
            text = json.dumps(known, sort_keys=True)
            self._changed = False
        partial = f"{self.path}.part"
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(partial, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            os.replace(partial, self.path)
        except OSError:
            try:
                os.remove(partial)
            except OSError:
                pass
