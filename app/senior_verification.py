"""Senior Citizen discount helpers: read the date of birth from an ID and check the age.

The cashier scans/uploads an ID (or types the date of birth). OCR text is searched for the
date of birth only; the discount is allowed when the holder is 60 years old or above.
The date of birth itself is never stored, only used to decide eligibility.
"""

import re
from dataclasses import dataclass
from datetime import date

SENIOR_MIN_AGE = 60

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_NAMES = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)

_DASH_LIKE_RE = re.compile(r"[\u2010-\u2015\u2212_]")

# 1960-05-12 / 1960/05/12
_ISO_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)")
# 05/12/1960, 12-05-1960, 12.05.1960 (4-digit year only, no spaces: avoids ID numbers)
_NUMERIC_RE = re.compile(r"(?<!\d)(\d{1,2})[-/.](\d{1,2})[-/.]((?:19|20)\d{2})(?!\d)")
# 12 May 1960 / 12-MAY-1960 / 12 May, 1960
_DAY_MONTH_RE = re.compile(
    rf"(?<!\d)(\d{{1,2}})[\s\-/.]*({_MONTH_NAMES})\.?[\s,\-/.]*((?:19|20)\d{{2}})(?!\d)", re.I
)
# May 12, 1960 / MAY 12 1960
_MONTH_DAY_RE = re.compile(
    rf"(?<![a-z])({_MONTH_NAMES})\.?[\s\-/.]*(\d{{1,2}})(?:st|nd|rd|th)?[\s,\-/.]+((?:19|20)\d{{2}})(?!\d)", re.I
)

_BIRTH_HINT_RE = re.compile(
    r"birth|\bdob\b|\bborn\b|b-?day|kapanganakan|petsa\s+ng\s+kapanganakan|\bbd\b", re.I
)
_OTHER_DATE_HINT_RE = re.compile(
    r"expir|valid\s*(?:until|thru|till)|\bexp\b|issue|date\s+of\s+issue|member\s*since|registered|"
    r"date\s+signed|effectivity|renewal",
    re.I,
)


@dataclass
class BirthDateResult:
    dob: date
    ambiguous: bool = False
    alt_dob: date | None = None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        value = date(year, month, day)
    except ValueError:
        return None
    return value


def _month_number(name: str) -> int | None:
    return _MONTHS.get(name.strip().lower()[:3])


def age_on(dob: date, today: date) -> int:
    years = today.year - dob.year
    if (today.month, today.day) < (dob.month, dob.day):
        years -= 1
    return years


def is_senior_age(dob: date, today: date) -> bool:
    return age_on(dob, today) >= SENIOR_MIN_AGE


def parse_iso_date(value) -> date | None:
    """Parse a strict YYYY-MM-DD string (what the popup sends). None if invalid."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\s*(\d{4})-(\d{2})-(\d{2})\s*", value)
    if not match:
        return None
    return _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _candidates(text: str):
    """Yield (start, end, date, ambiguous, alt_date) for every date-looking text."""
    for m in _ISO_RE.finditer(text):
        dob = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if dob:
            yield m.start(), m.end(), dob, False, None
    for m in _NUMERIC_RE.finditer(text):
        a, b, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if a > 12 and b <= 12:
            dob, ambiguous, alt = _safe_date(year, b, a), False, None      # DD/MM/YYYY
        elif b > 12 and a <= 12:
            dob, ambiguous, alt = _safe_date(year, a, b), False, None      # MM/DD/YYYY
        elif a == b:
            dob, ambiguous, alt = _safe_date(year, a, b), False, None
        else:
            dob = _safe_date(year, a, b)                                   # default MM/DD/YYYY (PH usage)
            alt = _safe_date(year, b, a)
            ambiguous = dob is not None and alt is not None
        if dob:
            yield m.start(), m.end(), dob, ambiguous, alt if ambiguous else None
    for m in _DAY_MONTH_RE.finditer(text):
        month = _month_number(m.group(2))
        dob = _safe_date(int(m.group(3)), month, int(m.group(1))) if month else None
        if dob:
            yield m.start(), m.end(), dob, False, None
    for m in _MONTH_DAY_RE.finditer(text):
        month = _month_number(m.group(1))
        dob = _safe_date(int(m.group(3)), month, int(m.group(2))) if month else None
        if dob:
            yield m.start(), m.end(), dob, False, None


def extract_birth_date(text: str, today: date) -> BirthDateResult | None:
    """Find the date of birth in scanned/typed/OCR text.

    A date next to a "birth / DOB / born" label wins. Dates next to "expiry / issued" labels
    are ignored. Without a label, the oldest plausible date is used (a date of birth is
    always older than an issue or expiry date printed on the same ID).
    """
    raw = _DASH_LIKE_RE.sub("-", text or "")
    if not raw.strip():
        return None
    found = sorted(_candidates(raw), key=lambda item: item[0])
    scored = []
    seen = set()
    prev_end = 0
    for pos, end, dob, ambiguous, alt in found:
        window_start = max(0, pos - 45, prev_end if prev_end <= pos else 0)
        prev_end = max(prev_end, end)
        if dob > today or dob.year < 1900:
            continue
        key = (pos, dob)
        if key in seen:
            continue
        seen.add(key)
        before = raw[window_start:pos]
        after = raw[end:end + 12]
        score = 0
        if _BIRTH_HINT_RE.search(before):
            score += 10
        if _OTHER_DATE_HINT_RE.search(before[-25:]) and not _BIRTH_HINT_RE.search(before[-25:]):
            score -= 10
        if _OTHER_DATE_HINT_RE.search(after) and not _BIRTH_HINT_RE.search(after):
            score -= 5
        scored.append((score, dob, ambiguous, alt))
    if not scored:
        return None
    best_score = max(item[0] for item in scored)
    best = [item for item in scored if item[0] == best_score]
    if best_score < 0:
        return None
    best.sort(key=lambda item: item[1])      # oldest date among the best-scored
    _, dob, ambiguous, alt = best[0]
    return BirthDateResult(dob=dob, ambiguous=ambiguous, alt_dob=alt)


def describe_birth_check(dob: date, today: date, ambiguous: bool = False) -> dict:
    age = age_on(dob, today)
    eligible = age >= SENIOR_MIN_AGE
    shown = dob.strftime("%B %d, %Y").replace(" 0", " ")
    if eligible:
        message = (
            f"Date of birth {shown} (age {age}): eligible for the Senior Citizen discount (60 and above). "
            "Check it against the physical ID, then tick the confirmation box."
        )
    else:
        message = f"Date of birth {shown} (age {age}): below 60, not eligible for the Senior Citizen discount."
    if ambiguous:
        message += " The day and month order on the ID was unclear (read as month/day). Check the date."
    return {
        "dob": dob.isoformat(),
        "age": age,
        "eligible": eligible,
        "ambiguous": ambiguous,
        "message": message,
    }
