# yandex-disk-rescue

A Linux command-line tool that downloads everything from a Yandex Disk account, or one folder of it, to local
storage. It uses the official Yandex Disk REST API with an OAuth token that can only read the Disk, never change it.
Every file is checked against the size and checksums the Disk has for it, and an interrupted run continues where it
stopped.

- **Whole-account or one-folder backup**, keeping the folder structure, empty folders and modification times.
- **Resumable and crash-safe.** Interrupted transfers continue from the last confirmed byte; a rerun fetches only
  what is missing or changed.
- **Verified.** Every file is checked against its cloud size, MD5 and SHA-256. Failures are reported, never
  silently skipped; a copy that fails the check is kept in quarantine, never deleted, and `verify` re-hashes a
  mirror later, offline.
- **Fast but polite.** Parallel downloads, segmented transfers of large files, and adaptive back-off that honours
  the API's rate limits.
- **Scriptable.** Progress on a terminal, status lines and a log file elsewhere, `--json` reports and documented
  exit codes for cron and systemd.

## Install

```bash
uv tool install yandex-disk-rescue
```

uv fetches Python 3.14 when the system has none. With pipx, which needs a Python 3.14 interpreter:

```bash
pipx install yandex-disk-rescue
pipx install --python python3.14 yandex-disk-rescue   # when the default Python is older
```

## Quick start

```bash
yandex-disk-rescue auth login                     # open the printed URL, allow access, paste the token
yandex-disk-rescue download disk:/ /mnt/backup    # the whole Disk into /mnt/backup
yandex-disk-rescue download disk:/ /mnt/backup    # after an interruption: continues where it stopped
yandex-disk-rescue verify /mnt/backup             # re-hash the mirror offline
```

## Requirements

- Linux.
- Python 3.14 or newer.

## Links

- [Documentation](https://github.com/azorej/yandex-disk-rescue/blob/main/README.md)
- [Changelog](https://github.com/azorej/yandex-disk-rescue/blob/main/CHANGELOG.md)
- [Issues](https://github.com/azorej/yandex-disk-rescue/issues)

This is an independent, unofficial tool. It is not affiliated with Yandex.
