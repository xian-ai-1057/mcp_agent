"""Pure prompt helpers owned by the optional translation capability."""

from contracts.glossary import TermMatch
from contracts.tools import VerifyResult

TRANSLATION_RULES = """\
翻譯任務規則：
1. 只要使用者要求把中文翻譯成英文，先查詢術語；查詢結果優先於模型偏好。
2. 逐字使用查到的英文術語，不改寫或調換詞序。
3. 翻譯任務只輸出英文譯文本身，不加說明、註解、拼音或引號。
"""

TRANSLATE_INSTRUCTION = "請將以下中文翻譯成英文，只輸出英文譯文：\n{text}"


def format_glossary_block(matches: list[TermMatch]) -> str:
    """Render de-duplicated terms in first-occurrence order."""
    lines: list[str] = []
    seen: set[str] = set()
    for match in matches:
        if match.zh in seen:
            continue
        seen.add(match.zh)
        lines.append(f"- {match.zh} → {match.en}")
    return "\n".join(lines)


def glossary_prompt(text: str, block: str) -> str:
    if not block:
        return TRANSLATE_INSTRUCTION.format(text=text)
    return (
        "以下術語對照表為權威譯法，必須逐字採用：\n"
        f"{block}\n\n" + TRANSLATE_INSTRUCTION.format(text=text)
    )


def retranslate_prompt(source: str, translation: str, verify: VerifyResult) -> str:
    corrections = "\n".join(
        f"- {verdict.zh} 必須譯為 {verdict.expected_en}"
        + (
            f"（你寫的是「{verdict.found}」）"
            if verdict.found
            else "（譯文中找不到這個術語）"
        )
        for verdict in verify.results
        if verdict.zh in set(verify.missed)
    )
    return (
        "你的譯文沒有正確使用下列術語：\n"
        f"{corrections}\n\n"
        f"原文：\n{source}\n\n"
        f"你的譯文：\n{translation}\n\n"
        "請修正上述術語後重新輸出完整英文譯文，只輸出譯文本身。"
    )
