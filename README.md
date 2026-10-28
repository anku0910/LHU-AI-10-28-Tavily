# 截圖問答工具（Gemini Vision + Tavily）

2026/10/28 課堂練習。`Ctrl+Alt+S` 框選畫面，交給 Gemini 讀圖、Tavily 查證，最後用繁體中文整合，結果寫成 Markdown 並在本機網站顯示。

![平台](https://img.shields.io/badge/平台-Windows-0078d4?style=flat-square&logo=windows&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.13-3776ab?style=flat-square&logo=python&logoColor=white)
![模型](https://img.shields.io/badge/Gemini-3--flash--preview-4285f4?style=flat-square&logo=google&logoColor=white)
![搜尋](https://img.shields.io/badge/Tavily-search__depth_basic-0f9d8f?style=flat-square)
![依賴](https://img.shields.io/badge/依賴-Pillow%20%7C%20requests%20%7C%20pystray-1f6feb?style=flat-square)

## 功能

- 全域快捷鍵 `Ctrl+Alt+S`，或點系統匣圖示，兩種都能叫出截圖
- 全螢幕遮罩自由框選，放開滑鼠立刻裁切
- 彈窗輸入問題，右側 Debug 視窗同步顯示 API 走到第幾步與初步分析
- 結果輸出成 Markdown，本機網站每 2 秒自動更新

## 套件與寫法

| 套件 | 版本 | 用途 |
| --- | --- | --- |
| `tkinter` | 3.13（內建） | 全螢幕遮罩、彈窗、Debug 視窗 |
| `Pillow` | 13.0.0 | 螢幕截取、調暗、縮圖 |
| `requests` | 2.34.2 | 直接打 Gemini / Tavily REST API |
| `pystray` | 0.19.5 | 系統匣圖示與右鍵選單 |
| `ctypes` | 內建 | 全域快捷鍵、DPI 宣告、單一實例 |

沒有用 PyQt6。Qt6 的 wheel 是 78 MB，這台機器實測下載到 4.2 MB 就 DNS 失敗重試五次，而 tkinter 方案額外下載量是 0（Pillow 與 requests 本來就裝好了）。

```text
PyQt6 + Qt6    84.8 MB  [████████████████████████] 100.0%
pystray         0.05 MB [▏                       ]   0.1%
```

### 幾個實作選擇

- **遮罩**：不用 alpha 合成，改成「整張截圖調暗 45% 當底圖，拖曳時把原圖的選取區貼回上層」。視覺上就是半透明遮罩，而且裁切時直接從原圖取，像素無損。
- **DPI**：開頭呼叫 `SetProcessDpiAwareness(2)`，否則 125% 縮放時框選座標與實際截圖會錯位。
- **執行緒**：API 呼叫在背景 thread，結果一律用 `self.after(0, ...)` 丟回 Tk 主執行緒，不跨執行緒碰 widget。
- **快捷鍵**：`RegisterHotKey(hWnd=None, ...)` 是綁「呼叫它的執行緒」的訊息佇列，所以註冊和 `GetMessageW` 必須在同一個執行緒；分開寫會收不到 `WM_HOTKEY`。
- **pythonw 陷阱**：用 `pythonw.exe` 啟動時 `sys.stderr` 是 `None`，`SimpleHTTPRequestHandler.log_message` 會嘗試寫入而拋錯，連線在送標頭前就斷掉，瀏覽器只看到 `ERR_EMPTY_RESPONSE`。解法是把 `log_message` 覆寫成空方法。

## 連接到 Gemini 與 Tavily

兩組金鑰都放環境變數，不寫進程式碼，`GEMINI_MODEL` 可覆寫模型：

| 變數 | 用途 |
| --- | --- |
| `GEMINI_API_KEY` | 讀圖與整合，放在 `x-goog-api-key` header |
| `TAVILY_API_KEY` | 網路搜尋，放在 `Authorization: Bearer` |
| `GEMINI_MODEL` | 選填，預設 `gemini-3-flash-preview` |

```python
r = requests.post(
    f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
    headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
    json={"contents": [{"parts": parts}]}, timeout=120)

r = requests.post(
    "https://api.tavily.com/search",
    headers={"Authorization": f"Bearer {os.environ['TAVILY_API_KEY']}"},
    json={"query": query, "search_depth": "basic", "max_results": 5}, timeout=60)
```

金鑰不走 URL 查詢參數，因為 `raise_for_status()` 會把整串網址印進錯誤訊息，等於把 key 印出來。

## Tavily 的角色

Tavily 不讀圖，它只負責「拿 Gemini 想出來的那組關鍵字去查資料」，回傳標題、網址、摘要。真正做判斷的是第二次 Gemini。

```mermaid
flowchart LR
    A["Ctrl+Alt+S<br/>框選截圖"] --> B["Gemini #1<br/>vision 讀圖"]
    B -->|"最後一行<br/>QUERY: 關鍵字"| C["Tavily<br/>search_depth=basic"]
    C -->|"5 筆 title/url/content"| D["Gemini #2<br/>整合回答"]
    D --> E["主視窗"]
    D --> F["shots/*.md"]
    F --> G["127.0.0.1:8765<br/>marked.js"]
```

這樣切的理由：模型自己講的內容無法查證，搜尋結果又沒有看過圖。拆成兩段後，第一段只描述圖片看到什麼、哪裡需要查，第二段拿搜尋結果回來對照，衝突的地方要明講，來源網址也一併附上。

## 三個步驟

| 步驟 | 呼叫 | 輸入 | 產出 |
| ---: | --- | --- | --- |
| 1 | Gemini vision | 截圖 + 使用者問題 + `PROMPT_PLAN` | 思考、初步回答、`QUERY:` 一行 |
| 2 | Tavily | 步驟 1 的 `QUERY` | 5 筆搜尋結果 |
| 3 | Gemini | 問題 + 初步分析 + 搜尋結果 + `PROMPT_FINAL` | 繁體中文整合回答 |

兩次 Gemini、一次 Tavily，順序固定，Debug 視窗就照這個順序顯示 `1/3`、`2/3`、`3/3`。

實際使用的兩段 prompt：

```text
你是截圖分析助理。請依序完成：
1. 思考：判斷圖片內容與使用者真正想解決的問題，指出需要上網查證的關鍵事實。
2. 初步回答：只寫圖片本身能確認的內容，不確定的地方標為（需查證）。
3. 搜尋：給出一組最適合查證的關鍵字，專有名詞請用原文。
全部使用繁體中文。最後一行固定格式：QUERY: <關鍵字>
```

```text
以下是你剛才對截圖的初步分析與網路搜尋結果。請整合成最終答案：
- 使用繁體中文（台灣用語），先給結論，再列重點。
- 只採用搜尋結果中確實存在的資訊，並在句子後標註來源網址。
- 若搜尋結果推翻或補充了初步分析，請明確說明差異。
- 不要臆測搜尋結果未提及的背景或原因。
- 精簡，不要整段貼上原始搜尋片段。
```

## 輸出形式

| 位置 | 內容 |
| --- | --- |
| 主視窗 | 整合回答 + 來源清單（Markdown 原文） |
| Debug 視窗 | `API 進度 n / 3`、`QUERY`、初步分析全文 |
| `shots/shot-YYYYmmdd-HHMMSS.md` | 每次結果留一份 |
| `shots/latest.md` | 永遠是最新一份，網站讀這個 |
| `http://127.0.0.1:8765/` | `marked.js` 渲染，每 2 秒重新抓 `latest.md` |

Markdown 長這樣：

```markdown
# 截圖分析

**問題**：這張圖是什麼？

**搜尋關鍵字**：`Limbus Company Haute Couture King of Needleworks`

## 整合回答

（Gemini 整合後的繁體中文內容，句子後附來源）

## 來源

- [Haute Couture::Le Rouge King of Needleworks Heathcliff](https://limbuscompany.wiki.gg/wiki/...)
- [ProjectMoon Official YouTube](https://www.youtube.com/watch?v=...)
```

本機網站是 `http.server.ThreadingHTTPServer` 起在 `127.0.0.1`，只綁 loopback，不對外開放。前端只有一段 fetch + `setInterval`，所以分析完不用手動重整。網址固定 8765，被佔用會往後找 8766～8784。

## 專案結構

```text
main.py            全部程式（約 370 行）
app.ico            系統匣與捷徑圖示，程式產生
requirements.txt   三個外部套件
shots/             執行後產生：index.html、latest.md、歷史 md
```

## 執行

```powershell
pip install Pillow requests pystray

$env:GEMINI_API_KEY=[Environment]::GetEnvironmentVariable('GEMINI_API_KEY','User')
$env:TAVILY_API_KEY=[Environment]::GetEnvironmentVariable('TAVILY_API_KEY','User')

python main.py
```

不想要黑色終端機視窗就用 `pythonw main.py`，或跑一次 `Screenshot Q&A.lnk`（用 `pythonw` + `app.ico`）。程式有做單一實例（具名 mutex），重複點擊不會出現第二個托盤圖示。

日常操作：`Ctrl+Alt+S` 截圖 → 輸入問題 → Enter → 看主視窗或瀏覽器。

## 備註

- `gemini-2.5-flash` 這個金鑰的配額已用完，API 實際回 429，但外層被包成 503，所以只會看到 Service Unavailable。預設模型因此改成 `gemini-3-flash-preview`。
- 3.x 模型會回傳 `thought` 片段，全部 join 會把模型的內心話也貼進答案，取文字時要過濾 `part.get("thought")`。
- 高瘦的截圖（例如整段網頁）若照原比例縮圖，會把下方輸出框壓成 1px，所以預覽圖限高 150px。
- 截圖只存在記憶體，不落地；寫到磁碟的只有 Markdown。
- `marked.js` 走 jsDelivr CDN，第一次載入需要網路，之後瀏覽器會快取。要完全離線得把檔案放進 `shots/` 改相對路徑。
- Debug 視窗是主視窗的子視窗，關掉主視窗會一起關；它仍會佔一個工作列按鈕。
