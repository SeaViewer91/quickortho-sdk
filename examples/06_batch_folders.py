"""여러 영상 폴더를 차례로 처리하고 결과를 CSV로 정리한다. 한 폴더가 실패해도 나머지는 계속 처리함.

폴더 구조 예:
    flights/
    ├── 2026-09-01_A/   (영상)
    ├── 2026-09-01_B/
    └── ...

실행:
    python 06_batch_folders.py <상위 폴더> <결과 상위 폴더>
결과:
    <결과 상위 폴더>/<폴더명>/orthomosaic.tif ..., <결과 상위 폴더>/summary.csv
"""

import csv
import sys
from pathlib import Path

import quickortho as qo


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    root, out_root = Path(sys.argv[1]), Path(sys.argv[2])
    out_root.mkdir(parents=True, exist_ok=True)
    rows = []
    folders = sorted(p for p in root.iterdir() if p.is_dir())
    for i, folder in enumerate(folders, 1):
        print(f"[{i}/{len(folders)}] {folder.name}")
        row = {"folder": folder.name, "status": "", "code": "", "images": "", "registered": "",
               "gsd_cm": "", "gps_residual_m": "", "time_s": "", "orthomosaic": "", "message": ""}
        try:
            res = qo.process(folder, out_root / folder.name)
            rep = res.report
            row.update(status="ok", images=rep["sfm"]["num_input_images"], registered=rep["sfm"]["num_registered"],
                       gsd_cm=round(res.gsd_m * 100, 2), gps_residual_m=round(rep["georef"]["gps_residual_rms_m"], 2),
                       time_s=round(res.total_time_s), orthomosaic=str(res.orthomosaic),
                       message=" / ".join(res.warnings))
        except qo.QuickOrthoError as exc:
            row.update(status="failed", code=exc.code, message=exc.message)
            print(f"  실패 [{exc.code}] {exc.message}")
        rows.append(row)

    with open(out_root / "summary.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["folder"])
        w.writeheader()
        w.writerows(rows)
    ok = sum(r["status"] == "ok" for r in rows)
    print(f"완료 {ok}/{len(rows)} → {out_root / 'summary.csv'}")
    return 0 if ok == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
