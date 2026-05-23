from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class AssistantExtraction:
    options: dict[str, str | int] = field(default_factory=dict)
    transaction_id: str | None = None
    intent: str | None = None
    notes: list[str] = field(default_factory=list)


class PrintAssistant:
    """Small local assistant for print-shop conversations.

    This gives the product useful AI-like extraction without making printing or
    payment decisions. Payment approval remains an explicit operator action.
    """

    TXN_PATTERNS = (
        re.compile(r"\b(?:txn|txnid|transaction|trx|ref|reference)\s*[:#-]?\s*([A-Z0-9-]{5,})\b", re.I),
        re.compile(r"\b([A-Z]{2,5}[0-9][A-Z0-9-]{5,})\b"),
        re.compile(r"\b([0-9]{8,18})\b"),
    )

    def extract(self, text: str | None) -> AssistantExtraction:
        normalized = (text or "").strip()
        lower = normalized.lower()
        result = AssistantExtraction()
        if not normalized:
            return result

        if any(word in lower for word in ("print", "copies", "copy", "page", "duplex", "color", "colour")):
            result.intent = "print_request"
        if any(word in lower for word in ("paid", "payment", "sent", "transaction", "txn", "trx", "receipt")):
            result.intent = "payment_proof"

        result.options.update(self._extract_options(lower))
        result.transaction_id = self._extract_transaction_id(normalized)
        if result.options:
            result.notes.append("Extracted print options from student message.")
        if result.transaction_id:
            result.notes.append("Possible transaction ID detected.")
        return result

    def _extract_options(self, lower: str) -> dict[str, str | int]:
        options: dict[str, str | int] = {}
        if "a3" in lower:
            options["paper_size"] = "A3"
        elif "a4" in lower:
            options["paper_size"] = "A4"

        if any(word in lower for word in ("glossy", "shining", "shine", "poster", "photo paper")):
            options["paper_finish"] = "glossy"
        elif any(word in lower for word in ("normal", "plain", "regular")):
            options["paper_finish"] = "normal"

        if any(word in lower for word in ("color", "colour", "colored", "coloured")):
            options["color_mode"] = "color"
        elif any(word in lower for word in ("black", "white", "b/w", "bw", "grayscale", "grey")):
            options["color_mode"] = "bw"

        if any(word in lower for word in ("double sided", "double-sided", "duplex", "both sides", "2 sided")):
            options["sides"] = "double"
        elif any(word in lower for word in ("single sided", "single-sided", "one side", "1 sided")):
            options["sides"] = "single"

        copies_match = re.search(r"\b(\d{1,3})\s*(?:copies|copy|sets?)\b", lower)
        if copies_match:
            options["copies"] = max(1, int(copies_match.group(1)))

        page_match = re.search(
            r"\b(?:pages?|pg)\s*[:#-]?\s*([0-9][0-9,\-\s]*?)(?=\s*(?:,?\s*\d+\s*(?:copies|copy|sets?)\b|$|[.;]|and\b|color\b|colour\b|black\b|white\b|bw\b|double\b|single\b|glossy\b|normal\b))",
            lower,
        )
        if page_match:
            options["page_range"] = page_match.group(1).replace(" ", "")
        return options

    def _extract_transaction_id(self, text: str) -> str | None:
        for pattern in self.TXN_PATTERNS:
            match = pattern.search(text)
            if match:
                return match.group(1).strip("-")
        return None


assistant = PrintAssistant()
