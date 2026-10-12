from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .common import AppError, Config, stamp
from .exporter import export_run
from .service import capture, demo, reanalyze
from .store import Store, WorkspaceLock


def main():
    parser = argparse.ArgumentParser(description="CzzEdit — 방송자 계정 없이 다시보기 채팅을 수집하고 편집점을 찾습니다.")
    parser.add_argument("--workspace", type=Path, default=Path(".czzedit"), help="저장 폴더 (기본 .czzedit)")
    commands = parser.add_subparsers(dest="command")
    serve = commands.add_parser("serve", help="로컬 화면 열기 (기본)")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--no-open", action="store_true")
    collect_parser = commands.add_parser("collect", help="다시보기 수집 + TXT + 자동 편집점")
    collect_parser.add_argument("url")
    collect_parser.add_argument("--resume", help="이어받을 수집 작업 ID")
    collect_parser.add_argument("--login", action="store_true", help="본인 일반 시청 계정으로 브라우저 로그인")
    collect_parser.add_argument("--max-pages", type=int, default=0, help="이번에 받을 페이지 수; 0이면 끝까지")
    demo_parser = commands.add_parser("demo", help="계정·네트워크 없는 합성 데이터 체험")
    analyze_parser = commands.add_parser("analyze", help="저장된 채팅 재분석")
    analyze_parser.add_argument("session_id")
    for sub in (collect_parser, demo_parser, analyze_parser):
        sub.add_argument("--sensitive", action="store_true", help="소규모 채널용 민감 프리셋")
        sub.add_argument("--config", type=Path, help="탐지 설정 JSON (README 참고)")
    export_parser = commands.add_parser("export", help="저장된 분석 결과를 파일로 복구")
    export_parser.add_argument("run_id")
    comment_parser=commands.add_parser('comments',help='영상 댓글과 시청자 추천 수집')
    comment_parser.add_argument('url')
    comment_parser.add_argument('--text-file',type=Path,help='공개 댓글 TXT 직접 가져오기')
    import_parser=commands.add_parser('viewer-import',help='시청자 표본 JSON 목록 가져오기')
    import_parser.add_argument('file',type=Path)
    map_parser=commands.add_parser('viewer-map',help='확인한 VOD 시간 대응 JSON 저장')
    map_parser.add_argument('session_id');map_parser.add_argument('monitor_id');map_parser.add_argument('file',type=Path)
    map_parser.add_argument('--confirmed',action='store_true')
    commands.add_parser("list", help="저장된 수집 작업 목록")
    args = parser.parse_args()

    def progress(value):
        phase = value.get("phase")
        if value.get("session_id"):
            print("작업 ID:", value["session_id"], flush=True)
        if "position_ms" in value:
            print(f'{phase or "진행"}: {stamp(value["position_ms"])}'
                  + (f' / {value["pages"]}페이지' if "pages" in value else ""), flush=True)
        if value.get("output_error"):
            print(value["output_error"], file=sys.stderr)

    try:
        if args.command in (None, "serve"):
            from .server import serve
            serve(args.workspace, getattr(args, "port", 8765), not getattr(args, "no_open", False))
            return 0
        settings = {}
        if getattr(args, "config", None):
            try:
                settings = json.loads(args.config.read_text(encoding="utf-8"))
                if not isinstance(settings, dict):
                    raise ValueError
            except (OSError, ValueError):
                raise AppError("CONFIG", "설정 파일은 올바른 JSON 객체여야 합니다.") from None
        if getattr(args, "sensitive", False):
            settings["sensitive"] = True
        try:
            config = Config(**settings)
        except TypeError:
            raise AppError("CONFIG", "지원하지 않는 설정 항목이 있습니다.") from None
        if args.command == "collect":
            if args.max_pages < 0:
                raise AppError("CONFIG", "페이지 제한은 0 이상의 정수여야 합니다.")
            result = capture(args.workspace, args.url, config, args.resume, args.login,
                             max_pages=args.max_pages, progress=progress)
        elif args.command == "demo":
            result = demo(args.workspace, config, progress)
        elif args.command == "analyze":
            result = reanalyze(args.workspace, args.session_id, config, progress)
        else:
            with WorkspaceLock(args.workspace):
                store = Store(args.workspace)
                try:
                    if args.command=='comments':
                        from . import timeline,comments
                        sid=timeline.source(store,args.url)
                        cid=comments.capture(store,sid,text=args.text_file.read_text(encoding='utf-8') if args.text_file else None)
                        runs=store.db.execute('SELECT id FROM runs WHERE session_id=? ORDER BY created DESC LIMIT 1',(sid,)).fetchone()
                        print('타임라인:',timeline.create(store,sid,runs[0] if runs else None,cid))
                    elif args.command=='viewer-import':
                        from .viewers import import_samples
                        print('시청자 기록:',import_samples(store,json.loads(args.file.read_text(encoding='utf-8'))))
                    elif args.command=='viewer-map':
                        from .viewers import mapping
                        print('시간 대응:',mapping(store,args.session_id,args.monitor_id,json.loads(args.file.read_text(encoding='utf-8')),args.confirmed))
                    elif args.command == "list":
                        print(json.dumps(store.sessions(), ensure_ascii=False, indent=2))
                    else:
                        print(export_run(store, args.run_id))
                    return 0
                finally:
                    store.close()
        print(f'편집 후보 {result["marker_count"]}개\n결과: {result["directory"]}')
        if result.get("source_state") != "finished":
            print("부분 수집 결과입니다. 작업 ID로 이어받을 수 있습니다.")
        return 0
    except AppError as error:
        print(f"[{error.code}] {error}", file=sys.stderr)
        return 2
    except (ValueError,TypeError,AttributeError):
        print("입력 자료의 형식을 확인해 주세요.",file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error):
        print("저장소 또는 로컬 서버에 접근하지 못했습니다. 저장 공간·권한·포트를 확인해 주세요.", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("\n중단했습니다. 저장된 데이터는 작업 폴더에 남습니다.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
