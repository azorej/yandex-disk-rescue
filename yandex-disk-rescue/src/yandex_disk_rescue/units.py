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
