"""Publishable image directory: an explicit public manifest, nothing else.

Only regular files named in <media_dir>/manifest.json with "publishable": true
are offered. Traversal, symlinks (file or directory component) and oversized or
mislabelled files are rejected; private galleries are never read.
"""
import json, os, re, stat
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024
MAX_ITEMS = 60
MAGIC = {'.png': (b'\x89PNG\r\n\x1a\n',), '.jpg': (b'\xff\xd8\xff',), '.jpeg': (b'\xff\xd8\xff',), '.webp': (b'RIFF',)}
ID = re.compile(r'[A-Za-z0-9_-]{1,40}')
FILE = re.compile(r'[A-Za-z0-9_-]{1,60}\.(?:png|jpg|jpeg|webp)')


class MediaError(ValueError):
    pass


def _regular(path, root):
    """lstat the whole chain below root: no symlink components, regular file."""
    rel = path.relative_to(root)
    current = root
    for part in rel.parts:
        current = current / part
        if stat.S_ISLNK(os.lstat(current).st_mode):
            raise MediaError('symlink_rejected')
    if not stat.S_ISREG(os.lstat(path).st_mode):
        raise MediaError('not_regular_file')


def check_dir_chain(directory):
    """Every ancestor, not only the leaf, must be a real directory: no symlinked path component.
    Shared by reading the manifest and by the text-card writer."""
    absolute = Path(os.path.abspath(directory))
    for part in (absolute, *absolute.parents):
        if stat.S_ISLNK(os.lstat(part).st_mode):
            raise MediaError('media_dir_symlink_rejected')
    if not stat.S_ISDIR(os.lstat(absolute).st_mode):
        raise MediaError('media_dir_not_directory')


class PublishableMedia:
    def __init__(self, media_dir):
        self.root = Path(media_dir)

    def _manifest(self):
        try:
            check_dir_chain(self.root)
            root = self.root.resolve(strict=True)
            path = root / 'manifest.json'
            _regular(path, root)
            data = json.loads(path.read_text(encoding='utf-8'))
        except MediaError:
            raise
        except (OSError, ValueError):
            raise MediaError('manifest_unreadable') from None
        if not isinstance(data, dict) or data.get('public') is not True or not isinstance(data.get('items'), list):
            raise MediaError('manifest_must_declare_public_true')
        return root, data['items'][:MAX_ITEMS]

    def items(self):
        """[{'id','caption','file'}] for entries that pass every check."""
        try:
            root, raw = self._manifest()
        except MediaError:
            return []
        out = []
        for item in raw:
            try:
                out.append(self._validate(root, item))
            except (MediaError, OSError):  # a missing/unreadable file skips only that entry
                continue
        return out

    def _validate(self, root, item):
        if (not isinstance(item, dict) or item.get('publishable') is not True or not ID.fullmatch(str(item.get('id', '')))
                or not FILE.fullmatch(str(item.get('file', ''))) or not isinstance(item.get('caption', ''), str)):
            raise MediaError('invalid_item')
        path = root / item['file']
        _regular(path, root)
        if path.resolve(strict=True).parent != root:
            raise MediaError('path_escape')
        size = os.lstat(path).st_size
        if not 0 < size <= MAX_BYTES:
            raise MediaError('size_rejected')
        with open(path, 'rb') as handle:
            head = handle.read(12)
        suffix = path.suffix.lower()
        if not any(head.startswith(sig) for sig in MAGIC[suffix]) or (suffix == '.webp' and head[8:12] != b'WEBP'):
            raise MediaError('content_type_mismatch')
        return {'id': item['id'], 'caption': item.get('caption', '')[:120], 'file': str(path)}

    def resolve(self, image_id):
        """Absolute path for a published id, re-validated at use time."""
        for item in self.items():
            if item['id'] == image_id:
                return item['file']
        raise MediaError('image_not_publishable')
