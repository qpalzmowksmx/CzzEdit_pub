"""Resolve public playback only; never persist signed media URLs."""
import json
from urllib.parse import urlparse

from .common import AppError
from .transport import PublicTransport


def public_source(data):
    if data.get("adult") or data.get("paidProductId") or data.get("paidProduct") or data.get("encryptionType"):
        raise AppError("PLAYBACK_RESTRICTED", "이 영상은 앱 내부 공개 재생을 지원하지 않습니다. 치지직에서 시청 권한을 확인해 주세요.")
    try:
        playback = json.loads(data.get("liveRewindPlaybackJson") or "{}")
        meta = playback.get("meta", {})
        if meta.get("paidLive") or meta.get("playbackAuthType") != "NONE":
            raise ValueError
        for media in playback.get("media", []):
            url = media.get("path", "")
            parsed = urlparse(url)
            if (media.get("protocol") == "HLS" and parsed.scheme == "https"
                    and parsed.hostname and parsed.hostname.endswith(".navercdn.com")
                    and parsed.port in (None, 443) and not parsed.username and not parsed.password):
                return {"url": url, "type": "hls"}
    except (ValueError, TypeError, AttributeError):
        pass
    raise AppError("PLAYBACK_UNAVAILABLE", "공개 재생 주소를 확인하지 못했습니다. 이 영상은 치지직에서 확인해 주세요.")


def playback_source(store, rid):
    run = store.run(rid)
    session = store.session(run["session_id"])
    if session["video_id"] == "demo":
        raise AppError("DEMO", "샘플 채팅에는 연결된 영상이 없습니다.")
    transport = PublicTransport()
    try:
        return public_source(transport.get(f'/service/v3/videos/{session["video_id"]}'))
    finally:
        transport.close()
