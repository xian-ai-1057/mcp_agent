"""Capability-neutral prompts owned by the generic agent runtime."""

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
2. 只使用與目前請求相關、且在本次對話中可用的工具。
3. 工具回報錯誤時，如實告訴使用者哪裡失敗，不要編造結果。
4. 工具造成外部變更時，確認使用者已明確要求該變更。
"""
