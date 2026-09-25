"""GCP 보정: 측량 성과 파일과 사진 표시 좌표로 번들 조정 후 정사 모자이크를 다시 만든다.

준비물:
    1) 정렬을 마친 워크스페이스 (01·02 예제 또는 Project.process()로 만든 것)
    2) GCP 측량 성과 파일 (CSV·TXT). 예: 점명,X,Y,Z
    3) 사진 표시 파일 marks.csv: 점명,사진 파일명,x,y (원본 사진 화소 좌표, 점마다 2장 이상)

실행:
    python 03_gcp_refine.py <워크스페이스> <GCP 파일> <EPSG> <marks.csv> [검사점 이름,...]

예:
    python 03_gcp_refine.py ws/ gcp.csv 5186 marks.csv C1,C2
"""

import csv
import sys
from collections import defaultdict

import quickortho as qo


def main() -> int:
    if len(sys.argv) not in (5, 6):
        print(__doc__)
        return 2
    ws, gcp_file, epsg, marks_file = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
    check = sys.argv[5].split(",") if len(sys.argv) == 6 else []

    project = qo.Project.open(ws)
    gcps = qo.load_gcps(gcp_file, epsg=epsg, check=check)
    print(f"GCP {len(gcps)}점 (검사점 {sum(g.role == 'check' for g in gcps)}점)")

    # 사진 표시 좌표 읽기
    marks: dict[str, list[qo.Mark]] = defaultdict(list)
    with open(marks_file, encoding="utf-8-sig", newline="") as f:
        for row in csv.reader(f):
            if not row or row[0].startswith("#"):
                continue
            name, image, x, y = row[:4]
            marks[name].append(qo.Mark(image, float(x), float(y)))
    for g in gcps:
        g.marks = marks.get(g.name, [])
        if len(g.marks) < 2:
            # 표시가 부족한 점은 어느 사진에 보이는지 예측해 안내함
            pred = project.predict(world={"x": g.x, "y": g.y, "z": g.z, "epsg": g.epsg})
            cands = ", ".join(c["image"] for c in pred["candidates"][:5])
            print(f"  {g.name}: 표시 {len(g.marks)}장 → 이 사진들에서 찾아 표시해야 함: {cands}")

    project.set_edits(gcps=gcps, max_reproj_error_px=2.0)
    result = project.refine(on_event=qo.print_progress)

    print(f"\n좌표 기준: {result.mode}")
    print(f"재투영 RMSE: {result.rmse_before_px:.2f} → {result.rmse_after_px:.2f} px")
    for label, s in (("기준점", result.control), ("검사점", result.check)):
        if s:
            print(f"{label} {s['count']}점: 수평 {s['rmse_xy']:.3f} m, 수직 {s['rmse_z']:.3f} m")
    for row in result.gcps:
        if row["dxy"] is not None:
            print(f"  {row['name']:>6} ({row['role']}): dXY {row['dxy']:.3f} m, dZ {row['dz']:+.3f} m")
    for w in result.warnings:
        print(f"경고: {w}")
    print(f"결과: {result.ortho.orthomosaic} (EPSG:{result.ortho.epsg})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
