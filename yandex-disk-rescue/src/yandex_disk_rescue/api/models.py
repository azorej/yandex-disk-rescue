"""The account and resource records the client returns, parsed from Disk API response bodies."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from yandex_disk_rescue.api.remote_paths import ROOT


@dataclass(frozen=True)
class Account:
    uid: str
    login: str
    total_space: int
    used_space: int


@dataclass(frozen=True)
class Resource:
    path: str
    kind: Literal['file', 'dir']
    size: int | None
    md5: str | None
    sha256: str | None
    modified: datetime


@dataclass(frozen=True)
class FolderPage:
    kind: Literal['file', 'dir']
    items: tuple[Resource, ...]
    total: int


class MalformedBodyError(ValueError):
    """A response body that does not have the expected shape; the message is the reason, for the caller to wrap."""


def parse_account(content: bytes) -> Account:
    body = _json_object(content)
    user = body.get('user')
    user = user if isinstance(user, dict) else {}
    login = _non_empty_string(user, 'login', 'user.login')
    total_space = _byte_count(body, 'total_space', 'total_space')
    used_space = _byte_count(body, 'used_space', 'used_space')
    uid = _non_empty_string(user, 'uid', 'user.uid')
    return Account(uid=uid, login=login, total_space=total_space, used_space=used_space)


def parse_folder_page(content: bytes) -> FolderPage:
    """Parse a `GET /v1/disk/resources` body; a file has no `_embedded` and gives a page without items."""
    body = _json_object(content)
    kind = _kind(body, 'type')
    if kind == 'file':
        return FolderPage(kind=kind, items=(), total=0)
    embedded = body.get('_embedded')
    if not isinstance(embedded, dict):
        raise MalformedBodyError('_embedded is missing or not an object')
    items = embedded.get('items')
    if not isinstance(items, list):
        raise MalformedBodyError('_embedded.items is missing or not a list')
    total = _byte_count(embedded, 'total', '_embedded.total')
    return FolderPage(
        kind=kind,
        items=tuple(_resource(item, f'_embedded.items[{index}].') for index, item in enumerate(items)),
        total=total,
    )


def parse_resource(content: bytes) -> Resource:
    """Parse the `GET /v1/disk/resources` body of one file or folder below the root."""
    return _resource(_json_object(content), '')


def _resource(item: object, prefix: str) -> Resource:
    """Parse one resource object; `prefix` locates it in the body for error messages (`''` for the body itself)."""
    if not isinstance(item, dict):
        raise MalformedBodyError(f'{prefix.removesuffix(".")} is not an object')
    path = _non_empty_string(item, 'path', f'{prefix}path')
    if not path.startswith(ROOT) or path == ROOT:
        raise MalformedBodyError(f'{prefix}path is not a disk:/ path below the root')
    kind = _kind(item, f'{prefix}type')
    return Resource(
        path=path.rstrip('/'),
        kind=kind,
        size=_byte_count(item, 'size', f'{prefix}size') if kind == 'file' else None,
        md5=_optional_string(item, 'md5', f'{prefix}md5'),
        sha256=_optional_string(item, 'sha256', f'{prefix}sha256'),
        modified=_timestamp(item, 'modified', f'{prefix}modified'),
    )


def _json_object(content: bytes) -> dict[str, object]:
    try:
        body = json.loads(content)
    except ValueError:
        raise MalformedBodyError('the body is not JSON') from None
    if not isinstance(body, dict):
        raise MalformedBodyError('the body is not a JSON object')
    return body


def _kind(body: dict[str, object], name: str) -> Literal['file', 'dir']:
    match body.get('type'):
        case 'file':
            return 'file'
        case 'dir':
            return 'dir'
        case _:
            raise MalformedBodyError(f"{name} is missing or not 'file' or 'dir'")


def _non_empty_string(body: dict[str, object], key: str, name: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise MalformedBodyError(f'{name} is missing or not a non-empty string')
    return value


def _optional_string(body: dict[str, object], key: str, name: str) -> str | None:
    value = body.get(key)
    if value is not None and (not isinstance(value, str) or not value):
        raise MalformedBodyError(f'{name} is not a non-empty string')
    return value


def _byte_count(body: dict[str, object], key: str, name: str) -> int:
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MalformedBodyError(f'{name} is missing or not a non-negative integer')
    return value


def _timestamp(body: dict[str, object], key: str, name: str) -> datetime:
    value = body.get(key)
    reason = f'{name} is missing or not an ISO 8601 timestamp with a UTC offset'
    if not isinstance(value, str):
        raise MalformedBodyError(reason)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise MalformedBodyError(reason) from None
    if parsed.tzinfo is None:
        raise MalformedBodyError(reason)
    return parsed.astimezone(UTC)
