# MCP Agent

可設定、可擴充的通用 tool-calling Agent。Agent core 只負責 model → tool → model
迴圈；翻譯、一般工具與 RAG 上傳分別由獨立 MCP server 提供。

```text
Browser / other app ──HTTP──> FastAPI / AgentService ──┐
CLI ────────────────────────────────────────────────────┤
                                                       v
                                               generic AgentLoop
                                                ├─ Gateway ──> Model API
                                                └─ ToolRunner
                                                     v
                                               MCPToolPool (stdio)
                                                ├─ utilities MCP   [default]
                                                ├─ translation MCP [default]
                                                └─ RAG upload MCP  [explicit config]
                                                        └─HTTP──> RAG Manager API
```

舊版 `server.py` 仍保留為五工具聚合 server，供 0.3.x 相容與 regression tests 使用；
CLI 與 FastAPI 預設都只啟動分離的 utility 與 translation servers。

## 快速開始

需求：Python 3.11 以上。

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"

# 不需模型憑證，驗證多 MCP server 的完整線路
.venv/bin/python -m agent.cli --gateway fake "現在幾點"

# 翻譯 profile 才會加入術語規則與 bounded self-check
.venv/bin/python -m agent.cli --gateway fake --profile translation \
  "請幫我翻譯：客戶申請提高臨時額度"

# 真實 OpenAI-compatible gateway
cp .env.example .env
.venv/bin/python -m agent.cli "你的問題"
```

模型 gateway 預設必須使用 HTTPS；同機的 `http://127.0.0.1`／`http://localhost` 可直接使用。
若受控的開發或內網 gateway 只有 HTTP，可在 server 端明確設定
`GATEWAY_ALLOW_INSECURE_HTTP=true`。啟用後 API key、prompt、工具 schema 與結果都會以明文傳輸，
因此不應用於不受信任的網路，也不能由 API request 動態開啟。
實際模型請求送往 `GATEWAY_BASE_URL/chat/completions`；它不是下方的 Agent API URL。

## FastAPI 與網頁測試介面

專案內附一個只綁定本機的單次執行工作台，可直接查看回答、工具呼叫、回合數與術語驗證結果：

```bash
.venv/bin/python -m agent.web
# 瀏覽器開啟 http://127.0.0.1:8000
# Swagger UI: http://127.0.0.1:8000/docs

# 也可交給 Uvicorn 載入 ASGI app；此路徑需明確提供 env file
.venv/bin/uvicorn --env-file .env agent.web:app --host 127.0.0.1 --port 8000
```

頁面預設使用不需憑證的 `fake` gateway，適合先驗證 agent → MCP → tool 的完整線路；
若 `.env` 已設定 `GATEWAY_BASE_URL`，也可在頁面切換成真實的 OpenAI-compatible gateway。
每次送出都是獨立測試，不會保留前一次輸入的對話上下文。可用 `--port` 更換連接埠，或以
`--mcp-config` 載入自訂的 MCP server 組合。

### 流程動畫：`http://127.0.0.1:8000/flow`

同一個服務另外提供一個給**非工程讀者**看的頁面。它把一次 run 拆成節點跑一遍：上排是 Agent
的判斷與決策，下排是獨立運作的工具服務，並可點開每個工具內部做了什麼。節點名稱一律使用業務
語言，不出現函式名與行號 —— 需要那些細節時勾「工程視角」。

四個內建情境會走出明顯不同的路徑：術語翻譯（會自我檢查並自動重譯）、查時間（只選一個工具）、
一般問答（**完全不呼叫工具**）、工具故障（工具壞掉但服務不中斷）。切「真實執行」則會真的送
`POST /api/v1/runs`，把後端跑完的 `RunResult` 播成同一張圖。

真實執行常在數十毫秒內結束，所以頁面提供「慢速 0.75×」與「逐步模式」（用 `→` 一步一步走，
空白鍵播放／暫停）。畫面上的回合數、工具、參數與命中率都是真實值；只有步與步之間的節奏是為了
觀看而設定的。

其他應用可呼叫版本化 API；`fake` 不需憑證，`http` 則使用伺服器端 `.env`，API key 不會送到
呼叫端：

```bash
curl -s http://127.0.0.1:8000/api/v1/runs \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "現在台北幾點？",
    "gateway": "fake",
    "profile": "generic",
    "max_turns": 6
  }'
```

主要接口：

| Method | Path | 用途 |
|---|---|---|
| `POST` | `/api/v1/runs` | 執行一次獨立 agent run，回傳 run ID、耗時與完整 `RunResult` |
| `GET` | `/api/v1/capabilities` | 查看 gateway 狀態、profiles 與 MCP 工具名稱 |
| `GET` | `/flow` | 流程動畫：把一次 run 拆成節點播放，給非工程讀者看 |
| `GET` | `/healthz` | readiness probe |
| `GET` | `/docs` | Swagger UI；`/openapi.json` 可用來產生 client SDK |

跨來源瀏覽器前端可重複傳入 `--cors-origin https://app.example.com`，或設定逗號分隔的
`AGENT_CORS_ORIGINS`；server-to-server 呼叫不需要 CORS。這個測試服務沒有登入驗證，因此 CLI
只允許綁定 loopback。若直接以 Uvicorn 綁定 LAN interface，還必須把瀏覽器實際送出的 hostname
或 IP 加入逗號分隔的 `AGENT_TRUSTED_HOSTS`，否則會收到 `400 Invalid host header`；單獨設定
`--host 0.0.0.0` 只負責 listen，並不放寬 Host allowlist。例如：

```bash
AGENT_TRUSTED_HOSTS=127.0.0.1,localhost,10.1.102.113 \
  .venv/bin/uvicorn --env-file .env agent.web:app --host 0.0.0.0 --port 8803
```

這個服務本身沒有登入驗證。跨主機使用仍應限制在受控網路，正式部署則在前方加入具驗證與 TLS
的 API gateway／反向代理，並只 allowlist 實際 public hostname。

通用 profile 是預設值。它仍能看見已連線 MCP server 的工具，但不把任何翻譯規則寫死在
Agent core。`--profile translation` 是一個可選 capability：增加翻譯 prompt 與術語驗證 policy。

## MCP servers

| Server | 工具 | 啟用方式 | 性質 |
|---|---|---|---|
| `mcp_servers.utilities` | `say_hello`, `get_time`, `get_weather` | 預設 | 通用、唯讀 |
| `mcp_servers.translation` | `lookup_terms`, `verify_translation` | 預設 | 翻譯 capability、唯讀 |
| `mcp_servers.rag_upload` | `upload_document` | MCP JSON explicit config | RAG ingestion 寫入操作 |

每個 server 都能獨立啟動：

```bash
.venv/bin/python -m mcp_servers.utilities
.venv/bin/python -m mcp_servers.translation
.venv/bin/python -m mcp_servers.rag_upload
```

自訂組合使用 [`config/mcp_servers.example.json`](config/mcp_servers.example.json)：

```bash
.venv/bin/python -m agent.cli \
  --mcp-config config/mcp_servers.example.json \
  "把指定文件上傳到 knowledge_base"
```

設定格式：

```json
{
  "servers": [
    {
      "name": "my-server",
      "command": "{python}",
      "args": ["-m", "my_mcp_server"],
      "inherit_env": ["ONLY_THIS_SERVER_SETTING"],
      "tool_prefix": "optional_namespace",
      "required": true
    }
  ]
}
```

- `env` 是明確提供給該 server 的固定值；敏感值建議放在 process environment，再以
  `inherit_env` 逐項授權。
- `command: "{python}"` 會使用目前執行 `mcp-agent` 的同一個 Python interpreter，確保
  venv 內已安裝的 MCP 套件可被 child server 載入；外部 server 仍可填實際 executable。
- MCP child process 只收到最小 runtime environment 與明確 allowlist；
  `GATEWAY_API_KEY` 不會再自動流入工具 server。
- 多 server 的公開工具名稱若碰撞，啟動會 fail fast。可用 `tool_prefix` 產生
  `prefix__tool` 名稱。
- `required: false` 的 server 啟動失敗時會被略過，錯誤可由 `connection_errors` 檢查。

目前 host transport 是 stdio。設定模型已為後續 transport adapter 留出邊界，但尚未實作
Streamable HTTP client。

### Glossary CSV 衝突處理

`GLOSSARY_CSV` 可指向部署時掛載的正式對照表。公開的 `load_glossary()`／`GlossaryLoader`
預設採 strict validation；translation MCP 使用的 process-wide runtime 則明確採 quarantine：

- 同一 `zh`、相同 normalized English 的重複列安全合併，aliases 去重並記 warning。
- 同一 `zh` 卻有不同英文時，不依 row order 選 first/last；該 surface 會被隔離。
- 無關詞仍可正常查詢；只有輸入真的匹配隔離詞時，`lookup_terms`／`verify_translation` 才回傳
  含來源行號的可預期 tool error。
- alias 被多個 canonical 詞共用時只隔離該 alias，兩個 canonical 詞仍可使用。

`aliases` 是 `|` 分隔的**中文來源別名**，不是英文同義譯法；CSV 額外欄位目前不會改變
translation matcher。若同一中文詞需要依 category 使用不同英文，現行 text-only lookup 無法
安全消歧，應先修正資料 surface 或擴充帶 context 的契約。

## RAG Upload MCP

`mcp_servers.rag_upload` 是全新手寫的 HTTP adapter，只依賴既有 RAG Manager 的公開上傳
契約；repository **不包含、複製或修改任何既有 RAG 專案程式、設定、資料或憑證**。

它送出：

- `POST /datacenter/v1/file`
- multipart fields：`kb_name`、`file`、可選整數 `expire_at`

成功只代表 ingestion job 已接受，回傳 `accepted: true` 與 `file_id/status`；不代表 parse、
embedding 或 indexing 已完成。這個 server 刻意只做使用者要求的 upload boundary，沒有搬入
parser、worker、MongoDB 或 vector database。

啟用前至少設定：

```bash
export RAG_UPLOAD_BASE_URL=https://rag-manager.example.com
export RAG_UPLOAD_ALLOWED_ROOTS=/srv/app/trusted-upload-staging
export RAG_UPLOAD_ALLOWED_KB_NAMES=manuals,customer_support
```

其他設定見 [`.env.example`](.env.example)。安全預設：

- 只讀 allowlisted root 內的 regular file；拒絕 symlink 與 path escape。
- 拒絕空檔、超過大小上限的檔案，以及 ZIP/7z（可明確覆寫）。
- 非 loopback endpoint 強制 HTTPS；Bearer token 僅傳給 RAG server。
- KB 名稱依上游契約限制為 ASCII letters/numbers/underscore，且不能以數字開頭。
- upstream timeout、連線錯誤、非 2xx 與 malformed receipt 都轉成不洩漏內容的工具錯誤。

寫入操作仍應由上層 UI/API 確認使用者意圖。這個版本以 staging-root sandbox 做執行邊界，
尚未提供跨租戶 ACL 或通用 approval protocol。

## Agent core 與 capability

核心介面保持精簡：

- `Gateway.complete(messages, tools) -> AssistantTurn`
- `ToolRunner.openai_tools / tool_names / call(...)`
- `RunPolicy.after_run(PolicyContext) -> PolicyOutcome`

`AgentLoop` 不 import glossary、翻譯 prompt 或翻譯結果 schema。翻譯自檢位於
`capabilities/translation/`，而 RAG upload 的所有檔案位於 `mcp_servers/rag_upload/`。
未來的 RAG retrieval、格式檢查、權限或其他 domain workflow 應各自新增 policy/capability，
不要把領域規則塞回 loop。

### 從 0.3.x 遷移

`server.py` 保留的是既有五工具的 MCP wire/tool compatibility，不代表 Python API 或預設行為
完全相容。`TranslationSelfCheck` 已移到
`capabilities.translation.policy.TranslationSelfCheck`，翻譯 prompt helpers 位於
`capabilities.translation.prompts`；`AgentLoop` 與 CLI 現在預設為 generic profile，舊版的翻譯
self-check 行為需明確傳入 policy 或使用 `--profile translation`。

工具呼叫的重要防護：

- gateway 的現代 `role=tool` 訊息只送 `role/content/tool_call_id`，不再夾帶會讓 strict
  OpenAI-compatible gateway 回 400 的 legacy `name` 欄位。
- MCP initialize/read/call 都有 timeout；transport failure 會正規化為可回報的 tool error。
- 翻譯 self-check 只在 core 正常完成且 output 非空時執行，只認成功、由 model 發起的 lookup，
  並要求 exact pair 或唯一同 namespace 的 lookup／verify pair。
- repair model calls 使用 `tools=None`，同時受 `max_retranslate` 與當次
  `AgentLoop.max_turns` 剩餘額度限制（CLI／API 可 override）；candidate 的 hit rate 較差時保留原譯文。
- 非文字 MCP result 目前明確報錯，不再靜默轉成空的成功結果。

## 新增工具或 server

舊聚合 server 的簡單工具仍可在 `tools/` 新增一個 `SPEC`；`server.py` 會自動 discover：

```python
from tools.base import ToolSpec, object_schema

SPEC = ToolSpec(
    name="my_tool",
    description="清楚說明何時使用與副作用。",
    input_schema=object_schema(
        {"value": {"type": "string", "description": "..."}},
        required=["value"],
    ),
    handler=lambda arguments: {"value": arguments["value"]},
)
```

新的 domain capability 建議建立獨立 MCP module，使用 `mcp_servers.common.run_server(...)`，
再加入 MCP JSON config；不必修改 Agent loop。

## 相依與封裝

直接相依的最低版本已對齊 2026-07-28 官方 PyPI 最新穩定版：FastAPI 0.140.7、
Uvicorn 0.51.0、MCP Python SDK 1.28.1、Pydantic 2.13.4、HTTPX 0.28.1 與
python-dotenv 1.2.2。MCP 明確限制 `<2`，因 v2 目前仍是 alpha，不能當 production stable 使用。

`data/glossary.csv`、eval fixtures 與 HTML test bench 都是 package data；測試會實際 build
wheel、安裝到隔離目錄並讀取資產，避免 source checkout 可跑、wheel 卻壞掉。

## 測試

```bash
.venv/bin/python -m pytest tests/ -q

# 評測 harness（fake 只驗證流程，不代表真實模型品質）
.venv/bin/python -m evals.run_eval --suite routing --gateway fake --limit 4
```

有設定真實 gateway 時，三項 model-behaviour tests 才會執行；沒有設定會明確 skip，不會用
fake 結果冒充模型準確率。

## 已知限制

- interactive CLI 尚未保存跨輸入的 conversation session。
- FastAPI 的每個 request 也是獨立 run，尚未提供 conversation/session API 或 streaming。
- 尚未支援 streaming、Streamable HTTP MCP、tenant/principal/scopes 與通用 approval hook。
- RAG 本次只有安全上傳 adapter；retrieval/rerank/citation 應另做 MCP capability。
- `data/glossary.csv` 是 sample，不是正式術語資產。
- 工具選擇仍由模型依當次可見 schema/description 決定；大型 catalog 後續應增加 capability
  routing 與 top-k selection。

glossary 演算法與驗收規格仍保留於 `specs/`；production 架構、FastAPI lifecycle、Agent
loop、translation policy、glossary quarantine 與 legacy 相容路徑的完整圖見
[`docs/architecture.md`](docs/architecture.md)。
