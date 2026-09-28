# Copied from icp-bot/backend/domains/mahak/people/mahak/persona/schema.py (read-only reference repo).
# Unchanged except this header; see api/persona/generator.py for what Rasa Incanta adapts.
"""JSON schema the model must fill in, via Anthropic's structured-output
`output_config: json_schema` mechanism (see `..icp.anthropic_client._stream_structured_json`,
reused here instead of the older forced tool-use pattern the original
Node prototype used). Mirrors the 8-section structure of the Practus
persona 1-pager template — ported 1:1 from the prototype's `lib/schema.js`.
"""

from __future__ import annotations

_BULLET_ITEM = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": ["observed", "inferred", "gap"]},
        "text": {"type": "string"},
    },
    "required": ["label", "text"],
}

PERSONA_REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "person": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "roleTitle": {"type": "string", "description": "e.g. 'Chief Operating Officer, Sanctum Wealth'"},
                "subtitle": {"type": "string", "description": "Location | education | headline experience summary"},
                "initials": {"type": "string", "description": "2-letter initials for the avatar"},
                "badges": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "3-4 short badge labels, e.g. seniority, functional focus, sector tags",
                },
            },
            "required": ["name", "roleTitle", "subtitle", "initials", "badges"],
        },
        "sourceValidationNote": {
            "type": "string",
            "description": (
                "1-3 sentence note on what sources were actually used (a scraped LinkedIn profile/posts, "
                "a live web search, or a pre-loaded stored profile — only the source material given in "
                "this prompt, never outside general knowledge) and any name-collision or data-quality caveats."
            ),
        },
        "section1": {
            "type": "object",
            "properties": {
                "factsLeft": {"type": "array", "items": _BULLET_ITEM},
                "factsRight": {"type": "array", "items": _BULLET_ITEM},
                "timeline": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "date": {"type": "string"},
                            "role": {"type": "string"},
                            "firm": {"type": "string"},
                        },
                        "required": ["date", "role", "firm"],
                    },
                },
            },
            "required": ["factsLeft", "factsRight", "timeline"],
        },
        "section2": {
            "type": "object",
            "properties": {
                "note": {
                    "type": "string",
                    "description": "Caveat about how much LinkedIn content was actually available in the provided input.",
                },
                "contentThemes": {"type": "array", "items": {"type": "string"}},
                "contentTypeBullets": {"type": "array", "items": _BULLET_ITEM},
                "engagementBullets": {"type": "array", "items": _BULLET_ITEM},
            },
            "required": ["note", "contentThemes", "contentTypeBullets", "engagementBullets"],
        },
        "section3": {
            "type": "object",
            "properties": {
                "note": {"type": "string"},
                "left": {"type": "array", "items": _BULLET_ITEM},
                "right": {"type": "array", "items": _BULLET_ITEM},
            },
            "required": ["note", "left", "right"],
        },
        "section4": {
            "type": "object",
            "properties": {
                "interestAreas": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "5-7 high-probability professional interest areas, short phrases.",
                },
                "hooks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"title": {"type": "string"}, "text": {"type": "string"}},
                        "required": ["title", "text"],
                    },
                    "description": "Ready-to-use conversation hooks, each tied to a specific item in the supplied material.",
                },
            },
            "required": ["interestAreas", "hooks"],
        },
        "section5": {
            "type": "object",
            "properties": {
                "resonates": {"type": "array", "items": {"type": "string"}},
                "avoid": {"type": "array", "items": {"type": "string"}},
                "openingLines": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "2-3 opening line variants for email/LinkedIn.",
                },
                "icebreakers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "2-3 meeting icebreaker lines referencing a talk, article, milestone, or sector trend.",
                },
            },
            "required": ["resonates", "avoid", "openingLines", "icebreakers"],
        },
        "section6": {
            "type": "object",
            "properties": {
                "cards": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "body": {"type": "string"},
                            "questions": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "1-3 entry-point questions/hypotheses for this relevance area.",
                            },
                        },
                        "required": ["title", "body", "questions"],
                    },
                    "description": (
                        "One card per relevant Practus opportunity area: transformation, cost optimization, "
                        "performance improvement, value creation. Omit an area only if genuinely not relevant "
                        "to this person's role."
                    ),
                },
            },
            "required": ["cards"],
        },
        "section7": {
            "type": "object",
            "properties": {
                "flags": {"type": "array", "items": {"type": "string"}},
                "noFlagsFound": {"type": "boolean"},
            },
            "required": ["flags", "noFlagsFound"],
        },
        "section8": {
            "type": "object",
            "properties": {
                "overall": {"type": "string", "description": "e.g. 'High', 'Medium', 'Medium-High', 'Low'"},
                "bars": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "pct": {"type": "number"},
                            "rating": {"type": "string"},
                        },
                        "required": ["label", "pct", "rating"],
                    },
                },
                "notes": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["overall", "bars", "notes"],
        },
        "optionalEnhancement": {
            "type": "object",
            "properties": {
                "include": {"type": "boolean"},
                "psychDrivers": {"type": "array", "items": {"type": "string"}},
                "engagementStrategy": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["include", "psychDrivers", "engagementStrategy"],
        },
    },
    "required": [
        "person",
        "sourceValidationNote",
        "section1",
        "section2",
        "section3",
        "section4",
        "section5",
        "section6",
        "section7",
        "section8",
        "optionalEnhancement",
    ],
}


def _require_no_additional_properties(node: object) -> None:
    """Anthropic's `output_config: json_schema` mode (unlike the older
    forced tool-use mechanism this schema was originally written for)
    rejects any object-type schema node that doesn't explicitly set
    `additionalProperties: false` — confirmed live: a real call against
    this schema 400'd with exactly that message before this walk was
    added. Mutates every object node in place instead of hand-annotating
    each of the schema's ~15 nested objects above, so a future edit here
    can't silently reintroduce the same 400."""
    if isinstance(node, dict):
        if node.get("type") == "object":
            node.setdefault("additionalProperties", False)
        for value in node.values():
            _require_no_additional_properties(value)
    elif isinstance(node, list):
        for item in node:
            _require_no_additional_properties(item)


_require_no_additional_properties(PERSONA_REPORT_SCHEMA)
