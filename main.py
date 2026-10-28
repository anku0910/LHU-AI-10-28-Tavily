import base64
import ctypes
import ctypes.wintypes
import http.server
import io
import os
import queue
import re
import socketserver
import sys
import threading
import time
import tkinter as tk
import webbrowser
from datetime import datetime

import pystray
import requests
from PIL import Image, ImageDraw, ImageEnhance, ImageGrab, ImageTk

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview")
DIM = 0.45
HOTKEY_MODS = 0x0001 | 0x0002  # Ctrl + Alt
HOTKEY_VK = 0x53               # S
WM_HOTKEY = 0x0312

PROMPT_PLAN = """你是截圖分析助理。請依序完成：
1. 思考：判斷圖片內容與使用者真正想解決的問題，指出需要上網查證的關鍵事實。
2. 初步回答：只寫圖片本身能確認的內容，不確定的地方標為（需查證）。
3. 搜尋：給出一組最適合查證的關鍵字，專有名詞請用原文。
全部使用繁體中文。最後一行固定格式：QUERY: <關鍵字>"""

PROMPT_FINAL = """以下是你剛才對截圖的初步分析與網路搜尋結果。請整合成最終答案：
- 使用繁體中文（台灣用語），先給結論，再列重點。
- 只採用搜尋結果中確實存在的資訊，並在句子後標註來源網址。
- 若搜尋結果推翻或補充了初步分析，請明確說明差異。
- 不要臆測搜尋結果未提及的背景或原因。
- 精簡，不要整段貼上原始搜尋片段。

【使用者問題】{q}
【初步分析】{draft}
【搜尋結果】{hits}"""

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shots")
PREVIEW = {"port": None, "opened": False}

INDEX_HTML = """<!doctype html><meta charset="utf-8"><title>截圖分析</title>
<style>
 body{max-width:900px;margin:40px auto;padding:0 20px;color:#1f2328;background:#fff;
      font:16px/1.75 -apple-system,"Segoe UI","Noto Sans TC",sans-serif}
 h1,h2{border-bottom:1px solid #e5e7eb;padding-bottom:.3em}
 a{color:#0969da} code{background:#f0f0f0;padding:2px 5px;border-radius:4px}
 pre{background:#f6f8fa;padding:14px;border-radius:8px;overflow:auto}
 li{margin:.25em 0} #meta{color:#6b7280;font-size:13px}
</style>
<div id="meta">本機預覽 · 每 2 秒自動重新載入</div>
<div id="out">載入中…</div>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script>
async function load(){
  try{
    const r = await fetch('latest.md?t=' + Date.now());
    if(!r.ok) throw new Error(r.status);
    document.getElementById('out').innerHTML = marked.parse(await r.text());
  }catch(e){ document.getElementById('out').textContent = '尚未有結果'; }
}
load(); setInterval(load, 2000);
</script>"""


def save_md(md: str) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    name = datetime.now().strftime("shot-%Y%m%d-%H%M%S.md")
    for path in (os.path.join(OUT_DIR, name), os.path.join(OUT_DIR, "latest.md")):
        with open(path, "w", encoding="utf-8") as f:
            f.write(md)
    return name


class PreviewHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=OUT_DIR, **kwargs)

    def log_message(self, *args):
        pass


def start_server() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(INDEX_HTML)
    latest = os.path.join(OUT_DIR, "latest.md")
    if not os.path.exists(latest):
        with open(latest, "w", encoding="utf-8") as f:
            f.write("# 尚未有截圖結果\n\n按 `Ctrl+Alt+S` 截圖並提問，結果會出現在這裡。\n")
    for port in range(8765, 8785):
        try:
            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), PreviewHandler)
        except OSError:
            continue
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return port
    raise RuntimeError("no free port for preview server")


def preview(md: str) -> str:
    name = save_md(md)
    if PREVIEW["port"] is None:
        PREVIEW["port"] = start_server()
    if not PREVIEW["opened"]:
        PREVIEW["opened"] = True
        webbrowser.open(f"http://127.0.0.1:{PREVIEW['port']}/")
    return name


def gemini(parts):
    for attempt in range(2):
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
            headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
            json={"contents": [{"parts": parts}]}, timeout=120)
        if r.ok:
            break
        time.sleep(1.5)
    r.raise_for_status()
    out = r.json()["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in out if not p.get("thought")).strip()


def ask_gemini(img: Image.Image, question: str):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    text = gemini([
        {"text": f"{PROMPT_PLAN}\n\n使用者問題：{question}"},
        {"inline_data": {"mime_type": "image/png",
                         "data": base64.b64encode(buf.getvalue()).decode()}},
    ])
    draft, _, query = text.rpartition("QUERY:")
    draft = re.sub(r"(?m)^\s*\d?\.?\s*\**\s*搜尋[^\n]*$", "", draft).strip()
    query = query.strip().splitlines()[0] if query.strip() else question
    return (draft or text), query


def synthesize(question, draft, query, hits):
    body = "\n".join(
        f"- {h.get('title')} ({h.get('url')})\n  {(h.get('content') or '')[:400]}"
        for h in hits) or "（無搜尋結果）"
    return gemini([{"text": PROMPT_FINAL.format(q=question, draft=draft, hits=body)}])


def pipeline(img: Image.Image, question: str, step=lambda *a: None) -> str:
    step(1, "Gemini 分析圖片（vision）")
    draft, query = ask_gemini(img, question)
    step(2, "Tavily 網路搜尋", f"QUERY: {query}\n\n[初步分析]\n{draft}")
    hits = tavily(query)
    step(3, f"Gemini 整合回答（{len(hits)} 筆搜尋結果）")
    final = synthesize(question, draft, query, hits)
    sources = "\n".join(f"- [{h.get('title')}]({h.get('url')})" for h in hits)
    return (f"# 截圖分析\n\n**問題**：{question}\n\n**搜尋關鍵字**：`{query}`\n\n"
            f"## 整合回答\n\n{final}\n\n## 來源\n\n{sources}")


def tavily(query: str):
    r = requests.post(
        "https://api.tavily.com/search",
        headers={"Authorization": f"Bearer {os.environ['TAVILY_API_KEY']}"},
        json={"query": query, "search_depth": "basic", "max_results": 5}, timeout=60)
    r.raise_for_status()
    return r.json().get("results", [])


class Picker:
    def __init__(self, root):
        self.orig = ImageGrab.grab()
        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        self.top.geometry(f"{self.orig.width}x{self.orig.height}+0+0")
        self.canvas = tk.Canvas(self.top, highlightthickness=0, cursor="cross")
        self.canvas.pack(fill="both", expand=True)
        self.bg = ImageTk.PhotoImage(ImageEnhance.Brightness(self.orig).enhance(DIM))
        self.canvas.create_image(0, 0, anchor="nw", image=self.bg)
        self.start = self.sel = self.rect = None
        self.canvas.bind("<ButtonPress-1>", self.down)
        self.canvas.bind("<B1-Motion>", self.drag)
        self.canvas.bind("<ButtonRelease-1>", self.up)
        self.top.bind("<Escape>", lambda e: self.top.destroy())
        self.top.focus_force()

    def box(self, e):
        x0, y0 = self.start
        return min(x0, e.x), min(y0, e.y), max(x0, e.x), max(y0, e.y)

    def down(self, e):
        self.start = (e.x, e.y)

    def drag(self, e):
        b = self.box(e)
        if b[2] - b[0] < 2 or b[3] - b[1] < 2:
            return
        self.thumb = ImageTk.PhotoImage(self.orig.crop(b))
        for item in (self.sel, self.rect):
            if item:
                self.canvas.delete(item)
        self.sel = self.canvas.create_image(b[0], b[1], anchor="nw", image=self.thumb)
        self.rect = self.canvas.create_rectangle(*b, outline="white")

    def up(self, e):
        b = self.box(e)
        self.top.destroy()
        if b[2] - b[0] > 3 and b[3] - b[1] > 3:
            Popup(self.top.master, self.orig.crop(b))


class Debug(tk.Toplevel):
    def __init__(self, master, x, y, w, h):
        super().__init__(master)
        self.title("Debug — API 進度")
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.attributes("-topmost", True)
        self.head = tk.Label(self, text="API 進度 0 / 3", anchor="w",
                             font=("Segoe UI", 10, "bold"))
        self.head.pack(fill="x", padx=8, pady=(8, 0))
        self.text = tk.Text(self, wrap="word", height=10)
        self.text.pack(fill="both", expand=True, padx=8, pady=8)
        self.log("等待 Submit…")

    def log(self, s):
        self.text.insert("end", s + "\n\n")
        self.text.see("end")

    def step(self, i, label, extra=""):
        self.head.config(text=f"API 進度 {i} / 3 — {label}")
        self.log(f"[{i}/3] {label}" + (f"\n{extra}" if extra else ""))

    def finish(self):
        self.head.config(text="API 進度 3 / 3 — 完成")

    def fail(self, err):
        self.head.config(text="API 進度 — 失敗")
        self.log(f"ERROR: {err}")


class Popup(tk.Toplevel):
    def __init__(self, master, img):
        super().__init__(master)
        self.img = img
        self.title("Screenshot Q&A")
        self.attributes("-topmost", True)
        w, h = 540, 640
        x = max(0, min(self.winfo_pointerx() - w // 2, self.winfo_screenwidth() - w - 480))
        y = max(0, min(self.winfo_pointery() - 120, self.winfo_screenheight() - h - 60))
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.debug = Debug(self, x + w + 8, y, 460, h)
        scale = min(500 / img.width, 150 / img.height, 1)
        self.preview = ImageTk.PhotoImage(
            img.resize((int(img.width * scale), int(img.height * scale))))
        tk.Label(self, image=self.preview).pack(padx=8, pady=8)
        self.q = tk.Entry(self)
        self.q.pack(fill="x", padx=8)
        self.q.insert(0, "Describe this screenshot")
        self.q.focus_set()
        self.btn = tk.Button(self, text="Submit", command=self.submit)
        self.btn.pack(anchor="e", padx=8, pady=6)
        self.out = tk.Text(self, wrap="word", height=16)
        self.out.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.q.bind("<Return>", lambda e: self.submit())

    def submit(self):
        self.btn.config(state="disabled")
        self.out.delete("1.0", "end")
        self.out.insert("end", "Analyzing...")
        self.debug.text.delete("1.0", "end")
        self.debug.head.config(text="API 進度 0 / 3")
        threading.Thread(target=self.work, daemon=True).start()

    def work(self):
        def step(i, label, extra=""):
            self.after(0, lambda: self.debug.step(i, label, extra))
        try:
            text = pipeline(self.img, self.q.get().strip() or "說明這張截圖，並查證畫面重點", step)
            self.after(0, self.debug.finish)
        except Exception as exc:
            text = f"ERROR: {exc}"
            self.after(0, lambda: self.debug.fail(str(exc)))
        self.after(0, lambda: self.show(text))

    def show(self, text):
        self.out.delete("1.0", "end")
        self.out.insert("end", text)
        self.out.see("1.0")
        self.btn.config(state="normal")
        try:
            name = preview(text)
            self.debug.log(f"已輸出 shots/{name}（瀏覽器預覽每 2 秒更新）")
        except Exception as exc:
            self.debug.log(f"輸出 md 失敗: {exc}")
        if sys.stdout:
            print(text)


def tray_image():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((5, 12, 59, 52), radius=10, fill="#1f6feb", outline="white", width=3)
    d.ellipse((21, 21, 43, 43), outline="white", width=3)
    return img


def hotkey_listener(events):
    u = ctypes.windll.user32
    if not u.RegisterHotKey(None, 1, HOTKEY_MODS, HOTKEY_VK):
        events.put("failed")
        return
    msg = ctypes.wintypes.MSG()
    while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == WM_HOTKEY:
            events.put("hit")


class App:
    def __init__(self, root):
        self.root = root
        self.picker = None
        self.events = queue.Queue()
        self.icon = pystray.Icon("shot", tray_image(), "Screenshot Q&A  (Ctrl+Alt+S)", menu=pystray.Menu(
            pystray.MenuItem("Capture  Ctrl+Alt+S", self.capture, default=True),
            pystray.MenuItem("Open preview", self.open_preview),
            pystray.MenuItem("Quit", self.quit)))
        threading.Thread(target=self.icon.run, daemon=True).start()
        threading.Thread(target=hotkey_listener, args=(self.events,), daemon=True).start()
        try:
            PREVIEW["port"] = start_server()
        except Exception:
            pass
        self.root.after(100, self.poll)

    def poll(self):
        try:
            while True:
                ev = self.events.get_nowait()
                if ev == "hit":
                    self._capture()
                elif ev == "failed":
                    self.icon.notify("Ctrl+Alt+S 註冊失敗，可能被其他程式占用", "Screenshot Q&A")
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def capture(self, *_):
        self.root.after(0, self._capture)

    def open_preview(self, *_):
        if PREVIEW["port"]:
            webbrowser.open(f"http://127.0.0.1:{PREVIEW['port']}/")

    def _capture(self):
        if self.picker is None or not self.picker.top.winfo_exists():
            self.picker = Picker(self.root)

    def quit(self, *_):
        self.icon.stop()
        self.root.after(0, self.root.destroy)


if __name__ == "__main__":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    if ctypes.windll.kernel32.CreateMutexW(None, False, "ScreenshotQA") and \
            ctypes.windll.kernel32.GetLastError() == 183:
        sys.exit(0)
    root = tk.Tk()
    root.withdraw()
    App(root)
    root.mainloop()
