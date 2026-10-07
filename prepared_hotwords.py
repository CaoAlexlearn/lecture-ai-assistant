"""Load the canonical Pre Agent hotword artifact for one lecture."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path

logger = logging.getLogger(__name__)


def load_prepared_hotword_terms(
    courses_root: Path, *, course_id: str, lecture_id: str
) -> tuple[str, ...]:
    """Return validated terms in canonical artifact order, or no terms."""

    root = courses_root.resolve()
    path = (
        root / course_id / "lectures" / lecture_id / "prepared" / "hotwords.json"
    ).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        logger.warning(
            "Ignoring unsafe hotword path course_id=%s lecture_id=%s",
            course_id,
            lecture_id,
        )
        return ()
    if not path.is_file():
        logger.info("No prepared hotwords found path=%s", path)
        return ()

    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Ignoring unreadable hotword artifact path=%s error=%s", path, exc)
        return ()
    if not isinstance(artifact, Mapping):
        logger.warning("Ignoring invalid hotword artifact path=%s", path)
        return ()
    if artifact.get("schema_version") != "1.0" or artifact.get("lecture_id") != lecture_id:
        logger.warning("Ignoring mismatched hotword artifact path=%s", path)
        return ()

    hotwords = artifact.get("hotwords")
    if not isinstance(hotwords, list):
        logger.warning("Ignoring invalid hotword list path=%s", path)
        return ()

    terms: list[str] = []
    seen: set[str] = set()
    for item in hotwords:
        if not isinstance(item, Mapping):
            logger.warning("Ignoring invalid hotword entry path=%s", path)
            return ()
        term = item.get("term")
        if not isinstance(term, str) or not term.strip():
            logger.warning("Ignoring invalid hotword term path=%s", path)
            return ()
        normalized = term.strip()
        key = normalized.casefold()
        if key in seen:
            logger.warning("Ignoring duplicate hotword artifact path=%s", path)
            return ()
        seen.add(key)
        terms.append(normalized)

    logger.info("Loaded prepared hotwords path=%s count=%s", path, len(terms))
    return tuple(terms)
