"""Interactive local access diagnostic; no paper queries and no authentication automation."""
from __future__ import annotations

import asyncio
import json

from .access import BrowserAccess
from .config import Mode, Settings
from .core import AppError, Store


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


async def run_access_check(settings: Settings, institution_id: str, mode: Mode, config_path: str | None = None):
    # Keep this diagnostic separate from an already-running MCP service and its jobs.
    diagnostic_settings = settings.model_copy(update={
        "data_dir": settings.data_dir / "diagnostics" / "access-check",
        "browser_headless": False,
    })
    store = Store(diagnostic_settings.data_dir)
    access = BrowserAccess(diagnostic_settings, store)
    session_id = None
    try:
        session = await access.start(institution_id, mode)
        session_id = session["id"]
        emit(session)
        emit(await access.verify(session_id))
        print("请在专用浏览器亲自完成验证。命令：verify 核验 / inspect 诊断 / reload 重载配置 / reopen 重开入口 / close。", flush=True)
        while True:
            try:
                command = (await asyncio.to_thread(input, "access-check> ")).strip().lower()
            except EOFError:
                break
            if command in {"close", "exit", "quit"}:
                break
            try:
                if command in {"verify", ""}:
                    emit(await access.verify(session_id))
                elif command == "inspect":
                    emit(await access.inspect(session_id))
                elif command == "reload":
                    if not config_path:
                        raise AppError("CONFIG_PATH_REQUIRED", "没有指定可重新加载的本地配置路径。")
                    loaded = Settings.load(config_path)
                    if loaded.data_dir.resolve() != settings.data_dir.resolve():
                        raise AppError("CONFIG_RESTART_REQUIRED", "数据目录变化需要重启接入诊断。")
                    updated = loaded.model_copy(update={
                        "data_dir": diagnostic_settings.data_dir, "browser_headless": False,
                    })
                    emit(await access.reconfigure(session_id, updated))
                elif command == "reopen":
                    emit(await access.reopen(session_id))
                else:
                    print("可用命令：verify / inspect / reload / reopen / close。不会执行检索或下载。", flush=True)
            except AppError as error:
                emit({"code": error.code, "message": error.message, "user_action": error.action})
            except Exception:
                emit({"code": "BROWSER_SESSION_UNAVAILABLE", "message": "专用页面已关闭或不可读取，请重建会话。"})
    finally:
        if session_id:
            await access.close(session_id)
        await access.shutdown()
        store.close()
