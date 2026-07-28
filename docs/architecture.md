# 架構圖與流程圖

本文件同時描述兩條路徑：0.4 之後的 production 多 MCP 架構，以及仍受 regression tests
保護的 0.3.x legacy `server.py` 相容路徑。除非章節明確標示 legacy，下方的「現行流程」都指
CLI 或 FastAPI adapter 建立 `AgentLoop`、透過 `MCPToolPool` 聚合隔離的 MCP server。

快速總覽：Agent HTTP 服務、模型 API 與 RAG Manager 是三個不同端點；RAG upload server
不是預設能力，必須由部署者透過 MCP JSON config 明確啟用。

```mermaid
flowchart LR
    BROWSER["Browser<br/>HTML test bench"] -->|"GET / · POST /api/v1/runs"| WEB["FastAPI adapter<br/>AgentService"]
    APP["Other application"] -->|"versioned JSON API"| WEB
    CLI["CLI"] --> CORE["AgentLoop<br/>one isolated run"]
    WEB --> CORE

    CORE --> GW{"Gateway"}
    GW --> FAKE["RuleBasedGateway<br/>offline test"]
    GW --> HTTPGW["HTTPGateway"]
    HTTPGW --> MODEL["OpenAI-compatible<br/>model API"]

    CORE -->|"ToolRunner"| POOL["MCPToolPool<br/>shared by FastAPI lifespan"]
    POOL -->|"stdio · default"| UTIL["utilities MCP"]
    POOL -->|"stdio · default"| TRANS["translation MCP"]
    POOL -.->|"stdio · explicit config"| RAG["RAG upload MCP"]
    RAG --> RAGAPI["Existing RAG Manager API"]
    POLICY["optional capability policy"] -.-> CORE
```

核心依賴方向：`agent/loop.py` 只依賴 `Gateway`、`ToolRunner`、`RunPolicy` 三個通用介面；
翻譯 prompt/policy 位於 `capabilities/translation/`；各 server 透過 `mcp_servers/common.py`
共用 MCP wire glue。RAG server 是手寫 HTTP adapter，不包含任何外部 RAG repository 的程式。

| # | 圖 | 回答什麼問題 |
|---|---|---|
| 1 | [高階流程](#1-高階流程圖) | 一次 CLI／API 請求如何變成結果？ |
| 2 | [系統架構](#2-系統架構圖) | 誰擁有生命週期、Gateway 與 MCP 邊界？ |
| 3 | [服務與端到端序列](#3-服務生命週期與端到端流程圖) | FastAPI 如何啟動、執行請求與處理外部 HTTP？ |
| 4 | [Agent Loop](#4-agent-loop-流程圖) | 回合怎麼算？工具失敗怎麼辦？ |
| 5 | [翻譯 policy](#5-自檢重譯決策流程) | 什麼時候驗證／重譯？為什麼不會無限迴圈？ |
| 6 | [最長優先掃描](#6-術語掃描最長優先) | 為什麼「臨時額度」會壓過「額度」？ |
| 7 | [命中判定](#7-命中判定hit--wrong--miss) | HIT / WRONG / MISS 怎麼分？ |
| 8 | [對照表重載](#8-對照表-mtime-重載) | 改了 CSV 為什麼不用重啟？ |
| 9 | [能力註冊與聚合](#9-能力註冊與多-mcp-聚合) | 新能力如何加入 production pool？legacy 自動註冊保留在哪？ |
| 10 | [契約流向](#10-契約流向) | 哪個型別跨過哪條邊界？ |

---

## 1. 高階流程圖

30 秒版本。CLI、網頁與其他應用共用同一個 capability-neutral loop；差別只在 adapter
如何驗證輸入、管理生命週期與輸出 envelope。

```mermaid
flowchart TD
    REQ(["① 一次獨立請求"]) --> ENTRY{"入口"}
    ENTRY -->|"CLI"| CLI["載入 env／MCP config<br/>命令生命週期持有 tool pool"]
    ENTRY -->|"HTML 或其他應用"| HTTP["FastAPI middleware + Pydantic<br/>Host／body／content-type 邊界"]
    HTTP --> SERVICE["② AgentService<br/>等待單一 run slot + 總逾時"]
    CLI --> BUILD["③ 選 profile 與 Gateway<br/>建立 AgentLoop"]
    SERVICE --> BUILD

    BUILD --> MODEL["④ model complete<br/>fake 或 OpenAI-compatible HTTP"]
    MODEL --> CALLS{"回傳 tool_calls？"}
    CALLS -->|"是"| TOOL["⑤ MCPToolPool 路由 public name<br/>stdio tools/call"]
    TOOL --> RESULT["工具結果或可恢復錯誤<br/>加入 messages"]
    RESULT --> MODEL
    CALLS -->|"否"| POLICIES["⑥ 依序執行 capability policies<br/>例如 bounded translation self-check"]
    POLICIES --> OUT(["⑦ RunResult<br/>answer + metrics + tool trace + artifacts"])
    OUT --> RENDER{"adapter"}
    RENDER -->|"CLI"| TEXT["文字／JSON 輸出"]
    RENDER -->|"FastAPI"| ENVELOPE["AgentRunResponse<br/>或安全錯誤 envelope"]
```

模型可以不呼叫任何工具；這是正常、可量測的結果。翻譯 profile 只有在模型成功呼叫
`lookup_terms` 後才可能執行術語 policy，generic profile 不會把翻譯規則塞進 core。

---

## 2. 系統架構圖

### 2.1 分層依賴圖

實線代表 runtime 呼叫或持有關係；虛線代表設定、資料或可選 policy。FastAPI 與 CLI 是
兩個 adapter，但最後都只把 `Gateway` 與 `ToolRunner` 交給同一個 `AgentLoop`。

```mermaid
flowchart LR
    subgraph CLIENTS["呼叫端"]
        BROWSER["Browser<br/>HTML test bench"]
        APPC["Other API client"]
        CLIC["CLI"]
    end

    subgraph HOST["Agent host process"]
        API["agent/web.py<br/>FastAPI + middleware + lifespan"]
        SVC["AgentService<br/>single-run lock · queue/run timeout"]
        CLI["agent/cli.py<br/>argument + output adapter"]
        LOOP["agent/loop.py<br/>AgentLoop"]
        POLICY["agent/policy.py<br/>RunPolicy protocol"]
        GW["agent/gateway.py<br/>Gateway protocol + HTTPGateway"]
        POOL["agent/mcp_client.py<br/>MCPToolPool"]
    end

    BROWSER -->|"same-origin HTTP"| API
    APPC -->|"/api/v1/*"| API
    CLIC --> CLI
    API --> SVC
    SVC -->|"one loop per request"| LOOP
    CLI -->|"one loop per command"| LOOP
    LOOP -->|"Gateway"| GW
    LOOP -->|"ToolRunner"| POOL
    LOOP -->|"after_run hook"| POLICY

    GW --> FAKE["RuleBasedGateway<br/>test double"]
    GW -->|"POST GATEWAY_BASE_URL/chat/completions"| MODEL["Model API"]
    API -.->|"lifespan owns one shared pool"| POOL
    CLI -.->|"command owns pool"| POOL

    POOL -->|"stdio"| UTIL["mcp_servers.utilities<br/>default"]
    POOL -->|"stdio"| TRANS["mcp_servers.translation<br/>default"]
    POOL -.->|"stdio · explicit config"| RAG["mcp_servers.rag_upload"]
    POOL -.->|"stdio · config"| CUSTOM["other MCP servers"]

    TRANS --> CSV[("glossary.csv")]
    STAGING[("allowlisted staging roots")] --> RAG
    RAG -->|"POST RAG_UPLOAD_BASE_URL/datacenter/v1/file"| RAGAPI["RAG Manager API"]

    ENV["server env<br/>model URL/key/model/HTTP opt-in"] -.-> GW
    MCPCFG["MCP JSON config<br/>command/env/inherit_env/prefix/required"] -.-> POOL
    CT["contracts/<br/>Pydantic boundary types"] -.-> API
    CT -.-> LOOP
    CT -.-> TRANS
```

### 2.2 模組職責

| 層 | 模組 | 負責 | 明確不負責 |
|---|---|---|---|
| **entry adapters** | `agent/cli.py` | 載入 env、選 profile/Gateway、管理 command-lifetime pool、渲染輸出 | HTTP 契約、工具實作 |
| | `agent/web.py` | FastAPI lifespan、middleware、版本化 API、HTML test bench、排隊與總逾時 | Agent／工具業務邏輯 |
| **agent core** | `agent/loop.py` | model → tool → model、多輪預算、通用 policy hooks、`RunResult` | capability-specific 判定 |
| | `agent/gateway.py` | OpenAI-compatible HTTP、URL/timeout/明文 opt-in 驗證、response drift normalization | 工具語意、API caller 認證 |
| | `agent/tooling.py` | `ToolRunner` protocol 與一致的 invocation error | MCP process 細節 |
| | `agent/policy.py` | `RunPolicy`／`PolicyContext`／`PolicyOutcome` | 翻譯或 RAG 規則 |
| | `agent/bridge.py` | MCP schema ↔ OpenAI tools/messages | domain 邏輯 |
| | `agent/metrics.py` | 評測報表彙總 | 線上 verdict 實作 |
| **MCP host** | `agent/mcp_config.py` | 驗證 server command、env allowlist、prefix、required | capability policy |
| | `agent/mcp_client.py` | spawn、initialize/list/call timeout、聚合 public names、collision check、最小 child env | 模型路由決策 |
| **capabilities/** | `translation/prompts.py` | 翻譯規則、glossary block 與 repair prompt 純文字 renderer | Gateway、HTTP、MCP session |
| | `translation/policy.py` | 成功 lookup 後的 bounded verify/repair | generic loop 控制流 |
| **MCP servers** | `mcp_servers/common.py` | 明確 registry → MCP list/call wire glue | 工具 domain 邏輯 |
| | `mcp_servers/utilities.py` | 預設 utility capability registry | translation／RAG |
| | `mcp_servers/translation.py` | 預設 translation capability registry | upload／模型呼叫 |
| | `mcp_servers/rag_upload/` | 檔案邊界、immutable snapshot、RAG multipart adapter | indexing 完成保證 |
| **legacy** | `server.py` | 0.3.x 五工具聚合相容與 regression tests | production 預設啟動路徑 |
| **tools/** | `registry.py` | legacy root server 掃描 `tools/` | split-server production registry |
| | `base.py` | `ToolSpec` 契約與 argument 驗證 | MCP transport |
| | 各工具模組 | 單一 handler，自帶 name/description/schema | Gateway、Agent loop |
| **glossary/** | `loader.py` | 讀 CSV、展開別譯、預編譯、mtime 重載 | 掃描、判定 |
| | `scanner.py` | 從中文問句找術語（最長優先） | 格式化、翻譯 |
| | `matcher.py` | 判定譯文是否命中（唯一實作） | 決定要不要重譯 |
| | `normalize.py` | 正規化、英文詞形展開、樣式編譯 | 決定要找什麼 |
| **contracts/** | `contracts/api.py`、`agent.py`、`tools.py` | HTTP、run、tool result 的跨層型別 | process lifecycle |

### 權責一句話

> **glossary 不知道 MCP 存在；tools 不知道 gateway 存在；agent 不知道對照表怎麼比對。**

再加兩條 deployment 邊界：Gateway credential 只留在 Agent host；MCP child 預設只收到 runtime
allowlist 與該 server 明列的 `inherit_env`。`GATEWAY_API_KEY` 和
`GATEWAY_ALLOW_INSECURE_HTTP` 都不會自動流入任何 MCP server。

### 純文字 renderer 的刻意共享

`tools/translate_lookup.py` 與 translation profile adapter 共用
`capabilities/translation/prompts.py`。這條依賴之所以被允許，只因為該模組是純文字 renderer：
只 import `contracts`，沒有 gateway、HTTP、MCP session 或 glossary runtime。

```mermaid
flowchart LR
    TL["tools/translate_lookup.py"] -->|"format_glossary_block"| P["capabilities/translation/prompts.py"]
    ADAPTER["agent/cli.py + agent/web.py"] -->|"TRANSLATION_RULES"| P
    P -->|"only import"| C["contracts/"]
    P -.->|"forbidden"| X["gateway / loop / httpx / glossary runtime"]

    AST["tests/test_prompts.py<br/>AST import purity check"] -.->|"enforces"| P

    style X stroke-dasharray: 5 5
```

`agent/prompts.py` 仍是 generic system prompt 的純文字模組；兩個 prompt modules 都由 AST test
限制 import。工具名稱仍只經 API 的 `tools` 欄位進模型，不寫死在 generic system prompt。

---

## 3. 服務生命週期與端到端流程圖

FastAPI 在 lifespan 只建立一個共享 pool；每個 request 則建立自己的 Gateway、messages 與
`AgentLoop`。CLI 不經 HTTP adapter，但同樣使用 pool → stdio MCP 的路徑。

```mermaid
sequenceDiagram
    autonumber
    actor C as Browser / API caller
    participant API as FastAPI adapter
    participant SVC as AgentService
    participant POOL as shared MCPToolPool
    participant MCP as selected MCP server
    participant AL as isolated AgentLoop
    participant TP as TranslationSelfCheck
    participant GW as selected Gateway
    participant MODEL as Model API
    participant GLOSS as glossary runtime
    participant RAGM as RAG Manager API

    Note over API,MCP: Application startup — lifespan owns the pool
    API->>API: load default or explicit MCP config
    API->>POOL: enter pool(config)
    Note over POOL,MCP: Default: utilities + translation; RAG requires explicit config
    POOL->>MCP: spawn stdio + initialize + tools/list
    MCP-->>POOL: tool schemas
    POOL-->>API: aggregated public catalog

    C->>API: POST /api/v1/runs
    API->>API: middleware + Pydantic boundary checks
    API->>SVC: AgentRunRequest
    SVC->>SVC: queue timeout → acquire single-run lock
    SVC->>GW: create fake or HTTP gateway from server config
    SVC->>AL: run one loop with shared pool
    Note over SVC,AL: The whole loop is bounded by the configured run timeout

    loop Model turns within max_model_turns
        AL->>GW: complete(messages, aggregated tools)
        opt HTTPGateway
            GW->>MODEL: POST GATEWAY_BASE_URL/chat/completions
            MODEL-->>GW: assistant content / tool_calls
        end
        GW-->>AL: normalized AssistantTurn

        alt Assistant returned tool_calls
            loop Every call in this assistant turn
                AL->>POOL: call(public_name, arguments)
                POOL->>MCP: stdio tools/call(remote_name)
                alt translation capability
                    MCP->>GLOSS: lookup / verify
                    GLOSS-->>MCP: structured result
                else explicitly enabled RAG upload
                    MCP->>RAGM: POST RAG_UPLOAD_BASE_URL/datacenter/v1/file
                    RAGM-->>MCP: upstream response or transport failure
                    MCP->>MCP: validate receipt or normalize a safe tool error
                else utility / custom server
                    MCP->>MCP: run registered ToolSpec
                end
                MCP-->>POOL: MCP result
                POOL-->>AL: content or recoverable invocation error
            end
        else Terminal assistant turn
            AL->>AL: map finish reason to stop_reason
        end
    end

    opt translation profile and eligible successful lookup
        AL->>TP: after_run(PolicyContext)
        TP->>POOL: policy call verify_translation
        POOL-->>TP: VerifyResult or invocation error
        opt bounded repair is needed
            TP->>GW: complete(repair conversation, tools=None)
            GW-->>TP: candidate translation
            TP->>POOL: verify candidate
            POOL-->>TP: candidate VerifyResult
        end
        Note over TP,GW: Repairs share the remaining model-turn budget
        TP-->>AL: bounded PolicyOutcome
    end

    AL-->>SVC: RunResult
    SVC->>GW: close per-request gateway
    SVC->>SVC: release lock
    SVC-->>API: AgentRunResponse
    API-->>C: JSON envelope

    Note over API,POOL: Application shutdown — close all child sessions once
```

圖中有三條互不相同的 HTTP：呼叫端進 `/api/v1/runs`、Agent host 往
`GATEWAY_BASE_URL/chat/completions`、以及可選 RAG MCP 往
`RAG_UPLOAD_BASE_URL/datacenter/v1/file`。MCP host 與 child server 之間目前是 stdio，並非 HTTP。

---

## 4. Agent Loop 流程圖

回合怎麼算、工具壞掉怎麼辦、模型不呼叫工具怎麼辦。

```mermaid
flowchart TD
    START(["run(user_text)"]) --> INIT["messages = [system, user]<br/>model_turns = 0"]
    INIT --> CHECK{"model_turns < max_model_turns？<br/>預設 6"}

    CHECK -->|否| EXHAUST["stop_reason = MAX_TURNS<br/>保留 last_content"]
    CHECK -->|是| CALL["model_turns += 1<br/>gateway.complete(messages, tools)"]
    CALL --> KEEP["若有 content<br/>更新 last_content"]
    KEEP --> WANT{"AssistantTurn 有 tool_calls？"}

    WANT -->|沒有| TERMINAL["以 refusal / finish_reason 決定<br/>COMPLETED · REFUSED<br/>LENGTH_LIMIT · CONTENT_FILTER"]
    WANT -->|有| APPEND["先加入完整 assistant message"]
    APPEND --> NEXT["取同一 assistant turn<br/>下一個 tool call"]
    NEXT --> PARSE{"參數解析成功？"}
    PARSE -->|否| ERRMSG["不呼叫 handler<br/>建立可恢復錯誤內容"]
    PARSE -->|是| RUN["MCPToolPool.call<br/>路由到 child server"]

    RUN --> OK{"工具成功？"}
    OK -->|否| ERRMSG
    OK -->|是| RESULT["取得工具內容"]
    ERRMSG --> BAD["記錄 ToolCallRecord<br/>ok = False"]
    RESULT --> GOOD["記錄 ToolCallRecord<br/>ok = True"]
    BAD --> TOOLMSG["加入對應 tool result message"]
    GOOD --> TOOLMSG
    TOOLMSG --> MORE{"同一 assistant turn<br/>還有 tool call？"}
    MORE -->|有| NEXT
    MORE -->|沒有，全部執行完| CHECK

    TERMINAL --> HOOKS["依序呼叫所有 RunPolicy.after_run"]
    EXHAUST --> HOOKS
    HOOKS --> ELIGIBLE{"該 capability policy<br/>自己的條件成立？"}
    ELIGIBLE -->|否| OUT(["RunResult"])
    ELIGIBLE -->|是| POLICY["執行 bounded policy<br/>結果、records、artifacts 合併"]
    POLICY --> OUT
```

### 三個刻意的設計

| 情況 | 行為 | 為什麼 |
|---|---|---|
| 一般回答且沒有 tool calls | **正常完成**，`called_any_tool = False` | 不假設每個問題都需要工具；是否呼叫仍必須量測 |
| 無 tool calls，但 finish reason 不是一般完成 | 記為 `REFUSED`／`LENGTH_LIMIT`／`CONTENT_FILTER` | 「沒有工具」不代表模型正常回答，terminal reason 必須保留 |
| 同一回合要求多個工具 | **全部依序執行**後才再次呼叫模型 | assistant message 與每個 tool result 必須成套，不能漏掉後面的 call |
| 工具丟出錯誤 | 錯誤訊息回給模型，讓它自己補救 | 一個壞掉的工具不該讓整個 run 死掉 |
| 參數是壞掉的 JSON | **不呼叫工具**，直接回錯誤 | gateway 格式差異不該變成 handler 的例外 |

總 model-turn 預算是所有模型呼叫的硬邊界；每個 capability 還可以有更小的自身上限。
耗盡是一個**被記錄的正常結果**，最後一段文字仍會回傳。所有 policy 都會收到 hook，但可以依
`completed`、工具紀錄與輸出內容自行拒絕執行。

---

## 5. 自檢重譯決策流程

`verify_translation` 只回報；決策者是
`capabilities/translation/policy.py::TranslationSelfCheck`。`AgentLoop` 只提供通用 hook、剩餘
預算與 invocation function，讓工具保持無狀態、core 保持 capability-neutral。

```mermaid
flowchart TD
    START(["TranslationSelfCheck.after_run"]) --> COMPLETE{"core stop_reason<br/>是 COMPLETED？"}
    COMPLETE -->|否| SKIP(["跳過 policy"])
    COMPLETE -->|是| TEXT{"output 非空？"}
    TEXT -->|否| SKIP
    TEXT -->|是| PAIR{"能解析可用的 lookup / verify pair？"}
    PAIR -->|否| SKIP
    PAIR -->|exact pair| LOOKUP
    PAIR -->|唯一且同 namespace 的 prefix pair| LOOKUP{"lookup 曾成功，且<br/>initiator = MODEL？"}
    LOOKUP -->|否| SKIP
    LOOKUP -->|是| V1["policy 呼叫 verify_translation<br/>initiator = POLICY"]

    V1 --> VALID{"呼叫成功且<br/>VerifyResult schema 有效？"}
    VALID -->|否| VERIFYFAIL(["停止；保留目前 output<br/>記錄失敗 record"])
    VALID -->|是| RATE{"hit_rate == 1.0？"}
    RATE -->|是| GOOD(["完成<br/>附上 VerifyResult"])
    RATE -->|否| CAP{"同時還有兩種預算？<br/>retranslations < max_retranslate<br/>model_turns < max_model_turns"}

    CAP -->|否| STOP(["停止<br/>如實回報未達 100%"])
    CAP -->|是| ASK["retranslate_prompt<br/>逐條列出漏掉的術語 + 正確英文<br/>附上前一次譯文"]

    ASK --> GEN["gateway.complete(conversation, tools=None)<br/>extra model_turns += 1"]
    GEN --> EMPTY{"回傳空字串？"}
    EMPTY -->|是| STOP
    EMPTY -->|否| V2["retranslations += 1<br/>policy 再次 verify candidate"]

    V2 --> VALID2{"呼叫成功且 schema 有效？"}
    VALID2 -->|否| VERIFYFAIL
    VALID2 -->|是| BETTER{"candidate hit_rate<br/>>= 目前 hit_rate？"}
    BETTER -->|是| ADOPT["採用新譯文"]
    BETTER -->|否| DISCARD["丟棄，保留舊譯文"]
    ADOPT --> RATE
    DISCARD --> RATE
```

### 為什麼觸發條件看成功紀錄，而不是問句裡有沒有「翻譯」

看**模型實際成功做了什麼**，不看使用者措辭。失敗的 lookup、policy 自己發起的 call、跨
namespace 的工具拼接，以及非正常完成的 core run 都不構成驗證依據。不做關鍵字嗅探，agent
才保持通用；未來的能力要自檢就寫自己的 policy，不需要在 core 加一條 `if`。

### 為什麼終止的保證來自上限而不是模型的進步

一個一直回傳同樣譯文的模型，`hit_rate` 永遠不變。如果只用「沒有進步就停」當條件，
`>=` 會一直成立而永遠不停。**總 model-turn 預算與 `max_retranslate`，不是進步，保證了終止**。
較差 candidate 不會覆蓋目前譯文，但只要兩種預算都還有剩餘，policy 仍可做下一次 bounded repair。

---

## 6. 術語掃描：最長優先

```mermaid
flowchart TD
    IN["問句：客戶申請提高臨時額度"] --> PAT["scan_pattern<br/>所有術語的 alternation<br/>依長度由長到短排序"]
    PAT --> IT["re.finditer 由左至右單次掃描"]

    IT --> POS["位置 6：<br/>先試最長的候選"]
    POS --> M1{"臨時額度 匹配？"}
    M1 -->|是| ACCEPT["接受 [6, 10)"]
    ACCEPT --> SKIP["finditer 從 10 繼續<br/>額度 [8,10) 永遠不會被檢查"]
    SKIP --> OUT["TermMatch: 臨時額度<br/>額度：不出現"]
```

**那個排序本身就是演算法。** `Glossary.scan_pattern` 是一個依 surface 長度由長到短排序的
alternation；因為 `finditer` 匹配後會跳過整段，巢狀在裡面的短詞根本不會被重新檢查。

| 輸入 | 接受 | 被壓過 |
|---|---|---|
| `客戶申請提高臨時額度` | `臨時額度` | `額度` |
| `永久額度和臨時額度` | `永久額度`, `臨時額度` | `額度` ×2 |
| `警示帳戶通報機制` | `警示帳戶通報機制` | `警示帳戶`, `帳戶` |
| `外幣帳戶的牌告利率` | `外幣帳戶`, `牌告利率` | `帳戶`, `利率` |
| `額度與臨時額度` | `額度`, `臨時額度` | 無 —— 壓制是**依區間**不是依詞 |

**為什麼要這樣做**：對照表裡 `額度 / 臨時額度 / 永久額度` 這種家族，短詞的英文對長詞來說是
**錯的**，不只是比較不精確。同時注入兩者，等於對同一段文字給模型兩個互相矛盾的指令 ——
實測比兩個都不給還糟。

---

## 7. 命中判定：HIT / WRONG / MISS

**這是全系統唯一的判定實作**（`glossary/matcher.py`）。離線評測與線上 `verify_translation`
共用同一個函式；複製一份出去會產生一種在使用者面前才會現形的 bug。

```mermaid
flowchart TD
    IN["術語 T + 譯文"] --> NORM["雙邊 normalize_en<br/>大小寫、連字號、空白一律折平"]
    NORM --> FORMS["展開 T 的可接受詞形<br/>本體 / 去括號 / 縮寫 / 複數"]
    FORMS --> FIND["找出 T 的所有出現位置"]

    FIND --> NB["同時找出<br/>重疊術語的出現位置"]
    NB --> SWALLOW{"T 的每個 span 都被<br/>更長的重疊術語包住？"}

    SWALLOW -->|否，有 span 存活| HIT(["HIT"])
    SWALLOW -->|是，全被吞掉| W1{"有重疊術語出現？"}
    FIND -->|完全找不到| W1

    W1 -->|有| WRONG(["WRONG<br/>found = 最長的那個"])
    W1 -->|沒有| MISS(["MISS"])
```

### swallowed span：這條規則抓到的實際 bug

`額度` 展開成 `credit limit`，而它也是 `temporary credit limit` 的子字串。若不處理，
把 `額度` 譯成 "temporary credit limit" 會判成 **HIT** —— 系統對「用錯術語」完全盲目，
而那正是這一層存在的理由。這個問題是 golden fixture 抓出來的。

| 原文術語 | 譯文 | 判定 |
|---|---|---|
| `臨時額度` | "…the temporary credit limit." | HIT |
| `臨時額度` | "…the credit limit." | WRONG（`credit limit`）|
| `額度` | "Insufficient temporary credit limit." | WRONG（`temporary credit limit`）|
| `額度` | "There are limitations…" | MISS |

規則是對稱的：兩個方向都判對，而且不需要知道兩者之中哪一個才是「真正的那個」。

---

## 8. 對照表 mtime 重載

公開的 `load_glossary()`／`GlossaryLoader` 預設維持 strict：任何重複 canonical `zh` 都報錯。
process-wide production runtime 明確採 `conflict_policy="quarantine"`，讓資料問題只影響有歧義的
surface，而不是讓無關的 `lookup_terms` 全部失效。

```mermaid
flowchart TD
    CALL(["任何一次 get()"]) --> STAT["stat CSV<br/>取 (mtime_ns, size)"]
    STAT --> FAIL{"stat 失敗？"}
    FAIL -->|是，且有快取| SERVE["回傳快取<br/>記一次 warning"]
    FAIL -->|是，且無快取| RAISE(["GlossaryError"])
    FAIL -->|否| SAME{"和已載入的<br/>stamp 相同？"}

    SAME -->|相同| CACHED(["回傳快取<br/>不做任何事"])
    SAME -->|不同| LOAD["重新讀取、驗證 row、分組"]

    LOAD --> STRUCT{"欄位與每列結構有效？"}
    STRUCT -->|否，且有快取| STALE(["保留舊的可用版本<br/>每個 stamp 只記一次 log"])
    STRUCT -->|否，且無快取| RAISE
    STRUCT -->|是| DUP{"production quarantine 下<br/>有重複／衝突 surface？"}
    DUP -->|沒有| COMPILE["建立 indexes + longest-first pattern"]
    DUP -->|相同 zh、相同 normalized en| MERGE["合併一筆 + aliases 去重<br/>記 warning"]
    DUP -->|相同 zh、不同 en| QUAR["不選 first / last<br/>從 authoritative indexes 排除<br/>保留 conflict surface"]
    DUP -->|alias 多重擁有| QUAR
    MERGE --> COMPILE
    QUAR --> COMPILE
    COMPILE --> NEW(["原子換上新的 Glossary<br/>reload_count += 1"])

    REQUEST["之後的 scan(text)"] --> PICK{"longest-first 選到<br/>quarantined surface？"}
    PICK -->|否| MATCHED(["照常回傳 TermMatch"])
    PICK -->|是| CONFLICT(["GlossaryConflictError<br/>tool 回傳可預期錯誤"])
```

| 決策 | 理由 |
|---|---|
| 每次存取都 stat，而不是啟動時載入一次 | 對照表更新時間不定。長時間運行的 server 會**靜默地**供應舊譯法 —— 靜默才是真正的危險 |
| 不做推送 / webhook 失效機制 | 目前 71 詞、正式 379 詞，重載是次毫秒等級。任何失效協定的維運成本都高於它省下的東西 |
| stamp 同時看 size，不只看 mtime | 某些檔案系統 mtime 只到秒；同一秒內的編輯會被藏起來 |
| 結構解析失敗保留舊版本 | 缺欄、空必填值等壞編輯降級成「過時」，不是「掛掉」 |
| production 隔離衝突詞 | 無關詞仍可查；真正碰到歧義時明確失敗，絕不依 row order 猜譯法 |
| strict API 仍預設拒絕 duplicate | 資料驗證／CI 可以維持 fail-fast；只有 runtime 明確選擇 availability policy |
| `RLock` 保護 | 並行讀取只會看到舊的或新的 `Glossary`，不會看到蓋到一半的 |

CSV 的 `aliases` 欄仍是 `|` 分隔的**中文來源別名**，不是可接受的英文同義詞；額外欄位不會
偷偷改變 matcher 語意。若同一中文詞確實需要依 category 使用不同英文，現行
`lookup_terms(text)` 沒有足夠的 disambiguation 維度，資料必須先拆成可判別的 source surface，
或另行擴充帶 context/category 的契約。

---

## 9. 能力註冊與多 MCP 聚合

Production 與 legacy 有兩種刻意不同的擴充路徑。新 domain capability 採明確 registry 與設定；
「加一個檔案就自動出現」只屬於 0.3.x root-server 相容路徑。

```mermaid
flowchart LR
    subgraph PROD["Production split-server path"]
        CONFIG["default_mcp_server_configs()<br/>或 MCP JSON config"] --> CLIENTS["MCPToolPool<br/>建立 configured clients"]
        IMPL["新增 capability module<br/>一或多個 ToolSpec"] --> REG["該 MCP server 的<br/>explicit registry"]
        REG --> CHILD["capability MCP child<br/>mcp_servers/common.py wire glue"]
        CLIENTS -->|"依 config spawn + initialize"| CHILD
        CHILD -->|"tools/list schemas"| CLIENTS
        CLIENTS --> NAMES{"套用 prefix 後<br/>public name collision？"}
        NAMES -->|是| FAIL(["startup fail fast"])
        NAMES -->|否| CATALOG["聚合 schema + route table"]
        CATALOG --> AGENT["AgentLoop 的 tools 欄位"]
    end

    subgraph LEGACY["0.3.x legacy root-server path"]
        FILE["新增 tools/my_tool.py<br/>匯出一個 SPEC"] --> DISC["tools/registry.py<br/>pkgutil discover"]
        DISC --> ROOT["server.py<br/>五工具聚合相容 server"]
        ROOT --> OLDLIST["legacy tools/list"]
    end
```

預設 production config 只列 `mcp_servers.utilities`（`say_hello`、`get_time`、`get_weather`）與
`mcp_servers.translation`（`lookup_terms`、`verify_translation`）；
`mcp_servers.rag_upload` 的 `upload_document` 因為有外部寫入副作用，必須由部署者在 JSON config
明確加入。每個 child process 只得到最小 runtime env、server 固定 `env` 與逐項
`inherit_env`，pool 再處理 timeout、optional server、prefix 與 collision。

### Legacy 自動註冊仍有 regression test

驗收條件 6（`tests/acceptance/test_phase1.py::TestCriterion6Pluggability`）仍會實際新增一個
`tools/probe_tool.py`、啟動 legacy `server.py`、驗證可 list/call，再刪除檔案並確認
`server.py` 與 generic `agent/prompts.py` 的 SHA-256 沒變。這保護舊相容承諾，但不代表新
production capability 應塞回 root server。

### Description 仍承擔 model routing 責任

無論是哪一條註冊路徑，generic system prompt 都不列舉工具；模型看到的是 API `tools` 欄位。
因此每個 `ToolSpec.description` 必須說清楚**何時呼叫、何時不要呼叫與副作用**。工具 catalog
變大後若 routing 準確率下降，先量測 description／capability selection，而不是把 domain 名稱
硬寫進 `AgentLoop`。

---

## 10. 契約流向

`contracts/` 是跨層資料契約的集中處；process lifecycle、stdio transport 與外部 HTTP 則由各
adapter 擁有。圖中的 RAG branch 是獨立 capability，不依賴 glossary contract。

```mermaid
flowchart LR
    subgraph TRANS["Translation capability contracts"]
        CSV[("glossary.csv")] -->|GlossaryEntry| LOADER["glossary/loader.py"]
        LOADER -->|Glossary| SCAN["glossary/scanner.py"]
        SCAN -->|"TermMatch[]"| LOOKUP["lookup_terms ToolSpec"]
        SCAN -->|"TermMatch[]"| MATCHER["glossary/matcher.py"]
        MATCHER -->|"TermVerdict[]"| VERIFY["verify_translation ToolSpec"]
        LOOKUP -->|LookupResult| TSERVER["mcp_servers/translation.py"]
        VERIFY -->|VerifyResult| TSERVER
    end

    POOL["MCPToolPool"] -->|"stdio tools/call"| TSERVER
    TSERVER -->|"stdio schemas + results"| POOL
    UTIL["utilities MCP"] -->|"stdio schemas + results"| POOL
    POOL -->|"stdio tools/call"| UTIL
    POOL -->|"schema + tool content"| BRIDGE["agent/bridge.py"]
    BRIDGE --> LOOP["AgentLoop"]
    LOOP -->|"public tool name + arguments"| POOL

    LOOP -->|RunResult| CLI["agent/cli.py"]
    LOOP -->|RunResult| EVAL["evals/"]
    EVAL -->|Report| REPORT[("evals/reports/*.json")]
    LOOP -->|RunResult| SVC["AgentService"]
    REQ["AgentRunRequest"] --> SVC
    SVC -->|AgentRunResponse| API["FastAPI /api/v1/runs"]
    API --> CALLER["Browser / other application"]

    subgraph RAGBRANCH["Optional, independent RAG capability"]
        RAG["mcp_servers/rag_upload"] -->|"multipart POST"| RAGAPI["RAG Manager API"]
        RAGAPI -->|"upstream receipt"| RAG
    end
    POOL -->|"stdio tools/call<br/>upload_document arguments"| RAG
    RAG -->|"safe stdio MCP result"| POOL
```

| 型別 | 跨過的邊界 | 為什麼長這樣 |
|---|---|---|
| `TermMatch` | glossary → tools | 帶 `start`/`end`，所以呼叫端可以自己決定要不要去重；alias 命中時回報正規 `zh`，但 span 指向實際寫的字 |
| `LookupResult` | tools → agent | `matches` 給程式、`glossary_block` 給模型 —— 兩種消費者不同，所以兩種形狀並存 |
| `VerifyResult` | tools → agent | 只回報，不決策 |
| `ToolCallRecord.initiator` | agent 內部 | 分開 `MODEL` 與 `POLICY`：自檢的 verify 呼叫是真的，但它**不是模型選了工具的證據**，所以不計入工具呼叫率 |
| `RunResult` | agent → CLI / evals / AgentService | 同一個 core 結果可由不同 adapter 渲染或包裝 |
| `AgentRunRequest` / `AgentRunResponse` | FastAPI caller ↔ AgentService | 版本化 HTTP envelope 不滲入 core loop；API key 與 insecure-HTTP opt-in 不在 request contract |
| RAG upload arguments / safe result | AgentLoop／pool ↔ RAG MCP ↔ RAG Manager | 表達接受 upload job，不承諾 parse/index 已完成，也不與 translation contract 混用 |

---

## 延伸閱讀

- `specs/001-glossary-core/spec.md` —— 載入、mtime 重載、最長優先掃描、命中判定
- `specs/002-mcp-tools/spec.md` —— 工具契約、自動註冊、五個工具的 schema
- `specs/003-agent-client/spec.md` —— gateway、格式轉換、agent loop、prompts、CLI
- `README.md` —— 啟動方式、FastAPI 契約、MCP 設定與部署安全邊界
