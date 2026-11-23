"""실제 uvicorn 서버를 띄워 주요 API를 검증하는 스모크 테스트.

테스트용 임시 DB를 사용하므로 data/jobs.db 는 건드리지 않는다.

    cd medigate-salary-site
    python scripts/verify_server.py
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_DB = Path(tempfile.gettempdir()) / "medigate_verify_server.db"
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["MEDIGATE_DB"] = str(TEST_DB)

import httpx  # noqa: E402
import uvicorn  # noqa: E402

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    RESULTS.append((ok, label))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}" + (f" — {detail}" if detail else ""))


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    port = free_port()
    config = uvicorn.Config("backend.main:app", host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.15)
    if not server.started:
        print("서버가 시작되지 않았습니다.")
        return 1
    print(f"uvicorn 기동: http://127.0.0.1:{port}\n")

    base = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(base_url=base, timeout=20.0) as client:
            # 1) 대시보드
            res = client.get("/")
            check(res.status_code == 200, "GET / (대시보드)", f"{res.status_code}, {len(res.content)} bytes")
            check("메디게이트 급여 정리" in res.text, "GET / HTML 제목 포함")
            check("/api/jobs" in res.text or "api/stats" in res.text, "GET / 대시보드 스크립트 포함")

            # 2) 헬스/메타
            res = client.get("/api/health")
            body = res.json()
            check(res.status_code == 200 and body["jobs"] == 30, "GET /api/health", json.dumps(body))

            res = client.get("/api/meta")
            meta = res.json()
            check(meta["total"] == 30 and len(meta["sidos"]) >= 10, "GET /api/meta", f"sidos={len(meta['sidos'])}")

            # 3) 통계 (전체/필터/연 환산)
            res = client.get("/api/stats")
            stats = res.json()
            s = stats["summary"]
            check(
                res.status_code == 200 and s["job_count"] == 30 and s["pay_count"] == 28,
                "GET /api/stats (전체)",
                f"job={s['job_count']}, pay={s['pay_count']}, avg={s['avg_manwon']}, median={s['median_manwon']}",
            )
            check(len(stats["by_sido"]) == 13 and len(stats["by_specialty"]) >= 8, "GET /api/stats 지역/과목 집계",
                  f"sido={len(stats['by_sido'])}, specialty={len(stats['by_specialty'])}")

            res = client.get("/api/stats", params={"sido": "서울", "specialty": "성형외과"})
            f = res.json()["summary"]
            check(f["job_count"] == 2 and f["pay_count"] == 2, "GET /api/stats 필터", json.dumps(f, ensure_ascii=False))

            res = client.get("/api/stats", params={"pay_basis": "annual"})
            annual = res.json()["summary"]
            check(abs(annual["avg_manwon"] - s["avg_manwon"] * 12) < 1, "GET /api/stats 연 환산",
                  f"annual avg={annual['avg_manwon']}")

            # 4) 공고 목록 정렬/검색/세후 필터
            res = client.get("/api/jobs", params={"sort": "pay_desc", "limit": 5})
            jobs = res.json()
            values = [j["monthly_manwon"] for j in jobs["items"] if j["monthly_manwon"] is not None]
            check(res.status_code == 200 and jobs["total"] == 30 and values == sorted(values, reverse=True),
                  "GET /api/jobs 급여 내림차순", f"total={jobs['total']}, top={jobs['items'][0]['hospital']}")

            res = client.get("/api/jobs", params={"q": "강남"})
            check(res.json()["total"] >= 3, "GET /api/jobs 검색", f"q=강남 -> {res.json()['total']}건")

            res = client.get("/api/jobs", params={"after_tax": "after", "limit": 100})
            items = res.json()["items"]
            check(bool(items) and all(i["is_after_tax"] for i in items), "GET /api/jobs 세후 필터", f"{len(items)}건")

            # 5) CSV 업로드 + 중복 방지
            csv_text = (
                "제목,병원,지역,진료과목,급여,등록일,링크\n"
                "피부과 전문의,검증피부과의원,서울 마포구,피부과,\"세후 2,000~2,300만원\",2026-11-21,https://example.com/v1\n"
                "내과 전문의,검증내과의원,부산 해운대구,내과,연봉 2억,2026-11-21,https://example.com/v2\n"
            )
            res = client.post("/api/upload", files={"file": ("up.csv", csv_text.encode("utf-8-sig"), "text/csv")})
            up = res.json()
            check(res.status_code == 200 and up["inserted"] == 2 and up["total"] == 32, "POST /api/upload CSV",
                  json.dumps(up, ensure_ascii=False))

            res = client.post("/api/upload", files={"file": ("up.csv", csv_text.encode("utf-8-sig"), "text/csv")})
            dup = res.json()
            check(dup["inserted"] == 0 and dup["skipped"] == 2 and dup["total"] == 32, "POST /api/upload 중복 제외",
                  json.dumps(dup, ensure_ascii=False))

            res = client.get("/api/jobs", params={"q": "검증피부과"})
            item = res.json()["items"][0]
            check(item["is_after_tax"] and item["monthly_manwon"] == 2150 and item["sido"] == "서울",
                  "업로드 행 정규화 (세후/월 환산/지역)", json.dumps(
                      {k: item[k] for k in ("sido", "sigungu", "specialty", "pay_raw", "monthly_manwon", "is_after_tax")},
                      ensure_ascii=False))

            # 6) JSON 업로드
            payload = [{
                "title": "영상의학과 전문의", "hospital": "검증영상의원", "region": "대구 수성구",
                "specialty": "영상의학과", "pay_text": "월 1,900만원", "url": "https://example.com/v3",
            }]
            res = client.post("/api/upload", files={"file": ("up.json", json.dumps(payload).encode(), "application/json")})
            check(res.status_code == 200 and res.json()["inserted"] == 1 and res.json()["total"] == 33,
                  "POST /api/upload JSON", json.dumps(res.json(), ensure_ascii=False))

            # 7) 잘못된 파일
            res = client.post("/api/upload", files={"file": ("bad.csv", b"foo,bar\n1,2\n", "text/csv")})
            check(res.status_code == 400, "POST /api/upload 잘못된 형식 -> 400", res.json().get("detail", ""))
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        if TEST_DB.exists():
            TEST_DB.unlink()
        print("\n서버 종료 완료")

    failed = [label for ok, label in RESULTS if not ok]
    print(f"\n결과: {len(RESULTS) - len(failed)}/{len(RESULTS)} 통과")
    if failed:
        print("실패:", ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
