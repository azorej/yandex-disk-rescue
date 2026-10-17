"""Remote paths in the `disk:/a/b` form the Disk API reports."""

import re

from yandex_disk_rescue.api.errors import UnsupportedPathError

ROOT = 'disk:/'
_OTHER_SCHEME = re.compile(r'[A-Za-z][A-Za-z0-9+.-]*:(/|$)')


def normalize_remote_path(raw: str) -> str:
    """Return `raw` as `disk:/a/b`: with the `disk:` scheme, one leading slash and no trailing slash.

    `/a/b/`, `a/b` and `disk:/a/b/` all give `disk:/a/b`; `''`, `/`, `disk:` and `disk:/` give `disk:/`.
    A prefix such as `trash:` or `app:/` is another Disk section and raises `UnsupportedPathError`;
    a colon later in a name (`notes:2024`) is part of the name.
    """
    if raw.startswith('disk:'):
        rest = raw.removeprefix('disk:')
    elif _OTHER_SCHEME.match(raw):
        raise UnsupportedPathError(f"Unsupported remote path '{raw}': only disk:/ paths can be read.")
    else:
        rest = raw
    return ROOT + rest.strip('/')
