"""Tool definitions shared by the MCP server and the CLI.

Descriptions are deliberately terse: every client sends them on *every*
request, so each word here is paid for again and again. The workflow hint
lives in one place (the search tool) instead of being repeated.
"""
from __future__ import annotations

from . import db

TOOLS = [
    {
        "name": "goae_search",
        "description": "Find GOÄ codes for a service description. Returns 'code|points|text' lines. "
                       "Then use goae_get for details.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "service, German keywords"},
                "k": {"type": "integer", "description": "max results, default 5"},
                "section": {"type": "string", "description": "optional section letter A-P"},
            },
            "required": ["q"],
        },
    },
    {
        "name": "goae_get",
        "description": "Full text, points, fees (1.0x/threshold/max) and own rules of one code.",
        "inputSchema": {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
    },
    {
        "name": "goae_check",
        "description": "Exclusion rules between codes billed together.",
        "inputSchema": {
            "type": "object",
            "properties": {"codes": {"type": "array", "items": {"type": "string"}}},
            "required": ["codes"],
        },
    },
    {
        "name": "goae_rules",
        "description": "All general rules mentioning a code.",
        "inputSchema": {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
    },
]

# A typical "first draft" tool set, kept for the benchmark comparison.
VERBOSE_TOOLS = [
    {
        "name": "search_goae_fee_codes",
        "description": (
            "Searches the German medical fee schedule (Gebührenordnung für Ärzte, GOÄ) for fee "
            "codes (Gebührenziffern) matching a free-text description of a medical service. "
            "Use this tool whenever you need to find out which GOÄ number applies to a service "
            "that was performed. The tool returns a list of matching entries as JSON, including "
            "the code, the full official service description, the point value, the section and "
            "subsection with titles, and the factor class used for fee calculation. Results are "
            "ordered by relevance."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The free-text description of the medical service to search for, in German."},
                "limit": {"type": "integer", "description": "The maximum number of results to return. Defaults to 10."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_goae_fee_code_details",
        "description": (
            "Returns all available details for a single GOÄ fee code: the complete official "
            "service description, the point value, section, the calculated fees for the simple "
            "rate, the threshold rate and the maximum rate, and all billing notes that belong "
            "to the code."),
        "inputSchema": {
            "type": "object",
            "properties": {"code": {"type": "string", "description": "The GOÄ fee code, e.g. '250' or 'K 1'."}},
            "required": ["code"],
        },
    },
    {
        "name": "calculate_goae_fee",
        "description": "Calculates the fee in Euro for a GOÄ code and a multiplication factor.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "The GOÄ fee code."},
                "factor": {"type": "number", "description": "The multiplication factor, e.g. 2.3."},
            },
            "required": ["code"],
        },
    },
    {
        "name": "check_goae_code_combination",
        "description": (
            "Checks whether a set of GOÄ fee codes may be billed together, based on the "
            "exclusion rules ('nicht neben') in the official fee schedule. Returns every rule "
            "that forbids a combination of the given codes."),
        "inputSchema": {
            "type": "object",
            "properties": {"codes": {"type": "array", "items": {"type": "string"}, "description": "The GOÄ fee codes to check."}},
            "required": ["codes"],
        },
    },
    {
        "name": "list_goae_rules_for_code",
        "description": "Lists all general billing rules (Allgemeine Bestimmungen) and notes that mention the given GOÄ fee code.",
        "inputSchema": {
            "type": "object",
            "properties": {"code": {"type": "string", "description": "The GOÄ fee code."}},
            "required": ["code"],
        },
    },
]


def call(name: str, args: dict, profile: db.Profile = db.DEFAULT) -> str:
    """Dispatch a tool call (both naming schemes) to the query layer."""
    if name in ("goae_search", "search_goae_fee_codes"):
        return db.search(args.get("q") or args.get("query", ""),
                         k=args.get("k") or args.get("limit"),
                         section=args.get("section"), profile=profile)
    if name in ("goae_get", "get_goae_fee_code_details"):
        return db.get(str(args["code"]))
    if name == "calculate_goae_fee":
        return db.price(str(args["code"]), args.get("factor"))
    if name in ("goae_check", "check_goae_code_combination"):
        return db.check([str(c) for c in args["codes"]])
    if name in ("goae_rules", "list_goae_rules_for_code"):
        return db.rules(str(args["code"]))
    raise KeyError(name)
