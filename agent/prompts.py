"""System prompt, user templates, and the one glossary-block renderer.

**This module is pure text.** It imports `contracts` and nothing else — no
gateway, no HTTP, no loop, no glossary internals. That is what makes it safe for
`tools/translate_lookup.py` to import (spec 002 §5.1) without the tool layer
acquiring a dependency on the client layer.

See `specs/003-agent-client/spec.md` §3.
"""

from contracts.glossary import TermMatch
from contracts.tools import VerifyResult

# No tool names, no tool count, no examples naming a tool. The tool list travels
# in the API's `tools` field, which is where the model actually reads it from;
# repeating it here would create a second copy that goes stale the moment a tool
# is added, and would break the pluggability guarantee (acceptance criterion 6).
#
# Rule 2's bluntness is bought with data: without glossary assistance the two
# measured models scored 42.7% and 49.7%; with it, 98.1% and 99.0%.
SYSTEM_PROMPT = """\
你是一個具備工具能力的助理。

工作原則：
1. 當有工具能提供你需要的資訊時，一律呼叫工具，不要憑記憶回答。
2. 只要使用者要求把中文翻譯成英文，先查詢術語，再翻譯。術語查詢的結果具有最高優先權，\
即使你認為有更自然的說法，也必須採用查到的譯法。
3. 逐字使用查到的英文術語，不要改寫、不要調換詞序、不要「順一下」。
4. 翻譯任務只輸出英文譯文本身，不要加說明、註解、拼音或引號。
5. 工具回報錯誤時，如實告訴使用者哪裡失敗，不要編造結果。
"""

TRANSLATE_INSTRUCTION = "請將以下中文翻譯成英文，只輸出英文譯文：\n{text}"


def format_glossary_block(matches: list[TermMatch]) -> str:
    """Render matched terms as a block the model can use verbatim.

    De-duplicated by canonical term, first-occurrence order. The single renderer:
    the `lookup_terms` result and any prompt-side rendering come from here, so
    the two cannot drift, and changing this format is not a tool-schema change.
    """
    lines: list[str] = []
    seen: set[str] = set()
    for match in matches:
        if match.zh in seen:
            continue
        seen.add(match.zh)
        lines.append(f"- {match.zh} → {match.en}")
    return "\n".join(lines)


def glossary_prompt(text: str, block: str) -> str:
    """Translation request with the glossary attached. Used by the CLI/eval."""
    if not block:
        return TRANSLATE_INSTRUCTION.format(text=text)
    return (
        "以下術語對照表為權威譯法，必須逐字採用：\n"
        f"{block}\n\n" + TRANSLATE_INSTRUCTION.format(text=text)
    )


def retranslate_prompt(source: str, translation: str, verify: VerifyResult) -> str:
    """Ask for a repair, naming each term that was not used correctly.

    The previous attempt is included so the model repairs rather than restarts —
    a fresh translation tends to lose the terms it had already got right.
    """
    corrections = "\n".join(
        f"- {v.zh} 必須譯為 {v.expected_en}"
        + (f"（你寫的是「{v.found}」）" if v.found else "（譯文中找不到這個術語）")
        for v in verify.results
        if v.zh in set(verify.missed)
    )
    return (
        "你的譯文沒有正確使用下列術語：\n"
        f"{corrections}\n\n"
        f"原文：\n{source}\n\n"
        f"你的譯文：\n{translation}\n\n"
        "請修正上述術語後重新輸出完整英文譯文，只輸出譯文本身。"
    )
