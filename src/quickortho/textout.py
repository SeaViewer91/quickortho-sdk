"""명령줄 사람용 출력 (``--format text``).

JSON-lines 이벤트와 같은 내용을 진행 막대와 요약으로 보여 줌. 오류는 표준 오류로 씀.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from typing import Any, TextIO

from ._core.protocol import Emitter
from .events import STAGES


class TextEmitter(Emitter):
    def __init__(self, stream: TextIO | None = None, err: TextIO | None = None) -> None:
        super().__init__(stream if stream is not None else sys.stdout)
        self._err = err if err is not None else sys.stderr
        self._tty = bool(getattr(self._stream, "isatty", lambda: False)())
        self._bar_open = False
        self._last_pct: dict[str, int] = {}
        self._t0 = time.perf_counter()
        self._stage_t0 = self._t0

    # ── 출력 도우미 ──
    def _w(self, text: str) -> None:
        self._stream.write(text)
        self._stream.flush()

    def _line(self, text: str) -> None:
        if self._bar_open:
            self._w("\n")
            self._bar_open = False
        self._w(text + "\n")

    def _elapsed(self) -> str:
        s = int(time.perf_counter() - self._t0)
        return f"{s // 60:02d}:{s % 60:02d}"

    # ── 이벤트 ──
    def _emit(self, event: dict[str, Any]) -> None:
        t = event.get("type")
        if t == "stage":
            self._stage_t0 = time.perf_counter()
            label = event.get("message") or STAGES.get(event.get("name", ""), event.get("name", ""))
            self._line(f"[{self._elapsed()}] ▶ {label}")
        elif t == "progress":
            self._progress(event)
        elif t == "log":
            mark = "⚠ " if event.get("level") in ("warn", "warning", "error") else "  "
            self._line(f"{mark}{event.get('message', '')}")
        elif t == "result":
            self._line("")
            self._result(event.get("command", ""), event.get("data"))
        elif t == "error":
            if self._bar_open:
                self._w("\n")
                self._bar_open = False
            code = event.get("code")
            self._err.write(f"오류{f' [{code}]' if code else ''}: {event.get('message', '')}\n")
            self._err.flush()

    def _progress(self, ev: dict[str, Any]) -> None:
        cur, total = int(ev.get("current") or 0), int(ev.get("total") or 0)
        if total <= 0:
            return
        pct = int(100 * min(cur, total) / total)
        stage = ev.get("stage", "")
        if self._tty:
            width = max(10, min(40, shutil.get_terminal_size((80, 20)).columns - 40))
            fill = int(width * pct / 100)
            self._w(f"\r      [{'█' * fill}{'·' * (width - fill)}] {pct:3d}%  {cur}/{total}")
            self._bar_open = True
            if cur >= total:
                self._w("\n")
                self._bar_open = False
        else:  # 파일로 보낼 때는 25% 단위로만
            step = pct // 25
            if step > self._last_pct.get(stage, -1):
                self._last_pct[stage] = step
                self._line(f"      {stage} {pct}% ({cur}/{total})")

    # ── 결과 요약 ──
    def _result(self, command: str, data: Any) -> None:
        f = getattr(self, f"_r_{command.replace('-', '_')}", None)
        if f is not None and isinstance(data, dict):
            try:
                f(data)
                return
            except (KeyError, TypeError, ValueError):
                pass
        self._line(json.dumps(data, ensure_ascii=False, indent=2))

    def _kv(self, rows: list[tuple[str, Any]]) -> None:
        w = max(len(k) for k, _ in rows) if rows else 0
        for k, v in rows:
            self._line(f"  {k.ljust(w)}  {v}")

    def _r_version(self, d: dict) -> None:
        self._kv([("SDK", d["sdk"]), ("프로토콜", d["protocol"]), ("Python", d["python"]),
                  ("OS", f"{d['os']} {d['arch']}")])

    def _r_scan(self, d: dict) -> None:
        s = d["summary"]
        rows = [("영상", f"{s['total_files']}장 (처리 대상 {s['selected']}장)"), ("GPS 없음", f"{s['no_gps']}장"),
                ("경사 촬영", f"{s['oblique']}장"), ("롤링 셔터", "있음" if s["rolling_shutter_warning"] else "없음")]
        for key, cam in d["cameras"].items():
            rows.append(("카메라", f"{key} ×{cam['count']}{'' if cam['selected'] else ' (제외)'}"))
        self._kv(rows)

    def _r_preview(self, d: dict) -> None:
        c = d["coverage"]
        self._kv([("영상", f"{d['num_images']}장"), ("누락 구역", f"{d['gaps']}곳 ({c['gap_area_m2']:.0f} m²)"),
                  ("중복도 중앙값", f"{c['overlap_median']:.0f}장"),
                  ("전방 중복률", "-" if d.get("forward_overlap_median") is None else f"{d['forward_overlap_median']:.0%}"),
                  ("결과", d["outputs"]["coverage"])])
        for w in d.get("warnings", []):
            self._line(f"⚠ {w}")

    def _r_align(self, d: dict) -> None:
        s, g = d["sfm"], d["georef"]
        self._kv([("정합", f"{s['num_registered']}/{s['num_input_images']}장"),
                  ("재투영 오차", f"{s['mean_reprojection_error_px']:.2f} px"),
                  ("좌표계", f"EPSG:{g['epsg']}"), ("GPS 잔차", f"{g['gps_residual_rms_m']:.2f} m"),
                  ("시간", f"{d['timings_s']['total_s']:.0f}초")])
        for w in d.get("warnings", []):
            self._line(f"⚠ {w}")

    def _r_ortho(self, d: dict) -> None:
        o, g = d["ortho"], d["georef"]
        rows = []
        if "sfm" in d:
            rows.append(("정합", f"{d['sfm']['num_registered']}/{d['sfm']['num_input_images']}장, "
                                  f"재투영 {d['sfm']['mean_reprojection_error_px']:.2f} px"))
        rows += [("좌표계", f"EPSG:{g['epsg']}"), ("GSD", f"{o['gsd_m'] * 100:.1f} cm"),
                 ("크기", f"{o['width']} × {o['height']} px"),
                 ("시간", f"{d['timings_s']['total_s']:.0f}초, 최대 메모리 {d['peak_memory_mb'] / 1024:.2f} GB"),
                 ("정사 모자이크", d["outputs"]["orthomosaic"])]
        if "refine" in d:
            r = d["refine"]
            chk = (r.get("gcp_summary") or {}).get("check")
            rows.insert(0, ("보정", f"{r['mode']}, 재투영 {r['before']['rmse_px']:.2f} → {r['after']['rmse_px']:.2f} px"))
            if chk:
                rows.insert(1, ("검사점", f"{chk['count']}점, 수평 {chk['rmse_xy']:.3f} m, 수직 {chk['rmse_z']:.3f} m"))
        self._kv(rows)
        for w in d.get("warnings", []):
            self._line(f"⚠ {w}")

    _r_render = _r_ortho
    _r_refine = _r_ortho

    def _r_doctor(self, d: dict) -> None:
        for c in d["checks"]:
            self._line(f"  {'✓' if c['ok'] else '✗'} {c['name']}: {c['detail']}")
        self._line("")
        self._line("모든 항목 정상" if d["ok"] else f"문제 {sum(not c['ok'] for c in d['checks'])}건. 위 ✗ 항목을 확인해야 함")

    def _r_export(self, d: dict) -> None:
        self._kv([(k, v) for k, v in d.items()])
