"""다중분광(MicaSense RedEdge·Altum 계열) 폴더로 반사율 정사 모자이크와 NDVI를 만들고 통계를 낸다.

실행:
    python 07_multispectral.py <밴드 TIFF 폴더> <워크스페이스>
"""

import sys

import numpy as np

import quickortho as qo


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    images, workspace = sys.argv[1], sys.argv[2]

    scan = qo.scan(images)
    if scan.multispectral is None:
        print("다중분광 영상이 없음 (MicaSense 밴드 TIFF 폴더를 지정해야 함)")
        return 1
    s = scan.multispectral
    print(f"{s['model']}: 캡처 {s['captures']}개, 패널 {s['panel_captures']}개, 밴드 {[b['name'] for b in s['bands']]}")

    try:
        result = qo.process(images, workspace, qo.OrthoOptions(radiometry="auto"), on_event=qo.print_progress)
    except qo.QuickOrthoError as exc:
        print(f"실패 [{exc.code}] {exc.message}")
        return 1

    ms = result.multispectral
    print()
    print(f"반사율 방식: {ms.radiometry}, 값: {ms.values}")
    print("밴드 정합 잔차(화소):", {b: round(v, 2) for b, v in ms.band_alignment_px.items()})
    if ms.capture_inconsistency_pct:
        print("캡처 간 불일치(%) 보정 전→후:", ms.capture_inconsistency_pct)
    for w in result.warnings:
        print("경고:", w)

    refl, transform, crs = result.read(scale=0.25)  # (H, W, 밴드) float32, 자료 없는 곳 NaN
    for k, name in enumerate(ms.bands):
        print(f"{name:>10}: 반사율 중앙값 {np.nanmedian(refl[..., k]):.3f}")
    if "NDVI" in ms.index_names:
        ndvi, _, _ = ms.read_indices("NDVI", scale=0.25)
        print(f"NDVI 5·50·95 백분위: {np.round(np.nanpercentile(ndvi, [5, 50, 95]), 3)}")
        valid = ndvi[np.isfinite(ndvi)]  # 자료 없는 곳(NaN)은 빼고 셈
        print(f"NDVI > 0.6 면적 비율: {np.mean(valid > 0.6) * 100:.1f}%")
        print("NDVI 미리보기:", ms.index_preview)
    return 0


if __name__ == "__main__":
    sys.exit(main())
