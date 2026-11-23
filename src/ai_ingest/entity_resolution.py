import json
import re
import unicodedata

from dataclasses import dataclass
from pathlib import Path


LEGAL_SUFFIXES = {
    "inc",
    "incorporated",
    "llc",
    "ltd",
    "limited",
    "corp",
    "corporation",
}


def normalize_entity_name(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Entity name must be a string.")

    normalized = unicodedata.normalize(
        "NFKC",
        value,
    ).casefold()

    # Preserve Unicode letters/numbers, replacing punctuation with spaces.
    characters = [
        char if char.isalnum() else " "
        for char in normalized
    ]

    tokens = "".join(characters).split()

    # Remove only recognized trailing legal suffixes.
    while tokens and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()

    key = "".join(tokens)

    if not key:
        raise ValueError(
            "Entity name is empty after normalization."
        )

    return key


@dataclass(frozen=True)
class CanonicalEntity:
    entity_id: str
    canonical_name: str
    aliases: tuple[str, ...] = ()
    identifiers: tuple[str, ...] = ()


class EntityResolver:
    def __init__(
        self,
        entities: list[CanonicalEntity],
    ) -> None:
        self.entities = {}
        self.alias_index = {}

        for entity in entities:
            if (
                not isinstance(entity.entity_id, str)
                or not entity.entity_id.strip()
            ):
                raise ValueError("Canonical entity ID is required.")

            if entity.entity_id in self.entities:
                raise ValueError(
                    f"Duplicate entity ID: {entity.entity_id}"
                )

            self.entities[entity.entity_id] = entity

            for name in (
                entity.canonical_name,
                *entity.aliases,
            ):
                key = normalize_entity_name(name)

                previous = self.alias_index.get(key)

                if (
                    previous is not None
                    and previous != entity.entity_id
                ):
                    raise ValueError(
                        "Conflicting normalized alias in seed data: "
                        f"{name}"
                    )

                self.alias_index[key] = entity.entity_id

    @classmethod
    def from_file(cls, path: Path) -> "EntityResolver":
        payload = json.loads(
            path.read_text(encoding="utf-8")
        )

        if not isinstance(payload, list):
            raise ValueError("Entity seed must be a JSON array.")

        entities = []

        for row in payload:
            if not isinstance(row, dict):
                raise ValueError("Seed entry must be an object.")

            aliases = row.get("aliases", [])
            identifiers = row.get("identifiers", [])

            if (
                not isinstance(aliases, list)
                or not all(
                    isinstance(alias, str)
                    for alias in aliases
                )
            ):
                raise ValueError(
                    "Entity aliases must be an array of strings."
                )

            if (
                not isinstance(identifiers, list)
                or not all(
                    isinstance(identifier, str)
                    for identifier in identifiers
                )
            ):
                raise ValueError(
                    "Entity identifiers must be an array of strings."
                )

            entities.append(
                CanonicalEntity(
                    entity_id=row["entity_id"],
                    canonical_name=row["canonical_name"],
                    aliases=tuple(aliases),
                    identifiers=tuple(identifiers),
                )
            )

        return cls(entities)

    def resolve(self, raw_name: str | None) -> dict:
        if raw_name is None:
            return {
                "raw_name": None,
                "normalized_key": None,
                "canonical_entity_id": None,
                "canonical_name": None,
                "status": "MISSING",
                "method": None,
            }

        key = normalize_entity_name(raw_name)
        entity_id = self.alias_index.get(key)

        if entity_id is None:
            return {
                "raw_name": raw_name,
                "normalized_key": key,
                "canonical_entity_id": None,
                "canonical_name": None,
                "status": "UNRESOLVED",
                "method": None,
            }

        entity = self.entities[entity_id]

        return {
            "raw_name": raw_name,
            "normalized_key": key,
            "canonical_entity_id": entity.entity_id,
            "canonical_name": entity.canonical_name,
            "status": "RESOLVED",
            "method": "NORMALIZED_EXACT_ALIAS_V1",
        }