from __future__ import annotations

import json
import time
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .common import AppError

API = "https://api.chzzk.naver.com"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://chzzk.naver.com/"}


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlparse(newurl)
        if target.scheme != "https" or target.hostname != "api.chzzk.naver.com":
            raise AppError("REDIRECT", "예상하지 못한 주소로 이동하여 요청을 중단했습니다.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Transport:
    def __init__(self, cancel=None, interval: float = 1.0):
        self.cancel = cancel
        self.interval = max(1.0, interval)
        self.last_request = 0.0

    def pause(self, seconds):
        if self.cancel:
            if self.cancel.wait(seconds):
                raise AppError("CANCELLED", "수집을 중단했습니다. 저장된 위치에서 이어받을 수 있습니다.")
        else:
            time.sleep(seconds)

    def get(self, path: str):
        if not path.startswith("/service/"):
            raise AppError("INVALID_PATH", "허용되지 않은 요청입니다.")
        for attempt in range(5):
            self.pause(max(0, self.interval - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                status, headers, body = self.request(API + path)
            except (URLError, TimeoutError, OSError):
                if attempt == 4:
                    raise AppError("NETWORK", "네트워크 요청에 실패했습니다. 연결을 확인한 뒤 이어받기를 눌러 주세요.") from None
                self.pause(2 ** attempt)
                continue
            if status in (429, 500, 502, 503, 504):
                if attempt == 4:
                    raise AppError("RETRY_EXHAUSTED", "서버 응답이 지연됩니다. 잠시 후 이어받아 주세요.")
                retry = headers.get("retry-after", "")
                try:
                    delay = max(0, float(retry))
                except ValueError:
                    try:
                        delay = max(0, parsedate_to_datetime(retry).timestamp() - time.time())
                    except (TypeError, ValueError, OverflowError):
                        delay = 2 ** attempt
                self.pause(min(delay, 300))
                continue
            if status == 401:
                raise AppError("LOGIN_REQUIRED", "일반 시청 계정 로그인이 필요합니다. 로그인 모드로 다시 시도해 주세요.")
            if status in (403, 404):
                raise AppError("ACCESS_DENIED", "영상 또는 채팅에 접근할 수 없습니다. 시청 권한·삭제 여부를 확인해 주세요.")
            if status != 200:
                raise AppError("HTTP_ERROR", f"채팅 서버가 HTTP {status}을 반환했습니다.")
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeError):
                raise AppError("SCHEMA_CHANGED", "서버가 올바른 JSON을 반환하지 않았습니다.") from None
            if not isinstance(payload, dict):
                raise AppError("SCHEMA_CHANGED", "서버 응답 형식이 변경되었습니다.")
            code = payload.get("code")
            if code in (401, "401"):
                raise AppError("LOGIN_REQUIRED", "일반 시청 계정 로그인이 필요합니다.")
            if code in (403, 404, "403", "404"):
                raise AppError("ACCESS_DENIED", "이 계정으로 영상 또는 채팅을 열람할 수 없습니다.")
            if code != 200 or not isinstance(payload.get("content"), dict):
                raise AppError("SCHEMA_CHANGED", "서버 응답 내용이 예상한 형식과 다릅니다.")
            return payload["content"]
        raise AssertionError("unreachable")

    def close(self):
        pass


class PublicTransport(Transport):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.opener = build_opener(SafeRedirect())

    def request(self, url):
        try:
            response = self.opener.open(Request(url, headers=HEADERS), timeout=20)
        except HTTPError as error:
            response = error
        with response:
            body = response.read(8 * 1024 * 1024 + 1)
            if len(body) > 8 * 1024 * 1024:
                raise AppError("RESPONSE_TOO_LARGE", "응답 크기가 예상 범위를 넘었습니다.")
            return response.status, {k.lower(): v for k, v in response.headers.items()}, body


class BrowserTransport(Transport):
    """An isolated, ephemeral viewer login; never reads an existing browser profile."""

    def __init__(self, login_ready, **kwargs):
        super().__init__(**kwargs)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise AppError("BROWSER_SETUP", "로그인 모드는 Playwright 설치가 필요합니다. README의 일반 로그인 준비를 확인해 주세요.") from None
        self.driver = sync_playwright().start()
        self.browser = None
        try:
            self.browser = self.driver.chromium.launch(headless=False)
            self.context = self.browser.new_context()
            self.page = self.context.new_page()
            self.page.goto("https://chzzk.naver.com/", wait_until="domcontentloaded", timeout=30000)
            login_ready()
        except AppError:
            self.close()
            raise
        except Exception:
            self.close()
            raise AppError("BROWSER_SETUP", "로그인 브라우저를 열지 못했습니다. Chromium 설치와 실행 환경을 확인해 주세요.") from None

    def request(self, url):
        try:
            response = self.context.request.get(url, headers=HEADERS, timeout=20000, max_redirects=0)
            try:
                body = response.body()
                if len(body) > 8 * 1024 * 1024:
                    raise AppError("RESPONSE_TOO_LARGE", "응답 크기가 예상 범위를 넘었습니다.")
                return response.status, response.headers, body
            finally:
                response.dispose()
        except AppError:
            raise
        except Exception:
            raise AppError("BROWSER_REQUEST", "시청 세션 요청에 실패했습니다. 브라우저 상태를 확인해 주세요.") from None

    def close(self):
        try:
            if self.browser:
                self.browser.close()
        finally:
            self.driver.stop()
