"""엔진 명령줄 진입점."""

from __future__ import annotations

import argparse
import platform
import sys
import traceback
from pathlib import Path

from . import __version__
from .protocol import Emitter
from .scan import scan_folder


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quickortho-engine")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="엔진 버전과 실행 환경 출력")

    p_scan = sub.add_parser("scan", help="영상 폴더의 EXIF/XMP 스캔")
    p_scan.add_argument("folder", type=Path)
    p_scan.add_argument("--recursive", action="store_true", help="하위 폴더까지 스캔")

    p_prev = sub.add_parser("preview", help="빠른 미리보기 (EXIF 기반 촬영 범위·중복도·간이 모자이크)")
    p_prev.add_argument("folder", type=Path, help="영상 폴더")
    p_prev.add_argument("-o", "--output", type=Path, required=True, help="결과 폴더")
    p_prev.add_argument("--max-size", type=int, default=2048, help="간이 모자이크 긴 변(px)")

    p_ortho = sub.add_parser("ortho", help="정사 모자이크 생성 (fast ortho)")
    p_ortho.add_argument("folder", type=Path, help="영상 폴더")
    p_ortho.add_argument("-o", "--output", type=Path, required=True, help="결과 폴더")
    p_ortho.add_argument("--gsd", type=float, default=None, help="출력 GSD(m). 기본값은 원본 GSD × --gsd-scale")
    p_ortho.add_argument("--gsd-scale", type=float, default=2.0, help="원본 GSD 대비 출력 배율 (기본 2)")
    p_ortho.add_argument("--max-image-size", type=int, default=2000, help="특징점 추출용 영상 긴 변(px)")
    p_ortho.add_argument("--max-features", type=int, default=4096, help="영상당 최대 특징점 수")
    p_ortho.add_argument("--threads", type=int, default=-1, help="스레드 수 (-1: 전체)")
    p_ortho.add_argument("--keep-work", action="store_true", help="중간 산출물(SfM DB 등) 보존")
    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows에서 한글 경로·메시지가 깨지지 않도록 stdout을 UTF-8로 고정
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    args = _build_parser().parse_args(argv)
    out = Emitter()
    try:
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

            out.result("preview", run_preview(args.folder, args.output, out, max_size=args.max_size))
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
        return 0
    except Exception as exc:  # 앱이 항상 error 이벤트를 받도록 함
        out.error(str(exc), traceback.format_exc())
        return 1
