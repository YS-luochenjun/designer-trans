"""User-driven login in a project-owned browser; no credentials reach the UI."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

from .mcp import FireplayMCP
from .credentials import CredentialSession


class FireplayLogin:
    def __init__(self, root, bridge):
        self.root, self.bridge = Path(root), bridge
        self.lock = threading.Lock()
        self.state = {"status": "idle", "message": ""}

    def status(self):
        with self.lock:
            return dict(self.state)

    def _set(self, status, message):
        with self.lock:
            self.state = {"status": status, "message": message}

    def start(self):
        with self.lock:
            if self.state["status"] in {"starting", "waiting", "checking"}:
                return dict(self.state)
            if not self.bridge.mcp:
                raise ValueError("当前连接模式不支持统一登录")
            self.state = {"status": "starting", "message": "正在打开登录窗口"}
            threading.Thread(target=self._run, daemon=True, name="fireplay-login").start()
            return dict(self.state)

    def _accept(self, token, refresh_token=None):
        config = self.bridge.mcp.config
        candidate = replace(config, _credential_file=None, _environment={config.token_env: token})
        with FireplayMCP(candidate) as client:
            result = client.status(probe=True)
            if not result.get("ready"):
                raise ValueError("登录身份未通过 Fireplay 连接核验")
            client.requirement_delivery_options()
        path = self.root / "runtime/config/fireplay-login-token.txt"
        CredentialSession(path).install(token, refresh_token)
        with self.bridge.lock:
            self.bridge.mcp.config = replace(config, _credential_file=path)
            self.bridge._refresh_identity()

    def _run(self):
        phase = "browser"
        try:
            from playwright.sync_api import sync_playwright

            artifacts = self.root / "runtime/tmp/fireplay-login"
            artifacts.mkdir(parents=True, exist_ok=True)
            with sync_playwright() as playwright:
                # This profile belongs only to this project, never the user's everyday browser.
                context = playwright.chromium.launch_persistent_context(
                    str(self.root / "runtime/config/fireplay-login-browser"),
                    channel="chrome", headless=False, artifacts_dir=str(artifacts),
                )
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    phase = "navigation"
                    page.goto("https://authhub.cms1.cc/login", wait_until="domcontentloaded")
                    self._set("waiting", "请在打开的窗口中完成飞书登录")
                    deadline = time.monotonic() + 600
                    seen = set()
                    while time.monotonic() < deadline and not page.is_closed():
                        location = urlsplit(page.url)
                        if location.scheme == "https" and location.netloc == "authhub.cms1.cc":
                            phase = "token"
                            try:
                                pair = page.evaluate("({access: localStorage.getItem('auth_token'), "
                                                     "refresh: localStorage.getItem('refresh_token')})")
                                token, refresh_token = pair.get("access"), pair.get("refresh")
                            except Exception as exc:
                                # OAuth redirects may replace the JS context between URL check and evaluation.
                                if "Execution context was destroyed" in str(exc):
                                    continue
                                raise
                            if token and token not in seen:
                                seen.add(token)
                                config = replace(self.bridge.mcp.config, _credential_file=None,
                                                 _environment={self.bridge.mcp.config.token_env: token})
                                expiry = config.credential_expiry()
                                if expiry is not None and expiry > time.time():
                                    self._set("checking", "正在核验 Fireplay 连接")
                                    phase = "verification"
                                    self._accept(token, refresh_token)
                                    self._set("succeeded", "登录成功，已启用自动续期" if refresh_token else
                                              "登录成功；登录服务未提供续期凭据，到期仍需重新登录")
                                    return
                        page.wait_for_timeout(1000)
                    self._set("cancelled", "登录窗口已关闭或等待超时，可重新登录")
                finally:
                    context.close()
        except Exception as exc:
            # Browser errors can contain callback URLs; never return raw exceptions.
            messages = {
                "browser": "无法打开登录窗口，请关闭已有的项目登录窗口后重试",
                "navigation": "登录页面无法加载，请检查网络后重试",
                "token": "读取登录状态失败，请重新登录",
                "verification": "登录已返回，但 Fireplay 权限核验失败",
            }
            self._set("failed", messages[phase] + f"（{type(exc).__name__}）")
