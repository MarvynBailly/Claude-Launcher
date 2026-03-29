#!/usr/bin/env python3
"""
Claude Remote Control Dashboard
A web interface for managing Claude Code remote-control sessions.
Run on your always-on desktop, access from phone/laptop via Tailscale.
"""

import asyncio
import hmac
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("claude-dashboard")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CONFIG_PATH = Path(__file__).parent / "config.json"
DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 7878,
    "api_key": "",
    "permission_mode": "default",
    "auto_trust_directories": False,
    "pinned_dirs": [],
    "browse_root": str(Path.home()),
    "claude_binary": "claude",
    "git_binary": "git",
    "git_log_count": 20,
    "git_show_remote_branches": True,
    "cors_origins": [],
}

VALID_PERMISSION_MODES = {"default", "plan", "autoApprove", "bypassPermissions"}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                user = json.load(f)
            merged = {**DEFAULT_CONFIG, **user}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load config.json: %s — using defaults", e)
            return DEFAULT_CONFIG.copy()
    else:
        merged = DEFAULT_CONFIG.copy()

    # Validate config values
    if not isinstance(merged.get("port"), int) or not (1 <= merged["port"] <= 65535):
        logger.warning("Invalid port %r, using default 7878", merged.get("port"))
        merged["port"] = 7878

    if not isinstance(merged.get("host"), str):
        merged["host"] = "127.0.0.1"

    perm = merged.get("permission_mode", "default")
    if perm not in VALID_PERMISSION_MODES:
        logger.warning("Invalid permission_mode %r, using 'default'", perm)
        merged["permission_mode"] = "default"

    api_key = merged.get("api_key", "")
    if api_key and len(api_key) < 8:
        logger.warning("api_key is very short (%d chars) — consider using a longer key", len(api_key))

    if not isinstance(merged.get("pinned_dirs"), list):
        merged["pinned_dirs"] = []

    return merged


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


config = load_config()

# ---------------------------------------------------------------------------
# Input validation helpers
# ---------------------------------------------------------------------------
_SAFE_BRANCH_RE = re.compile(r"^[a-zA-Z0-9._\-/]+$")
_MAX_BRANCH_LEN = 255
_MAX_COMMIT_MSG_LEN = 5000


def _validate_branch_name(name: str) -> str:
    name = name.strip()
    if not name or len(name) > _MAX_BRANCH_LEN:
        raise HTTPException(400, "Invalid branch name length")
    if not _SAFE_BRANCH_RE.match(name):
        raise HTTPException(400, "Branch name contains invalid characters")
    if name.startswith("-") or ".." in name or name.endswith(".lock"):
        raise HTTPException(400, "Invalid branch name")
    return name


def _validate_git_ref(ref: str) -> str:
    ref = ref.strip()
    if not ref or len(ref) > _MAX_BRANCH_LEN:
        raise HTTPException(400, "Invalid git ref length")
    if ref.startswith("-"):
        raise HTTPException(400, "Invalid git ref")
    if ".." in ref:
        raise HTTPException(400, "Invalid git ref")
    return ref


def _validate_commit_message(msg: str) -> str:
    msg = msg.strip()
    if not msg:
        raise HTTPException(400, "Commit message cannot be empty")
    if len(msg) > _MAX_COMMIT_MSG_LEN:
        raise HTTPException(400, f"Commit message too long (max {_MAX_COMMIT_MSG_LEN} chars)")
    return msg


def _validate_file_paths(files: list[str]) -> list[str]:
    validated = []
    for f in files:
        f = f.strip()
        if not f:
            continue
        if f.startswith("-"):
            raise HTTPException(400, f"Invalid file path: {f}")
        parts = Path(f).parts
        if ".." in parts:
            raise HTTPException(400, f"Invalid file path: {f}")
        validated.append(f)
    return validated


def _validate_directory(directory: str) -> str:
    """Resolve directory and ensure it's within browse_root or pinned dirs."""
    d = os.path.expanduser(directory)
    resolved = Path(d).resolve()

    root = Path(os.path.expanduser(config.get("browse_root", "~"))).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        # Allow pinned directories even if outside browse_root
        pinned_resolved = {str(Path(p).resolve()) for p in config.get("pinned_dirs", [])}
        if str(resolved) not in pinned_resolved:
            logger.warning("Directory access denied (outside scope): %s", resolved)
            raise HTTPException(403, "Directory is outside allowed scope")

    if not resolved.is_dir():
        raise HTTPException(400, f"Not a valid directory: {d}")
    return str(resolved)


# ---------------------------------------------------------------------------
# Session tracking
# ---------------------------------------------------------------------------

class SessionInfo:
    def __init__(self, directory: str, process: asyncio.subprocess.Process, name: Optional[str] = None):
        self.directory = directory
        self.process = process
        self.name = name
        self.url: Optional[str] = None
        self.started_at: float = time.time()
        self.output_lines: list[str] = []
        self.status: str = "starting"  # starting | active | stopped | error

    def to_dict(self) -> dict:
        return {
            "directory": self.directory,
            "name": self.name,
            "url": self.url,
            "started_at": self.started_at,
            "status": self.status,
            "output_tail": self.output_lines[-30:],
            "pid": self.process.pid if self.process.returncode is None else None,
        }


sessions: dict[str, SessionInfo] = {}  # keyed by directory

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="Claude Remote Dashboard")

# CORS middleware
cors_origins = config.get("cors_origins", [])
if not cors_origins:
    port = config.get("port", 7878)
    cors_origins = [f"http://localhost:{port}", f"http://127.0.0.1:{port}"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# --- Authentication middleware ---
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    api_key = config.get("api_key", "")
    if not api_key:
        return await call_next(request)

    # Exempt frontend routes from auth
    path = request.url.path
    if path in ("/", "/favicon.ico"):
        return await call_next(request)

    # Check Authorization header
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        provided = auth_header[7:]
        if hmac.compare_digest(provided, api_key):
            return await call_next(request)

    logger.warning("Auth failed from %s for %s", request.client.host if request.client else "unknown", path)
    return JSONResponse(status_code=401, content={"ok": False, "message": "Unauthorized — provide a valid API key"})


# --- Models ---
class StartRequest(BaseModel):
    directory: str
    name: Optional[str] = None


class BrowseRequest(BaseModel):
    path: str


class PinRequest(BaseModel):
    directory: str


class GitInfoRequest(BaseModel):
    directory: str


class GitCreateBranchRequest(BaseModel):
    directory: str
    branch: str
    from_ref: Optional[str] = None


class GitAddRequest(BaseModel):
    directory: str
    files: Optional[list[str]] = None


class GitCommitRequest(BaseModel):
    directory: str
    message: str


class GitPushPullRequest(BaseModel):
    directory: str


# --- Session management ---
URL_PATTERN = re.compile(r"(https://claude\.ai/code[^\s\x1b]+)")


async def _read_output(session: SessionInfo):
    """Background task: read stdout from the claude process and extract URL."""
    try:
        while True:
            if session.status == "stopped":
                break
            line = await session.process.stdout.readline()
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace").strip()
            if decoded:
                session.output_lines.append(decoded)
                if len(session.output_lines) > 200:
                    session.output_lines = session.output_lines[-100:]
                m = URL_PATTERN.search(decoded)
                if m and session.url is None:
                    session.url = m.group(1)
                    session.status = "active"
                    logger.info("Session URL ready for %s: %s", session.directory, session.url)
    except asyncio.CancelledError:
        pass
    finally:
        if session.status != "stopped" and session.process.returncode is not None:
            session.status = "stopped"


async def _read_stderr(session: SessionInfo):
    """Background task: read stderr and append to output."""
    try:
        while True:
            if session.status == "stopped":
                break
            line = await session.process.stderr.readline()
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace").strip()
            if decoded:
                session.output_lines.append(f"[stderr] {decoded}")
                if len(session.output_lines) > 200:
                    session.output_lines = session.output_lines[-100:]
                m = URL_PATTERN.search(decoded)
                if m and session.url is None:
                    session.url = m.group(1)
                    session.status = "active"
    except asyncio.CancelledError:
        pass


def _ensure_trusted(directory: str):
    """Pre-trust a directory in Claude's config so remote-control skips the trust dialog.
    Only called when auto_trust_directories is enabled in config."""
    claude_json = Path.home() / ".claude.json"
    try:
        if claude_json.exists():
            with open(claude_json) as f:
                data = json.load(f)
        else:
            data = {}
        projects = data.setdefault("projects", {})
        key = directory.replace("\\", "/")
        project = projects.setdefault(key, {})
        if not project.get("hasTrustDialogAccepted"):
            project["hasTrustDialogAccepted"] = True
            with open(claude_json, "w") as f:
                json.dump(data, f, indent=2)
            logger.info("Auto-trusted directory: %s", directory)
    except (json.JSONDecodeError, OSError):
        pass


@app.post("/api/sessions/start")
async def start_session(req: StartRequest):
    d = _validate_directory(req.directory)
    if d in sessions and sessions[d].status in ("starting", "active"):
        return JSONResponse({"ok": True, "session": sessions[d].to_dict(), "already_running": True})

    if config.get("auto_trust_directories", False):
        _ensure_trusted(d)

    claude_bin = config.get("claude_binary", "claude")
    kwargs = dict(
        cwd=d,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["preexec_fn"] = os.setsid

    cmd = [claude_bin, "remote-control"]
    perm_mode = config.get("permission_mode", "default")
    if perm_mode != "default":
        cmd.extend(["--permission-mode", perm_mode])
    if req.name:
        cmd.extend(["--name", req.name])

    logger.info("Starting session in %s (permission_mode=%s)", d, perm_mode)
    proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)
    proc.stdin.write(b"y\n")
    await proc.stdin.drain()
    session = SessionInfo(directory=d, process=proc, name=req.name)
    sessions[d] = session
    asyncio.create_task(_read_output(session))
    asyncio.create_task(_read_stderr(session))
    return {"ok": True, "session": session.to_dict()}


@app.post("/api/sessions/stop")
async def stop_session(req: StartRequest):
    d = os.path.expanduser(req.directory)
    if d not in sessions:
        raise HTTPException(404, "No session for that directory")
    session = sessions[d]
    session.status = "stopped"
    logger.info("Stopping session in %s", d)
    if session.process.returncode is None:
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(session.process.pid)],
                    capture_output=True,
                )
            else:
                os.killpg(os.getpgid(session.process.pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        await asyncio.sleep(0.5)
        if session.process.returncode is None:
            try:
                if sys.platform != "win32":
                    os.killpg(os.getpgid(session.process.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
    return {"ok": True, "message": "Session stopped"}


@app.get("/api/sessions")
async def list_sessions():
    for s in sessions.values():
        if s.process.returncode is not None and s.status not in ("stopped", "error"):
            s.status = "stopped"
    return {d: s.to_dict() for d, s in sessions.items()}


@app.get("/api/sessions/{directory:path}")
async def get_session(directory: str):
    d = os.path.expanduser("/" + directory)
    if d not in sessions:
        raise HTTPException(404, "No session found")
    s = sessions[d]
    if s.process.returncode is not None and s.status not in ("stopped", "error"):
        s.status = "stopped"
    return s.to_dict()


# --- Filesystem browsing ---
@app.post("/api/browse")
async def browse_directory(req: BrowseRequest):
    p = Path(os.path.expanduser(req.path)).resolve()
    browse_root = Path(os.path.expanduser(config.get("browse_root", "~"))).resolve()
    try:
        p.relative_to(browse_root)
    except ValueError:
        p = browse_root

    if not p.is_dir():
        raise HTTPException(400, "Not a directory")

    entries = []
    try:
        for child in sorted(p.iterdir()):
            if child.name.startswith("."):
                continue
            if child.is_dir():
                is_project = any(
                    (child / marker).exists()
                    for marker in [
                        ".git", "package.json", "Cargo.toml", "pyproject.toml",
                        "go.mod", "Makefile", "CLAUDE.md", ".claude",
                    ]
                )
                entries.append({
                    "name": child.name,
                    "path": str(child),
                    "type": "dir",
                    "is_project": is_project,
                })
    except PermissionError:
        raise HTTPException(403, "Permission denied")

    return {
        "current": str(p),
        "parent": str(p.parent) if p != browse_root else None,
        "entries": entries,
    }


# --- Pinned directories ---
@app.get("/api/pinned")
async def get_pinned():
    return config.get("pinned_dirs", [])


@app.post("/api/pinned/add")
async def add_pinned(req: PinRequest):
    d = os.path.expanduser(req.directory)
    if d not in config["pinned_dirs"]:
        config["pinned_dirs"].append(d)
        save_config(config)
    return {"ok": True, "pinned_dirs": config["pinned_dirs"]}


@app.post("/api/pinned/remove")
async def remove_pinned(req: PinRequest):
    d = os.path.expanduser(req.directory)
    config["pinned_dirs"] = [x for x in config["pinned_dirs"] if x != d]
    save_config(config)
    return {"ok": True, "pinned_dirs": config["pinned_dirs"]}


# --- Git helpers ---
async def _run_git(directory: str, *args: str, timeout: float = 10.0) -> tuple[int, str, str]:
    git_bin = config.get("git_binary", "git")
    proc = await asyncio.create_subprocess_exec(
        git_bin, *args,
        cwd=directory,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return (-1, "", "git command timed out")
    return (proc.returncode, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace"))


@app.post("/api/git-info")
async def git_info(req: GitInfoRequest):
    d = _validate_directory(req.directory)

    rc, _, _ = await _run_git(d, "rev-parse", "--git-dir")
    if rc != 0:
        return {"is_git_repo": False}

    log_count = config.get("git_log_count", 20)
    show_remote = config.get("git_show_remote_branches", True)

    branch_args = ["branch", "--format=%(refname:short)|%(HEAD)|%(upstream:short)|%(upstream:track)"]
    if show_remote:
        branch_args.insert(1, "-a")

    results = await asyncio.gather(
        _run_git(d, *branch_args),
        _run_git(d, "status", "--porcelain=v2", "--branch"),
        _run_git(d, "log", f"--max-count={log_count}", "--all",
                 "--format=%h|%an|%ar|%s|%D"),
        _run_git(d, "rev-parse", "--abbrev-ref", "HEAD"),
        _run_git(d, "rev-parse", "--short", "HEAD"),
        _run_git(d, "log", f"--max-count={log_count}", "--all",
                 "--graph", "--oneline", "--decorate", "--color=never"),
    )

    branches_result, status_result, log_result, head_result, head_short, graph_result = results

    current_branch = head_result[1].strip() if head_result[0] == 0 else "unknown"
    head_hash = head_short[1].strip() if head_short[0] == 0 else ""

    branches = []
    if branches_result[0] == 0:
        for line in branches_result[1].strip().splitlines():
            parts = line.split("|")
            if len(parts) >= 1:
                name = parts[0].strip()
                if not name or "HEAD" in name:
                    continue
                branches.append({
                    "name": name,
                    "is_current": parts[1].strip() == "*" if len(parts) > 1 else False,
                    "upstream": parts[2].strip() or None if len(parts) > 2 else None,
                    "track": parts[3].strip() or None if len(parts) > 3 else None,
                })

    dirty_files = []
    ahead = 0
    behind = 0
    if status_result[0] == 0:
        for line in status_result[1].splitlines():
            if line.startswith("# branch.ab"):
                parts = line.split()
                if len(parts) > 2:
                    ahead = int(parts[2].lstrip("+"))
                if len(parts) > 3:
                    behind = abs(int(parts[3]))
            elif line and not line.startswith("#"):
                dirty_files.append(line)

    commits = []
    if log_result[0] == 0:
        for line in log_result[1].strip().splitlines():
            parts = line.split("|", 4)
            if len(parts) >= 5:
                commits.append({
                    "hash": parts[0],
                    "author": parts[1],
                    "time": parts[2],
                    "message": parts[3],
                    "refs": parts[4] if parts[4] else None,
                })

    return {
        "is_git_repo": True,
        "current_branch": current_branch,
        "head_hash": head_hash,
        "is_detached": current_branch == "HEAD",
        "branches": branches,
        "ahead": ahead,
        "behind": behind,
        "is_clean": len(dirty_files) == 0,
        "dirty_file_count": len(dirty_files),
        "commits": commits,
        "graph": graph_result[1] if graph_result[0] == 0 else "",
    }


@app.post("/api/git/create-branch")
async def git_create_branch(req: GitCreateBranchRequest):
    d = _validate_directory(req.directory)
    branch = _validate_branch_name(req.branch)
    args = ["checkout", "-b", branch, "--"]
    if req.from_ref:
        ref = _validate_git_ref(req.from_ref)
        args = ["checkout", "-b", branch, ref, "--"]
    rc, out, err = await _run_git(d, *args)
    if rc != 0:
        return {"ok": False, "message": err.strip() or out.strip()}
    return {"ok": True, "message": f"Created and switched to '{branch}'"}


@app.post("/api/git/add")
async def git_add(req: GitAddRequest):
    d = _validate_directory(req.directory)
    if req.files:
        validated = _validate_file_paths(req.files)
        rc, out, err = await _run_git(d, "add", "--", *validated)
    else:
        rc, out, err = await _run_git(d, "add", "--all")
    if rc != 0:
        return {"ok": False, "message": err.strip() or out.strip()}
    return {"ok": True, "message": "Files staged"}


@app.post("/api/git/commit")
async def git_commit(req: GitCommitRequest):
    d = _validate_directory(req.directory)
    message = _validate_commit_message(req.message)
    rc, out, err = await _run_git(d, "commit", "-m", message)
    if rc != 0:
        return {"ok": False, "message": err.strip() or out.strip()}
    return {"ok": True, "message": out.strip().split('\n')[0]}


@app.post("/api/git/push")
async def git_push(req: GitPushPullRequest):
    d = _validate_directory(req.directory)
    rc, out, err = await _run_git(d, "push", timeout=30.0)
    if rc != 0:
        if "no upstream branch" in err or "has no upstream" in err:
            rc2, branch, _ = await _run_git(d, "rev-parse", "--abbrev-ref", "HEAD")
            if rc2 == 0:
                rc, out, err = await _run_git(d, "push", "-u", "origin", branch.strip(), timeout=30.0)
        if rc != 0:
            return {"ok": False, "message": err.strip() or out.strip()}
    return {"ok": True, "message": "Pushed successfully"}


@app.post("/api/git/pull")
async def git_pull(req: GitPushPullRequest):
    d = _validate_directory(req.directory)
    rc, out, err = await _run_git(d, "pull", timeout=30.0)
    if rc != 0:
        return {"ok": False, "message": err.strip() or out.strip()}
    return {"ok": True, "message": out.strip().split('\n')[0] or "Up to date"}


# --- Frontend ---
@app.get("/favicon.ico")
async def favicon():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40">'
        '<rect width="40" height="40" rx="10" fill="#e8784e"/>'
        '<text x="20" y="27" text-anchor="middle" font-family="monospace" '
        'font-weight="700" font-size="18" fill="#0c0e12">RC</text>'
        '</svg>'
    )
    return HTMLResponse(content=svg, media_type="image/svg+xml")


@app.get("/", response_class=HTMLResponse)
async def index():
    return FRONTEND_HTML


# ---------------------------------------------------------------------------
# Frontend HTML (single-page app)
# ---------------------------------------------------------------------------
FRONTEND_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>Claude Remote Dashboard</title>
<link rel="icon" href="/favicon.ico" type="image/svg+xml">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0c0e12;
    --surface: #13161c;
    --surface2: #1a1e26;
    --border: #252a35;
    --border-hi: #3a4255;
    --text: #d4d8e0;
    --text-dim: #6b7280;
    --text-bright: #f0f2f5;
    --accent: #e8784e;
    --accent-glow: #e8784e33;
    --green: #34d399;
    --green-dim: #34d39944;
    --yellow: #fbbf24;
    --yellow-dim: #fbbf2444;
    --red: #f87171;
    --red-dim: #f8717144;
    --blue: #60a5fa;
    --radius: 10px;
    --mono: 'JetBrains Mono', monospace;
    --sans: 'DM Sans', system-ui, sans-serif;
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    font-family: var(--sans);
    background: var(--bg);
    color: var(--text);
    min-height: 100dvh;
    -webkit-font-smoothing: antialiased;
  }

  /* --- Layout --- */
  .app {
    max-width: 720px;
    margin: 0 auto;
    padding: 24px 16px 80px;
  }

  header {
    display: flex;
    align-items: center;
    gap: 14px;
    margin-bottom: 32px;
    padding-bottom: 20px;
    border-bottom: 1px solid var(--border);
  }

  header .logo {
    width: 40px; height: 40px;
    background: var(--accent);
    border-radius: 10px;
    display: grid; place-items: center;
    font-family: var(--mono);
    font-weight: 700;
    font-size: 18px;
    color: var(--bg);
    flex-shrink: 0;
  }

  header h1 {
    font-size: 20px;
    font-weight: 700;
    color: var(--text-bright);
    letter-spacing: -0.3px;
  }

  header p {
    font-size: 13px;
    color: var(--text-dim);
    margin-top: 2px;
  }

  /* --- Sections --- */
  .section { margin-bottom: 28px; }

  .section-title {
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    color: var(--text-dim);
    margin-bottom: 12px;
    padding-left: 2px;
  }

  /* --- Cards --- */
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 14px 16px;
    margin-bottom: 8px;
    transition: border-color 0.15s;
  }
  .card:hover { border-color: var(--border-hi); }

  .card-row {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .card-icon {
    width: 36px; height: 36px;
    border-radius: 8px;
    display: grid; place-items: center;
    flex-shrink: 0;
    font-size: 16px;
  }

  .card-body { flex: 1; min-width: 0; }

  .card-name {
    font-weight: 600;
    font-size: 14px;
    color: var(--text-bright);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .card-path {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--text-dim);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    margin-top: 2px;
  }

  /* --- Buttons --- */
  .btn {
    font-family: var(--sans);
    font-size: 13px;
    font-weight: 600;
    border: none;
    border-radius: 8px;
    padding: 8px 16px;
    cursor: pointer;
    transition: all 0.15s;
    white-space: nowrap;
    flex-shrink: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    min-height: 36px;
  }

  .btn-primary { background: var(--accent); color: #fff; }
  .btn-primary:hover { filter: brightness(1.15); }
  .btn-primary:disabled { opacity: 0.4; cursor: default; filter: none; }

  .btn-ghost { background: transparent; color: var(--text-dim); padding: 8px 10px; }
  .btn-ghost:hover { color: var(--text); background: var(--surface2); }

  .btn-danger { background: var(--red-dim); color: var(--red); }
  .btn-danger:hover { background: var(--red); color: #fff; }

  .btn-sm { padding: 6px 12px; font-size: 12px; min-height: 32px; }

  .btn:focus-visible, .tab:focus-visible, .copy-btn:focus-visible, .pin-btn:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }

  /* --- Status badges --- */
  .badge {
    font-family: var(--mono);
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    padding: 3px 8px;
    border-radius: 6px;
    flex-shrink: 0;
  }
  .badge-active { background: var(--green-dim); color: var(--green); }
  .badge-starting { background: var(--yellow-dim); color: var(--yellow); }
  .badge-stopped { background: var(--red-dim); color: var(--red); }

  /* --- Session URL --- */
  .session-url {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-top: 10px;
    padding: 10px 12px;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
  }
  .session-url a {
    font-family: var(--mono);
    font-size: 12px;
    color: var(--blue);
    text-decoration: none;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1;
  }
  .session-url a:hover { text-decoration: underline; }

  .copy-btn {
    background: var(--surface2);
    border: 1px solid var(--border);
    color: var(--text-dim);
    font-size: 11px;
    padding: 4px 10px;
    border-radius: 6px;
    cursor: pointer;
    font-family: var(--mono);
    flex-shrink: 0;
    min-height: 28px;
  }
  .copy-btn:hover { color: var(--text); border-color: var(--border-hi); }

  /* --- Browser --- */
  .browser-bar {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 10px;
  }
  .browser-path {
    flex: 1;
    font-family: var(--mono);
    font-size: 12px;
    padding: 8px 12px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text);
    outline: none;
    min-height: 36px;
  }
  .browser-path:focus { border-color: var(--accent); }

  .dir-entry {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 14px;
    cursor: pointer;
    border-bottom: 1px solid var(--border);
    transition: background 0.1s;
    min-height: 44px;
  }
  .dir-entry:last-child { border-bottom: none; }
  .dir-entry:hover { background: var(--surface2); }

  .dir-entry .icon { font-size: 16px; flex-shrink: 0; width: 20px; text-align: center; }
  .dir-entry .name {
    font-size: 13px;
    font-weight: 500;
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .dir-entry .project-tag {
    font-family: var(--mono);
    font-size: 9px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    color: var(--accent);
    background: var(--accent-glow);
    padding: 2px 7px;
    border-radius: 4px;
    flex-shrink: 0;
  }

  .dir-list {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
    max-height: 400px;
    overflow-y: auto;
  }

  /* --- Output log --- */
  .log-toggle {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--text-dim);
    cursor: pointer;
    margin-top: 8px;
    user-select: none;
  }
  .log-toggle:hover { color: var(--text); }

  .log-box {
    margin-top: 8px;
    padding: 10px;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    font-family: var(--mono);
    font-size: 11px;
    color: var(--text-dim);
    max-height: 200px;
    overflow-y: auto;
    white-space: pre-wrap;
    word-break: break-all;
    line-height: 1.6;
  }

  /* --- Empty state --- */
  .empty {
    text-align: center;
    padding: 32px 16px;
    color: var(--text-dim);
    font-size: 13px;
  }
  .empty .hint {
    font-family: var(--mono);
    font-size: 11px;
    margin-top: 8px;
    color: var(--text-dim);
    opacity: 0.6;
  }

  /* --- Tabs --- */
  .tabs {
    display: flex;
    gap: 4px;
    margin-bottom: 20px;
    background: var(--surface);
    padding: 4px;
    border-radius: 10px;
    border: 1px solid var(--border);
  }
  .tab {
    flex: 1;
    text-align: center;
    padding: 9px 0;
    font-size: 13px;
    font-weight: 600;
    color: var(--text-dim);
    border-radius: 7px;
    cursor: pointer;
    transition: all 0.15s;
    border: none;
    background: none;
    font-family: var(--sans);
    min-height: 36px;
  }
  .tab:hover { color: var(--text); }
  .tab.active {
    background: var(--surface2);
    color: var(--text-bright);
    box-shadow: 0 1px 3px rgba(0,0,0,0.3);
  }
  .tab-content { display: none; }
  .tab-content.active { display: block; }

  /* --- Misc --- */
  .pin-btn {
    background: none;
    border: none;
    cursor: pointer;
    font-size: 16px;
    padding: 4px 8px;
    opacity: 0.4;
    transition: opacity 0.15s;
    flex-shrink: 0;
    min-width: 32px;
    min-height: 32px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }
  .pin-btn:hover { opacity: 1; }
  .pin-btn.pinned { opacity: 1; }

  .spinner {
    display: inline-block;
    width: 14px; height: 14px;
    border: 2px solid var(--text-dim);
    border-top-color: transparent;
    border-radius: 50%;
    animation: spin 0.6s linear infinite;
    vertical-align: middle;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* --- Git panel --- */
  .git-toggle {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--text-dim);
    cursor: pointer;
    margin-top: 8px;
    user-select: none;
  }
  .git-toggle:hover { color: var(--text); }

  .git-panel {
    margin-top: 8px;
    padding: 12px;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    font-size: 12px;
  }

  .git-summary {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    margin-bottom: 10px;
  }

  .git-branch-name {
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 600;
    color: var(--blue);
  }

  .git-badge {
    font-family: var(--mono);
    font-size: 9px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    padding: 2px 7px;
    border-radius: 4px;
  }
  .git-badge-clean { background: var(--green-dim); color: var(--green); }
  .git-badge-dirty { background: var(--yellow-dim); color: var(--yellow); }
  .git-badge-ahead { background: var(--green-dim); color: var(--green); }
  .git-badge-behind { background: var(--red-dim); color: var(--red); }

  .git-section-title {
    font-family: var(--mono);
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--text-dim);
    margin: 10px 0 6px;
  }

  .git-branch-list {
    list-style: none;
    font-family: var(--mono);
    font-size: 11px;
    color: var(--text-dim);
    max-height: 120px;
    overflow-y: auto;
  }
  .git-branch-list li {
    padding: 3px 0;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .git-branch-list .current { color: var(--blue); font-weight: 600; }
  .git-branch-list .track { font-size: 10px; color: var(--text-dim); opacity: 0.7; }
  .git-branch-list .remote { color: var(--text-dim); opacity: 0.6; }

  .git-commits { max-height: 250px; overflow-y: auto; }

  .git-commit {
    display: flex;
    align-items: baseline;
    gap: 8px;
    padding: 4px 0;
    border-bottom: 1px solid var(--border);
    font-size: 11px;
  }
  .git-commit:last-child { border-bottom: none; }
  .git-commit .hash { font-family: var(--mono); color: var(--yellow); flex-shrink: 0; }
  .git-commit .msg {
    flex: 1; min-width: 0;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    color: var(--text);
  }
  .git-commit .refs {
    font-family: var(--mono); font-size: 9px;
    color: var(--accent); background: var(--accent-glow);
    padding: 1px 5px; border-radius: 3px;
    flex-shrink: 0; white-space: nowrap;
  }
  .git-commit .meta {
    font-family: var(--mono); font-size: 10px;
    color: var(--text-dim); flex-shrink: 0; white-space: nowrap;
  }

  .git-inline {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    margin-top: 3px;
  }
  .git-inline .branch { font-family: var(--mono); font-size: 10px; color: var(--blue); }
  .git-inline .status-dot {
    width: 6px; height: 6px;
    border-radius: 50%;
    flex-shrink: 0;
  }
  .git-inline .status-dot.clean { background: var(--green); }
  .git-inline .status-dot.dirty { background: var(--yellow); }

  .git-actions {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    margin-bottom: 10px;
    padding-bottom: 10px;
    border-bottom: 1px solid var(--border);
  }

  .git-graph {
    font-family: var(--mono);
    font-size: 11px;
    line-height: 1.5;
    color: var(--text);
    max-height: 300px;
    overflow-y: auto;
    overflow-x: auto;
    white-space: pre;
    margin: 0;
    padding: 8px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
  }
  .graph-line { color: var(--blue); }
  .graph-hash { color: var(--yellow); }
  .graph-refs { color: var(--accent); font-weight: 600; }
  .graph-msg { color: var(--text); }

  .git-view-tabs {
    display: flex;
    gap: 2px;
    margin-bottom: 8px;
    background: var(--surface);
    padding: 2px;
    border-radius: 6px;
    border: 1px solid var(--border);
  }
  .git-view-tab {
    flex: 1;
    text-align: center;
    padding: 5px 0;
    font-size: 11px;
    font-weight: 600;
    font-family: var(--mono);
    color: var(--text-dim);
    border-radius: 4px;
    cursor: pointer;
    border: none;
    background: none;
    min-height: 28px;
  }
  .git-view-tab:hover { color: var(--text); }
  .git-view-tab.active { background: var(--surface2); color: var(--text-bright); }

  /* Scrollbar */
  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
  ::-webkit-scrollbar-thumb:hover { background: var(--border-hi); }

  /* --- Toast notifications --- */
  .toast-container {
    position: fixed;
    bottom: 24px;
    right: 24px;
    z-index: 10000;
    display: flex;
    flex-direction: column-reverse;
    gap: 8px;
    pointer-events: none;
    max-width: 380px;
  }
  .toast {
    padding: 12px 16px;
    border-radius: 8px;
    font-size: 13px;
    font-weight: 500;
    pointer-events: auto;
    animation: toastIn 0.25s ease-out;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    box-shadow: 0 4px 16px rgba(0,0,0,0.4);
    display: flex;
    align-items: center;
    gap: 8px;
    word-break: break-word;
  }
  .toast.success { border-color: var(--green); color: var(--green); }
  .toast.error { border-color: var(--red); color: var(--red); }
  .toast.info { border-color: var(--blue); color: var(--blue); }
  .toast-icon { flex-shrink: 0; font-size: 16px; }
  .toast.removing { animation: toastOut 0.2s ease-in forwards; }
  @keyframes toastIn { from { opacity: 0; transform: translateY(12px); } to { opacity: 1; transform: translateY(0); } }
  @keyframes toastOut { from { opacity: 1; transform: translateY(0); } to { opacity: 0; transform: translateY(12px); } }

  /* --- Modal --- */
  .modal-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.6);
    z-index: 9000;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 16px;
    animation: fadeIn 0.15s ease-out;
  }
  .modal {
    background: var(--surface);
    border: 1px solid var(--border-hi);
    border-radius: 12px;
    padding: 24px;
    width: 100%;
    max-width: 400px;
    box-shadow: 0 8px 32px rgba(0,0,0,0.5);
    animation: modalIn 0.2s ease-out;
  }
  .modal h3 {
    font-size: 16px;
    font-weight: 700;
    color: var(--text-bright);
    margin-bottom: 12px;
  }
  .modal p {
    font-size: 13px;
    color: var(--text);
    margin-bottom: 16px;
    line-height: 1.5;
  }
  .modal input[type="text"], .modal input[type="password"] {
    width: 100%;
    padding: 10px 12px;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text);
    font-family: var(--mono);
    font-size: 13px;
    margin-bottom: 16px;
    outline: none;
    min-height: 40px;
  }
  .modal input:focus { border-color: var(--accent); }
  .modal-actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
  }
  @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
  @keyframes modalIn { from { opacity: 0; transform: scale(0.95); } to { opacity: 1; transform: scale(1); } }

  /* --- Login overlay --- */
  .login-overlay {
    position: fixed;
    inset: 0;
    background: var(--bg);
    z-index: 9999;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 16px;
  }
  .login-box {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 32px;
    width: 100%;
    max-width: 360px;
    text-align: center;
  }
  .login-box .logo {
    width: 56px; height: 56px;
    background: var(--accent);
    border-radius: 14px;
    display: inline-grid; place-items: center;
    font-family: var(--mono);
    font-weight: 700;
    font-size: 24px;
    color: var(--bg);
    margin-bottom: 16px;
  }
  .login-box h2 {
    font-size: 18px;
    font-weight: 700;
    color: var(--text-bright);
    margin-bottom: 4px;
  }
  .login-box .subtitle {
    font-size: 13px;
    color: var(--text-dim);
    margin-bottom: 24px;
  }
  .login-box input {
    width: 100%;
    padding: 12px 14px;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text);
    font-family: var(--mono);
    font-size: 13px;
    margin-bottom: 12px;
    outline: none;
    text-align: center;
    min-height: 44px;
  }
  .login-box input:focus { border-color: var(--accent); }
  .login-error {
    color: var(--red);
    font-size: 12px;
    margin-bottom: 12px;
    display: none;
  }

  /* --- Search input --- */
  .search-input {
    width: 100%;
    padding: 8px 12px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text);
    font-family: var(--sans);
    font-size: 13px;
    outline: none;
    margin-bottom: 12px;
    min-height: 36px;
  }
  .search-input:focus { border-color: var(--accent); }
  .search-input::placeholder { color: var(--text-dim); }

  /* Responsive */
  @media (max-width: 768px) {
    .app { padding: 20px 14px 80px; }
    .git-commit .meta { display: none; }
  }
  @media (max-width: 480px) {
    .app { padding: 16px 12px 80px; }
    header h1 { font-size: 17px; }
    .card { padding: 12px; }
    .btn { padding: 10px 14px; min-height: 44px; }
    .btn-sm { padding: 8px 12px; min-height: 40px; }
    .tab { padding: 12px 0; min-height: 44px; }
    .dir-entry { padding: 12px 14px; min-height: 48px; }
    .toast-container { left: 16px; right: 16px; bottom: 16px; max-width: none; }
  }

  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; }
  }
</style>
</head>
<body>

<!-- Login overlay (shown when API key auth is enabled) -->
<div class="login-overlay" id="login-overlay" style="display:none">
  <div class="login-box">
    <div class="logo">RC</div>
    <h2>Remote Dashboard</h2>
    <p class="subtitle">Enter your API key to continue</p>
    <input type="password" id="login-key" placeholder="API key" onkeydown="if(event.key==='Enter') doLogin()">
    <p class="login-error" id="login-error">Invalid API key</p>
    <button class="btn btn-primary" onclick="doLogin()" style="width:100%">Sign In</button>
  </div>
</div>

<div class="app" id="app">
  <header>
    <div class="logo">RC</div>
    <div>
      <h1>Remote Dashboard</h1>
      <p>Claude Code remote-control session manager</p>
    </div>
  </header>

  <nav class="tabs" role="tablist">
    <button class="tab active" data-tab="sessions" onclick="switchTab('sessions')" role="tab" aria-selected="true">Sessions</button>
    <button class="tab" data-tab="browse" onclick="switchTab('browse')" role="tab" aria-selected="false">Browse</button>
    <button class="tab" data-tab="pinned" onclick="switchTab('pinned')" role="tab" aria-selected="false">Projects</button>
  </nav>

  <!-- SESSIONS TAB -->
  <div class="tab-content active" id="tab-sessions" role="tabpanel" aria-live="polite">
    <div class="section">
      <div class="section-title">Active Sessions</div>
      <div id="sessions-list">
        <div class="empty">
          No sessions running<br>
          <span class="hint">Start one from Projects or Browse</span>
        </div>
      </div>
    </div>
  </div>

  <!-- BROWSE TAB -->
  <div class="tab-content" id="tab-browse" role="tabpanel">
    <div class="section">
      <div class="section-title">File Browser</div>
      <div class="browser-bar">
        <button class="btn btn-ghost btn-sm" onclick="browseUp()" aria-label="Parent directory">&#8593;</button>
        <input class="browser-path" id="browse-path" value="" onkeydown="if(event.key==='Enter') browseTo(this.value)" aria-label="Directory path">
        <button class="btn btn-primary btn-sm" onclick="browseTo(document.getElementById('browse-path').value)">Go</button>
      </div>
      <div id="browser-list" class="dir-list"></div>
    </div>
  </div>

  <!-- PINNED TAB -->
  <div class="tab-content" id="tab-pinned" role="tabpanel">
    <div class="section">
      <div class="section-title">Projects</div>
      <input class="search-input" id="pinned-search" placeholder="Filter projects..." oninput="filterPinned(this.value)" aria-label="Filter projects">
      <div id="pinned-list"></div>
    </div>
  </div>
</div>

<div class="toast-container" id="toast-container" aria-live="polite"></div>

<script>
// --- Auth ---
let authKey = localStorage.getItem('claude_dash_key') || '';
let authRequired = false;

function apiFetch(url, opts = {}) {
  if (!opts.headers) opts.headers = {};
  if (authKey) opts.headers['Authorization'] = 'Bearer ' + authKey;
  if (!opts.headers['Content-Type'] && opts.body) opts.headers['Content-Type'] = 'application/json';
  return fetch(url, opts).then(r => {
    if (r.status === 401) {
      authRequired = true;
      showLoginOverlay();
      throw new Error('Unauthorized');
    }
    return r;
  });
}

function showLoginOverlay() {
  document.getElementById('login-overlay').style.display = '';
  document.getElementById('app').style.display = 'none';
  document.getElementById('login-key').focus();
}

function hideLoginOverlay() {
  document.getElementById('login-overlay').style.display = 'none';
  document.getElementById('app').style.display = '';
}

async function doLogin() {
  const key = document.getElementById('login-key').value.trim();
  if (!key) return;
  authKey = key;
  try {
    const r = await fetch('/api/sessions', { headers: { 'Authorization': 'Bearer ' + key } });
    if (r.status === 401) {
      document.getElementById('login-error').style.display = 'block';
      authKey = '';
      return;
    }
    localStorage.setItem('claude_dash_key', key);
    document.getElementById('login-error').style.display = 'none';
    authRequired = false;
    hideLoginOverlay();
    init();
  } catch (e) {
    document.getElementById('login-error').style.display = 'block';
  }
}

// --- Toast notifications ---
function showToast(message, type = 'info', duration = 3500) {
  const container = document.getElementById('toast-container');
  const icons = { success: '\u2713', error: '\u2717', info: '\u2139' };
  const toast = document.createElement('div');
  toast.className = 'toast ' + type;
  toast.innerHTML = '<span class="toast-icon">' + (icons[type] || '') + '</span>' + esc(message);
  container.appendChild(toast);
  setTimeout(() => {
    toast.classList.add('removing');
    setTimeout(() => toast.remove(), 200);
  }, duration);
}

// --- Modal dialogs ---
function showPrompt(title, placeholder = '', defaultValue = '') {
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `<div class="modal">
      <h3>${esc(title)}</h3>
      <input type="text" id="modal-input" placeholder="${esc(placeholder)}" value="${esc(defaultValue)}">
      <div class="modal-actions">
        <button class="btn btn-ghost btn-sm" id="modal-cancel">Cancel</button>
        <button class="btn btn-primary btn-sm" id="modal-ok">OK</button>
      </div>
    </div>`;
    document.body.appendChild(overlay);
    const input = overlay.querySelector('#modal-input');
    input.focus();
    input.select();
    const close = (val) => { overlay.remove(); resolve(val); };
    overlay.querySelector('#modal-cancel').onclick = () => close(null);
    overlay.querySelector('#modal-ok').onclick = () => close(input.value);
    input.onkeydown = (e) => { if (e.key === 'Enter') close(input.value); if (e.key === 'Escape') close(null); };
    overlay.onclick = (e) => { if (e.target === overlay) close(null); };
  });
}

function showConfirm(message) {
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `<div class="modal">
      <p>${esc(message)}</p>
      <div class="modal-actions">
        <button class="btn btn-ghost btn-sm" id="modal-cancel">Cancel</button>
        <button class="btn btn-primary btn-sm" id="modal-ok">Confirm</button>
      </div>
    </div>`;
    document.body.appendChild(overlay);
    overlay.querySelector('#modal-ok').focus();
    const close = (val) => { overlay.remove(); resolve(val); };
    overlay.querySelector('#modal-cancel').onclick = () => close(false);
    overlay.querySelector('#modal-ok').onclick = () => close(true);
    overlay.onclick = (e) => { if (e.target === overlay) close(false); };
  });
}

// --- Loading button helper ---
async function withLoading(btn, asyncFn) {
  if (!btn) return asyncFn();
  const orig = btn.innerHTML;
  const origWidth = btn.offsetWidth;
  btn.style.minWidth = origWidth + 'px';
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>';
  try {
    return await asyncFn();
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
    btn.style.minWidth = '';
  }
}

// --- State ---
const API = '';
let currentBrowsePath = '';
let pinnedDirs = [];
let sessionsData = {};
let pollTimer = null;
let openLogs = {};
let openGit = {};
let gitCache = {};
let gitActiveView = {};

// --- Tab switching ---
function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => {
    const isActive = t.dataset.tab === name;
    t.classList.toggle('active', isActive);
    t.setAttribute('aria-selected', isActive);
  });
  document.querySelectorAll('.tab-content').forEach(c => c.classList.toggle('active', c.id === 'tab-' + name));
  if (name === 'browse' && !currentBrowsePath) initBrowser();
  if (name === 'pinned') loadPinned();
  if (name === 'sessions') refreshSessions();
}

// --- Sessions ---
async function refreshSessions() {
  try {
    const r = await apiFetch(API + '/api/sessions');
    sessionsData = await r.json();
    renderSessions();
  } catch (e) { if (!authRequired) console.error(e); }
}

function renderSessions() {
  const el = document.getElementById('sessions-list');
  const entries = Object.entries(sessionsData);
  if (entries.length === 0) {
    el.innerHTML = '<div class="empty">No sessions running<br><span class="hint">Start one from Projects or Browse</span></div>';
    return;
  }
  el.innerHTML = entries.map(([dir, s]) => {
    const dirName = dir.split(/[/\\]/).filter(Boolean).pop();
    const name = s.name || dirName;
    const badge = s.status === 'active' ? 'badge-active'
                : s.status === 'starting' ? 'badge-starting' : 'badge-stopped';
    const urlBlock = s.url ? `
      <div class="session-url">
        <a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.url)}</a>
        <button class="copy-btn" onclick="copyText('${jesc(s.url)}', this)" aria-label="Copy URL">copy</button>
      </div>` : (s.status === 'starting' ? `
      <div style="margin-top:10px;color:var(--text-dim);font-size:12px">
        <span class="spinner"></span>&ensp;Waiting for session URL...
      </div>` : '');
    const isOpen = !!openLogs[dir];
    return `
      <div class="card">
        <div class="card-row">
          <div class="card-icon" style="background:var(--green-dim);color:var(--green)">&#9654;</div>
          <div class="card-body">
            <div class="card-name">${esc(name)}</div>
            <div class="card-path">${esc(dir)}</div>
          </div>
          <span class="badge ${badge}">${esc(s.status)}</span>
          ${s.status !== 'stopped' ? `<button class="btn btn-danger btn-sm" onclick="stopSession('${jesc(dir)}', this)">Stop</button>` : `<button class="btn btn-primary btn-sm" onclick="startSession('${jesc(dir)}', this)">Restart</button>`}
        </div>
        ${urlBlock}
        <div class="git-inline" data-git-inline="${esc(dir)}"></div>
        <div class="git-toggle" onclick="toggleGit('${jesc(dir)}')">${openGit[dir] ? '&#9650;' : '&#9660;'} git info</div>
        <div class="git-panel" data-git-dir="${esc(dir)}" style="display:${openGit[dir] ? 'block' : 'none'}">${gitCache[dir] ? renderGitPanel(gitCache[dir], dir) : ''}</div>
        <div class="log-toggle" onclick="toggleLog('${jesc(dir)}')">${isOpen ? '&#9650;' : '&#9660;'} output log</div>
        <div class="log-box" data-dir="${esc(dir)}" style="display:${isOpen ? 'block' : 'none'}">${esc((s.output_tail || []).join('\n')) || '(empty)'}</div>
      </div>`;
  }).join('');
  el.querySelectorAll('.log-box').forEach(box => {
    if (box.style.display === 'block') box.scrollTop = box.scrollHeight;
  });
  entries.forEach(([dir]) => loadGitInline(dir));
}

function toggleLog(dir) {
  const el = document.querySelector(`.log-box[data-dir="${CSS.escape(dir)}"]`);
  if (!el) return;
  const isOpen = !!openLogs[dir];
  if (isOpen) {
    delete openLogs[dir];
    el.style.display = 'none';
    el.previousElementSibling.innerHTML = '&#9660; output log';
  } else {
    openLogs[dir] = true;
    el.style.display = 'block';
    el.scrollTop = el.scrollHeight;
    el.previousElementSibling.innerHTML = '&#9650; output log';
  }
}

async function startSession(dir, btn) {
  const name = await showPrompt('Start Session', 'Session name (optional)');
  if (name === null) return;
  await withLoading(btn, async () => {
    try {
      const body = {directory: dir};
      if (name.trim()) body.name = name.trim();
      const r = await apiFetch(API + '/api/sessions/start', {
        method: 'POST',
        body: JSON.stringify(body),
      });
      const data = await r.json();
      if (!r.ok) { showToast(data.detail || data.message || 'Error starting session', 'error'); return; }
      showToast('Session started', 'success');
      switchTab('sessions');
      startPolling();
      setTimeout(refreshSessions, 300);
    } catch (e) { if (!authRequired) showToast('Failed: ' + e.message, 'error'); }
  });
}

async function stopSession(dir, btn) {
  await withLoading(btn, async () => {
    try {
      await apiFetch(API + '/api/sessions/stop', {
        method: 'POST',
        body: JSON.stringify({directory: dir}),
      });
      showToast('Session stopped', 'info');
      setTimeout(refreshSessions, 500);
    } catch (e) { if (!authRequired) showToast('Failed: ' + e.message, 'error'); }
  });
}

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(refreshSessions, 2000);
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

// --- Browser ---
async function initBrowser() {
  try {
    const r = await apiFetch(API + '/api/browse', {
      method: 'POST',
      body: JSON.stringify({path: '~'}),
    });
    const data = await r.json();
    currentBrowsePath = data.current;
    document.getElementById('browse-path').value = currentBrowsePath;
    renderBrowser(data);
  } catch (e) { if (!authRequired) console.error(e); }
}

async function browseTo(path) {
  try {
    const r = await apiFetch(API + '/api/browse', {
      method: 'POST',
      body: JSON.stringify({path}),
    });
    if (!r.ok) return;
    const data = await r.json();
    currentBrowsePath = data.current;
    document.getElementById('browse-path').value = currentBrowsePath;
    renderBrowser(data);
  } catch (e) { if (!authRequired) console.error(e); }
}

function browseUp() {
  const sep = currentBrowsePath.includes('\\') ? '\\' : '/';
  const parts = currentBrowsePath.split(sep).filter(Boolean);
  if (parts.length > 1) {
    parts.pop();
    const parent = sep === '\\' ? parts.join('\\') : '/' + parts.join('/');
    browseTo(parent);
  }
}

function renderBrowser(data) {
  const el = document.getElementById('browser-list');
  if (!data.entries.length) {
    el.innerHTML = '<div class="empty">No subdirectories</div>';
    return;
  }
  el.innerHTML = data.entries.map(e => `
    <div class="dir-entry" ondblclick="browseTo('${jesc(e.path)}')" onclick="this.classList.toggle('selected')">
      <span class="icon">${e.is_project ? '&#128230;' : '&#128193;'}</span>
      <span class="name">${esc(e.name)}</span>
      ${e.is_project ? '<span class="project-tag">project</span>' : ''}
      <button class="pin-btn ${pinnedDirs.includes(e.path) ? 'pinned' : ''}" onclick="event.stopPropagation(); togglePin('${jesc(e.path)}')" aria-label="Pin project">&#128204;</button>
      <button class="btn btn-primary btn-sm" onclick="event.stopPropagation(); startSession('${jesc(e.path)}', this)">Start</button>
    </div>
  `).join('');
}

// --- Pinned ---
async function loadPinned() {
  try {
    const r = await apiFetch(API + '/api/pinned');
    pinnedDirs = await r.json();
    renderPinned();
  } catch (e) { if (!authRequired) console.error(e); }
}

function renderPinned() {
  const el = document.getElementById('pinned-list');
  if (!pinnedDirs.length) {
    el.innerHTML = '<div class="empty">No projects added<br><span class="hint">Pin directories from Browse tab</span></div>';
    return;
  }
  const query = (document.getElementById('pinned-search')?.value || '').toLowerCase();
  const filtered = query
    ? pinnedDirs.filter(dir => dir.toLowerCase().includes(query))
    : pinnedDirs;

  if (!filtered.length) {
    el.innerHTML = '<div class="empty">No matching projects</div>';
    return;
  }

  el.innerHTML = filtered.map(dir => {
    const name = dir.split(/[/\\]/).filter(Boolean).pop();
    const session = sessionsData[dir];
    const isRunning = session && (session.status === 'active' || session.status === 'starting');
    return `
      <div class="card">
        <div class="card-row">
          <div class="card-icon" style="background:var(--accent-glow);color:var(--accent)">&#128230;</div>
          <div class="card-body">
            <div class="card-name">${esc(name)}</div>
            <div class="card-path">${esc(dir)}</div>
          </div>
          ${isRunning ? `<span class="badge badge-active">active</span>` : ''}
          <button class="btn ${isRunning ? 'btn-ghost btn-sm' : 'btn-primary btn-sm'}"
            onclick="${isRunning ? `switchTab('sessions')` : `startSession('${jesc(dir)}', this)`}">
            ${isRunning ? 'View' : 'Start'}
          </button>
          <button class="pin-btn pinned" onclick="togglePin('${jesc(dir)}')" aria-label="Unpin project">&#128204;</button>
        </div>
        <div class="git-inline" data-git-inline="${esc(dir)}"></div>
        <div class="git-toggle" onclick="toggleGit('${jesc(dir)}')">${openGit[dir] ? '&#9650;' : '&#9660;'} git info</div>
        <div class="git-panel" data-git-dir="${esc(dir)}" style="display:${openGit[dir] ? 'block' : 'none'}">${gitCache[dir] ? renderGitPanel(gitCache[dir], dir) : ''}</div>
      </div>`;
  }).join('');
  filtered.forEach(dir => loadGitInline(dir));
}

function filterPinned(query) {
  renderPinned();
}

async function togglePin(dir) {
  const isPinned = pinnedDirs.includes(dir);
  try {
    await apiFetch(API + `/api/pinned/${isPinned ? 'remove' : 'add'}`, {
      method: 'POST',
      body: JSON.stringify({directory: dir}),
    });
    showToast(isPinned ? 'Project unpinned' : 'Project pinned', 'success');
    await loadPinned();
  } catch (e) { if (!authRequired) showToast('Failed: ' + e.message, 'error'); }
}

// --- Git info ---
async function fetchGitInfo(dir) {
  try {
    const r = await apiFetch(API + '/api/git-info', {
      method: 'POST',
      body: JSON.stringify({directory: dir}),
    });
    if (!r.ok) return null;
    const data = await r.json();
    if (data.is_git_repo) {
      gitCache[dir] = data;
      return data;
    }
  } catch (e) { if (!authRequired) console.error(e); }
  return null;
}

async function loadGitInline(dir) {
  let data = gitCache[dir];
  if (!data) data = await fetchGitInfo(dir);
  const el = document.querySelector(`[data-git-inline="${CSS.escape(dir)}"]`);
  if (!el || !data) return;
  const dot = data.is_clean ? 'clean' : 'dirty';
  const branch = data.is_detached ? data.head_hash : data.current_branch;
  let info = `<span class="status-dot ${dot}"></span><span class="branch">${esc(branch)}</span>`;
  if (data.ahead > 0) info += `<span class="git-badge git-badge-ahead">&uarr;${data.ahead}</span>`;
  if (data.behind > 0) info += `<span class="git-badge git-badge-behind">&darr;${data.behind}</span>`;
  if (!data.is_clean) info += `<span class="git-badge git-badge-dirty">${data.dirty_file_count} changed</span>`;
  el.innerHTML = info;
}

async function toggleGit(dir) {
  const panel = document.querySelector(`.git-panel[data-git-dir="${CSS.escape(dir)}"]`);
  if (!panel) return;
  const isOpen = !!openGit[dir];
  if (isOpen) {
    delete openGit[dir];
    panel.style.display = 'none';
    panel.previousElementSibling.innerHTML = '&#9660; git info';
  } else {
    openGit[dir] = true;
    panel.style.display = 'block';
    panel.previousElementSibling.innerHTML = '&#9650; git info';
    panel.innerHTML = '<span class="spinner"></span> Loading...';
    await fetchGitInfo(dir);
    if (gitCache[dir]) {
      panel.innerHTML = renderGitPanel(gitCache[dir], dir);
    } else {
      panel.innerHTML = '<span style="color:var(--text-dim)">Not a git repository</span>';
    }
  }
}

function renderGitPanel(g, dir) {
  const d = jesc(dir);
  let html = '<div class="git-summary">';
  const branch = g.is_detached ? `detached @ ${esc(g.head_hash)}` : esc(g.current_branch);
  html += `<span class="git-branch-name">${branch}</span>`;
  html += g.is_clean
    ? '<span class="git-badge git-badge-clean">clean</span>'
    : `<span class="git-badge git-badge-dirty">${g.dirty_file_count} changed</span>`;
  if (g.ahead > 0) html += `<span class="git-badge git-badge-ahead">&uarr; ${g.ahead} ahead</span>`;
  if (g.behind > 0) html += `<span class="git-badge git-badge-behind">&darr; ${g.behind} behind</span>`;
  html += '</div>';

  html += '<div class="git-actions">';
  html += `<button class="btn btn-sm btn-ghost" onclick="gitPull('${d}', this)">Pull</button>`;
  html += `<button class="btn btn-sm btn-ghost" onclick="gitPush('${d}', this)">Push</button>`;
  html += `<button class="btn btn-sm btn-ghost" onclick="gitStageAndCommit('${d}', this)">Commit</button>`;
  html += `<button class="btn btn-sm btn-ghost" onclick="gitNewBranch('${d}', this)">New Branch</button>`;
  html += '</div>';

  const activeView = gitActiveView[dir] || 'branches';
  html += '<div class="git-view-tabs">';
  html += `<button class="git-view-tab ${activeView === 'branches' ? 'active' : ''}" onclick="switchGitView(this, 'branches')">Branches</button>`;
  html += `<button class="git-view-tab ${activeView === 'commits' ? 'active' : ''}" onclick="switchGitView(this, 'commits')">Commits</button>`;
  html += `<button class="git-view-tab ${activeView === 'graph' ? 'active' : ''}" onclick="switchGitView(this, 'graph')">Graph</button>`;
  html += '</div>';

  html += `<div class="git-view" data-view="branches" style="display:${activeView === 'branches' ? 'block' : 'none'}">`;
  if (g.branches.length > 0) {
    html += '<ul class="git-branch-list">';
    g.branches.forEach(b => {
      const cls = b.is_current ? 'current' : (b.name.includes('/') ? 'remote' : '');
      html += `<li class="${cls}">${esc(b.name)}`;
      if (b.track) html += ` <span class="track">${esc(b.track)}</span>`;
      html += '</li>';
    });
    html += '</ul>';
  } else {
    html += '<div style="color:var(--text-dim);font-size:12px">No branches found</div>';
  }
  html += '</div>';

  html += `<div class="git-view" data-view="commits" style="display:${activeView === 'commits' ? 'block' : 'none'}">`;
  if (g.commits.length > 0) {
    html += '<div class="git-commits">';
    g.commits.forEach(c => {
      html += '<div class="git-commit">';
      html += `<span class="hash">${esc(c.hash)}</span>`;
      if (c.refs) html += `<span class="refs">${esc(c.refs)}</span>`;
      html += `<span class="msg">${esc(c.message)}</span>`;
      html += `<span class="meta">${esc(c.author)} &middot; ${esc(c.time)}</span>`;
      html += '</div>';
    });
    html += '</div>';
  }
  html += '</div>';

  html += `<div class="git-view" data-view="graph" style="display:${activeView === 'graph' ? 'block' : 'none'}">`;
  if (g.graph) {
    html += renderGitGraph(g.graph);
  } else {
    html += '<div style="color:var(--text-dim);font-size:12px">No history</div>';
  }
  html += '</div>';

  return html;
}

function switchGitView(btn, view) {
  const panel = btn.closest('.git-panel');
  const dir = panel.dataset.gitDir;
  if (dir) gitActiveView[dir] = view;
  panel.querySelectorAll('.git-view-tab').forEach(t => t.classList.toggle('active', false));
  btn.classList.add('active');
  panel.querySelectorAll('.git-view').forEach(v => v.style.display = v.dataset.view === view ? 'block' : 'none');
}

function renderGitGraph(graphText) {
  if (!graphText) return '';
  const lines = graphText.split('\n').filter(l => l.length);
  const escaped = lines.map(line => {
    let safe = esc(line);
    safe = safe.replace(/\(([^)]+)\)/g, (m, refs) =>
      `<span class="graph-refs">(${refs})</span>`
    );
    safe = safe.replace(/^([ *|/\\]+?) ([a-f0-9]{7,})\b/, (m, graph, hash) =>
      `<span class="graph-line">${graph}</span> <span class="graph-hash">${hash}</span>`
    );
    return safe;
  }).join('\n');
  return `<pre class="git-graph">${escaped}</pre>`;
}

// --- Git actions ---
async function gitActionPost(url, body) {
  const r = await apiFetch(API + url, {
    method: 'POST',
    body: JSON.stringify(body),
  });
  return await r.json();
}

async function refreshGitPanel(dir) {
  await fetchGitInfo(dir);
  const panel = document.querySelector(`.git-panel[data-git-dir="${CSS.escape(dir)}"]`);
  if (panel && openGit[dir]) panel.innerHTML = renderGitPanel(gitCache[dir], dir);
  loadGitInline(dir);
}

async function gitPull(dir, btn) {
  await withLoading(btn, async () => {
    try {
      const data = await gitActionPost('/api/git/pull', {directory: dir});
      showToast(data.message, data.ok ? 'success' : 'error');
      await refreshGitPanel(dir);
    } catch (e) { if (!authRequired) showToast('Pull failed: ' + e.message, 'error'); }
  });
}

async function gitPush(dir, btn) {
  if (!await showConfirm('Push to remote?')) return;
  await withLoading(btn, async () => {
    try {
      const data = await gitActionPost('/api/git/push', {directory: dir});
      showToast(data.message, data.ok ? 'success' : 'error');
      await refreshGitPanel(dir);
    } catch (e) { if (!authRequired) showToast('Push failed: ' + e.message, 'error'); }
  });
}

async function gitStageAndCommit(dir, btn) {
  const msg = await showPrompt('Commit', 'Enter commit message');
  if (!msg || !msg.trim()) return;
  await withLoading(btn, async () => {
    try {
      let data = await gitActionPost('/api/git/add', {directory: dir});
      if (!data.ok) { showToast('Stage failed: ' + data.message, 'error'); return; }
      data = await gitActionPost('/api/git/commit', {directory: dir, message: msg.trim()});
      showToast(data.message, data.ok ? 'success' : 'error');
      await refreshGitPanel(dir);
    } catch (e) { if (!authRequired) showToast('Commit failed: ' + e.message, 'error'); }
  });
}

async function gitNewBranch(dir, btn) {
  const name = await showPrompt('New Branch', 'Branch name');
  if (!name || !name.trim()) return;
  await withLoading(btn, async () => {
    try {
      const data = await gitActionPost('/api/git/create-branch', {directory: dir, branch: name.trim()});
      showToast(data.message, data.ok ? 'success' : 'error');
      await refreshGitPanel(dir);
    } catch (e) { if (!authRequired) showToast('Failed: ' + e.message, 'error'); }
  });
}

// --- Utilities ---
function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
function jesc(s) {
  return String(s).replace(/\\/g,'\\\\').replace(/'/g,"\\'");
}

function copyText(text, btn) {
  navigator.clipboard.writeText(text).then(() => {
    const orig = btn.textContent;
    btn.textContent = 'copied!';
    btn.style.color = 'var(--green)';
    showToast('URL copied to clipboard', 'success', 2000);
    setTimeout(() => { btn.textContent = orig; btn.style.color = ''; }, 1500);
  });
}

// --- Init ---
async function init() {
  await loadPinned();
  await refreshSessions();
  startPolling();
}

(async function() {
  // Test if auth is required
  try {
    const r = await fetch('/api/sessions');
    if (r.status === 401) {
      authRequired = true;
      // Try saved key
      if (authKey) {
        const r2 = await fetch('/api/sessions', { headers: { 'Authorization': 'Bearer ' + authKey } });
        if (r2.status === 401) {
          localStorage.removeItem('claude_dash_key');
          authKey = '';
          showLoginOverlay();
          return;
        }
      } else {
        showLoginOverlay();
        return;
      }
    }
  } catch (e) { console.error(e); }
  init();
})();
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    save_config(config)

    host = config["host"]
    port = config["port"]
    api_key = config.get("api_key", "")

    logger.info("Claude Remote Dashboard starting")
    logger.info("Listening on http://%s:%d", host, port)

    if host == "0.0.0.0":
        logger.info("Accessible from LAN / Tailscale at http://<your-ip>:%d", port)
        if not api_key:
            logger.warning(
                "WARNING: Dashboard is exposed on all interfaces with NO authentication. "
                "Set 'api_key' in config.json to secure it."
            )

    if api_key:
        logger.info("API key authentication is ENABLED")
    else:
        logger.info("API key authentication is disabled (set 'api_key' in config.json to enable)")

    logger.info("Permission mode: %s", config.get("permission_mode", "default"))

    uvicorn.run(app, host=host, port=port, log_level="info")
