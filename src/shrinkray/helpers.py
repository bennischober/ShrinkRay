from enum import Enum
from typing import Any

from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)


class SIZE_UNIT(Enum):
    BYTES = 1
    KB = 2
    MB = 3
    GB = 4


def convert_unit(size_in_bytes: int, unit: SIZE_UNIT) -> float:
    """Convert the size from bytes to other units like KB, MB or GB"""
    if unit == SIZE_UNIT.KB:
        return round(size_in_bytes / 1024, 2)
    elif unit == SIZE_UNIT.MB:
        return round(size_in_bytes / (1024 * 1024), 2)
    elif unit == SIZE_UNIT.GB:
        return round(size_in_bytes / (1024 * 1024 * 1024), 2)
    else:
        return size_in_bytes


# function to sanitize decimal numbers for excel
def sanitize_number(number: object) -> str:
    """Converts a number to a string with ',' as decimal separator (excel friendly).

    Integers are converted without decimal places (e.g. 0 => '0').

    Args:
        number: The number to sanitize.

    Returns:
        str: The sanitized number as string.
    """
    number = str(number)
    parts = number.split(".")
    if len(parts) == 1:
        return parts[0]
    return parts[0] + "," + parts[1]


def generate_string(item: dict[str, Any], separator: str = ";", useCat: bool = False) -> str:
    """Function that generates an output string (row) with the given dictionary data.

    Args:
        item (dict): The movie data.
        separator (str): The separator between the columns.
        useCat (bool): Whether the category should be written or not.

    Returns:
        str: The output string.
    """
    cat = str(item["category"])
    name = str(item["name"])
    duration = str(item["duration"])
    size = sanitize_number(item["size"])
    codec = str(item["codec"])
    bitrate = sanitize_number(item["bitrate"])
    aspect_ratio = str(item["aspect_ratio"])
    dimensions = str(item["dimensions"]["width"]) + "x" + str(item["dimensions"]["height"])
    comp_rate = sanitize_number(item["compression_rate"])
    fps = str(item["fps"])
    crop = str(item["crop"])
    hdr = str(item.get("hdr", ""))
    other_codecs = str(item.get("other_codecs", ""))
    audio_languages = str(item.get("audio_languages", ""))

    row = [
        cat if useCat else "",
        name,
        duration,
        size,
        codec,
        bitrate,
        aspect_ratio,
        dimensions,
        comp_rate,
        fps,
        crop,
        hdr,
        other_codecs,
        audio_languages,
    ]
    return separator.join(row) + "\n"


def timestr_to_int(time: str | None) -> int:
    """Converts a 'HH:MM:SS' string into seconds.

    Args:
        time (str): The time as 'HH:MM:SS' string.

    Returns:
        int: The time in seconds, 0 if the string is invalid.
    """
    if not time:
        return 0
    parts = time.split(":")
    if len(parts) < 2:
        return 0

    try:
        hours = int(parts[0]) * 3600
        minutes = int(parts[1]) * 60
        seconds = int(float(parts[2]))
        return hours + minutes + seconds
    except ValueError:
        return 0


def calc_time(lhs: str | None, rhs: str | None) -> str | None:
    """Adds two 'HH:MM:SS' time strings and returns the sum.

    Args:
        lhs (str | None): The first time as 'HH:MM:SS' string (or None).
        rhs (str | None): The second time as 'HH:MM:SS' string (or None).

    Returns:
        str | None: The sum as 'HH:MM:SS' string.
    """
    if lhs is None:
        return rhs
    if rhs is None:
        return lhs

    lhs_parts = lhs.split(":")
    if len(lhs_parts) < 2:
        return rhs

    rhs_parts = rhs.split(":")
    if len(rhs_parts) < 2:
        return lhs

    try:
        # add seconds first
        seconds = int(float(lhs_parts[2])) + int(float(rhs_parts[2]))
        # check for seconds > 60
        minutes = int(seconds / 60)
        # add rest to seconds
        seconds = seconds % 60

        # add existing minutes
        minutes += int(lhs_parts[1]) + int(rhs_parts[1])
        hours = int(minutes / 60)
        minutes = minutes % 60

        # add existing hours
        hours += int(lhs_parts[0]) + int(rhs_parts[0])
    except ValueError:
        return lhs

    # checks for putting a 0 before the time value
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def get_other_codecs(other_codecs: list[dict[str, Any]]) -> str:
    """Converts the other codecs list into a comma separated string of codec names.

    Args:
        other_codecs (list): List of codec dictionaries.

    Returns:
        str: Comma separated codec names, e.g. 'aac, subrip'.
    """
    return ", ".join(codec["name"] for codec in other_codecs)


def create_movie_progress() -> Progress:
    """Creates the rich progress bar used while scanning movies.

    Returns:
        Progress: The progress bar.
    """
    return Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
    )
