"""HTTP + SSE 预览服务器。

提供 Web 服务，实时可视化 Markdown 文件变更。
- HTTP: Python stdlib http.server
- SSE: Server-Sent Events 推送文件变更
- 前端：单页应用，全部内联在 HTML 中
"""

import io
import json
import os
import threading
import time
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
try:
    from http.server import ThreadingHTTPServer
except ImportError:
    # Python < 3.7 fallback
    import socketserver
    class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
        daemon_threads = True
from pathlib import Path
from typing import Optional

from .snapshot import SnapshotManager
from .watcher import FileWatcher, FileChangeEvent
from .renderer import (
    render_md_to_html,
    render_overlay_diff_html,
    compute_diff_lines,
    split_sections,
)


# ─── SSE 客户端管理器 ───────────────────────────────────────

class SSEClientManager:
    """SSE 客户端连接管理器。"""

    def __init__(self):
        self._clients: list[io.StringIO] = []
        self._lock = threading.Lock()

    def register(self, buffer: io.StringIO) -> io.StringIO:
        """注册一个新的 SSE 客户端。返回用于写入的 buffer。"""
        with self._lock:
            self._clients.append(buffer)
        return buffer

    def unregister(self, buffer: io.StringIO) -> None:
        """注销一个 SSE 客户端。"""
        with self._lock:
            if buffer in self._clients:
                self._clients.remove(buffer)

    def broadcast(self, event_type: str, data: str) -> None:
        """广播事件到所有客户端。"""
        with self._lock:
            dead: list[io.StringIO] = []
            for buf in self._clients:
                try:
                    payload = (
                        f"event: {event_type}\n"
                        f"data: {data}\n\n"
                    )
                    buf.write(payload)
                    buf.seek(0)
                except Exception:
                    dead.append(buf)
            for buf in dead:
                self._clients.remove(buf)


_sse_manager = SSEClientManager()


# ─── HTTP 请求处理器 ────────────────────────────────────────

PREVIEW_PORT: int = 8765


class PreviewHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器。"""

    # 静态文件根目录（用于自定义资源）
    static_dir: Optional[Path] = None
    # 快照管理器引用
    snapshots: Optional[SnapshotManager] = None
    # 原始文件路径
    original_file: Optional[Path] = None

    def log_message(self, format: str, *args) -> None:
        """静默日志（不输出到控制台）。"""
        pass

    def _send_json(self, data: dict, status: int = 200) -> None:
        """发送 JSON 响应。"""
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _send_text(self, text: str, content_type: str = "text/html; charset=utf-8",
                   status: int = 200) -> None:
        """发送文本响应。"""
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(text.encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _send_error(self, status: int, message: str) -> None:
        """发送错误响应。"""
        self._send_json({"error": message}, status)

    def _parse_path(self) -> tuple[str, dict[str, str]]:
        """解析路径和查询参数。"""
        path = self.path
        query: dict[str, str] = {}
        if "?" in path:
            path, qs = path.split("?", 1)
            for pair in qs.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    from urllib.parse import unquote_plus
                    query[k] = unquote_plus(v)
        return path, query

    def do_GET(self) -> None:
        sm = self.__class__.snapshots
        if sm is None:
            self._send_error(500, "SnapshotManager not initialized")
            return

        path, query = self._parse_path()

        # ── 主页面 ──
        if path == "/":
            html = self._render_index_page(sm)
            self._send_text(html)
            return

        # ── SSE 推送端点 ──
        if path == "/events":
            self._handle_sse()
            return

        # ── API: 版本列表 ──
        if path == "/api/versions":
            versions = [v.to_dict() for v in sm.get_all_versions()]
            self._send_json({"versions": versions})
            return

        # ── API: 版本详情 ──
        if path.startswith("/api/versions/"):
            vid = path.split("/")[-1]
            if not vid:
                self._send_error(400, "Missing version ID")
                return
            content = sm.get_version_content(vid)
            if content is None:
                self._send_error(404, f"Version {vid} not found")
                return

            # 获取 diff 信息（如果指定了 from 参数）
            from_id = query.get("from", "")
            extra_css = ""
            extra_js = ""

            if from_id:
                from_content = sm.get_version_content(from_id)
                if from_content is not None:
                    overlay_mode = query.get("mode", "overlay")
                    if overlay_mode == "overlay":
                        html = render_overlay_diff_html(
                            title=f"{from_id} → {vid}",
                            text_a=from_content,
                            text_b=content,
                        )
                        self._send_text(html)
                        return
                    else:
                        # side-by-side 模式：返回纯渲染
                        html = render_md_to_html(
                            md_text=content,
                            title=f"Version {vid}",
                        )
                        self._send_text(html)
                        return

            # 无 diff：纯渲染
            html = render_md_to_html(
                md_text=content,
                title=f"Version {vid}",
            )
            self._send_text(html)
            return

        # ── API: Diff JSON ──
        if path == "/api/diff":
            from_id = query.get("from", "")
            to_id = query.get("to", "")
            if not from_id or not to_id:
                self._send_error(400, "Need 'from' and 'to' params")
                return

            content_a = sm.get_version_content(from_id)
            content_b = sm.get_version_content(to_id)
            if content_a is None or content_b is None:
                self._send_error(404, "Version not found")
                return

            line_states, line_old_texts = compute_diff_lines(content_a, content_b)

            # 构建 diff 数据
            lines_b = content_b.split("\n")
            diff_lines = []
            for i, line in enumerate(lines_b):
                state = line_states.get(i, "unchanged")
                old = line_old_texts.get(i, "")
                diff_lines.append({
                    "line": i,
                    "state": state,
                    "old": old,
                    "text": line,
                })

            self._send_json({
                "from": from_id,
                "to": to_id,
                "totalLines": len(lines_b),
                "added": sum(1 for d in diff_lines if d["state"] == "added"),
                "removed": sum(1 for d in diff_lines if d["state"] == "removed"),
                "changed": sum(1 for d in diff_lines if d["state"] == "changed"),
                "lines": diff_lines,
            })
            return

        # ── API: 章节数据 ──
        if path == "/api/sections":
            vid = query.get("version", "")
            if not vid:
                self._send_error(400, "Need 'version' param")
                return
            content = sm.get_version_content(vid)
            if content is None:
                self._send_error(404, f"Version {vid} not found")
                return
            sections = split_sections(content)
            self._send_json({
                "version": vid,
                "sections": [
                    {
                        "index": i + 1,
                        "level": s["level"],
                        "title": s["title"],
                        "startLine": s["start_line"],
                        "endLine": s["end_line"],
                        "lineCount": len(s["content_lines"]),
                    }
                    for i, s in enumerate(sections)
                ],
            })
            return

        # ── 404 ──
        self._send_error(404, "Not found")

    def _handle_sse(self) -> None:
        """处理 SSE 连接。"""
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            buffer = io.StringIO()
            _sse_manager.register(buffer)

            try:
                # 发送初始连接事件
                self.wfile.write(b"event: connected\ndata: {}\n\n")
                self.wfile.flush()

                while True:
                    time.sleep(0.5)
                    data = buffer.getvalue()
                    if data:
                        self.wfile.write(data.encode("utf-8"))
                        self.wfile.flush()
                        buffer.truncate(0)
                        buffer.seek(0)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                _sse_manager.unregister(buffer)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _render_index_page(self, sm: SnapshotManager) -> str:
        """渲染主页面（单页应用）。"""
        versions = sm.get_all_versions()
        versions_json = json.dumps(
            [v.to_dict() for v in versions],
            ensure_ascii=False,
        )

        # 找到默认的 from 和 to
        default_from = versions[0].version_id if versions else ""
        default_to = versions[-1].version_id if versions else ""

        return f"""\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>doc-ultra Preview - 实时文档可视化</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Noto Sans SC", sans-serif;
    background: #f0f2f5;
    color: #1a1a2e;
    min-height: 100vh;
}}
@media (prefers-color-scheme: dark) {{
    body {{ background: #121216; color: #e2e2e8; }}
    .app-header {{ background: #1a1a20; border-color: #2e2e38; }}
    .app-header .port-badge {{ background: #2e2e38; color: #888; }}
    .timeline {{ background: #1a1a20; border-color: #2e2e38; }}
    .timeline-node {{ background: #2e2e38; color: #888; }}
    .timeline-node.active {{ background: #3b82f6; color: #fff; }}
    .timeline-node.active.selected {{ background: #22c55e; color: #fff; }}
    .timeline-line {{ background: #33333d; }}
    .controls {{ background: #1a1a20; border-color: #2e2e38; }}
    .controls select {{ background: #2e2e38; color: #e2e2e8; border-color: #3e3e48; }}
    .preview-frame {{ background: #fff; }}
    .view-toggle button {{ color: #888; }}
    .view-toggle button.active {{ background: #3b82f6; color: #fff; }}
    .stat-box {{ background: #2e2e38; }}
    .stat-box .stat-value {{ color: #e2e2e8; }}
}}

.app-header {{
    background: #fff;
    border-bottom: 1px solid #e2e8f0;
    padding: 0.75rem 1.5rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    position: sticky;
    top: 0;
    z-index: 100;
}}
.app-header .logo {{
    font-size: 1.1rem;
    font-weight: 700;
    color: #3b82f6;
}}
.app-header .logo span {{ color: #1a1a2e; }}
.app-header .port-badge {{
    font-size: 0.8rem;
    color: #666;
    background: #f1f5f9;
    padding: 0.25rem 0.6rem;
    border-radius: 4px;
}}

.timeline {{
    background: #fff;
    border-bottom: 1px solid #e2e8f0;
    padding: 1rem 1.5rem;
    overflow-x: auto;
    white-space: nowrap;
}}
.timeline-track {{
    display: flex;
    align-items: center;
    gap: 0;
}}
.timeline-node {{
    display: inline-flex;
    flex-direction: column;
    align-items: center;
    gap: 0.25rem;
    cursor: pointer;
    padding: 0.4rem 0.75rem;
    border-radius: 8px;
    font-size: 0.8rem;
    color: #666;
    transition: all 0.15s;
    min-width: 60px;
    border: none;
    background: none;
    position: relative;
}}
.timeline-node:hover {{ background: #f1f5f9; }}
.timeline-node .dot {{
    width: 28px;
    height: 28px;
    border-radius: 50%;
    background: #e2e8f0;
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 700;
    font-size: 0.7rem;
    color: #666;
    transition: all 0.15s;
}}
.timeline-node.active .dot {{
    background: #3b82f6;
    color: #fff;
    box-shadow: 0 2px 8px rgba(59,130,246,0.3);
}}
.timeline-node.active.selected .dot {{
    background: #22c55e;
    color: #fff;
    box-shadow: 0 2px 8px rgba(34,197,94,0.3);
}}
.timeline-node .label {{
    font-size: 0.7rem;
    white-space: nowrap;
}}
.timeline-line {{
    width: 24px;
    height: 2px;
    background: #d0d5dd;
    flex-shrink: 0;
}}

.controls {{
    background: #fff;
    border-bottom: 1px solid #e2e8f0;
    padding: 0.75rem 1.5rem;
    display: flex;
    align-items: center;
    gap: 1rem;
    flex-wrap: wrap;
}}
.controls label {{
    font-size: 0.85rem;
    color: #666;
    font-weight: 500;
}}
.controls select {{
    padding: 0.35rem 0.6rem;
    border: 1px solid #d0d5dd;
    border-radius: 6px;
    font-size: 0.85rem;
    background: #fff;
    color: #1a1a2e;
    cursor: pointer;
}}
.controls .divider {{
    width: 1px;
    height: 24px;
    background: #e2e8f0;
}}
.view-toggle {{
    display: flex;
    gap: 0.25rem;
    background: #f1f5f9;
    border-radius: 6px;
    padding: 2px;
}}
.view-toggle button {{
    padding: 0.3rem 0.75rem;
    border: none;
    border-radius: 4px;
    font-size: 0.8rem;
    cursor: pointer;
    background: transparent;
    color: #666;
    transition: all 0.15s;
}}
.view-toggle button.active {{
    background: #fff;
    color: #1a1a2e;
    box-shadow: 0 1px 3px rgba(0,0,0,0.1);
}}
.view-toggle button:hover:not(.active) {{ color: #3b82f6; }}

.stats-bar {{
    display: flex;
    gap: 1rem;
    padding: 0.5rem 1.5rem;
    background: #f8f9fc;
    border-bottom: 1px solid #e2e8f0;
    font-size: 0.8rem;
}}
@media (prefers-color-scheme: dark) {{
    .stats-bar {{ background: #16161a; border-color: #2e2e38; }}
}}
.stat-box {{
    background: #fff;
    border-radius: 6px;
    padding: 0.25rem 0.75rem;
    display: flex;
    align-items: center;
    gap: 0.4rem;
}}
.stat-box .stat-label {{ color: #888; }}
.stat-box .stat-value {{ font-weight: 600; }}
.stat-box .stat-value.added {{ color: #22c55e; }}
.stat-box .stat-value.removed {{ color: #ef4444; }}
.stat-box .stat-value.changed {{ color: #eab308; }}

.preview-container {{
    padding: 1rem 1.5rem;
    width: 100%;
    height: calc(100vh - 180px);
}}
.preview-container.side-by-side {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1rem;
    height: calc(100vh - 180px);
}}
.preview-frame {{
    background: #fff;
    border-radius: 8px;
    overflow: hidden;
    box-shadow: 0 1px 4px rgba(0,0,0,0.06);
    display: flex;
    flex-direction: column;
    height: 100%;
}}
.preview-frame .frame-header {{
    padding: 0.5rem 1rem;
    background: #f8f9fc;
    border-bottom: 1px solid #e2e8f0;
    font-size: 0.8rem;
    font-weight: 600;
    color: #666;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-shrink: 0;
}}
@media (prefers-color-scheme: dark) {{
    .preview-frame .frame-header {{ background: #1e1e24; border-color: #2e2e38; color: #888; }}
}}
.preview-frame .frame-content {{
    padding: 0.5rem 1rem;
    flex: 1;
    overflow-y: auto;
    overflow-x: hidden;
}}
.preview-frame .frame-content iframe {{
    width: 100%;
    height: 100%;
    border: none;
    min-height: 400px;
}}

.loading {{
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 300px;
    color: #888;
    font-size: 0.9rem;
}}
.loading::after {{
    content: "";
    width: 20px;
    height: 20px;
    border: 2px solid #e2e8f0;
    border-top-color: #3b82f6;
    border-radius: 50%;
    animation: spin 0.6s linear infinite;
    margin-left: 0.5rem;
}}
@keyframes spin {{ to {{ transform: rotate(360deg); }} }}

.sse-status {{
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    font-size: 0.75rem;
    color: #888;
}}
.sse-status .dot {{
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: #22c55e;
    animation: pulse 2s ease-in-out infinite;
}}
.sse-status .dot.disconnected {{ background: #ef4444; animation: none; }}
@keyframes pulse {{
    0%, 100% {{ opacity: 1; }}
    50% {{ opacity: 0.4; }}
}}

.empty-state {{
    text-align: center;
    padding: 3rem;
    color: #888;
}}
.empty-state h3 {{ margin-bottom: 0.5rem; color: #666; }}

@media (max-width: 768px) {{
    .preview-container.side-by-side {{
        grid-template-columns: 1fr;
    }}
    .controls {{ flex-direction: column; align-items: flex-start; }}
}}
</style>
</head>
<body>

<div class="app-header">
    <div class="logo">doc-ultra <span>Preview</span></div>
    <div style="display:flex;align-items:center;gap:1rem;">
        <span class="sse-status" id="sse-status">
            <span class="dot" id="sse-dot"></span>
            <span id="sse-label">实时</span>
        </span>
        <span class="port-badge">:8765</span>
    </div>
</div>

<div class="timeline" id="timeline">
    <div class="timeline-track" id="timeline-track"></div>
</div>

<div class="controls" id="controls">
    <label>基准版本</label>
    <select id="select-from"></select>
    <span style="color:#888;font-size:0.85rem;">→</span>
    <label>目标版本</label>
    <select id="select-to"></select>
    <div class="divider"></div>
    <div class="view-toggle">
        <button class="active" data-mode="overlay" onclick="switchView('overlay')">修订视图</button>
        <button data-mode="side-by-side" onclick="switchView('side-by-side')">并排视图</button>
        <button data-mode="single" onclick="switchView('single')">仅目标</button>
    </div>
    <div class="divider"></div>
    <button onclick="refreshPreview()" style="padding:0.3rem 0.75rem;border:1px solid #d0d5dd;border-radius:6px;background:transparent;cursor:pointer;font-size:0.8rem;color:#666;">刷新</button>
</div>

<div class="stats-bar" id="stats-bar">
    <span style="color:#888;">Diff 统计:</span>
</div>

<div class="preview-container" id="preview-container">
    <div class="loading">加载中...</div>
</div>

<script>
// ─── 全局状态 ───
var VERSIONS = {versions_json};
var currentMode = 'overlay';
var currentFrom = '{default_from}';
var currentTo = '{default_to}';
var sseConnected = false;

// ─── 初始化 ───
document.addEventListener('DOMContentLoaded', function() {{
    initTimeline();
    initSelects();
    connectSSE();
    refreshPreview();
}});

// ─── 版本时间线 ───
function initTimeline() {{
    var track = document.getElementById('timeline-track');
    track.innerHTML = '';
    for (var i = 0; i < VERSIONS.length; i++) {{
        var v = VERSIONS[i];
        if (i > 0) {{
            var line = document.createElement('div');
            line.className = 'timeline-line';
            track.appendChild(line);
        }}
        var node = document.createElement('button');
        node.className = 'timeline-node';
        if (v.id === currentFrom) node.classList.add('active');
        if (v.id === currentTo) node.classList.add('active', 'selected');
        node.dataset.vid = v.id;
        node.innerHTML = '<span class="dot">' + v.id + '</span><span class="label">' + v.name + '</span>';
        node.addEventListener('click', function() {{ onTimelineClick(this.dataset.vid); }});
        track.appendChild(node);
    }}
}}

function onTimelineClick(vid) {{
    if (vid === currentFrom) {{
        // 点击同一个节点：切换为 from
        return;
    }}
    if (vid === currentTo) {{
        // 交换 from 和 to
        var tmp = currentFrom;
        currentFrom = currentTo;
        currentTo = tmp;
    }} else {{
        // 把点击的节点设为 to，旧的 to 变成 from（如果 from 是 to 的话）
        currentFrom = currentTo;
        currentTo = vid;
    }}
    updateSelects();
    updateTimelineNodes();
    refreshPreview();
}}

function updateTimelineNodes() {{
    var nodes = document.querySelectorAll('.timeline-node');
    nodes.forEach(function(n) {{
        n.classList.remove('active', 'selected');
        if (n.dataset.vid === currentFrom) n.classList.add('active');
        if (n.dataset.vid === currentTo) n.classList.add('active', 'selected');
    }});
}}

// ─── 下拉选择 ───
function initSelects() {{
    var selFrom = document.getElementById('select-from');
    var selTo = document.getElementById('select-to');
    selFrom.innerHTML = '';
    selTo.innerHTML = '';
    VERSIONS.forEach(function(v) {{
        var opt1 = document.createElement('option');
        opt1.value = v.id;
        opt1.textContent = v.id + ' - ' + v.name;
        if (v.id === currentFrom) opt1.selected = true;
        selFrom.appendChild(opt1);

        var opt2 = document.createElement('option');
        opt2.value = v.id;
        opt2.textContent = v.id + ' - ' + v.name;
        if (v.id === currentTo) opt2.selected = true;
        selTo.appendChild(opt2);
    }});
    selFrom.addEventListener('change', function() {{
        currentFrom = this.value;
        updateTimelineNodes();
        refreshPreview();
    }});
    selTo.addEventListener('change', function() {{
        currentTo = this.value;
        updateTimelineNodes();
        refreshPreview();
    }});
}}

function updateSelects() {{
    document.getElementById('select-from').value = currentFrom;
    document.getElementById('select-to').value = currentTo;
}}

// ─── 视图切换 ───
function switchView(mode) {{
    currentMode = mode;
    document.querySelectorAll('.view-toggle button').forEach(function(b) {{
        b.classList.toggle('active', b.dataset.mode === mode);
    }});
    refreshPreview();
}}

// ─── 刷新预览 ───
function refreshPreview() {{
    var container = document.getElementById('preview-container');
    var statsBar = document.getElementById('stats-bar');

    if (currentMode === 'side-by-side') {{
        container.className = 'preview-container side-by-side';
        container.innerHTML =
            '<div class="preview-frame">' +
            '    <div class="frame-header">' + currentFrom + ' - ' + getVersionName(currentFrom) + '</div>' +
            '    <div class="frame-content"><iframe srcdoc="<div class=loading>加载中...</div>" id="frame-from"></iframe></div>' +
            '</div>' +
            '<div class="preview-frame">' +
            '    <div class="frame-header">' + currentTo + ' - ' + getVersionName(currentTo) + '</div>' +
            '    <div class="frame-content"><iframe srcdoc="<div class=loading>加载中...</div>" id="frame-to"></iframe></div>' +
            '</div>';
        loadIframe('frame-from', '/api/versions/' + currentFrom);
        loadIframe('frame-to', '/api/versions/' + currentTo);
        statsBar.innerHTML = '<span style="color:#888;">并排视图 — 左侧基准 / 右侧目标</span>';
    }} else if (currentMode === 'single') {{
        container.className = 'preview-container';
        container.innerHTML =
            '<div class="preview-frame">' +
            '    <div class="frame-header">' + currentTo + ' - ' + getVersionName(currentTo) + '</div>' +
            '    <div class="frame-content"><iframe srcdoc="<div class=loading>加载中...</div>" id="frame-single"></iframe></div>' +
            '</div>';
        loadIframe('frame-single', '/api/versions/' + currentTo);
        statsBar.innerHTML = '<span style="color:#888;">仅查看目标版本</span>';
    }} else {{
        // overlay mode
        container.className = 'preview-container';
        container.innerHTML = '<div class="loading">加载 diff 中...</div>';
        statsBar.innerHTML = '<span style="color:#888;">修订视图 — 修订模式 (绿色=新增, 红色=删除, 黄色=修改，悬浮查看详情)</span>';

        fetch('/api/diff?from=' + encodeURIComponent(currentFrom) + '&to=' + encodeURIComponent(currentTo))
            .then(function(r) {{ return r.json(); }})
            .then(function(data) {{
                // 更新统计
                statsBar.innerHTML =
                    '<span style="color:#888;">Diff 统计:</span>' +
                    '<span class="stat-box"><span class="stat-label">新增</span> <span class="stat-value added">+' + data.added + '</span></span>' +
                    '<span class="stat-box"><span class="stat-label">删除</span> <span class="stat-value removed">-' + data.removed + '</span></span>' +
                    '<span class="stat-box"><span class="stat-label">修改</span> <span class="stat-value changed">~' + data.changed + '</span></span>' +
                    '<span class="stat-box"><span class="stat-label">总计</span> <span class="stat-value" style="color:#666;">' + data.totalLines + '</span></span>';

                // 加载 overlay 渲染
                container.innerHTML =
                    '<div class="preview-frame">' +
                    '    <div class="frame-header">' + currentFrom + ' → ' + currentTo + ' (修订模式·悬浮查看详情)</div>' +
                    '    <div class="frame-content"><iframe srcdoc="<div class=loading>加载中...</div>" id="frame-overlay"></iframe></div>' +
                    '</div>';
                loadIframe('frame-overlay', '/api/versions/' + currentTo + '?from=' + encodeURIComponent(currentFrom) + '&mode=overlay');
            }})
            .catch(function(err) {{
                container.innerHTML = '<div class="empty-state"><h3>加载失败</h3><p>' + err + '</p></div>';
            }});
    }}
}}

function loadIframe(frameId, url) {{
    fetch(url)
        .then(function(r) {{ return r.text(); }})
        .then(function(html) {{
            var iframe = document.getElementById(frameId);
            if (iframe) {{
                iframe.srcdoc = html;
            }}
        }})
        .catch(function(err) {{
            var iframe = document.getElementById(frameId);
            if (iframe) {{
                iframe.srcdoc = '<div class="empty-state"><h3>加载失败</h3><p>' + err + '</p></div>';
            }}
        }});
}}

function getVersionName(vid) {{
    for (var i = 0; i < VERSIONS.length; i++) {{
        if (VERSIONS[i].id === vid) return VERSIONS[i].name;
    }}
    return vid;
}}

// ─── SSE 连接 ───
function connectSSE() {{
    var evtSource = new EventSource('/events');
    var dot = document.getElementById('sse-dot');
    var label = document.getElementById('sse-label');

    evtSource.onopen = function() {{
        sseConnected = true;
        dot.className = 'dot';
        label.textContent = '实时';
    }};

    evtSource.addEventListener('connected', function() {{
        sseConnected = true;
        dot.className = 'dot';
        label.textContent = '实时';
    }});

    evtSource.addEventListener('file_changed', function(e) {{
        dot.className = 'dot';
        dot.style.animation = 'none';
        dot.style.opacity = '1';

        // 闪烁提示
        dot.style.background = '#f59e0b';
        setTimeout(function() {{
            dot.style.background = '';
            dot.style.animation = '';
        }}, 1000);

        label.textContent = '已更新';

        // 解析事件数据，获取完整版本列表
        try {{
            var payload = JSON.parse(e.data);
            if (payload.versions && payload.versions.length > 0) {{
                VERSIONS = payload.versions;
                initTimeline();
                initSelects();
                // 自动切换到最新版本对比
                if (payload.new_version) {{
                    var prevIdx = VERSIONS.length - 2;
                    if (prevIdx >= 0) {{
                        currentFrom = VERSIONS[prevIdx].id;
                    }}
                    currentTo = payload.new_version;
                    updateSelects();
                    updateTimelineNodes();
                }}
            }}
        }} catch(_) {{}}

        refreshPreview();
        label.textContent = '实时';
    }});

    evtSource.onerror = function() {{
        sseConnected = false;
        dot.className = 'dot disconnected';
        label.textContent = '断开';
        // 自动重连（SSE 内置机制）
    }};
}}
</script>
</body>
</html>"""


# ─── 预览服务器 ──────────────────────────────────────────────

class PreviewServer:
    """doc-ultra 预览服务器。

    启动 HTTP 服务 + 文件监听，提供 Markdown 文件实时可视化。

    两种模式：
    - pipeline 模式：关联流水线产物（默认）
    - file 模式：独立预览任意 .md 文件，每次保存=新快照
    """

    def __init__(
        self,
        work_dir: Path,
        port: int = 8765,
        original_file: Optional[Path] = None,
        open_browser: bool = True,
        mode: str = "pipeline",
    ):
        """初始化预览服务器。

        Args:
            work_dir: 工作目录（.doc-ultra/ 或文件所在目录）
            port: HTTP 端口
            original_file: 原始输入文件路径（可选）
            open_browser: 是否自动打开浏览器
            mode: 'pipeline' 关联流水线 | 'file' 独立文件预览
        """
        self.work_dir = work_dir
        self.port = port
        self.original_file = original_file
        self.open_browser = open_browser
        self.mode = mode
        self.snapshots = SnapshotManager(work_dir)
        self.watcher: Optional[FileWatcher] = None
        self._httpd: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """启动服务器 + 文件监听。"""
        # 1. 根据模式初始化快照
        if self.mode == "file" and self.original_file:
            self.snapshots.init_file_history(self.original_file)
        elif self.original_file and self.original_file.exists():
            content = self.original_file.read_text(encoding="utf-8")
            self.snapshots.set_original(content)
            # 也将原始文件添加到工作目录快照中
            self.snapshots.add_file("ORIG", "原始文件", self.original_file)
            # 2. 扫描现有中间产物
            self.snapshots.scan()

        # 2.5 流水线模式扫描中间产物（如果没有 original_file 或 file 模式）
        if self.mode == "pipeline" and not self.original_file:
            self.snapshots.scan()

        # 3. 设置 HTTP 处理器
        PreviewHandler.snapshots = self.snapshots
        global PREVIEW_PORT
        PREVIEW_PORT = self.port

        # 4. 启动 HTTP 服务器（多线程，防止 SSE 阻塞其他请求）
        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), PreviewHandler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

        # 5. 启动文件监听
        self._start_watcher()

        # 6. 自动打开浏览器
        if self.open_browser:
            url = f"http://127.0.0.1:{self.port}/"
            threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()

    def _start_watcher(self) -> None:
        """启动文件监听。"""
        if self.mode == "file" and self.original_file:
            # 单文件模式：仅监听目标文件所在目录
            watch_dir = self.original_file.parent
            _target_resolved = self.original_file.resolve()
            def on_file_change(event: FileChangeEvent) -> None:
                file_path = event.file_path
                # 使用路径比较（而非 inode/samefile），
                # 因为编辑器原子保存（rename over）会改变 inode
                if file_path.resolve() != _target_resolved:
                    return
                # 短暂延迟，等待文件写入完成
                import time as _time
                _time.sleep(0.5)
                changed = self.snapshots.record_file_snapshot(file_path)
                if changed:
                    # 重新读取版本列表，广播完整快照信息
                    versions_data = [v.to_dict() for v in self.snapshots.get_all_versions()]
                    new_version = f"V{len(self.snapshots._file_history) - 1}"
                    _sse_manager.broadcast(
                        "file_changed",
                        json.dumps({
                            "file": str(file_path),
                            "event": event.event_type,
                            "new_version": new_version,
                            "versions": versions_data,
                        }, ensure_ascii=False),
                    )
        else:
            # 流水线模式：监听整个工作目录
            watch_dir = self.work_dir
            def on_file_change(event: FileChangeEvent) -> None:
                changed = self.snapshots.on_file_changed(event.file_path)
                if changed:
                    _sse_manager.broadcast(
                        "file_changed",
                        json.dumps({
                            "file": str(event.file_path),
                            "event": event.event_type,
                        }, ensure_ascii=False),
                    )

        self.watcher = FileWatcher(watch_dir, on_file_change)
        self.watcher.start()

    def stop(self) -> None:
        """停止服务器（释放端口、停止文件监听）。"""
        if self.watcher:
            self.watcher.stop()
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def wait(self) -> None:
        """等待服务器停止。按 Ctrl+C 可安全退出。"""
        if self._thread:
            try:
                # 使用 while 轮询代替 join，确保 KeyboardInterrupt 能及时响应
                while self._thread.is_alive():
                    self._thread.join(timeout=0.5)
            except KeyboardInterrupt:
                self.stop()
