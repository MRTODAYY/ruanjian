"""零依赖 Web 界面。

作业要求「可通过命令行或简单 Web 界面交互」，本项目两者都提供。
实现上只用标准库 ``http.server`` + Server-Sent Events 实时推送 Agent 轨迹，
不需要 Flask / FastAPI / Node，也不用打包前端资源。

启动::

    python -m agent.web --port 8000
    # 然后浏览器打开 http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import json
import queue
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

from .config import Config
from .console import Console, setup_console
from .core import CodeReviewAgent
from .llm import LLMClient, MockLLMClient, default_mock_script
from .prompts import PromptLibrary
from .tools import build_registry

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Code Review Agent · 代码审查智能体</title>
<style>
  :root {
    --bg: #0f1117; --panel: #171a23; --panel-2: #1e222d; --line: #2a2f3d;
    --text: #e6e8ee; --muted: #8b93a7; --accent: #6ea8fe; --ok: #4ade80;
    --warn: #fbbf24; --err: #f87171; --code: #0b0d12;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
    font-size: 14px; line-height: 1.65;
  }
  header {
    padding: 16px 24px; border-bottom: 1px solid var(--line);
    display: flex; align-items: center; gap: 14px; background: var(--panel);
    position: sticky; top: 0; z-index: 10;
  }
  header h1 { font-size: 16px; margin: 0; font-weight: 600; }
  header .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--ok); }
  header .meta { margin-left: auto; color: var(--muted); font-size: 12px; }
  main { display: grid; grid-template-columns: 340px 1fr; gap: 18px; padding: 18px 24px 40px; }
  @media (max-width: 900px) { main { grid-template-columns: 1fr; } }
  .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 16px; }
  .panel h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .08em;
              color: var(--muted); margin: 0 0 12px; font-weight: 600; }
  label { display: block; font-size: 12px; color: var(--muted); margin: 10px 0 5px; }
  input, select, textarea, button {
    width: 100%; background: var(--panel-2); color: var(--text);
    border: 1px solid var(--line); border-radius: 7px; padding: 9px 11px;
    font-family: inherit; font-size: 13px;
  }
  textarea { resize: vertical; min-height: 76px; font-family: ui-monospace, Consolas, monospace; }
  input:focus, select:focus, textarea:focus { outline: none; border-color: var(--accent); }
  button {
    background: var(--accent); color: #08101f; border: none; font-weight: 600;
    cursor: pointer; margin-top: 14px; padding: 11px;
  }
  button:hover { filter: brightness(1.08); }
  button:disabled { opacity: .5; cursor: not-allowed; }
  .row { display: flex; gap: 8px; }
  .row > * { flex: 1; }
  #stream { max-height: 46vh; overflow-y: auto; font-family: ui-monospace, Consolas, monospace;
            font-size: 12.5px; }
  .ev { padding: 6px 0; border-bottom: 1px dashed var(--line); }
  .ev:last-child { border-bottom: none; }
  .ev .tag { display: inline-block; min-width: 74px; color: var(--muted); }
  .ev.thought .txt { color: #cbd5e1; white-space: pre-wrap; }
  .ev.tool { color: var(--accent); }
  .ev.ok { color: var(--ok); }
  .ev.fail { color: var(--err); }
  .ev .pre { color: var(--muted); white-space: pre-wrap; padding-left: 74px; }
  #report { background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
            padding: 22px 26px; min-height: 320px; overflow-x: auto; }
  #report h1, #report h2, #report h3 { border-bottom: 1px solid var(--line); padding-bottom: 6px; }
  #report h1 { font-size: 22px; } #report h2 { font-size: 18px; } #report h3 { font-size: 15px; }
  #report code { background: var(--code); padding: 2px 5px; border-radius: 4px;
                 font-family: ui-monospace, Consolas, monospace; color: #ffd580; }
  #report pre { background: var(--code); border: 1px solid var(--line); border-radius: 8px;
                padding: 12px 14px; overflow-x: auto; }
  #report pre code { background: none; color: #d6deeb; padding: 0; }
  #report table { border-collapse: collapse; width: 100%; margin: 10px 0; }
  #report th, #report td { border: 1px solid var(--line); padding: 7px 10px; text-align: left; }
  #report th { background: var(--panel-2); }
  #report blockquote { border-left: 3px solid var(--accent); margin: 10px 0;
                       padding: 2px 14px; color: var(--muted); }
  .stat { display: flex; gap: 18px; flex-wrap: wrap; color: var(--muted); font-size: 12px;
          margin-top: 10px; }
  .stat b { color: var(--text); font-weight: 600; }
  .hint { color: var(--muted); font-size: 12px; }
  .files { max-height: 180px; overflow-y: auto; margin-top: 8px; }
  .files div { padding: 5px 8px; border-radius: 6px; cursor: pointer;
               font-family: ui-monospace, Consolas, monospace; font-size: 12px; }
  .files div:hover { background: var(--panel-2); color: var(--accent); }
  .badge { display: inline-block; background: var(--panel-2); border: 1px solid var(--line);
           border-radius: 20px; padding: 1px 9px; font-size: 11px; color: var(--muted); }
</style>
</head>
<body>
<header>
  <span class="dot" id="status"></span>
  <h1>Code Review Agent</h1>
  <span class="badge" id="modelBadge">加载中…</span>
  <span class="meta" id="metaInfo"></span>
</header>

<main>
  <section>
    <div class="panel">
      <h2>任务设置</h2>
      <label>模式</label>
      <select id="mode">
        <option value="review">代码审查</option>
        <option value="explain">代码解释</option>
        <option value="test">生成单元测试</option>
        <option value="refactor">重构建议</option>
        <option value="ask">自由问答</option>
      </select>

      <label>目标 / 问题</label>
      <textarea id="target" placeholder="例如：demo/sample_buggy.py">demo/sample_buggy.py</textarea>

      <label>额外审查重点（可选）</label>
      <input id="goals" placeholder="例如：重点看异常处理与并发安全" />

      <button id="run">开始分析</button>

      <label>工作目录文件（点击填入）</label>
      <div class="files" id="files"><span class="hint">加载中…</span></div>
    </div>

    <div class="panel" style="margin-top:18px">
      <h2>Agent 实时轨迹</h2>
      <div id="stream"><span class="hint">尚未开始。点击「开始分析」后这里会实时显示推理与工具调用过程。</span></div>
    </div>
  </section>

  <section>
    <div id="report"><span class="hint">分析结果会渲染在这里。</span></div>
    <div class="stat" id="stat"></div>
  </section>
</main>

<script>
const $ = (id) => document.getElementById(id);
let running = false;

async function loadMeta() {
  try {
    const res = await fetch("/api/meta");
    const meta = await res.json();
    $("modelBadge").textContent = meta.model + (meta.has_key ? "" : "（未配置 Key）");
    $("metaInfo").textContent = "工作目录 " + meta.workspace + " · 工具 " + meta.tools.length + " 个";
    const box = $("files");
    box.innerHTML = "";
    if (!meta.files.length) { box.innerHTML = '<span class="hint">未找到代码文件</span>'; return; }
    meta.files.forEach((f) => {
      const el = document.createElement("div");
      el.textContent = f;
      el.onclick = () => { $("target").value = f; };
      box.appendChild(el);
    });
  } catch (e) { $("metaInfo").textContent = "无法读取服务信息"; }
}

function addEvent(cls, tag, text, pre) {
  const stream = $("stream");
  if (stream.querySelector(".hint")) stream.innerHTML = "";
  const wrap = document.createElement("div");
  wrap.className = "ev " + cls;
  wrap.innerHTML = '<span class="tag">' + tag + "</span>" +
                   '<span class="txt"></span>';
  wrap.querySelector(".txt").textContent = text;
  stream.appendChild(wrap);
  if (pre) {
    const p = document.createElement("div");
    p.className = "pre";
    p.textContent = pre;
    stream.appendChild(p);
  }
  stream.scrollTop = stream.scrollHeight;
}

// 极简 Markdown 渲染（标题 / 代码块 / 列表 / 表格 / 粗体 / 行内代码）
function renderMarkdown(md) {
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const blocks = [];
  md = md.replace(/```(\w*)\n?([\s\S]*?)```/g, (m, lang, code) => {
    blocks.push("<pre><code>" + esc(code) + "</code></pre>");
    return "\u0000BLOCK" + (blocks.length - 1) + "\u0000";
  });
  const lines = md.split("\n");
  let html = "", listOpen = false, tableBuf = [];
  const flushList = () => { if (listOpen) { html += "</ul>"; listOpen = false; } };
  const flushTable = () => {
    if (!tableBuf.length) return;
    let t = "<table>";
    tableBuf.forEach((row, i) => {
      const cells = row.split("|").slice(1, -1).map((c) => c.trim());
      if (i === 1 && cells.every((c) => /^-+$/.test(c.replace(/:/g, "")))) return;
      t += "<tr>" + cells.map((c) => (i === 0 ? "<th>" : "<td>") + inline(c) +
           (i === 0 ? "</th>" : "</td>")).join("") + "</tr>";
    });
    html += t + "</table>";
    tableBuf = [];
  };
  const inline = (s) => esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|\s)\*([^*]+)\*/g, "$1<em>$2</em>");

  for (let raw of lines) {
    const line = raw.replace(/\s+$/, "");
    if (/^\s*\|.*\|\s*$/.test(line)) { flushList(); tableBuf.push(line.trim()); continue; }
    flushTable();
    if (/^\u0000BLOCK\d+\u0000$/.test(line.trim())) { flushList(); html += line.trim(); continue; }
    let m;
    if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
      flushList(); html += "<h" + m[1].length + ">" + inline(m[2]) + "</h" + m[1].length + ">";
    } else if (/^\s*[-*+]\s+/.test(line)) {
      if (!listOpen) { html += "<ul>"; listOpen = true; }
      html += "<li>" + inline(line.replace(/^\s*[-*+]\s+/, "")) + "</li>";
    } else if (/^\s*\d+\.\s+/.test(line)) {
      if (!listOpen) { html += "<ul>"; listOpen = true; }
      html += "<li>" + inline(line.replace(/^\s*\d+\.\s+/, "")) + "</li>";
    } else if (/^\s*>\s?/.test(line)) {
      flushList(); html += "<blockquote>" + inline(line.replace(/^\s*>\s?/, "")) + "</blockquote>";
    } else if (!line.trim()) { flushList(); }
    else { flushList(); html += "<p>" + inline(line) + "</p>"; }
  }
  flushList(); flushTable();
  html = html.replace(/\u0000BLOCK(\d+)\u0000/g, (m, i) => blocks[i]);
  return html;
}

async function run() {
  if (running) return;
  running = true;
  $("run").disabled = true;
  $("run").textContent = "分析中…";
  $("stream").innerHTML = "";
  $("report").innerHTML = '<span class="hint">Agent 正在工作，请稍候…</span>';
  $("stat").innerHTML = "";

  const body = JSON.stringify({
    mode: $("mode").value,
    target: $("target").value.trim(),
    goals: $("goals").value.trim(),
  });

  try {
    const res = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
    });
    if (!res.body) throw new Error("浏览器不支持流式响应");
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split("\n\n");
      buffer = parts.pop();
      for (const part of parts) {
        const line = part.trim();
        if (!line.startsWith("data:")) continue;
        let ev;
        try { ev = JSON.parse(line.slice(5).trim()); } catch (e) { continue; }
        handle(ev);
      }
    }
  } catch (err) {
    addEvent("fail", "错误", String(err));
  } finally {
    running = false;
    $("run").disabled = false;
    $("run").textContent = "开始分析";
  }
}

function handle(ev) {
  switch (ev.type) {
    case "step": addEvent("", "步骤 " + ev.step + "/" + ev.max_steps, "推理中…"); break;
    case "thought": addEvent("thought", "推理", ev.text); break;
    case "tool_start":
      addEvent("tool", "调用工具", ev.name + " " + JSON.stringify(ev.args || {})); break;
    case "tool_call":
      addEvent(ev.ok ? "ok" : "fail", ev.ok ? "工具成功" : "工具失败",
               "返回 " + ev.duration_ms + " ms", (ev.preview || "").slice(0, 600)); break;
    case "error": addEvent("fail", "异常", ev.message); break;
    case "final":
      $("report").innerHTML = renderMarkdown(ev.text || "");
      addEvent("ok", "最终报告", "已生成 " + (ev.text || "").length + " 字符");
      break;
    case "stat":
      $("stat").innerHTML =
        "步数 <b>" + ev.steps + "</b> · 工具调用 <b>" + ev.tool_calls +
        "</b> 次（失败 " + ev.tool_failures + "）· 耗时 <b>" +
        (ev.duration_ms / 1000).toFixed(1) + "s</b> · Token <b>" +
        (ev.prompt_tokens + ev.completion_tokens) + "</b> · 状态 <b>" +
        ev.stop_reason + "</b>";
      break;
    case "done":
      $("status").style.background = "#4ade80";
      break;
  }
}

$("run").onclick = run;
$("target").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) run();
});
loadMeta();
</script>
</body>
</html>
"""


class AgentWebServer:
    """把 Agent 封装成一个可通过 HTTP 触发的服务。"""

    def __init__(self, config: Config, *, mock: bool = False, verbose: bool = False):
        self.config = config
        self.mock = mock
        self.verbose = verbose
        self.lock = threading.Lock()

    # ------------------------------------------------------------------ #
    def meta(self) -> Dict[str, Any]:
        registry = build_registry(self.config.workspace, enable_exec=self.config.enable_exec,
                                  exec_timeout=self.config.exec_timeout)
        files: list[str] = []
        for suffix in ("*.py", "*.js", "*.ts", "*.java", "*.go", "*.rs", "*.md"):
            for path in sorted(self.config.workspace.rglob(suffix)):
                if any(part in {".git", "__pycache__", "node_modules", ".venv", "reports"}
                       for part in path.parts):
                    continue
                files.append(path.relative_to(self.config.workspace).as_posix())
                if len(files) >= 200:
                    break
        return {
            "model": self.config.model,
            "has_key": self.config.has_api_key or self.mock,
            "workspace": str(self.config.workspace),
            "tools": registry.names(),
            "modes": PromptLibrary.modes(),
            "files": sorted(set(files))[:120],
            "mock": self.mock,
        }

    def stream(self, payload: Dict[str, Any]):
        """生成 SSE 事件流（生成器，边跑边推）。"""
        mode = (payload.get("mode") or "review").strip()
        target = (payload.get("target") or "").strip()
        goals = (payload.get("goals") or "").strip()

        if mode not in PromptLibrary.modes():
            mode = "review"
        if not target:
            yield {"type": "error", "message": "请填写目标文件路径或问题内容。"}
            yield {"type": "done"}
            return
        if not (self.config.has_api_key or self.mock):
            yield {"type": "error", "message": "服务端未配置 API Key（.env 中的 CODEAGENT_API_KEY）。"}
            yield {"type": "done"}
            return

        queue_: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()

        def on_event(event: str, data: Dict[str, Any]) -> None:
            if event in {"tool_start", "tool_call", "thought", "error", "step"}:
                queue_.put({"type": event, **data})

        def worker() -> None:
            try:
                registry = build_registry(self.config.workspace,
                                          enable_exec=self.config.enable_exec,
                                          exec_timeout=self.config.exec_timeout)
                if self.mock:
                    llm: LLMClient = MockLLMClient(self.config, default_mock_script(),
                                                   verbose=self.verbose)
                else:
                    llm = LLMClient(self.config, verbose=self.verbose)
                agent = CodeReviewAgent(self.config, llm, registry, on_event=on_event)
                run = agent.run(target, mode=mode, goals=goals)
                queue_.put({"type": "final", "text": run.answer})
                queue_.put({
                    "type": "stat", "steps": run.steps, "tool_calls": run.tool_calls,
                    "tool_failures": run.tool_failures, "duration_ms": run.duration_ms,
                    "prompt_tokens": run.prompt_tokens,
                    "completion_tokens": run.completion_tokens,
                    "stop_reason": run.stop_reason,
                })
            except Exception as exc:  # noqa: BLE001 - 流式接口必须兜底
                queue_.put({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            finally:
                queue_.put(None)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        while True:
            item = queue_.get()
            if item is None:
                break
            yield item
        yield {"type": "done"}


def make_handler(server: AgentWebServer) -> type:
    """构造 HTTP 处理器（闭包捕获 server 实例）。"""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "CodeReviewAgent/1.0"

        # -------------------------------------------------------------- #
        def log_message(self, fmt: str, *args: Any) -> None:  # 静音默认日志
            if server.verbose:
                Console().dim(f"HTTP {self.address_string()} {fmt % args}")

        def _send(self, code: int, body: bytes, content_type: str = "text/plain; charset=utf-8") -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload: Dict[str, Any]) -> None:
            self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        # -------------------------------------------------------------- #
        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?")[0]
            if path in {"/", "/index.html"}:
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/meta":
                self._json(200, server.meta())
            elif path == "/favicon.ico":
                self._send(204, b"")
            else:
                self._send(404, b"Not Found")

        def do_POST(self) -> None:  # noqa: N802
            if self.path.split("?")[0] != "/api/run":
                self._send(404, b"Not Found")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except (ValueError, json.JSONDecodeError):
                self._json(400, {"error": "请求体不是合法 JSON"})
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            try:
                for event in server.stream(payload):
                    chunk = f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass  # 浏览器提前关闭连接，正常现象
            except Exception as exc:  # noqa: BLE001
                try:
                    self.wfile.write(
                        f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"
                        .encode("utf-8")
                    )
                except Exception:  # noqa: BLE001
                    pass

    return Handler


def serve(config: Config, *, host: str = "127.0.0.1", port: int = 8000,
          mock: bool = False, verbose: bool = False, open_browser: bool = False) -> None:
    console = Console()
    server = AgentWebServer(config, mock=mock, verbose=verbose)
    httpd = ThreadingHTTPServer((host, port), make_handler(server))

    url = f"http://{host}:{port}"
    console.banner("Web 界面已启动")
    console.info(f"访问地址：{console.paint(url, 'bold', 'cyan')}")
    console.dim(f"模型 {config.model}｜工作目录 {config.workspace}｜Mock {mock}")
    if not (config.has_api_key or mock):
        console.warn("未检测到 API Key，页面上的分析请求会返回错误提示。请先配置 .env。")
    console.dim("按 Ctrl+C 停止服务。")
    console.write()

    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        console.write()
        console.info("正在关闭服务…")
    finally:
        httpd.server_close()


def main(argv: Optional[list[str]] = None) -> int:
    setup_console()
    parser = argparse.ArgumentParser(prog="agent.web", description="Code Review Agent Web 界面")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=8000, help="端口，默认 8000")
    parser.add_argument("--workspace", help="工作目录")
    parser.add_argument("--mock", action="store_true", help="Mock 模式（不消耗 API）")
    parser.add_argument("--model", help="覆盖模型名")
    parser.add_argument("--no-exec", action="store_true", help="禁用代码执行工具")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    config = Config.load()
    if args.model:
        config.model = args.model
    if args.workspace:
        config.workspace = Path(args.workspace).expanduser().resolve()
    if args.no_exec:
        config.enable_exec = False

    serve(config, host=args.host, port=args.port, mock=args.mock,
          verbose=args.verbose, open_browser=args.open)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
