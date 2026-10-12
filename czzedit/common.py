from __future__ import annotations

import json
import math
import os
import re
import tempfile
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse


class AppError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def video_id(value: str) -> str:
    if not isinstance(value, str):
        raise AppError("INVALID_URL", "치지직 다시보기 URL을 입력해 주세요.")
    value = value.strip()
    if re.fullmatch(r"[0-9]{1,20}", value):
        return value
    try:
        url = urlparse(value)
        match = re.fullmatch(r"/video/([0-9]{1,20})/?", url.path)
        if (url.scheme == "https" and url.hostname == "chzzk.naver.com"
                and url.port in (None, 443) and not url.username and not url.password and match):
            return match[1]
    except ValueError:
        pass
    raise AppError("INVALID_URL", "https://chzzk.naver.com/video/영상번호 형식으로 입력해 주세요.")


def normalized(text: str) -> str:
    text = " ".join(unicodedata.normalize("NFC", text).lower().split())
    return re.sub(r"(.)\1{2,}", r"\1\1", text)


def stamp(ms: int) -> str:
    seconds, milli = divmod(max(0, int(ms)), 1000)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}.{milli:03}"


def escaped(value: str) -> str:
    return (value.replace("\\", "\\\\").replace("\r", "\\r")
            .replace("\n", "\\n").replace("\t", "\\t"))


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


@dataclass(frozen=True)
class Config:
    sensitive: bool = False
    reaction_delay_ms: int = 0
    pre_roll_ms: int = 30000
    post_roll_ms: int = 30000
    ignored_phrases: tuple[str, ...] = ("안녕하세요", "ㅎㅇ", "출첵")
    viewer_adaptive: bool = False

    def __post_init__(self):
        if type(self.sensitive) is not bool or type(self.viewer_adaptive) is not bool:
            raise AppError("CONFIG", "sensitive는 true 또는 false여야 합니다.")
        for name in ("reaction_delay_ms", "pre_roll_ms", "post_roll_ms"):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= 300000:
                raise AppError("CONFIG", f"{name}: 0~300000 범위의 밀리초 정수가 필요합니다.")
        if (not isinstance(self.ignored_phrases, (list, tuple))
                or any(not isinstance(x, str) for x in self.ignored_phrases)):
            raise AppError("CONFIG", "ignored_phrases는 문자열 목록이어야 합니다.")

    def to_dict(self):
        return asdict(self)


def numeric(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise AppError("SCHEMA_CHANGED", f"{name} 필드를 해석할 수 없습니다.")
    return value
