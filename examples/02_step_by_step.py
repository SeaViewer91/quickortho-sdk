"""단계별 처리: 스캔 → 미리보기(누락 확인) → 정렬 → 정사 모자이크 두 가지 해상도.

정렬(SfM)은 한 번만 하고, 정사 모자이크는 GSD를 바꿔 여러 번 만들 수 있음을 보여 줌.

실행:
    python 02_step_by_step.py <영상 폴더> <워크스페이스>
"""

import sys

import quickortho as qo


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    project = qo.Project.create(sys.argv[1], sys.argv[2])

    # 1) 스캔: 헤더만 읽으므로 빠름
    scan = project.scan()
    print(f"영상 {scan.summary['total_files']}장, 처리 대상 {len(scan.selected_images)}장, GPS {scan.num_with_gps}장")
    for key, reason in scan.excluded_cameras.items():
        print(f"  제외한 카메라: {key} ({reason})")
    if scan.num_with_gps < 3:
        print("GPS가 있는 영상이 3장 미만이라 처리할 수 없음")
        return 1

    # 2) 빠른 미리보기: 촬영 누락 확인 (SfM 없이 수 초)
    pv = project.preview(qo.PreviewOptions(quicklook=False))
    print(f"누락 구역 {pv.num_gaps}곳, 중복도 중앙값 {pv.coverage_stats['overlap_median']:.0f}장")

    # 3) 정렬 (가장 오래 걸리는 단계)
    align = project.align(on_event=qo.print_progress)
    print(f"정합 {align.num_registered}/{align.num_images}장, 재투영 오차 {align.reprojection_error_px:.2f} px, "
          f"GPS 잔차 {align.gps_residual_m:.2f} m")

    # 4) 정사 모자이크: 빠른 확인용 저해상도 → 최종 해상도
    quick = project.orthomosaic(qo.OrthoOptions(gsd_scale=4.0))
    print(f"확인용: GSD {quick.gsd_m * 100:.1f} cm, {quick.width}×{quick.height}")
    final = project.orthomosaic()  # 기본값: 원본 GSD 그대로
    print(f"최종  : GSD {final.gsd_m * 100:.1f} cm, {final.width}×{final.height} → {final.orthomosaic}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
