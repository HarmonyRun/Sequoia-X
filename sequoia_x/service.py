"""Sequoia-X scheduler and lightweight management dashboard.

This module is primarily used by the native fnOS package. It keeps the
background service alive, runs the existing CLI in a child process, and
exposes a small authenticated dashboard for status, logs, and manual runs.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
from datetime import date, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from sequoia_x.analysis import LlmAnalyzer
from sequoia_x.core.config import Settings
from sequoia_x.notify.feishu import FeishuNotifier
from sequoia_x.runtime_config import RuntimeConfigStore

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("SEQUOIA_DATA_DIR", "/data"))
LOG_DIR = Path(os.getenv("SEQUOIA_LOG_DIR", "/logs"))
STATE_FILE = DATA_DIR / "service-state.json"
LOG_FILE = LOG_DIR / "sequoia-x.log"
DB_PATH = Path(os.getenv("DB_PATH", str(DATA_DIR / "sequoia_v2.db")))
RESULT_FILE = Path(os.getenv("SEQUOIA_RESULT_PATH", str(DATA_DIR / "results/latest.json")))


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class TaskSupervisor:
    """Serializes CLI jobs and persists their last known state."""

    def __init__(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.config = RuntimeConfigStore()
        self._process: subprocess.Popen[str] | None = None
        self._worker: threading.Thread | None = None
        self._state: dict[str, Any] = {
            "task": None,
            "running": False,
            "started_at": None,
            "finished_at": None,
            "exit_code": None,
            "last_error": None,
            "last_scheduled_date": None,
        }
        self._load_state()
        # A process cannot survive a container restart, so never restore it as running.
        self._state["running"] = False
        self._process = None
        self._save_state()

    def _load_state(self) -> None:
        try:
            saved = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                self._state.update(saved)
        except (OSError, ValueError):
            pass

    def _save_state(self) -> None:
        temp = STATE_FILE.with_suffix(".tmp")
        temp.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(STATE_FILE)

    def _append_log(self, message: str) -> None:
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(message.rstrip("\n") + "\n")

    def start(self, task: str, *, scheduled: bool = False) -> tuple[bool, str]:
        if task not in {"daily", "backfill", "update"}:
            return False, "未知任务"
        with self._lock:
            if self._state.get("running"):
                return False, "已有任务正在运行"
            now = datetime.now().isoformat(timespec="seconds")
            self._state.update(
                {
                    "task": task,
                    "running": True,
                    "started_at": now,
                    "finished_at": None,
                    "exit_code": None,
                    "last_error": None,
                }
            )
            if scheduled:
                self._state["last_scheduled_date"] = date.today().isoformat()
            self._save_state()
            self._worker = threading.Thread(
                target=self._run,
                args=(task,),
                name=f"sequoia-{task}",
                daemon=True,
            )
            self._worker.start()
            return True, "任务已启动"

    def _run(self, task: str) -> None:
        command = [sys.executable, "main.py"]
        if task == "backfill":
            command.append("--backfill")
        elif task == "update":
            command.append("--update-only")
        env = os.environ.copy()
        env["DB_PATH"] = str(DB_PATH)
        env["RESULT_PATH"] = str(RESULT_FILE)
        env.update(self.config.worker_env())
        env.setdefault("NO_COLOR", "1")
        self._append_log(
            f"[{datetime.now().isoformat(timespec='seconds')}] ===== {task} 开始 ====="
        )
        exit_code = 1
        error: str | None = None
        try:
            process = subprocess.Popen(
                command,
                cwd=PROJECT_DIR,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            with self._lock:
                self._process = process
            assert process.stdout is not None
            for line in process.stdout:
                self._append_log(line)
            exit_code = process.wait()
            if exit_code != 0:
                error = f"任务退出码为 {exit_code}"
        except Exception as exc:  # dashboard must stay alive to report failures
            error = str(exc)
            self._append_log(f"任务启动失败：{exc}")
        finally:
            self._append_log(
                f"[{datetime.now().isoformat(timespec='seconds')}] ===== {task} 结束，"
                f"退出码 {exit_code} ====="
            )
            with self._lock:
                self._process = None
                self._state.update(
                    {
                        "running": False,
                        "finished_at": datetime.now().isoformat(timespec="seconds"),
                        "exit_code": exit_code,
                        "last_error": error,
                    }
                )
                self._save_state()

    def cancel(self) -> tuple[bool, str]:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                return False, "当前没有运行中的任务"
            process.terminate()
            return True, "已发送停止信号"

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            result = dict(self._state)
        result["database"] = self._database_stats()
        config = self.config.load()
        result["schedule"] = {
            "time": config["run_time"],
            "weekdays": config["run_weekdays"],
            "auto_backfill": config["auto_backfill"],
            "data_source": config["data_source"],
        }
        result["log_tail"] = self._log_tail()
        return result

    @staticmethod
    def _database_stats() -> dict[str, Any]:
        if not DB_PATH.exists():
            return {"path": str(DB_PATH), "rows": 0, "symbols": 0, "latest_date": None}
        try:
            with sqlite3.connect(DB_PATH, timeout=2) as conn:
                row = conn.execute(
                    "SELECT COUNT(*), COUNT(DISTINCT symbol), MAX(date) FROM stock_daily"
                ).fetchone()
            return {
                "path": str(DB_PATH),
                "rows": row[0] if row else 0,
                "symbols": row[1] if row else 0,
                "latest_date": row[2] if row else None,
            }
        except sqlite3.Error as exc:
            return {"path": str(DB_PATH), "error": str(exc)}

    @staticmethod
    def _log_tail(lines: int = 120) -> str:
        try:
            content = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(content[-lines:])
        except OSError:
            return "暂无运行日志"


class Scheduler(threading.Thread):
    def __init__(self, supervisor: TaskSupervisor) -> None:
        super().__init__(name="sequoia-scheduler", daemon=True)
        self.supervisor = supervisor
        self.stop_event = threading.Event()
        self._trade_day_cache: dict[str, bool] = {}

    def _is_trading_day(self, day: date) -> bool:
        """Check the mainland trading calendar, falling back to weekdays."""
        key = day.isoformat()
        if key in self._trade_day_cache:
            return self._trade_day_cache[key]
        result = day.isoweekday() <= 5
        try:
            import akshare as ak

            calendar = ak.tool_trade_date_hist_sina()
            dates = set(
                calendar["trade_date"].astype(str).str[:10]
                if "trade_date" in calendar.columns
                else calendar.iloc[:, 0].astype(str).str[:10]
            )
            result = key in dates
        except Exception:
            # A calendar outage should not prevent the daily provider failover job.
            pass
        self._trade_day_cache = {key: result}
        return result

    def _parse_schedule(self: Scheduler | None = None) -> tuple[int, int, set[int]]:
        config = (
            self.supervisor.config.load()
            if self is not None
            else {
                "run_time": os.getenv("RUN_TIME", "16:00"),
                "run_weekdays": os.getenv("RUN_WEEKDAYS", "1,2,3,4,5"),
            }
        )
        try:
            hour_text, minute_text = str(config["run_time"]).split(":", 1)
            hour, minute = int(hour_text), int(minute_text)
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
        except ValueError:
            hour, minute = 16, 0
        try:
            weekdays = {
                int(item.strip())
                for item in str(config["run_weekdays"]).split(",")
                if item.strip()
            }
            weekdays &= set(range(1, 8))
        except ValueError:
            weekdays = {1, 2, 3, 4, 5}
        return hour, minute, weekdays or {1, 2, 3, 4, 5}

    def run(self) -> None:
        # Let the HTTP server become available before the potentially long first backfill.
        if self.stop_event.wait(2):
            return
        stats = self.supervisor.snapshot()["database"]
        if self.supervisor.config.load()["auto_backfill"] and not stats.get("rows", 0):
            self.supervisor.start("backfill")

        while not self.stop_event.wait(15):
            now = datetime.now()
            hour, minute, weekdays = self._parse_schedule()
            state = self.supervisor.snapshot()
            due = (now.hour, now.minute) >= (hour, minute)
            not_run_today = state.get("last_scheduled_date") != now.date().isoformat()
            if (
                now.isoweekday() in weekdays
                and due
                and not_run_today
                and self._is_trading_day(now.date())
            ):
                self.supervisor.start("daily", scheduled=True)


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "SequoiaX/2.0"

    @property
    def supervisor(self) -> TaskSupervisor:
        return self.server.supervisor  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: object) -> None:
        # Avoid duplicating every dashboard poll in the application log.
        return

    def _authorized(self) -> bool:
        config = self.supervisor.config.load()
        username = str(config["dashboard_username"])
        password = str(config["dashboard_password"])
        if not password:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
            supplied_user, supplied_password = decoded.split(":", 1)
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(supplied_user, username) and hmac.compare_digest(
            supplied_password, password
        )

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="Sequoia-X"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("无效的请求长度") from exc
        if length > 128 * 1024:
            raise ValueError("请求内容过大")
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("请求不是有效 JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("请求内容必须是 JSON 对象")
        return payload

    @staticmethod
    def _settings_from_runtime(values: dict[str, Any]) -> Settings:
        return Settings(
            db_path=str(DB_PATH),
            result_path=str(RESULT_FILE),
            **{
                key: values[key]
                for key in (
                    "data_source", "start_date", "feishu_webhook_url",
                    "push_strategy_results", "push_ai_analysis", "llm_enabled",
                    "llm_base_url", "llm_api_key", "llm_model", "llm_temperature",
                    "llm_extra_prompt",
                )
            },
        )

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._json({"status": "ok"})
            return
        if not self._require_auth():
            return
        if self.path == "/api/status":
            self._json(self.supervisor.snapshot())
            return
        if self.path == "/api/settings":
            self._json(self.supervisor.config.public())
            return
        if self.path == "/api/results":
            try:
                result = json.loads(RESULT_FILE.read_text(encoding="utf-8"))
                self._json(result if isinstance(result, dict) else {})
            except (OSError, ValueError):
                self._json({"generated_at": None, "strategies": [], "unique_symbols": [], "ai": {}})
            return
        if self.path in {"/", "/index.html"}:
            page = (PROJECT_DIR / "sequoia_x" / "dashboard.html").read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        if not self._require_auth():
            return
        actions = {
            "/api/run": lambda: self.supervisor.start("daily"),
            "/api/tasks/run": lambda: self.supervisor.start("daily"),
            "/api/backfill": lambda: self.supervisor.start("backfill"),
            "/api/tasks/backfill": lambda: self.supervisor.start("backfill"),
            "/api/update": lambda: self.supervisor.start("update"),
            "/api/tasks/update": lambda: self.supervisor.start("update"),
            "/api/cancel": self.supervisor.cancel,
            "/api/tasks/cancel": self.supervisor.cancel,
        }
        action = actions.get(self.path)
        if action is not None:
            ok, message = action()
            self._json(
                {"ok": ok, "message": message},
                HTTPStatus.ACCEPTED if ok else HTTPStatus.CONFLICT,
            )
            return
        try:
            if self.path == "/api/settings":
                self.supervisor.config.save(self._read_json())
                self._json(
                    {
                        "ok": True,
                        "message": "设置已保存",
                        "settings": self.supervisor.config.public(),
                    }
                )
                return
            if self.path == "/api/test/llm":
                values = self.supervisor.config.load()
                settings = self._settings_from_runtime(values)
                message = LlmAnalyzer(settings).test()
                self._json({"ok": True, "message": "大模型连接成功", "preview": message[:300]})
                return
            if self.path == "/api/test/feishu":
                values = self.supervisor.config.load()
                settings = self._settings_from_runtime(values)
                ok = FeishuNotifier(settings).send(["000001"], "Web UI 连接测试")
                self._json(
                    {
                        "ok": ok,
                        "message": (
                            "飞书测试消息发送成功"
                            if ok
                            else "发送失败，请检查 Webhook 和日志"
                        ),
                    },
                    HTTPStatus.OK if ok else HTTPStatus.BAD_GATEWAY,
                )
                return
        except Exception as exc:
            self._json({"ok": False, "message": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self.send_error(HTTPStatus.NOT_FOUND)


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], supervisor: TaskSupervisor) -> None:
        self.supervisor = supervisor
        super().__init__(address, DashboardHandler)


def main() -> None:
    supervisor = TaskSupervisor()
    scheduler = Scheduler(supervisor)
    scheduler.start()
    port = int(os.getenv("DASHBOARD_PORT", "18188"))
    server = DashboardServer(("0.0.0.0", port), supervisor)

    def shutdown(_signum: int, _frame: object) -> None:
        scheduler.stop_event.set()
        supervisor.cancel()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        scheduler.stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
