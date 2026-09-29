#!/usr/bin/env bash
# Register this repository's stdio server with local coding agents.
set -euo pipefail

usage() {
  cat <<'HELP'
Usage: bash scripts/install-mcp.sh [--client all|hermes|codex|claude]
                                   [--runtime auto|windows|native]
                                   [--name SERVER_NAME] [--dry-run]

Defaults: all installed clients; Windows runtime on WSL/Git Bash, native elsewhere.
Authenticate with `uv run urfu-mcp setup` on the SAME OS as the server first.
`--dry-run` prints registrations without modifying agent configuration.
HELP
}

error() { printf 'Error: %s\n' "$*" >&2; exit 1; }

client=all
runtime=auto
name=urfu-mcp
dry_run=false
while (($#)); do
  case "$1" in
    --client|--runtime|--name)
      (($# >= 2)) || error "Missing value for $1"
      case "$1" in
        --client) client=$2 ;;
        --runtime) runtime=$2 ;;
        --name) name=$2 ;;
      esac
      shift 2 ;;
    --dry-run) dry_run=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) error "Unknown option: $1" ;;
  esac
done
case "$client" in all|hermes|codex|claude) ;; *) error "Unsupported client: $client" ;; esac
case "$runtime" in auto|windows|native) ;; *) error "Unsupported runtime: $runtime" ;; esac
[[ "$name" =~ ^[A-Za-z][A-Za-z0-9_-]*$ ]] || error 'Server name must be letters, digits, underscores or hyphens (starting with a letter)'

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
[[ -f "$project_dir/pyproject.toml" ]] || error 'Cannot find the urfu-mcp project'

if [[ "$runtime" == auto ]]; then
  case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) runtime=windows ;;
    Linux)
      if [[ -n "${WSL_DISTRO_NAME:-}" ]] || { [[ -r /proc/version ]] && [[ "$(</proc/version)" == *[Mm]icrosoft* ]]; }; then
        runtime=windows
      else
        runtime=native
      fi ;;
    *) runtime=native ;;
  esac
fi

launcher=()
if [[ "$runtime" == windows ]]; then
  command -v cmd.exe >/dev/null 2>&1 || error 'Windows runtime needs cmd.exe'
  if command -v wslpath >/dev/null 2>&1; then
    windows_dir=$(wslpath -w "$project_dir")
  elif command -v cygpath >/dev/null 2>&1; then
    windows_dir=$(cygpath -w "$project_dir")
  else
    error 'Windows runtime needs wslpath (WSL) or cygpath (Git Bash)'
  fi
  # WSL-to-cmd.exe quoting can pass literal quotes to uv (Windows error 123).
  # Use an unquoted directory only after rejecting shell separators and spaces.
  case "$windows_dir" in
    *[[:space:]]*|*'%'*|*'!'*|*'^'*|*'&'*|*'|'*|*'<'*|*'>'*|*'"'*)
      error 'Windows project path contains spaces or characters unsafe for cmd.exe; move the checkout or choose --runtime native' ;;
  esac
  if [[ "$dry_run" == false ]]; then
    cmd.exe /d /s /c 'where uv >nul 2>nul' || error 'Install uv on Windows before registering the server'
  fi
  # Keep the Windows venv separate from WSL's .venv and keyring.
  windows_command="set UV_PROJECT_ENVIRONMENT=%LOCALAPPDATA%\\uv\\urfu-mcp&& uv run --frozen --directory ${windows_dir} urfu-mcp serve"
  launcher=(cmd.exe /d /s /c "$windows_command")
else
  uv_bin=$(command -v uv || true)
  [[ -n "$uv_bin" ]] || error 'Install uv before registering the server'
  launcher=("$uv_bin" run --frozen --directory "$project_dir" urfu-mcp serve)
fi

register() {
  local agent=$1
  if ! command -v "$agent" >/dev/null 2>&1; then
    if [[ "$client" == all ]]; then
      printf 'Skipping %s (not installed).\n' "$agent"
      return 0
    fi
    printf 'Error: %s is not installed.\n' "$agent" >&2
    return 1
  fi
  local args=()
  case "$agent" in
    hermes) args=(mcp add "$name" --command "${launcher[0]}" --args "${launcher[@]:1}") ;;
    codex)  args=(mcp add "$name" -- "${launcher[@]}") ;;
    claude) args=(mcp add --transport stdio --scope user "$name" -- "${launcher[@]}") ;;
  esac
  if [[ "$dry_run" == true ]]; then
    printf '%s: ' "$agent"
    printf '%q ' "$agent" "${args[@]}"
    printf '\n'
  elif "$agent" "${args[@]}"; then
    printf 'Added %s to %s.\n' "$name" "$agent"
  else
    printf 'Failed to add %s to %s.\n' "$name" "$agent" >&2
    return 1
  fi
}

failed=0
if [[ "$client" == all ]]; then
  for agent in hermes codex claude; do
    register "$agent" || failed=1
  done
else
  register "$client" || failed=1
fi
if ((failed)); then
  exit 1
fi
printf 'Restart the selected agent(s) to discover the tools. The MCP host launches the server; this script does not keep it running.\n'
