"""Conservative Markdown presentation cleanup for intake, without an LLM.

Keep clause numbering, link destinations, code contents, table cells and raw
HTML (which may be evidence). Never strip punctuation globally: underscores,
minus signs, section references and amounts can all carry legal meaning.
"""

from markdown_it import MarkdownIt


def plain_document_text(text: str) -> str:
    # Emit definitions in source order, including unused/duplicate references;
    # otherwise CommonMark consumes them into metadata and silently loses facts.
    parser = MarkdownIt("commonmark", {"inline_definitions": True}).enable("table")
    tokens = parser.parse(text)
    block_markup = {"heading_open", "bullet_list_open", "ordered_list_open",
                    "blockquote_open", "fence", "hr", "table_open"}
    inline_markup = {"em_open", "strong_open", "code_inline", "link_open", "image"}
    if not any(t.type in block_markup or any(c.type in inline_markup for c in t.children or [])
               for t in tokens):
        return text

    def inline(children):
        parts = []
        links = []
        for token in children:
            kind = token.type
            if kind == "link_open":
                links.append((token.attrGet("href") or "", token.attrGet("title")))
            elif kind == "link_close":
                destination, title = links.pop()
                if destination:
                    parts.append(f" ({destination})")
                if title:
                    parts.append(f" ({title})")
            elif kind == "image":
                parts.append(inline(token.children or []))
                if token.attrGet("src"):
                    parts.append(f" ({token.attrGet('src')})")
                if token.attrGet("title"):
                    parts.append(f" ({token.attrGet('title')})")
            elif kind in {"softbreak", "hardbreak"}:
                parts.append("\n")
            elif kind in {"text", "code_inline", "html_inline"}:
                parts.append(token.content)
        return "".join(parts)

    parts = []
    for token in tokens:
        kind = token.type
        if kind == "inline":
            parts.append(inline(token.children or []))
        elif kind in {"fence", "code_block", "html_block"}:
            parts.append(token.content)
            parts.append("\n\n")
        elif kind == "list_item_open":
            if parts and not parts[-1].endswith("\n"):
                parts.append("\n")
            if token.info:
                parts.append(f"{token.info}{token.markup} ")
        elif kind == "definition":
            meta = token.meta
            parts.append(f"{meta['label']}: {meta['url']}")
            if meta.get("title"):
                parts.append(f" ({meta['title']})")
            parts.append("\n\n")
        elif kind == "list_item_close":
            if parts and not parts[-1].endswith("\n"):
                parts.append("\n")
        elif kind in {"paragraph_close", "heading_close", "hr"} and not token.hidden:
            parts.append("\n\n")
        elif kind in {"th_open", "td_open"}:
            parts.append("| ")
        elif kind in {"th_close", "td_close"}:
            parts.append(" ")
        elif kind == "tr_close":
            parts.append("|\n")
    return "".join(parts)
