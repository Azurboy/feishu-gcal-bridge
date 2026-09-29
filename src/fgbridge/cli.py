"""Small interactive CLI for one local Feishu → Google mirror."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import plistlib
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .google_calendar import GoogleCalendar, GoogleError
from .source import FeishuSource, SourceError
from .storage import (
    AlreadyRunning, State, atomic_private_write, home, load_config,
    load_secrets, private_dir, process_lock, save_config, save_secrets,
)
from .sync import Reconciler, window_bounds


def _ask(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or (default or "")


def _yes(label: str) -> bool:
    return input(f"{label} [y/N]: ").strip().lower() in {"y", "yes"}


def _installed_client(path: Path) -> None:
    if not path.is_file():
        raise ValueError("google_client_json_missing")
    try:
        body = json.loads(path.read_text("utf-8"))
    except (ValueError, OSError) as exc:
        raise ValueError("google_client_json_invalid") from exc
    if "installed" not in body:
        raise ValueError("google_client_must_be_desktop")
    if path.resolve().is_relative_to(Path(__file__).resolve().parents[2]):
        raise ValueError("google_client_json_must_be_outside_repository")


def setup() -> int:
    private_dir(home())
    try:
        previous = load_config()
        existing_secrets = load_secrets()
    except FileNotFoundError:
        previous, existing_secrets = {}, {}

    base_url = _ask("飞书 CalDAV 地址", previous.get("caldav_url", "https://caldav.feishu.cn/"))
    username = _ask("飞书 CalDAV 用户名", existing_secrets.get("username"))
    if not username:
        raise ValueError("caldav_username_required")
    if existing_secrets.get("password") and _yes("沿用已保存的飞书 CalDAV 密码？"):
        password = existing_secrets["password"]
    else:
        password = getpass.getpass("飞书 CalDAV 专用密码（不回显）: ")
    if not password:
        raise ValueError("caldav_password_required")

    source = FeishuSource(base_url, username, password)
    calendars = source.calendars()
    print("可选日历：")
    for index, (name, _) in enumerate(calendars, 1):
        print(f"  {index}. {name}")
    chosen = _ask("选择编号", "1")
    if not chosen.isdigit() or not 1 <= int(chosen) <= len(calendars):
        raise ValueError("calendar_selection_invalid")
    name, calendar_url = calendars[int(chosen) - 1]
    zone_name = source.timezone(calendar_url) or _ask(
        "源日历时区 IANA 名称", previous.get("time_zone", "Asia/Shanghai")
    )
    try:
        ZoneInfo(zone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("invalid_source_timezone") from exc
    start, end = window_bounds(datetime.now(timezone.utc), zone_name)
    probe = source.scan(calendar_url, zone_name, start, end, "full")
    if not probe.complete:
        raise SourceError(probe.errors[0] if probe.errors else "caldav_probe_incomplete")
    if probe.private_marker_seen:
        print("源数据中检测到 CLASS:PRIVATE/CONFIDENTIAL 标记。")
    else:
        print("尚未验证飞书私密日程的 CalDAV 标记；本次仅允许 Busy 模式。")
    requested = _ask("导出模式 full（标题+时间）/ busy（仅忙闲）", previous.get("mode", "busy"))
    if requested not in {"full", "busy"}:
        raise ValueError("invalid_export_mode")
    privacy_verified = probe.private_marker_seen or bool(previous.get("privacy_verified"))
    if requested == "full" and not privacy_verified:
        raise ValueError("privacy_marker_not_verified_use_busy")
    print(f"读取预览：{len(probe.instances)} 个窗口内实例；模式 {requested}。")
    for item in list(probe.instances.values())[:3]:
        print(f"  {item.start} → {item.end}; {item.summary if requested == 'full' and not item.private else 'Busy'}")

    default_client = previous.get("google_client_json", "")
    client_json = Path(_ask("Google Desktop OAuth client JSON 的绝对路径", default_client)).expanduser()
    _installed_client(client_json)
    binding_changed = bool(previous) and (
        previous.get("calendar_url") != calendar_url
        or previous.get("caldav_username") != username
        or previous.get("caldav_url") != base_url.rstrip("/") + "/"
    )
    if binding_changed and not _yes("飞书来源已变化：创建新的 Google 镜像日历并保留旧日历？"):
        return 2
    if previous and _yes("重新授权 Google 账号？"):
        (home() / "token.json").unlink(missing_ok=True)
    google = GoogleCalendar.connect(client_json, interactive=True)
    install_id = secrets.token_hex(16) if not previous or binding_changed else previous["installation_id"]
    target_id = previous.get("google_calendar_id") if not binding_changed else None
    if target_id:
        google.assert_calendar(target_id, install_id)
    else:
        target_id = google.create_calendar("Feishu", zone_name, install_id)
        google.assert_calendar(target_id, install_id)
    config = {
        "installation_id": install_id,
        "caldav_url": base_url.rstrip("/") + "/",
        "calendar_url": calendar_url,
        "source_name": name,
        "caldav_username": username,
        "time_zone": zone_name,
        "mode": requested,
        "privacy_verified": privacy_verified,
        "google_calendar_id": target_id,
        "google_client_json": str(client_json.resolve()),
    }
    save_secrets({"username": username, "password": password})
    save_config(config)
    if binding_changed:
        state = State()
        try:
            state.reset_for_new_binding()
        finally:
            state.close()
    print("飞书读取与 Google 授权完成；开始首次同步。")
    return sync_once(dry_run=False, confirm_empty=False)


def sync_once(*, dry_run: bool, confirm_empty: bool) -> int:
    with process_lock():
        config = load_config()
        secret = load_secrets()
        state = State()
        try:
            if not dry_run:
                state.set("last_attempt", datetime.now(timezone.utc).isoformat())
            start, end = window_bounds(datetime.now(timezone.utc), config["time_zone"])
            source = FeishuSource(config["caldav_url"], secret["username"], secret["password"])
            snapshot = source.scan(
                config["calendar_url"], config["time_zone"], start, end, config["mode"]
            )
            google = GoogleCalendar.connect(Path(config["google_client_json"]))
            result = Reconciler(
                google, state, config["google_calendar_id"], config["installation_id"],
                config["mode"], start, end,
            ).run(snapshot, dry_run=dry_run, confirm_empty=confirm_empty)
            print(
                f"{result.status}{' (dry-run)' if dry_run else ''}: "
                f"新增 {result.created} / 更新 {result.updated} / 删除 {result.deleted} / "
                f"待确认缺失 {result.pending_delete}"
            )
            if result.errors:
                print("错误码：" + ", ".join(sorted(set(result.errors))))
                if result.empty_confirmation_needed:
                    print("源快照突然全空；确认源日历正确后可执行 fgbridge sync --confirm-empty。")
            return 0 if result.status == "ok" else 1
        except GoogleError as exc:
            if not dry_run:
                state.set("status", "needs_auth" if exc.code == "google_needs_auth" else "error")
                state.set("last_error", exc.code)
            raise
        except Exception:
            if not dry_run:
                state.set("status", "error")
                state.set("last_error", "sync_failed")
            raise
        finally:
            state.close()


def status() -> int:
    try:
        config = load_config()
    except FileNotFoundError:
        print("尚未设置。运行 fgbridge setup。")
        return 2
    state = State()
    try:
        last = state.get("last_success")
        last_attempt = state.get("last_attempt")
        base_status = state.get("status") or "error"
        if last:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(last)
            current = "stale" if age.total_seconds() > 600 else base_status
        else:
            current = "尚未完成首次同步" if base_status != "needs_auth" else "needs_auth"
        print(f"状态：{current}")
        print(f"飞书来源：{config['source_name']}")
        print(f"Google 目标 ID：{config['google_calendar_id']}")
        print(f"最后尝试：{last_attempt or '无'}")
        print(f"最后完整成功：{last or '无'}")
        if last:
            print(f"距上次完整成功：{int(age.total_seconds() // 60)} 分钟")
        if state.get("last_error"):
            print(f"最近错误码：{state.get('last_error')}")
        print(f"本地数据目录：{home()}")
        return 0
    finally:
        state.close()


def _launch_agent(action: str) -> int:
    if platform.system() != "Darwin":
        raise ValueError("schedule_requires_macos")
    load_config()
    label = "dev.feishu-gcal-bridge.sync"
    agents = Path.home() / "Library" / "LaunchAgents"
    plist_path = agents / f"{label}.plist"
    domain = f"gui/{os.getuid()}"
    if action == "remove":
        subprocess.run(["launchctl", "bootout", domain, str(plist_path)], capture_output=True)
        plist_path.unlink(missing_ok=True)
        print("已停用定时同步；Google 中的现有镜像保留。")
        return 0
    agents.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": label,
        "ProgramArguments": [sys.executable, "-m", "fgbridge.cli", "sync"],
        "StartInterval": 120,
        "RunAtLoad": True,
        "StandardOutPath": str(home() / "launchd.log"),
        "StandardErrorPath": str(home() / "launchd-error.log"),
        "EnvironmentVariables": {"FGBRIDGE_HOME": str(home())},
    }
    private_dir(home())
    atomic_private_write(plist_path, plistlib.dumps(payload))
    subprocess.run(["launchctl", "bootout", domain, str(plist_path)], capture_output=True)
    completed = subprocess.run(
        ["launchctl", "bootstrap", domain, str(plist_path)], capture_output=True
    )
    if completed.returncode:
        raise ValueError("launchd_bootstrap_failed")
    print("已安装当前用户定时任务：每 120 秒检查一次。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fgbridge")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup")
    sync_parser = sub.add_parser("sync")
    sync_parser.add_argument("--dry-run", action="store_true")
    sync_parser.add_argument("--confirm-empty", action="store_true")
    sub.add_parser("status")
    schedule_parser = sub.add_parser("schedule")
    schedule_parser.add_argument("action", choices=["install", "remove"])
    args = parser.parse_args(argv)
    try:
        if args.command == "setup":
            return setup()
        if args.command == "sync":
            if args.dry_run and args.confirm_empty:
                parser.error("--confirm-empty requires a writing sync")
            return sync_once(dry_run=args.dry_run, confirm_empty=args.confirm_empty)
        if args.command == "status":
            return status()
        return _launch_agent(args.action)
    except AlreadyRunning:
        print("已有同步进程在运行。", file=sys.stderr)
        return 3
    except (FileNotFoundError, KeyError, ValueError, SourceError, GoogleError) as exc:
        code = exc.code if isinstance(exc, GoogleError) else (
            str(exc) if isinstance(exc, (ValueError, SourceError)) else "setup_required"
        )
        print(f"需要处理：{code}", file=sys.stderr)
        return 2
    except Exception:
        print("同步失败：internal_error（运行 fgbridge status 查看最近状态）", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
