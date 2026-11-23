"""메디게이트 급여 정리 사이트 - FastAPI 백엔드.

실행 (프로젝트 루트에서):
    pip install -r requirements.txt
    uvicorn backend.main:app --reload
    -> http://127.0.0.1:8000

데이터는 data/jobs.db (SQLite)에 저장된다. DB가 비어 있으면
sample_data/jobs_sample.csv 를 자동으로 읽어 데모 데이터를 채운다.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sqlite3
import statistics
from contextlib import asynccontextmanager, closing
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

try:  # 패키지로 실행할 때
    from .parser import SPECIALTIES, SIDO_LIST, normalize_region, normalize_specialty, parse_salary
except ImportError:  # python backend/main.py 처럼 단독 실행할 때
    from parser import SPECIALTIES, SIDO_LIST, normalize_region, normalize_specialty, parse_salary  # type: ignore


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
FRONTEND_DIR = BASE_DIR / "frontend"
SAMPLE_CSV = BASE_DIR / "sample_data" / "jobs_sample.csv"
DB_PATH = Path(os.environ.get("MEDIGATE_DB", DATA_DIR / "jobs.db"))

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20MB


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key      TEXT NOT NULL UNIQUE,
    source_id       TEXT,
    title           TEXT,
    hospital        TEXT,
    region_raw      TEXT,
    sido            TEXT,
    sigungu         TEXT,
    specialty_raw   TEXT,
    specialty       TEXT,
    employment      TEXT,
    pay_raw         TEXT,
    pay_type        TEXT,
    pay_basis       TEXT,
    min_manwon      REAL,
    max_manwon      REAL,
    mid_manwon      REAL,
    monthly_manwon  REAL,
    is_after_tax    INTEGER DEFAULT 0,
    url             TEXT,
    posted_at       TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_sido ON jobs(sido);
CREATE INDEX IF NOT EXISTS idx_jobs_specialty ON jobs(specialty);
"""


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(connect()) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def count_jobs() -> int:
    with closing(connect()) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])


# ---------------------------------------------------------------------------
# 정규화 / 적재
# ---------------------------------------------------------------------------

COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "source_id": ("source_id", "id", "공고번호", "번호", "공고id"),
    "title": ("title", "제목", "공고제목", "공고명", "채용제목"),
    "hospital": ("hospital", "병원", "병원명", "의료기관", "기관명", "근무기관"),
    "region": ("region", "지역", "주소", "근무지", "근무지역", "소재지"),
    "specialty": ("specialty", "진료과", "진료과목", "과목", "전문과목", "모집과목"),
    "pay_text": ("pay_text", "pay", "salary", "급여", "급여조건", "연봉", "급여내용", "급여조건/연봉"),
    "employment": ("employment", "고용형태", "근무형태", "채용형태", "근무조건"),
    "posted_at": ("posted_at", "등록일", "게시일", "날짜", "작성일", "공고일"),
    "url": ("url", "link", "링크", "공고링크", "상세링크", "상세url"),
}


def _normalize_header(name: str) -> str:
    return (name or "").strip().lower().replace(" ", "").replace("_", "").replace("-", "")


_HEADER_LOOKUP = {
    _normalize_header(alias): field
    for field, aliases in COLUMN_ALIASES.items()
    for alias in aliases
}


def _map_row(row: dict[str, Any]) -> dict[str, str]:
    mapped: dict[str, str] = {}
    for key, value in row.items():
        field = _HEADER_LOOKUP.get(_normalize_header(str(key)))
        if field and field not in mapped:
            mapped[field] = "" if value is None else str(value).strip()
    return mapped


def build_record(row: dict[str, Any]) -> Optional[dict[str, Any]]:
    """CSV/JSON 한 행(dict)을 DB 레코드로 변환한다. 제목/병원이 모두 비면 None."""
    mapped = _map_row(row)
    title = mapped.get("title", "")
    hospital = mapped.get("hospital", "")
    if not (title or hospital):
        return None

    region = normalize_region(mapped.get("region", ""))
    specialty = normalize_specialty(mapped.get("specialty", ""))
    salary = parse_salary(mapped.get("pay_text", ""))

    url = mapped.get("url", "")
    source_id = mapped.get("source_id", "")
    dedupe_basis = url or f"{hospital}|{title}|{mapped.get('specialty','')}|{mapped.get('posted_at','')}"
    dedupe_key = hashlib.sha1(dedupe_basis.encode("utf-8")).hexdigest()

    return {
        "dedupe_key": dedupe_key,
        "source_id": source_id,
        "title": title,
        "hospital": hospital,
        "region_raw": mapped.get("region", ""),
        "sido": region["sido"],
        "sigungu": region["sigungu"],
        "specialty_raw": mapped.get("specialty", ""),
        "specialty": specialty,
        "employment": mapped.get("employment", ""),
        "pay_raw": mapped.get("pay_text", ""),
        "pay_type": salary["pay_type"],
        "pay_basis": salary["pay_basis"],
        "min_manwon": salary["min_manwon"],
        "max_manwon": salary["max_manwon"],
        "mid_manwon": salary["mid_manwon"],
        "monthly_manwon": salary["monthly_mid_manwon"],
        "is_after_tax": 1 if salary["is_after_tax"] else 0,
        "url": url,
        "posted_at": mapped.get("posted_at", ""),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def insert_records(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    inserted = 0
    skipped = 0
    errors: list[str] = []
    with closing(connect()) as conn:
        for record in records:
            try:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO jobs (
                        dedupe_key, source_id, title, hospital, region_raw, sido, sigungu,
                        specialty_raw, specialty, employment, pay_raw, pay_type, pay_basis,
                        min_manwon, max_manwon, mid_manwon, monthly_manwon, is_after_tax,
                        url, posted_at, created_at
                    ) VALUES (
                        :dedupe_key, :source_id, :title, :hospital, :region_raw, :sido, :sigungu,
                        :specialty_raw, :specialty, :employment, :pay_raw, :pay_type, :pay_basis,
                        :min_manwon, :max_manwon, :mid_manwon, :monthly_manwon, :is_after_tax,
                        :url, :posted_at, :created_at
                    )
                    """,
                    record,
                )
                if cur.rowcount:
                    inserted += 1
                else:
                    skipped += 1
            except sqlite3.Error as exc:  # pragma: no cover - 방어적 처리
                errors.append(f"{record.get('title', '')}: {exc}")
                skipped += 1
        conn.commit()
    return {"inserted": inserted, "skipped": skipped, "errors": errors, "total": count_jobs()}


def decode_bytes(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp949", "euc-kr"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_csv_text(text: str) -> list[dict[str, str]]:
    """CSV 문자열을 DictReader 형태의 dict 목록으로 바꾼다.

    엑셀에서 저장하지 않고 손으로 만든 CSV에서 흔한 실수(급여 칸의 "1,800"처럼
    따옴표 없이 들어간 콤마)를 자동 복구한다. 콤마 때문에 열이 밀리면
    급여 열에 남는 조각을 다시 합쳐 원래 열 순서로 돌려놓는다.
    """
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel

    raw_rows = list(csv.reader(io.StringIO(text), dialect))
    raw_rows = [r for r in raw_rows if any((c or "").strip() for c in r)]
    if not raw_rows:
        return []

    header = raw_rows[0]
    pay_idx = None
    for i, name in enumerate(header):
        if _HEADER_LOOKUP.get(_normalize_header(name)) == "pay_text":
            pay_idx = i
            break

    rows: list[dict[str, str]] = []
    for data in raw_rows[1:]:
        if len(data) > len(header) and pay_idx is not None:
            excess = len(data) - len(header)
            data = (
                data[:pay_idx]
                + [",".join(data[pay_idx:pay_idx + 1 + excess])]
                + data[pay_idx + 1 + excess:]
            )
        rows.append({header[i]: (data[i] if i < len(data) else "") for i in range(len(header))})
    return rows


def load_sample_if_empty() -> None:
    if count_jobs() > 0 or not SAMPLE_CSV.exists():
        return
    text = SAMPLE_CSV.read_text(encoding="utf-8-sig")
    records = [r for r in (build_record(row) for row in parse_csv_text(text)) if r]
    insert_records(records)


# ---------------------------------------------------------------------------
# 조회 / 집계
# ---------------------------------------------------------------------------

SORTS = {
    "pay_desc": "monthly_manwon IS NULL, monthly_manwon DESC, id DESC",
    "pay_asc": "monthly_manwon IS NULL, monthly_manwon ASC, id DESC",
    "recent": "posted_at DESC, id DESC",
    "default": "id DESC",
}


def _filters(
    sido: Optional[str],
    specialty: Optional[str],
    after_tax: Optional[str],
    q: Optional[str],
) -> tuple[str, list[Any]]:
    clauses: list[str] = ["(monthly_manwon IS NULL OR monthly_manwon >= 500)"]
    params: list[Any] = []
    if sido:
        clauses.append("sido = ?")
        params.append(sido)
    if specialty:
        clauses.append("specialty = ?")
        params.append(specialty)
    if after_tax == "after":
        clauses.append("is_after_tax = 1")
    elif after_tax == "before":
        clauses.append("is_after_tax = 0")
    if q:
        like = f"%{q}%"
        clauses.append(
            "(title LIKE ? OR hospital LIKE ? OR region_raw LIKE ? OR specialty_raw LIKE ? OR pay_raw LIKE ?)"
        )
        params.extend([like] * 5)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _pay_value(row: sqlite3.Row, basis: str) -> Optional[float]:
    monthly = row["monthly_manwon"]
    if monthly is None:
        return None
    return monthly * 12 if basis == "annual" else monthly


def _aggregate(rows: list[sqlite3.Row], basis: str) -> dict[str, Any]:
    values = [v for v in (_pay_value(r, basis) for r in rows) if v is not None]
    if not values:
        return {
            "job_count": len(rows),
            "pay_count": 0,
            "avg_manwon": None,
            "median_manwon": None,
            "min_manwon": None,
            "max_manwon": None,
        }
    return {
        "job_count": len(rows),
        "pay_count": len(values),
        "avg_manwon": round(sum(values) / len(values), 1),
        "median_manwon": round(statistics.median(values), 1),
        "min_manwon": round(min(values), 1),
        "max_manwon": round(max(values), 1),
    }


def _group(rows: list[sqlite3.Row], key_fields: tuple[str, ...], basis: str) -> list[dict[str, Any]]:
    groups: dict[tuple, list[sqlite3.Row]] = {}
    for row in rows:
        key = tuple(row[f] or "" for f in key_fields)
        groups.setdefault(key, []).append(row)
    result = []
    for key, group_rows in groups.items():
        entry = {f: v for f, v in zip(key_fields, key)}
        entry.update(_aggregate(group_rows, basis))
        result.append(entry)
    result.sort(key=lambda e: (e["avg_manwon"] is None, -(e["avg_manwon"] or 0), -e["job_count"]))
    return result


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["is_after_tax"] = bool(d.get("is_after_tax"))
    return d


# ---------------------------------------------------------------------------
# 앱
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    load_sample_if_empty()
    yield


app = FastAPI(title="메디게이트 급여 정리", version="1.0.0", lifespan=lifespan)


@app.get("/", include_in_schema=False)
def index():
    file_path = FRONTEND_DIR / "index.html"
    if not file_path.exists():
        raise HTTPException(status_code=500, detail="frontend/index.html 이 없습니다.")
    return FileResponse(file_path, media_type="text/html; charset=utf-8")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


@app.get("/api/health")
def health():
    return {"status": "ok", "jobs": count_jobs()}


@app.get("/api/meta")
def meta():
    with closing(connect()) as conn:
        sido_rows = conn.execute(
            "SELECT sido, COUNT(*) AS c FROM jobs WHERE sido IS NOT NULL AND sido != '미상' GROUP BY sido ORDER BY c DESC"
        ).fetchall()
        specialty_rows = conn.execute(
            "SELECT specialty, COUNT(*) AS c FROM jobs WHERE specialty IS NOT NULL AND specialty != '기타' GROUP BY specialty ORDER BY c DESC"
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    return {
        "total": total,
        "sidos": [{"sido": r["sido"], "count": r["c"]} for r in sido_rows],
        "specialties": [{"specialty": r["specialty"], "count": r["c"]} for r in specialty_rows],
        "sido_order": SIDO_LIST,
        "specialty_order": SPECIALTIES,
    }


@app.get("/api/stats")
def stats(
    sido: Optional[str] = None,
    specialty: Optional[str] = None,
    pay_basis: str = Query("monthly", pattern="^(monthly|annual)$"),
    after_tax: Optional[str] = Query(None, pattern="^(after|before|all)$"),
    q: Optional[str] = None,
):
    where, params = _filters(sido, specialty, after_tax if after_tax != "all" else None, q)
    with closing(connect()) as conn:
        rows = conn.execute(f"SELECT * FROM jobs {where}", params).fetchall()

    summary = _aggregate(rows, pay_basis)
    return {
        "filters": {
            "sido": sido or "",
            "specialty": specialty or "",
            "pay_basis": pay_basis,
            "after_tax": after_tax or "all",
            "q": q or "",
        },
        "unit": "만원",
        "summary": summary,
        "by_sido": _group(rows, ("sido",), pay_basis),
        "by_region": _group(rows, ("sido", "sigungu"), pay_basis),
        "by_specialty": _group(rows, ("specialty",), pay_basis),
    }


@app.get("/api/jobs")
def jobs(
    sido: Optional[str] = None,
    specialty: Optional[str] = None,
    after_tax: Optional[str] = Query(None, pattern="^(after|before|all)$"),
    q: Optional[str] = None,
    sort: str = Query("pay_desc", pattern="^(pay_desc|pay_asc|recent|default)$"),
    limit: int = Query(300, ge=1, le=2000),
    offset: int = Query(0, ge=0),
):
    where, params = _filters(sido, specialty, after_tax if after_tax != "all" else None, q)
    order = SORTS.get(sort, SORTS["pay_desc"])
    with closing(connect()) as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM jobs {where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM jobs {where} ORDER BY {order} LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [_row_to_dict(r) for r in rows],
    }


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="파일이 너무 큽니다. 20MB 이하 CSV/JSON을 올려 주세요.")
    if not raw:
        raise HTTPException(status_code=400, detail="빈 파일입니다.")

    name = (file.filename or "").lower()
    text = decode_bytes(raw)

    rows: list[dict[str, Any]]
    if name.endswith(".json") or text.lstrip().startswith(("{", "[")):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"JSON 파싱 실패: {exc.msg}") from exc
        if isinstance(payload, dict):
            payload = payload.get("jobs") or payload.get("items") or payload.get("data") or []
        if not isinstance(payload, list):
            raise HTTPException(status_code=400, detail="JSON은 공고 객체의 배열이어야 합니다.")
        rows = [r for r in payload if isinstance(r, dict)]
    else:
        rows = parse_csv_text(text)

    if not rows:
        raise HTTPException(status_code=400, detail="읽을 수 있는 행이 없습니다. 첫 줄에 헤더가 있는지 확인해 주세요.")

    records = [r for r in (build_record(row) for row in rows) if r is not None]
    if not records:
        raise HTTPException(
            status_code=400,
            detail="제목(title) 또는 병원(hospital) 열을 찾지 못했습니다. README의 열 이름 표를 확인해 주세요.",
        )

    result = insert_records(records)
    result["parsed_rows"] = len(rows)
    result["valid_rows"] = len(records)
    return JSONResponse(result)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("backend.main:app", host="127.0.0.1", port=8000, reload=True)
