# Claude Remote Dashboard

A lightweight, single-file web dashboard for managing [Claude Code](https://docs.anthropic.com/en/docs/claude-code) `remote-control` sessions from any device. Run it on your always-on desktop, access it from your phone or laptop over Tailscale or LAN.

```
+-------------+     Tailscale/LAN     +-------------------+     Anthropic API     +---------------+
|  Your Phone |  ------------------>  |  Desktop (this)   |  <-----------------   |  Claude Code  |
|  or Laptop  |    :7878 dashboard    |  app.py + claude  |    remote-control     |  Session      |
+-------------+                       +-------------------+     streaming bridge  +---------------+
       |                                                                                 |
       +--- opens claude.ai/code URL -----------------------------------------------+
```

## Quick Start

```bash
git clone https://github.com/YOUR_USER/claude-remote-dashboard.git
cd claude-remote-dashboard
pip install -r requirements.txt
python app.py
```

Open `http://localhost:7878` in your browser. That's it.

## How It Works

1. Open the dashboard from your phone (via Tailscale IP or LAN)
2. Pick a project directory from your pinned list or the file browser
3. Hit **Start** -- the dashboard spawns `claude remote-control` on the desktop
4. Once the session URL appears, tap it to open in Claude Code
5. You're now controlling your desktop's Claude Code session remotely

## Configuration

Copy the example config and edit it:

```bash
cp config.example.json config.json
```

| Key | Default | Description |
|-----|---------|-------------|
| `host` | `127.0.0.1` | Bind address. Set to `0.0.0.0` for LAN/Tailscale access. |
| `port` | `7878` | Port number. |
| `api_key` | `""` | API key for authentication. When set, all API requests must include `Authorization: Bearer <key>`. **Strongly recommended** when exposing on a network. |
| `permission_mode` | `"default"` | Claude Code permission mode for new sessions. Options: `default`, `plan`, `autoApprove`, `bypassPermissions`. |
| `auto_trust_directories` | `false` | When `true`, automatically accepts Claude's trust dialog for session directories by writing to `~/.claude.json`. |
| `pinned_dirs` | `[]` | Pre-configured project directories for quick access. |
| `browse_root` | `"~"` | Root directory for the file browser. Users cannot browse above this path. |
| `claude_binary` | `"claude"` | Path to the `claude` CLI binary. |
| `git_binary` | `"git"` | Path to the `git` binary. |
| `git_log_count` | `20` | Number of commits to show in the git panel. |
| `git_show_remote_branches` | `true` | Show remote branches in the git panel. |
| `cors_origins` | `[]` | Allowed CORS origins. Defaults to `localhost` at the configured port. |

## Security

This dashboard can start Claude Code sessions and execute git operations on your machine. Take these precautions:

### Authentication

Set an `api_key` in `config.json` to require authentication:

```json
{
  "api_key": "your-secret-key-here"
}
```

When enabled, the dashboard shows a login screen and all API requests require a Bearer token. The key is stored in the browser's `localStorage`.

**If `api_key` is empty and `host` is `0.0.0.0`, the dashboard prints a security warning on startup.**

### Network Exposure

- **Default bind is `127.0.0.1`** (localhost only). Change to `0.0.0.0` only when you have authentication configured or are on a fully trusted network.
- **Tailscale** is the recommended way to access the dashboard remotely -- it encrypts traffic and restricts access to your tailnet.
- Consider adding a reverse proxy with HTTPS for additional security.

### Directory Scoping

All session and git operations are restricted to directories within `browse_root` plus any explicitly pinned directories. Requests to directories outside this scope are rejected.

### Permission Modes

The `permission_mode` setting controls what Claude Code can do in sessions:

- `default` -- Claude's default permission behavior (prompts for dangerous operations)
- `plan` -- Read-only planning mode
- `autoApprove` -- Auto-approves most operations
- `bypassPermissions` -- Skips all permission checks (**use with caution**)

### Auto-Trust

The `auto_trust_directories` option (disabled by default) writes to `~/.claude.json` to pre-accept Claude's trust dialog for directories. Only enable this if you understand the implications.

## Running as a Service

### systemd (Linux)

```bash
sudo tee /etc/systemd/system/claude-dashboard.service << 'EOF'
[Unit]
Description=Claude Remote Dashboard
After=network.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/path/to/claude-remote-dashboard
ExecStart=/usr/bin/python3 /path/to/claude-remote-dashboard/app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable claude-dashboard
sudo systemctl start claude-dashboard
```

## Features

- **Session management** -- Start, stop, and monitor Claude Code remote-control sessions
- **File browser** -- Navigate your filesystem to find project directories, with auto-detection of projects (`.git`, `package.json`, `pyproject.toml`, etc.)
- **Pinned projects** -- Save your favorite directories for one-tap access with search/filter
- **Git integration** -- View branches, commits, and graph; pull, push, commit, and create branches from the UI
- **Authentication** -- Optional API key auth with a built-in login screen
- **Mobile-first** -- Designed for phone use with proper touch targets and responsive layout
- **Single file** -- The entire application is one Python file with an embedded frontend. No build step, no node_modules, no separate assets.
- **Cross-platform** -- Works on Windows, macOS, and Linux

## Requirements

- Python 3.10+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- Dependencies: `fastapi`, `uvicorn`, `pydantic` (see `requirements.txt`)

## License

MIT
