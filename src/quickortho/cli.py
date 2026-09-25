"""명령줄 진입점 (``quickortho``, ``quickortho-engine``).

모든 명령은 stdout으로 JSON-lines 이벤트를 출력함 (형식은 ``docs/cli.md`` 참고).
명령은 공개 SDK API를 그대로 호출하므로 CLI 결과와 SDK 결과는 같음.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
import traceback
from pathlib import Path

from ._core.protocol import PROTOCOL_VERSION, Emitter
from ._version import __version__
from .errors import QuickOrthoError


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="quickortho", description=f"QuickOrtho SDK {__version__} 명령줄 도구")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="SDK 버전과 실행 환경 출력")

    p_scan = sub.add_parser("scan", help="영상 폴더의 EXIF/XMP 스캔")
    p_scan.add_argument("folder", type=Path)
    p_scan.add_argument("--recursive", action="store_true", help="하위 폴더까지 스캔")

    p_prev = sub.add_parser("preview", help="빠른 미리보기 (EXIF 기반 촬영 범위·중복도·간이 모자이크)")
    p_prev.add_argument("folder", type=Path, help="영상 폴더")
    p_prev.add_argument("-o", "--output", type=Path, required=True, help="결과 폴더")
    p_prev.add_argument("--max-size", type=int, default=2048, help="결과 PNG 긴 변(px)")
    p_prev.add_argument(
        "--skip-quicklook", action="store_true", help="간이 모자이크 없이 촬영 범위·중복도만 계산 (데이터 불러오기)"
    )

    def add_sfm(p: argparse.ArgumentParser) -> None:
        p.add_argument("--max-image-size", type=int, default=2000, help="특징점 추출용 영상 긴 변(px)")
        p.add_argument("--max-features", type=int, default=4096, help="영상당 최대 특징점 수")
        p.add_argument("--threads", type=int, default=-1, help="스레드 수 (-1: 전체)")
        p.add_argument("--keep-work", action="store_true", help="중간 산출물(SfM DB 등) 보존")

    def add_render(p: argparse.ArgumentParser) -> None:
        p.add_argument("--gsd", type=float, default=None, help="출력 GSD(m). 기본값은 원본 GSD × --gsd-scale")
        p.add_argument("--gsd-scale", type=float, default=2.0, help="원본 GSD 대비 출력 배율 (기본 2)")

    p_ortho = sub.add_parser("ortho", help="정사 모자이크 생성 (정렬 + 정사 모자이크)")
    p_ortho.add_argument("folder", type=Path, help="영상 폴더")
    p_ortho.add_argument("-o", "--output", type=Path, required=True, help="워크스페이스(결과 폴더)")
    add_render(p_ortho)
    add_sfm(p_ortho)

    p_align = sub.add_parser("align", help="정렬만 실행 (스캔 → SfM → GPS 좌표 정렬)")
    p_align.add_argument("folder", type=Path, help="영상 폴더")
    p_align.add_argument("-o", "--output", type=Path, required=True, help="워크스페이스(결과 폴더)")
    add_sfm(p_align)

    p_render = sub.add_parser("render", help="정렬된 워크스페이스로 정사 모자이크만 다시 생성")
    p_render.add_argument("ortho_dir", type=Path, help="워크스페이스")
    add_render(p_render)

    # ── 정밀 보정 ──
    p_info = sub.add_parser("project-info", help="보정용 프로젝트 정보 (영상 목록, 좌표계, 수정 사항)")
    p_info.add_argument("ortho_dir", type=Path, help="워크스페이스")

    p_tp = sub.add_parser("tiepoints", help="타이포인트 오차 통계 (점별·영상별, 자동 제거 미리보기)")
    p_tp.add_argument("ortho_dir", type=Path)

    p_pred = sub.add_parser("predict", help="찍은 점 또는 측량 좌표로 다른 사진에서의 위치 예측")
    p_pred.add_argument("ortho_dir", type=Path)
    p_pred.add_argument("--spec", required=True, help='JSON: {"marks": [...], "world": {...}, "chips": true}')

    p_gcp = sub.add_parser("gcp-parse", help="GCP 측량 성과 파일(CSV·TXT) 읽기")
    p_gcp.add_argument("file", type=Path)
    p_gcp.add_argument("--encoding", default=None)
    p_gcp.add_argument("--delimiter", default=None, help="',', '\\t', ';', 'whitespace'")
    p_gcp.add_argument("--ortho", type=Path, default=None, help="좌표계 추정에 쓸 워크스페이스")

    p_ed = sub.add_parser("edits-save", help="보정 수정 사항(edits.json) 저장 (전체 교체)")
    p_ed.add_argument("ortho_dir", type=Path)
    p_ed.add_argument("--edits", required=True, help="JSON")

    p_ref = sub.add_parser("refine", help="수정 사항을 적용해 번들 조정 후 정사 모자이크 재생성")
    p_ref.add_argument("ortho_dir", type=Path)
    p_ref.add_argument("--reset", action="store_true", help="보정을 취소하고 최초 결과로 되돌림")

    sub.add_parser("serve", help="상주 모드: stdin으로 작업 요청(JSON-lines)을 받아 차례로 처리")
    return parser


class _ArgError(Exception):
    pass


class _RequestError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    """serve 모드에서 잘못된 요청이 프로세스를 종료시키지 않도록 예외로 바꾼다."""

    def error(self, message: str):  # type: ignore[override]
        raise _ArgError(message)


def _options(args: argparse.Namespace):
    from .options import OrthoOptions, SfmOptions

    sfm = SfmOptions(
        max_image_size=getattr(args, "max_image_size", 2000),
        max_num_features=getattr(args, "max_features", 4096),
        num_threads=getattr(args, "threads", -1),
    )
    return OrthoOptions(
        gsd_m=getattr(args, "gsd", None),
        gsd_scale=getattr(args, "gsd_scale", 2.0),
        keep_work=getattr(args, "keep_work", False),
        sfm=sfm,
    )


def _dispatch(args: argparse.Namespace, out: Emitter) -> None:
    import quickortho as qo
    from ._core import marking

    def on_event(ev: "qo.Event") -> None:
        out._emit(ev.to_dict())

    cmd = args.command
    if cmd == "version":
        out.result("version", {
            "engine": __version__,
            "sdk": __version__,
            "protocol": PROTOCOL_VERSION,
            "python": platform.python_version(),
            "os": platform.system(),
            "arch": platform.machine(),
        })
    elif cmd == "scan":
        out.result("scan", qo.scan(args.folder, recursive=args.recursive, on_event=on_event).raw)
    elif cmd == "preview":
        opts = qo.PreviewOptions(max_size=args.max_size, quicklook=not args.skip_quicklook)
        out.result("preview", qo.preview(args.folder, args.output, opts, on_event=on_event).raw)
    elif cmd == "ortho":
        res = qo.Project.create(args.folder, args.output).process(_options(args), on_event=on_event)
        out.result("ortho", res.report)
    elif cmd == "align":
        res = qo.Project.create(args.folder, args.output).align(_options(args), on_event=on_event)
        out.result("align", res.raw)
    elif cmd == "render":
        res = qo.Project.open(args.ortho_dir).orthomosaic(_options(args), on_event=on_event)
        out.result("render", res.report)
    elif cmd == "project-info":
        out.result(cmd, marking.project_info(args.ortho_dir))
    elif cmd == "tiepoints":
        out.result(cmd, qo.Project.open(args.ortho_dir).tiepoint_stats())
    elif cmd == "predict":
        spec = json.loads(args.spec)
        out.result(cmd, qo.Project.open(args.ortho_dir).predict(
            spec.get("marks", []), spec.get("world"), chips=bool(spec.get("chips"))))
    elif cmd == "gcp-parse":
        out.result(cmd, qo.read_gcp_file(args.file, args.encoding, args.delimiter, args.ortho))
    elif cmd == "edits-save":
        qo.Project.open(args.ortho_dir)  # 정렬 여부 확인
        try:
            res = marking.save_edits(args.ortho_dir, json.loads(args.edits))
        except (KeyError, TypeError, ValueError) as exc:
            raise qo.InputError(f"보정 수정 사항 형식이 잘못됨: {exc}", "invalid_edits") from exc
        out.result(cmd, res)
    elif cmd == "refine":
        proj = qo.Project.open(args.ortho_dir)
        if args.reset:
            out.result("refine", proj.reset_refinement(on_event=on_event).report)
        else:
            out.result("refine", proj.refine(on_event=on_event).ortho.report)
    else:
        raise _ArgError(f"지원하지 않는 명령: {cmd}")


def _report_error(out: Emitter, exc: BaseException) -> None:
    if isinstance(exc, _ArgError):
        out.error(f"잘못된 인자: {exc}", "", code="invalid_argument")
    elif isinstance(exc, _RequestError):
        out.error(str(exc), "", code="invalid_request")
    elif isinstance(exc, QuickOrthoError):
        out.error(exc.message, traceback.format_exc(), code=exc.code)
    else:
        out.error(str(exc), traceback.format_exc(), code="internal_error")


def serve(stdin=None, stdout=None) -> int:
    """상주 모드. 한 줄에 요청 하나: {"job": 1, "argv": ["preview", "<폴더>", "-o", "<결과>"]}

    - 시작 시 무거운 모듈을 미리 불러 두고 {"type": "ready", "protocol": 1, ...}을 출력한다.
    - 작업 이벤트에는 "job" 필드가 붙고, 끝나면 {"type": "done", "job": n, "code": 0|1}을 출력한다.
    - 작업 중단은 호출 측이 프로세스를 종료하고 새로 띄우는 방식으로 한다.
    - stdin이 닫히면 종료한다.
    """
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    base = Emitter(stdout)
    t0 = time.perf_counter()
    try:
        import quickortho  # noqa: F401  미리 불러와 첫 작업 대기를 줄임
        from ._core import marking, pipeline, preview, refine, sfm  # noqa: F401
    except Exception as exc:  # 번들 누락 등은 ready에 담아 알림
        base._emit({"type": "ready", "ok": False, "error": str(exc), "version": __version__,
                    "protocol": PROTOCOL_VERSION})
    else:
        base._emit({"type": "ready", "ok": True, "version": __version__, "protocol": PROTOCOL_VERSION,
                    "warmup_s": round(time.perf_counter() - t0, 2)})
    parser = _build_parser()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        job = None
        try:
            try:
                req = json.loads(line)
                job = int(req["job"])
                argv = [str(a) for a in req["argv"]]
            except (ValueError, KeyError, TypeError) as exc:
                raise _RequestError(f"잘못된 요청 형식 (필요: {{\"job\": 정수, \"argv\": [...]}}): {exc}") from exc
            out = Emitter(stdout, job=job)
            args = parser.parse_args(argv)
            if args.command == "serve":
                raise _ArgError("serve 안에서 serve를 실행할 수 없음")
            _dispatch(args, out)
            code = 0
        except Exception as exc:
            _report_error(Emitter(stdout, job=job), exc)
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
        out.error(f"잘못된 인자: {exc}", code="invalid_argument")
        return 2
    if args.command == "serve":
        return serve()
    try:
        _dispatch(args, out)
        return 0
    except Exception as exc:  # 호출 측이 항상 error 이벤트를 받도록 함
        _report_error(out, exc)
        return 1
