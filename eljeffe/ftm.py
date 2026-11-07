"""FtM JSON structure checks and conservative FBI notice normalization.

This is a structural validator, not a replacement for the upstream FtM ontology
validator. Upstream entities and relationship schemas are retained without
inventing classifications or merging people who happen to share a name.
"""

from __future__ import annotations

import copy
import hashlib
import re
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit

from .network import utc_now


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        elif tag in {"p", "br", "div", "li", "tr"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1
        elif tag in {"p", "div", "li", "tr"}:
            self.parts.append(" ")

    def handle_data(self, value):
        if not self.hidden:
            self.parts.append(value)


def plain_text(value) -> str:
    if value is None:
        return ""
    parser = _PlainText()
    parser.feed(str(value))
    return " ".join("".join(parser.parts).split())


def validate_entity(entity: dict) -> dict:
    if not isinstance(entity, dict):
        raise ValueError("FtM record must be a JSON object")
    identifier = entity.get("id")
    schema = entity.get("schema")
    if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 1024:
        raise ValueError("FtM record requires a nonempty string id (at most 1024 characters)")
    if any(ord(c) < 32 or ord(c) == 127 for c in identifier):
        raise ValueError("FtM id contains control characters")
    if not isinstance(schema, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,99}", schema):
        raise ValueError("FtM record requires a schema name")
    properties = entity.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("FtM properties must be an object")
    for key, values in properties.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", key):
            raise ValueError("FtM property names must be schema property identifiers")
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ValueError(f"FtM property {key!r} must be an array of strings, including entity references")
    for key in ("datasets", "referents"):
        if key in entity and (not isinstance(entity[key], list)
                              or not all(isinstance(value, str) for value in entity[key])):
            raise ValueError(f"FtM {key} must be an array of strings")
    for key in ("caption", "first_seen", "last_seen", "last_change"):
        if key in entity and entity[key] is not None and not isinstance(entity[key], str):
            raise ValueError(f"FtM {key} must be a string")
    if "target" in entity and not isinstance(entity["target"], bool):
        raise ValueError("FtM target must be a boolean")
    return copy.deepcopy(entity)


def classification(entity: dict) -> str:
    properties = entity.get("properties", {})
    topics = set(properties.get("topics", []))
    # target=true means a source's matching target, not an arrest warrant.
    if "wanted" in topics:
        return "wanted"
    if "sanction" in topics or entity.get("schema") == "Sanction":
        return "sanction"
    if topics.intersection({"role.pep", "role.pep.frmr"}):
        return "pep"
    if entity.get("_category") == "missing" or any(
        value.casefold() in {"missing", "missing person", "missing-person"}
        for value in properties.get("classification", [])
    ):
        return "missing"
    return "other"


_MONTHS = {name: number for number, name in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"), start=1
)}


def iso_date(value) -> str | None:
    """Normalize only unambiguous source dates; never fill missing month/day."""
    text = re.sub(r"\s*\(true\)\s*", "", str(value), flags=re.I).strip()
    if re.fullmatch(r"\d{4}", text):
        return text if 1 <= int(text) <= 9999 else None
    if re.fullmatch(r"\d{4}-\d{2}", text):
        try:
            date.fromisoformat(text + "-01")
            return text
        except ValueError:
            return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            return None
    # FBI's documented display uses English month names. Numeric slash dates
    # are deliberately left raw, avoiding US/European date ambiguity.
    match = re.fullmatch(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", text)
    if match:
        month_name = match.group(1).casefold()
        month = _MONTHS.get(month_name)
        if month is None and len(month_name) == 3:
            month = next((number for name, number in _MONTHS.items() if name.startswith(month_name)), None)
        if month is not None:
            try:
                return date(int(match.group(3)), month, int(match.group(2))).isoformat()
            except ValueError:
                pass
    return None


def _source_timestamp(value) -> str | None:
    if not isinstance(value, str):
        return None
    if iso_date(value):
        return iso_date(value)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc)
            return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")
        return parsed.isoformat(timespec="seconds")
    except ValueError:
        return None


def _values(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _country_code(value) -> str | None:
    # Exact country labels/codes from the attributed FtM model are safe to
    # normalize. Ambiguous adjectives and free text remain in source notes.
    from .model import model
    normalized = plain_text(value).casefold()
    countries = model()["types"]["country"]["values"]
    if normalized in countries:
        return normalized
    return next((code for code, label in countries.items() if label.casefold() == normalized), None)


def _notice_category(item: dict) -> str:
    """A positive wanted category is required; mixed/victim lists stay separate."""
    parts = urlsplit(str(item.get("url") or ""))
    segments = parts.path.strip("/").split("/")
    category = segments[1] if len(segments) > 1 and segments[0] == "wanted" else ""
    role = str(item.get("person_classification") or "").casefold()
    status = str(item.get("status") or "").strip().casefold()
    resolved = {"captured", "arrested", "deceased", "located", "recovered", "surrendered", "not wanted", "resolved"}
    if status in resolved:
        return "other"
    subjects = [plain_text(v).casefold() for v in _values(item.get("subjects"))]
    if category == "kidnap" or (role == "victim" and category == "parental-kidnappings"):
        return "missing"
    if role in {"victim", "missing", "missing person"}:
        return "missing" if any("missing" in subject for subject in subjects) else "other"
    if parts.hostname in {"fbi.gov", "www.fbi.gov"} and category in {
        # The official /wanted/fugitives page lists these subcategories; the
        # API URLs use their leaf names, not /fugitives/<name>.
        # https://www.fbi.gov/wanted/fugitives
        "topten", "fugitives", "most-wanted-fraudsters", "parental-kidnappings",
        "cyber", "cac", "murders", "additional", "wcc", "counterintelligence",
        "cei", "human-trafficking", "wanted_terrorists", "dt",
        # /wanted/terrorism is an aggregate containing seeking-information
        # notices as well; neither it nor /terrorinfo is a positive wanted rule.
    }:
        return "wanted"
    return "other"


def fbi_entity(item: dict, *, retrieved_at: str | None = None) -> dict:
    """Map an FBI public *notice*; a notice need not describe a wanted person."""
    if not isinstance(item, dict):
        raise ValueError("FBI item must be an object")
    source_id = item.get("uid") or item.get("@id") or item.get("url")
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("FBI record has no stable uid, @id, or source URL")
    identifier = "fbi-" + (source_id if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", source_id)
                           else hashlib.sha256(source_id.encode("utf-8")).hexdigest())
    title = plain_text(item.get("title"))
    if not title:
        raise ValueError(f"FBI record {identifier} has no title")
    # The API also publishes groups, events and unidentified scenes. A headline
    # or person_classification='main' alone is not evidence of a single person.
    person_fields = ("sex", "dates_of_birth_used", "place_of_birth", "age_min", "age_max",
                     "height_min", "height_max", "weight_min", "weight_max", "hair",
                     "eyes", "eyes_raw", "scars_and_marks")
    is_person = any(item.get(key) not in (None, "", []) for key in person_fields)
    schema = "Person" if is_person else "Thing"
    category = _notice_category(item)
    props: dict[str, list[str]] = {"name": [title], "publisher": ["Federal Bureau of Investigation"],
                                   "publisherUrl": ["https://www.fbi.gov/"],
                                   "retrievedAt": [retrieved_at or utc_now()]}

    def add(key, value):
        for entry in _values(value):
            text = plain_text(entry)
            if text and text not in props.setdefault(key, []):
                props[key].append(text)
        if not props.get(key):
            props.pop(key, None)

    add("alias", item.get("aliases"))
    add("sourceUrl", item.get("url"))
    add("description", item.get("description"))
    add("keywords", item.get("subjects"))
    if category == "wanted":
        add("topics", "wanted")
    if schema == "Person":
        add("birthPlace", item.get("place_of_birth"))
        add("hairColor", item.get("hair"))
        add("eyeColor", item.get("eyes_raw") or item.get("eyes"))
        add("appearance", item.get("scars_and_marks"))
        for key in ("nationality", "citizenship"):
            for value in _values(item.get(key)):
                code = _country_code(value)
                if code:
                    add(key, code)
                if key == "citizenship" and plain_text(value):
                    add("notes", f"Citizenship (source): {plain_text(value)}")
        sex = plain_text(item.get("sex")).casefold()
        if sex in {"male", "female", "other"}:
            add("gender", sex)
        elif sex:
            add("notes", f"Sex (source): {plain_text(item['sex'])}")
        for raw_date in _values(item.get("dates_of_birth_used")):
            normalized = iso_date(raw_date)
            if normalized:
                add("birthDate", normalized)
            else:
                add("notes", f"Date of birth used (source): {plain_text(raw_date)}")
        add("status", item.get("status"))
        if category == "missing":
            add("classification", "Missing person")
        add("classification", item.get("person_classification"))
    elif item.get("status"):
        add("notes", "Status (source): " + plain_text(item["status"]))
    for source_key, target_key in (("modified", "modifiedAt"), ("publication", "createdAt")):
        normalized = _source_timestamp(item.get(source_key))
        if normalized:
            add(target_key, normalized)
    for key, label in (
        ("warning_message", "Warning (source)"), ("caution", "Caution (source)"),
        ("remarks", "Remarks (source)"), ("details", "Details (source)"),
        ("reward_text", "Reward (source)"), ("nationality", "Nationality (source)"),
        ("age_range", "Age range (source)"), ("age_min", "Minimum age (source)"),
        ("age_max", "Maximum age (source)"), ("height_min", "Minimum height (source units)"),
        ("height_max", "Maximum height (source units)"), ("weight_min", "Minimum weight (source units)"),
        ("weight_max", "Maximum weight (source units)"), ("field_offices", "FBI field office (not whereabouts)"),
        ("person_classification", "Person classification (source)"),
        ("poster_classification", "Poster classification (source)"),
    ):
        for value in _values(item.get(key)):
            text = plain_text(value)
            if text:
                add("notes", f"{label}: {text}")

    files = []
    for record in item.get("files") or []:
        if isinstance(record, dict) and isinstance(record.get("url"), str):
            files.append({"url": record["url"], "name": plain_text(record.get("name")) or "FBI poster",
                          "kind": "pdf"})
    for record in item.get("images") or []:
        if isinstance(record, dict):
            url = record.get("original") or record.get("large") or record.get("thumb")
            if isinstance(url, str):
                files.append({"url": url, "name": plain_text(record.get("caption")) or "FBI source image",
                              "kind": "image"})
    entity = {"id": identifier, "schema": schema, "caption": title, "properties": props,
              "datasets": ["fbi_wanted_api"], "_files": files, "_category": category}
    return validate_entity(entity)
