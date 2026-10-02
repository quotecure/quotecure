"""Image shrinking shared by invoice uploads and AI visualizations. Render's database storage
and request memory are the limits that matter here -- every image is stored base64 in Postgres
and inlined into pages/PDFs, so a full-resolution phone photo (5-12MB) or a 4K AI render
(3-6MB PNG) is what was locking the app up."""
import base64
import io

_SHRINKABLE = ('jpg', 'jpeg', 'png', 'webp')


def shrink_image(file_bytes, ext, max_side=1800, quality=80):
    """Downscale to max_side px on the long side and re-encode as JPEG. Returns (bytes, ext);
    anything that isn't a plain photo, fails to decode (HEIC), or wouldn't get smaller comes
    back untouched."""
    if ext not in _SHRINKABLE:
        return file_bytes, ext
    try:
        from PIL import Image, ImageOps
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(file_bytes)))
        img.thumbnail((max_side, max_side))
        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGBA')
            flat = Image.new('RGB', img.size, (255, 255, 255))
            flat.paste(img, mask=img.split()[-1])
            img = flat
        elif img.mode != 'RGB':
            img = img.convert('RGB')
        out = io.BytesIO()
        img.save(out, 'JPEG', quality=quality, optimize=True)
        if len(out.getvalue()) < len(file_bytes):
            return out.getvalue(), 'jpg'
    except Exception:
        pass
    return file_bytes, ext


def split_data_uri(uri):
    """'data:image/png;base64,AAAA' -> ('image/png', bytes). (None, None) if it isn't one."""
    if not uri or not uri.startswith('data:') or ',' not in uri:
        return None, None
    header, payload = uri.split(',', 1)
    mime = header[5:].split(';')[0] or 'application/octet-stream'
    try:
        return mime, base64.b64decode(payload)
    except Exception:
        return None, None


def shrink_data_uri(uri, max_side, quality):
    """Same as shrink_image for a stored data URI; returns a (smaller) data URI, or the
    original string unchanged if it can't be improved."""
    mime, raw = split_data_uri(uri)
    if raw is None:
        return uri
    ext = {'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp'}.get(mime)
    data, out_ext = shrink_image(raw, ext, max_side, quality) if ext else (raw, None)
    if data is raw:
        return uri
    return f"data:image/jpeg;base64,{base64.b64encode(data).decode()}"
