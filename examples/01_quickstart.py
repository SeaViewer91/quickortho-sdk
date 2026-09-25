"""가장 간단한 사용법: 영상 폴더 하나로 정사 모자이크를 만든다.

실행:
    python 01_quickstart.py <영상 폴더> <워크스페이스>
"""

import sys

import quickortho as qo


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    images, workspace = sys.argv[1], sys.argv[2]

    try:
        result = qo.process(images, workspace, on_event=qo.print_progress)
    except qo.QuickOrthoError as exc:
        # code로 오류 종류를 구분할 수 있음 (예: too_few_images, sfm_failed)
        print(f"실패 [{exc.code}] {exc.message}")
        return 1

    print()
    print(f"정사 모자이크 : {result.orthomosaic}")
    print(f"좌표계        : EPSG:{result.epsg}")
    print(f"GSD           : {result.gsd_m * 100:.1f} cm")
    print(f"크기          : {result.width} × {result.height} px")
    print(f"처리 시간     : {result.total_time_s:.0f}초, 최대 메모리 {result.peak_memory_mb / 1024:.2f} GB")
    for w in result.warnings:
        print(f"경고          : {w}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
