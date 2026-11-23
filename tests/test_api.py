"""API 스모크 테스트 (샘플 데이터 자동 적재 + 조회 + 업로드).

실행:
    cd medigate-salary-site
    python -m pytest -q

테스트는 data/와 분리된 임시 SQLite 파일을 사용한다.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TEST_DB = Path(tempfile.gettempdir()) / "medigate_salary_test.db"
os.environ["MEDIGATE_DB"] = str(TEST_DB)
if TEST_DB.exists():
    TEST_DB.unlink()

from backend.main import app  # noqa: E402  (환경변수 설정 후 임포트)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c
    if TEST_DB.exists():
        TEST_DB.unlink()


def test_index_served(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "메디게이트 급여 정리" in res.text


def test_sample_data_loaded(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json()["jobs"] == 30


def test_meta(client):
    res = client.get("/api/meta")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 30
    sidos = {row["sido"] for row in body["sidos"]}
    assert "서울" in sidos and "경기" in sidos
    specialties = {row["specialty"] for row in body["specialties"]}
    assert "내과" in specialties and "피부과" in specialties


def test_stats(client):
    res = client.get("/api/stats")
    assert res.status_code == 200
    body = res.json()
    summary = body["summary"]
    assert summary["job_count"] == 30
    assert summary["pay_count"] == 28  # 협의 2건 제외
    assert 1500 < summary["avg_manwon"] < 2600
    assert summary["median_manwon"] > 0
    assert summary["min_manwon"] <= summary["median_manwon"] <= summary["max_manwon"]
    assert body["by_specialty"]
    assert body["by_sido"]
    assert body["by_region"]


def test_stats_filtered(client):
    res = client.get("/api/stats", params={"sido": "서울", "specialty": "피부과", "pay_basis": "monthly"})
    assert res.status_code == 200
    summary = res.json()["summary"]
    assert summary["job_count"] == 1
    assert summary["pay_count"] == 1


def test_stats_annual_basis(client):
    monthly = client.get("/api/stats", params={"pay_basis": "monthly"}).json()["summary"]
    annual = client.get("/api/stats", params={"pay_basis": "annual"}).json()["summary"]
    assert annual["avg_manwon"] == pytest.approx(monthly["avg_manwon"] * 12, rel=0.01)


def test_jobs_sorted(client):
    res = client.get("/api/jobs", params={"sort": "pay_desc", "limit": 10})
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 30
    assert len(body["items"]) == 10
    values = [item["monthly_manwon"] for item in body["items"] if item["monthly_manwon"] is not None]
    assert values == sorted(values, reverse=True)


def test_jobs_search(client):
    res = client.get("/api/jobs", params={"q": "강남"})
    assert res.status_code == 200
    assert res.json()["total"] >= 3


def test_jobs_after_tax_filter(client):
    res = client.get("/api/jobs", params={"after_tax": "after", "limit": 100})
    assert res.status_code == 200
    items = res.json()["items"]
    assert items and all(item["is_after_tax"] for item in items)


def test_upload_csv_and_dedupe(client):
    csv_text = (
        "제목,병원,지역,진료과목,급여,등록일,링크\n"
        "피부과 전문의,테스트피부과의원,서울 강남구,피부과,세후 2,000~2,300만원,2026-11-20,https://example.com/jobs/1\n"
        "내과 전문의,테스트내과의원,경기 성남시 분당구,내과,연봉 2억,2026-11-20,https://example.com/jobs/2\n"
    )
    res = client.post(
        "/api/upload",
        files={"file": ("upload.csv", csv_text.encode("utf-8-sig"), "text/csv")},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["inserted"] == 2
    assert body["total"] == 32

    # 같은 파일을 다시 올리면 중복으로 제외된다.
    res2 = client.post(
        "/api/upload",
        files={"file": ("upload.csv", csv_text.encode("utf-8-sig"), "text/csv")},
    )
    assert res2.status_code == 200
    assert res2.json()["inserted"] == 0
    assert res2.json()["skipped"] == 2
    assert res2.json()["total"] == 32

    # 새 행의 정규화 결과 확인
    res3 = client.get("/api/jobs", params={"q": "테스트피부과"})
    item = res3.json()["items"][0]
    assert item["sido"] == "서울"
    assert item["specialty"] == "피부과"
    assert item["is_after_tax"] is True
    assert item["monthly_manwon"] == 2150


def test_upload_bad_file(client):
    res = client.post(
        "/api/upload",
        files={"file": ("bad.csv", b"foo,bar\n1,2\n", "text/csv")},
    )
    assert res.status_code == 400
