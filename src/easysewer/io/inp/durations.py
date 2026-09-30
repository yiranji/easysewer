"""Explicit duration codecs; INP bare-number units are never guessed."""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, DecimalException, localcontext
from fractions import Fraction
import re

_FACTORS = {"seconds": Decimal(1), "minutes": Decimal(60), "hours": Decimal(3600)}
_CLOCK = re.compile(r"(\d+):(\d{1,2})(?::(\d{1,2}(?:\.\d+)?))?\Z", re.ASCII)
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")


@dataclass(frozen=True, kw_only=True)
class DurationCodec:
    numeric_unit: str
    resolution: timedelta = timedelta(microseconds=1)

    def __post_init__(self):
        if self.numeric_unit not in _FACTORS:
            raise ValueError("Declare numeric_unit as seconds, minutes or hours")
        if not isinstance(self.resolution, timedelta) or self.resolution <= timedelta(0):
            raise ValueError("Duration resolution must be positive")

    @staticmethod
    def _microseconds(value: timedelta) -> int:
        return (value.days * 86400 + value.seconds) * 1_000_000 + value.microseconds

    def _checked(self, seconds: Decimal) -> timedelta:
        if not seconds.is_finite() or seconds < 0:
            raise ValueError("Duration must be finite and nonnegative")
        if seconds >= Decimal(86400000000000):
            raise ValueError("Duration exceeds Python timedelta range")
        with localcontext() as context:
            context.prec = max(32, len(seconds.as_tuple().digits) + 12)
            micros = seconds * 1_000_000
            step = self._microseconds(self.resolution)
            if micros != micros.to_integral_value() or micros % step:
                raise ValueError("Duration cannot be represented at this field's resolution")
        try:
            return timedelta(microseconds=int(micros))
        except OverflowError:
            raise ValueError("Duration exceeds Python timedelta range") from None

    def parse(self, token: str) -> timedelta:
        if not isinstance(token, str) or token != token.strip() or not token:
            raise ValueError("Expected a single duration token")
        match = _CLOCK.fullmatch(token)
        try:
            with localcontext() as context:
                context.prec = max(32, len(token) + 16)
                return self._parse(token, match)
        except (DecimalException, OverflowError):
            raise ValueError(f"Invalid duration: {token!r}") from None

    def _parse(self, token, match):
        if match:
            hours, minutes = int(match[1]), int(match[2])
            seconds = Decimal(match[3] or "0")
            if minutes >= 60 or seconds >= 60:
                raise ValueError("Minute and second components must be less than 60")
            return self._checked(Decimal(hours * 3600 + minutes * 60) + seconds)
        if ":" in token:
            raise ValueError("Invalid duration format")
        if not _NUMBER.fullmatch(token):
            raise ValueError("Invalid numeric duration token")
        number = Decimal(token)
        if number.is_finite() and number != 0 and abs(number) < Decimal("1e-10"):
            raise ValueError("Duration cannot be represented at microsecond resolution")
        return self._checked(number * _FACTORS[self.numeric_unit])

    def format(self, value: timedelta, *, style: str = "clock") -> str:
        if not isinstance(value, timedelta):
            raise TypeError("Duration values must be timedelta objects")
        micros = self._microseconds(value)
        self._checked(Decimal(f"{micros}e-6"))
        if style == "numeric":
            number = Fraction(micros, 1_000_000 * int(_FACTORS[self.numeric_unit]))
            denominator = number.denominator
            for factor in (2, 5):
                while denominator % factor == 0:
                    denominator //= factor
            if denominator != 1:
                raise ValueError("Use clock format for this duration")
            with localcontext() as context:
                context.prec = len(str(abs(number.numerator))) + len(str(number.denominator)) + 10
                text = format(Decimal(number.numerator) / Decimal(number.denominator), "f")
            # Numeric hours may be recurring decimals. Reject instead of silently
            # emitting a value that cannot roundtrip at the selected precision.
            if self.parse(text) != value:
                raise ValueError("Use clock format for this duration")
            return text.rstrip("0").rstrip(".") if "." in text else text
        if style != "clock":
            raise ValueError("Duration style must be clock or numeric")
        hours, remaining = divmod(micros, 3_600_000_000)
        minutes, remaining = divmod(remaining, 60_000_000)
        seconds, fraction = divmod(remaining, 1_000_000)
        suffix = f".{fraction:06d}".rstrip("0") if fraction else ""
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}{suffix}"
