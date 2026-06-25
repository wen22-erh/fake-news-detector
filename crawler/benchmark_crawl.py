#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
benchmark_crawl.py
==================
對現有爬蟲做計時 benchmark。

用法：
    python benchmark_crawl.py --job job_100.json
    python benchmark_crawl.py --job job_1000.json --export-dir ./bench_exports

輸出：
    - 終端機：每筆進度 + 最終統計摘要
    - {export_dir}/{job_id}_bench.csv：每筆 URL 的詳細計時紀錄
    - {export_dir}/{job_id}_bench_summary.json：摘要統計（可用於 CI 比對）

依賴：
    - 與 crawler.py 放在同一目錄（或 PYTHONPATH 可找到 crawler 模組）
    - pip install numpy  （可選，若未安裝則用 statistics 標準庫計算分位數）
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── 從主爬蟲模組 import ────────────────────────────────────────────────────
# 請確保 crawler.py 與此腳本在同一目錄，或已在 PYTHONPATH 中
import fccna_worker as C  # type: ignore  # noqa: E402

# ── numpy 可選（精確分位數）────────────────────────────────────────────────
try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False


# =============================================================================
# 統計工具
# =============================================================================

def percentile(data: List[float], p: float) -> float:
    """計算第 p 百分位數（0~100）。優先用 numpy，否則 statistics.quantiles。"""
    if not data:
        return 0.0
    if _HAS_NUMPY:
        return float(np.percentile(data, p))
    # statistics.quantiles 需要 Python 3.8+，n=100 可以近似百分位
    sorted_data = sorted(data)
    idx = (p / 100) * (len(sorted_data) - 1)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return sorted_data[lo]
    frac = idx - lo
    return sorted_data[lo] * (1 - frac) + sorted_data[hi] * frac


def build_stats(latencies: List[float]) -> Dict[str, Any]:
    """從延遲列表建立統計摘要（單位：秒）。"""
    if not latencies:
        return {}
    return {
        "count":  len(latencies),
        "total_s":  round(sum(latencies), 3),
        "mean_s":   round(statistics.mean(latencies), 3),
        "median_s": round(percentile(latencies, 50), 3),
        "p75_s":    round(percentile(latencies, 75), 3),
        "p90_s":    round(percentile(latencies, 90), 3),
        "p95_s":    round(percentile(latencies, 95), 3),
        "p99_s":    round(percentile(latencies, 99), 3),
        "min_s":    round(min(latencies), 3),
        "max_s":    round(max(latencies), 3),
        "stdev_s":  round(statistics.stdev(latencies), 3) if len(latencies) > 1 else 0.0,
    }


def format_stats_table(label: str, stats: Dict[str, Any]) -> str:
    """把統計 dict 格式化成易讀的表格字串。"""
    lines = [
        f"\n{'='*55}",
        f"  {label}",
        f"{'='*55}",
        f"  {'筆數':<18} {stats.get('count', '-'):>10}",
        f"  {'總耗時':<18} {stats.get('total_s', '-'):>10.1f} 秒",
        f"{'─'*55}",
        f"  {'平均 (mean)':<18} {stats.get('mean_s', '-'):>10.3f} 秒",
        f"  {'中位數 (P50)':<18} {stats.get('median_s', '-'):>10.3f} 秒",
        f"  {'P75':<18} {stats.get('p75_s', '-'):>10.3f} 秒",
        f"  {'P90':<18} {stats.get('p90_s', '-'):>10.3f} 秒",
        f"  {'P95':<18} {stats.get('p95_s', '-'):>10.3f} 秒",
        f"  {'P99':<18} {stats.get('p99_s', '-'):>10.3f} 秒",
        f"  {'最快 (min)':<18} {stats.get('min_s', '-'):>10.3f} 秒",
        f"  {'最慢 (max)':<18} {stats.get('max_s', '-'):>10.3f} 秒",
        f"  {'標準差 (stdev)':<18} {stats.get('stdev_s', '-'):>10.3f} 秒",
        f"{'='*55}",
    ]
    return "\n".join(lines)


# =============================================================================
# 計時爬取
# =============================================================================

def timed_crawl(job_id: str, url: str) -> Dict[str, Any]:
    """
    包裝 crawl_one_url，加上計時。
    回傳原始 row dict，並附加：
      - elapsed_s：此 URL 實際花費秒數（含 Firecrawl 等待、重試、sleep 間隔）
    """
    t0 = time.perf_counter()
    row = C.crawl_one_url(job_id, url)
    elapsed = time.perf_counter() - t0
    row["elapsed_s"] = round(elapsed, 3)
    return row


# =============================================================================
# Benchmark 主流程
# =============================================================================

def run_benchmark(job: Dict[str, Any], export_dir: str) -> Dict[str, Any]:
    job_id = str(job["job_id"])
    raw_urls = job.get("urls", [])
    urls = C.prepare_urls(raw_urls)

    export_path = Path(export_dir)
    export_path.mkdir(parents=True, exist_ok=True)

    bench_csv_path = export_path / f"{job_id}_bench.csv"
    summary_json_path = export_path / f"{job_id}_bench_summary.json"

    print(f"\n[BENCH] Job: {job_id}")
    print(f"[BENCH] 輸入 {len(raw_urls)} 筆 → 過濾後 {len(urls)} 筆")
    print(f"[BENCH] 匯出目錄: {export_path.resolve()}\n")

    if not urls:
        print("[BENCH] 沒有可爬的 URL，結束。")
        return {}

    # ── 計時標記 ─────────────────────────────────────────────────────────────
    wall_start = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()

    rows: List[Dict[str, Any]] = []
    all_latencies: List[float] = []
    success_latencies: List[float] = []
    failed_latencies: List[float] = []

    success_count = 0
    failed_count = 0
    blocked_count = 0

    # ── 逐筆爬取 ─────────────────────────────────────────────────────────────
    for i, url in enumerate(urls, 1):
        row = timed_crawl(job_id, url)
        rows.append(row)

        elapsed = row["elapsed_s"]
        status = row["status"]
        all_latencies.append(elapsed)

        if status == "content_saved":
            success_count += 1
            success_latencies.append(elapsed)
            tag = "✓"
        elif status == "blocked":
            blocked_count += 1
            failed_count += 1
            failed_latencies.append(elapsed)
            tag = "✗ BLOCKED"
        else:
            failed_count += 1
            failed_latencies.append(elapsed)
            tag = "✗"

        print(
            f"[{i:>4}/{len(urls)}] {tag:<10} {elapsed:>6.2f}s  "
            f"{url[:80]}"
            + (f"  → {row['error_message'][:60]}" if row.get("error_message") else "")
        )

        # ── 請求間隔（最後一筆跳過，被封後不等）────────────────────────────
        if i < len(urls) and status != "blocked":
            delay = random.uniform(C.CRAWL_DELAY_MIN, C.CRAWL_DELAY_MAX)
            print(f"[BENCH] 等待 {delay:.1f} 秒...")
            time.sleep(delay)

    wall_elapsed = time.perf_counter() - wall_start
    finished_at = datetime.now(timezone.utc).isoformat()

    # ── 統計 ─────────────────────────────────────────────────────────────────
    overall_stats = build_stats(all_latencies)
    success_stats = build_stats(success_latencies)
    failed_stats  = build_stats(failed_latencies)

    # ── 終端機輸出 ────────────────────────────────────────────────────────────
    print(format_stats_table(f"全部 {len(urls)} 筆（含失敗）", overall_stats))
    if success_latencies:
        print(format_stats_table(f"成功 {success_count} 筆", success_stats))
    if failed_latencies:
        print(format_stats_table(f"失敗/封鎖 {failed_count} 筆", failed_stats))

    print(f"\n[BENCH] 整批 wall-clock 時間：{wall_elapsed:.1f} 秒")
    print(f"[BENCH] 成功率：{success_count}/{len(urls)} "
          f"({100*success_count/len(urls):.1f}%)  "
          f"封鎖：{blocked_count}\n")

    # ── 寫出 per-URL CSV ──────────────────────────────────────────────────────
    bench_fields = [
        "job_id", "url", "normalized_url", "domain",
        "status", "elapsed_s", "attempts", "proxy_used",
        "text_len", "title", "is_blocked", "block_reason", "error_message",
    ]
    with bench_csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=bench_fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in bench_fields})

    # ── 寫出 summary JSON ─────────────────────────────────────────────────────
    summary = {
        "job_id": job_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "wall_elapsed_s": round(wall_elapsed, 3),
        "input_count": len(raw_urls),
        "prepared_count": len(urls),
        "success_count": success_count,
        "failed_count": failed_count - blocked_count,
        "blocked_count": blocked_count,
        "success_rate_pct": round(100 * success_count / len(urls), 2) if urls else 0,
        "stats_all": overall_stats,
        "stats_success": success_stats,
        "stats_failed": failed_stats,
    }
    with summary_json_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"[BENCH] 詳細 CSV  → {bench_csv_path}")
    print(f"[BENCH] 摘要 JSON → {summary_json_path}")

    return summary


# =============================================================================
# CLI 入口
# =============================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="爬蟲 benchmark：計時 + P50/P95/P99 統計"
    )
    parser.add_argument(
        "--job",
        required=True,
        help="job JSON 檔路徑（格式同 crawler.py，內含 urls 陣列）",
    )
    parser.add_argument(
        "--export-dir",
        default=C.DEFAULT_EXPORT_DIR,
        help=f"輸出目錄（預設：{C.DEFAULT_EXPORT_DIR}）",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    job = C.load_job(args.job)
    summary = run_benchmark(job, args.export_dir)
    # 也把摘要印到 stdout（方便 CI 擷取）
    print("\n" + json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()