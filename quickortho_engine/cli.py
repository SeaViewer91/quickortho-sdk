"""엔진 명령줄 진입점."""

from __future__ import annotations

import argparse
import json
import platform
import sys
import traceback
from pathlib import Path

from . import __version__
from .protocol import Emitter
from .scan import scan_folder


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="quickortho-engine")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="엔진 버전과 실행 환경 출력")

    p_scan = sub.add_parser("scan", help="영상 폴더의 EXIF/XMP 스캔")
    p_scan.add_argument("folder", type=Path)
    p_scan.add_argument("--recursive", action="store_true", help="하위 폴더까지 스캔")

    p_prev = sub.add_parser("preview", help="빠른 미리보기 (EXIF 기반 촬영 범위·중복도·간이 모자이크)")
    p_prev.add_argument("folder", type=Path, help="영상 폴더")
    p_prev.add_argument("-o", "--output", type=Path, required=True, help="결과 폴더")
    p_prev.add_argument("--max-size", type=int, default=2048, help="간이 모자이크 긴 변(px)")
    p_prev.add_argument(
        "--skip-quicklook", action="store_true", help="간이 모자이크 없이 촬영 범위·중복도만 계산 (데이터 불러오기)"
    )

    p_ortho = sub.add_parser("ortho", help="정사 모자이크 생성 (fast ortho)")
    p_ortho.add_argument("folder", type=Path, help="영상 폴더")
    p_ortho.add_argument("-o", "--output", type=Path, required=True, help="결과 폴더")
    p_ortho.add_argument("--gsd", type=float, default=None, help="출력 GSD(m). 기본값은 원본 GSD × --gsd-scale")
    p_ortho.add_argument("--gsd-scale", type=float, default=2.0, help="원본 GSD 대비 출력 배율 (기본 2)")
    p_ortho.add_argument("--max-image-size", type=int, default=2000, help="특징점 추출용 영상 긴 변(px)")
    p_ortho.add_argument("--max-features", type=int, default=4096, help="영상당 최대 특징점 수")
    p_ortho.add_argument("--threads", type=int, default=-1, help="스레드 수 (-1: 전체)")
    p_ortho.add_argument("--keep-work", action="store_true", help="중간 산출물(SfM DB 등) 보존")

    sub.add_parser("serve", help="상주 모드: stdin으로 작업 요청(JSON-lines)을 받아 차례로 처리")
    return parser


class _ArgError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    """serve 모드에서 잘못된 요청이 프로세스를 종료시키지 않도록 예외로 바꾼다."""

    def error(self, message: str):  # type: ignore[override]
        raise _ArgError(message)


def _dispatch(args: argparse.Namespace, out: Emitter) -> None:
    if args.command == "version":
        out.result(
            "version",
            {
                "engine": __version__,
                "python": platform.python_version(),
                "os": platform.system(),
                "arch": platform.machine(),
            },
        )
    elif args.command == "scan":
        out.result("scan", scan_folder(args.folder, recursive=args.recursive, emitter=out))
    elif args.command == "preview":
        from .preview import run_preview

        res = run_preview(args.folder, args.output, out, max_size=args.max_size, quicklook=not args.skip_quicklook)
        out.result("preview", res)
    elif args.command == "ortho":
        # 무거운 의존성(pycolmap, rasterio 등)은 ortho 명령에서만 불러옴
        from .pipeline import OrthoOptions, run_ortho
        from .sfm import SfmOptions

        opts = OrthoOptions(
            gsd_m=args.gsd,
            gsd_scale=args.gsd_scale,
            keep_work=args.keep_work,
            sfm=SfmOptions(
                max_image_size=args.max_image_size,
                max_num_features=args.max_features,
                num_threads=args.threads,
            ),
        )
        out.result("ortho", run_ortho(args.folder, args.output, opts, out))
    else:
        raise _ArgError(f"지원하지 않는 명령: {args.command}")


def serve(stdin=None, stdout=None) -> int:
    """상주 모드. 한 줄에 요청 하나: {"job": 1, "argv": ["preview", "<폴더>", "-o", "<결과>"]}

    - 시작 시 무거운 모듈을 미리 불러 두고 {"type": "ready"}를 출력한다.
    - 작업 이벤트에는 "job" 필드가 붙고, 끝나면 {"type": "done", "job": n, "code": 0|1}을 출력한다.
    - 작업 중단은 앱이 프로세스를 종료하고 새로 띄우는 방식으로 한다.
    - stdin이 닫히면 종료한다.
    """
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    base = Emitter(stdout)
    t0 = __import__("time").perf_counter()
    try:
        from . import pipeline, preview, sfm  # noqa: F401  미리 불러와 첫 작업 대기를 줄임
    except Exception as exc:  # 번들 누락 등은 ready에 담아 앱에 알림
        base._emit({"type": "ready", "ok": False, "error": str(exc), "version": __version__})
    else:
        base._emit({"type": "ready", "ok": True, "version": __version__,
                    "warmup_s": round(__import__("time").perf_counter() - t0, 2)})
    parser = _build_parser()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        job = None
        try:
            req = json.loads(line)
            job = int(req["job"])
            out = Emitter(stdout, job=job)
            args = parser.parse_args([str(a) for a in req["argv"]])
            if args.command == "serve":
                raise _ArgError("serve 안에서 serve를 실행할 수 없음")
            _dispatch(args, out)
            code = 0
        except Exception as exc:
            Emitter(stdout, job=job).error(str(exc), traceback.format_exc())
            code = 1
        base._emit({"type": "done", "job": job, "code": code})
    return 0


def main(argv: list[str] | None = None) -> int:
    # Windows에서 한글 경로·메시지가 깨지지 않도록 표준 입출력을 UTF-8로 고정
    for stream in (sys.stdout, sys.stdin):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    out = Emitter()
    try:
        args = _build_parser().parse_args(argv)
    except _ArgError as exc:
        out.error(f"잘못된 인자: {exc}")
        return 2
    if args.command == "serve":
        return serve()
    try:
        _dispatch(args, out)
        return 0
    except Exception as exc:  # 앱이 항상 error 이벤트를 받도록 함
        out.error(str(exc), traceback.format_exc())
        return 1
