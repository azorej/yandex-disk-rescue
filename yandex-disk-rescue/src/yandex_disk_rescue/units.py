"""Human-readable units for command output."""


def format_size(size: int) -> str:
    """Format a byte count in binary units with one decimal: `512 B`, `1.5 KiB`, `10.0 GiB`."""
    if size < 1024:
        return f'{size} B'
    value = float(size)
    for unit in ('KiB', 'MiB', 'GiB', 'TiB', 'PiB'):
        value /= 1024
        if round(value, 1) < 1024 or unit == 'PiB':
            break
    return f'{value:.1f} {unit}'


def format_duration(seconds: float) -> str:
    """Format a duration in its two largest units, rounded to the second: `45 s`, `3 min 10 s`, `5 h 2 min`,
    `6 d 7 h`."""
    total = max(round(seconds), 0)
    minutes, secs = divmod(total, 60)
    hours, mins = divmod(minutes, 60)
    days, hrs = divmod(hours, 24)
    if days:
        return f'{days} d {hrs} h'
    if hours:
        return f'{hours} h {mins} min'
    if minutes:
        return f'{minutes} min {secs} s'
    return f'{secs} s'
