"""Original text card: a fixed-size image rendered only from already-approved text.

No image model, no private gallery, no photograph pretence. Each card has a
unique id (hash of the text and time) so every day's post can use a new image.
"""
import hashlib, json, os, re, stat
from pathlib import Path
from .media import MAX_ITEMS, MediaError, check_dir_chain

CARD_FILE = re.compile(r'card-[0-9a-f]{12}\.png')

CARD_SIZE = (1080, 1440)
KEEP_CARDS = 40
CAPTION = '原创文字卡（由已审核文字生成，不是真实照片）'
PALETTES = (((250, 244, 232), (60, 48, 40)), ((232, 240, 250), (30, 48, 72)), ((240, 250, 238), (38, 66, 46)), ((250, 238, 242), (84, 40, 56)))


class CardError(RuntimeError):
    pass


def _wrap(draw, text, font, width):
    lines, line = [], ''
    for ch in text:
        if ch == '\n' or draw.textlength(line + ch, font=font) > width:
            lines.append(line)
            line = '' if ch == '\n' else ch
        else:
            line += ch
    return lines + [line] if line else lines


def render(media_dir, font_path, title, line='', now=0):
    """Write card-<id>.png under media_dir, register it in manifest.json, return its id."""
    root = Path(media_dir)
    try:
        check_dir_chain(root)  # same rule as reading the manifest: no symlinked component anywhere in the path
    except (MediaError, OSError) as exc:
        raise CardError(str(exc) if isinstance(exc, MediaError) else 'card_paths_invalid') from None
    if not os.path.isfile(font_path):
        raise CardError('card_paths_invalid')
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        raise CardError('pillow_unavailable') from None
    title, line = title.strip()[:20], line.strip()[:60]
    if not title:
        raise CardError('card_text_required')
    digest = hashlib.sha256(json.dumps([title, line, now], ensure_ascii=False).encode()).hexdigest()[:12]
    card_id, name = 'card-' + digest, 'card-%s.png' % digest
    bg, fg = PALETTES[int(digest[:2], 16) % len(PALETTES)]
    try:
        image = Image.new('RGB', CARD_SIZE, bg)
        draw = ImageDraw.Draw(image)
        big, small = ImageFont.truetype(font_path, 88), ImageFont.truetype(font_path, 44)
        y = 360
        for text in _wrap(draw, title, big, CARD_SIZE[0] - 220):
            draw.text((110, y), text, font=big, fill=fg)
            y += 120
        draw.rectangle((110, y + 20, 330, y + 28), fill=fg)
        y += 90
        for text in _wrap(draw, line, small, CARD_SIZE[0] - 220):
            draw.text((110, y), text, font=small, fill=fg)
            y += 70
        target = root / name
        tmp = root / (name + '.tmp')
        image.save(tmp, 'PNG')
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    except OSError:
        raise CardError('card_render_failed') from None
    _register(root, card_id, name)
    return card_id


def _delete_generated(root, name):
    """Delete only a plain card-<12 hex>.png regular file directly inside root; never follow a link.
    A bad manifest entry (other name, path parts, symlink, directory) is just dropped from the manifest."""
    if not isinstance(name, str) or not CARD_FILE.fullmatch(name):
        return False
    path = root / name
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode) or path.parent != root:
            return False
        path.unlink()
    except OSError:
        return False
    return True


def _register(root, card_id, name):
    path = root / 'manifest.json'
    data = {'public': True, 'items': []}
    if path.exists():
        if stat.S_ISLNK(os.lstat(path).st_mode):
            raise CardError('manifest_symlink')
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except ValueError:
            raise CardError('manifest_unreadable') from None
        if data.get('public') is not True or not isinstance(data.get('items'), list):
            raise CardError('manifest_not_public')
    items = [i for i in data['items'] if isinstance(i, dict) and i.get('id') != card_id]
    items.append({'id': card_id, 'file': name, 'caption': CAPTION, 'publishable': True, 'source': 'generated_text_card'})
    cards = [i for i in items if str(i.get('source')) == 'generated_text_card']
    for old in cards[:-KEEP_CARDS]:
        items.remove(old)  # always leaves the manifest; the file is deleted only if it is provably ours
        _delete_generated(root, old.get('file'))
    data['items'] = items[-MAX_ITEMS:]
    tmp = root / 'manifest.json.tmp'
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(tmp, path)
