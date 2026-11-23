# 메디게이트 급여 정리 사이트

메디게이트 초빙정보(의사 채용 공고)를 **지역·진료과목별로 묶어 급여를 비교**하는 로컬 웹 대시보드입니다.
공고 원문의 급여 표기(`월 1,800만원`, `세후 2,200~2,500`, `연봉 2.4억`, `협의` …)를 자동으로 해석해
월 환산 금액으로 통일하고, 지역·과목별 평균/중앙값을 보여 줍니다.

> 메디게이트 초빙정보는 **의사 회원 로그인 후에만** 목록이 열립니다.
> 그래서 이 프로젝트는 ① 로그인 세션을 저장하는 수집 스크립트, ② CSV/JSON 업로드, ③ 바로 볼 수 있는 대시보드로 구성했습니다.
> 샘플 데이터 30건이 들어 있어 로그인 없이도 화면과 집계를 바로 확인할 수 있습니다.

---

## 1. 프로젝트 구조

```
medigate-salary-site/
├── backend/
│   ├── main.py          # FastAPI 앱 + SQLite 저장소 + 집계/업로드 API
│   ├── parser.py        # 급여·지역·진료과목 정규화 (외부 의존성 없음)
│   └── scraper.py       # Playwright 로그인 세션 저장 + 공고 수집
├── frontend/
│   └── index.html       # 빌드 없는 반응형 대시보드 (Tailwind + Chart.js CDN)
├── sample_data/
│   └── jobs_sample.csv  # 데모용 샘플 공고 30건 (실제 공고 아님)
├── tests/
│   ├── test_parser.py   # 급여/지역/과목 파싱 단위 테스트
│   └── test_api.py      # API 스모크 테스트 (샘플 적재·조회·업로드·중복)
├── scripts/
│   └── verify_server.py # 실제 uvicorn 서버를 띄워 17개 항목 검증
├── data/                # 실행 시 생성: jobs.db, storage_state.json, scraped_jobs.csv
├── requirements.txt
├── pytest.ini
└── README.md
```

---

## 2. 실행 방법 (3분)

Python 3.10 이상이 필요합니다.

```bash
cd medigate-salary-site

# 1) 가상환경 (권장)
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 2) 패키지 설치
pip install -r requirements.txt

# 3) 서버 실행
uvicorn backend.main:app --reload
```

브라우저에서 **http://127.0.0.1:8000** 을 엽니다.
첫 실행 시 `data/jobs.db`가 만들어지고 `sample_data/jobs_sample.csv` 30건이 자동으로 들어갑니다.

> `frontend/index.html`을 파일 탐색기에서 더블클릭(`file://`)하면 API에 연결되지 않습니다.
> 반드시 `uvicorn` 서버를 띄운 뒤 `http://127.0.0.1:8000` 으로 접속하세요.

### 테스트

```bash
python -m pytest -q
```

- `tests/test_parser.py`: 급여 표기 20여 종, 지역 9종, 진료과목 19종 파싱 검증
- `tests/test_api.py`: 샘플 자동 적재, `/api/stats`, `/api/jobs` 필터·정렬, CSV 업로드와 중복 방지 검증

실제 서버를 띄워 응답까지 확인하려면:

```bash
python scripts/verify_server.py
```

대시보드, 통계, 목록, CSV/JSON 업로드, 중복 제외 등 17개 항목을 검사하고 임시 DB를 사용합니다.

---

## 3. 메디게이트 로그인 세션 저장 → 공고 수집

### 3-1. 준비 (최초 1회)

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

### 3-2. 로그인 (사람이 직접)

```bash
python -m backend.scraper --login
```

- 크롬 창이 열리고 메디게이트 로그인 페이지(`/auth/login?retUrl=/recruit`)가 표시됩니다.
- **아이디·비밀번호는 스크립트가 저장하지 않습니다.** 준혁님이 브라우저에서 직접 로그인합니다.
- 로그인이 끝나 `/recruit` 등으로 이동하면 자동 감지해 `data/storage_state.json`에 세션을 저장합니다.
- 이미 로그인되어 있으면 바로 저장됩니다.

### 3-3. 수집

```bash
# 목록 3페이지, 최대 300건
python -m backend.scraper --collect --pages 3

# 상세 페이지까지 열어 급여를 보강 (정확도 ↑, 속도 ↓)
python -m backend.scraper --collect --pages 5 --detail

# 세션 유효성만 확인
python -m backend.scraper --check
```

결과는 `data/scraped_jobs.csv` 로 저장됩니다.
대시보드 우측 상단 **CSV/JSON 업로드** 버튼(또는 파일 끌어다 놓기)으로 이 파일을 올리면 즉시 집계됩니다.

### 3-4. 셀렉터가 안 맞을 때 (사이트 개편 대응)

로그인 전에는 실제 DOM을 볼 수 없어, `backend/scraper.py`의 `SELECTORS`는 **예시값**입니다.
사이트 구조가 다르면 다음 순서로 맞춰 주세요.

1. 수집 모드에서 "카드 0건" 또는 generic 모드로 넘어간 경우:
   ```bash
   python -m backend.scraper --dump
   ```
   → `data/debug_list.html` 저장
2. 브라우저에서 `data/debug_list.html`을 열고, 공고 카드 하나를 **우클릭 → 검사(F12)**.
3. 카드 전체를 감싸는 요소의 `class`를 `SELECTORS["list_item"]`에 적습니다.
   - 예: `<li class="recruit_item">` → `"list_item": "li.recruit_item"`
4. 카드 안의 제목/병원/지역/과목/급여 요소 class를 각각 `title`, `hospital`, `region`, `specialty`, `pay`에 적습니다.
5. `--pages 1 --headful` 로 다시 실행해 콘솔의 추출 건수를 확인합니다.

셀렉터를 하나도 못 찾아도 **generic 모드**가 `/recruit/...` 상세 링크와 카드 텍스트만으로
제목·지역·과목·급여를 최대한 추정합니다. 정확도가 낮으므로 셀렉터를 맞추는 것을 권합니다.

### 3-5. 주의사항

- 메디게이트 **이용약관과 robots 정책을 먼저 확인**하세요. 개인적·비상업적 분석 용도로만 사용하세요.
- 요청 사이에 1~2초 대기(`time.sleep`)를 넣어 두었습니다. 페이지 수를 늘릴 때도 과도한 요청은 피하세요.
- `data/storage_state.json`은 **계정 세션 그 자체**입니다. 공유·깃 커밋·클라우드 업로드하지 마세요. (`.gitignore`에 포함)
- 사이트가 캡차/2차 인증을 요구하면 자동 수집 대신 CSV를 직접 만들어 업로드하는 편이 안전합니다.

---

## 4. CSV / JSON 업로드 형식

첫 줄에 헤더가 있는 CSV(UTF-8 또는 CP949/EUC-KR 자동 인식) 또는 객체 배열 JSON을 지원합니다.

| 표준 열 | 인식하는 다른 이름 | 필수 | 설명 |
|---|---|---|---|
| `title` | 제목, 공고제목, 공고명 | △ | 공고 제목 |
| `hospital` | 병원, 병원명, 의료기관, 기관명 | △ | 병원/의원명 (`title` 또는 `hospital` 중 하나는 필요) |
| `region` | 지역, 주소, 근무지, 근무지역 | | `서울 강남구`, `경기 성남시 분당구` |
| `specialty` | 진료과, 진료과목, 과목 | | `내과`, `소아과`, `정신과` 등 |
| `pay_text` | 급여, 연봉, salary, pay | | `월 1,800만원`, `세후 2,200~2,500`, `연봉 2.4억` |
| `employment` | 고용형태, 근무형태 | | 정규직/계약직/파트타임 |
| `posted_at` | 등록일, 게시일, 날짜 | | `2026-11-18` |
| `url` | 링크, 공고링크, 상세링크 | | 중복 판단 기준으로 사용 |
| `id` | 공고번호, 번호 | | 원본 공고 ID(있으면 보관) |

- `url`이 있으면 URL 기준, 없으면 `병원|제목|과목|등록일` 기준으로 **중복을 자동 제외**합니다.
- 같은 파일을 다시 올려도 중복은 `skipped`로 집계되고 DB는 늘어나지 않습니다.
- 업로드 응답 예: `{"inserted": 28, "skipped": 2, "total": 58, "parsed_rows": 30, "valid_rows": 30}`

### 샘플 CSV

`sample_data/jobs_sample.csv` 30건은 **실존하지 않는 가상의 병원·공고**입니다. 형식 참고용으로만 쓰세요.

---

## 5. 급여 표기 해석 규칙 (`backend/parser.py`)

| 원문 예 | 해석 |
|---|---|
| `월 1,800만원` | 월급, 1,800만원 |
| `세후 1,800~2,200` | 월급, 1,800~2,200만원, **세후** |
| `월 2000만 원` | 월급, 2,000만원 (띄어쓰기 허용) |
| `연봉 2.4억` | 연봉, 24,000만원 → 월 환산 2,000만원 |
| `연 3억~3.5억` | 연봉, 30,000~35,000만원 → 월 환산 2,500~2,916.7만원 |
| `연봉 1억 8,000만원` | 연봉, 18,000만원 (복합 단위 합산) |
| `실수령 월 1,600만원` | 월급, 1,600만원, **세후** |
| `협의` / `면접 후 결정` | 협의 (금액 없음, 평균 집계에서 제외) |
| 빈 값 | 미상 |

- 단위 없는 숫자는 국내 의사 채용 공고 관행대로 **만원**으로 봅니다.
- `세후`·`실수령`이 없으면 `세전`으로 표시하되, 원문에 `세전`이 없던 경우까지 세전이라고 단정하지는 않습니다(대시보드 배지 참고).
- `(세후)`처럼 괄호 안에 있어도 인식합니다.
- `월 1,800만원 / 연봉 2.2억`처럼 두 기준이 섞이면 연봉 부분을 12로 나눠 같은 기준으로 비교합니다.

### 지역

`서울특별시 강남구` → `sido=서울`, `sigungu=강남구`
`경기 성남시 분당구` → `sido=경기`, `sigungu=성남시`, `detail=분당구`
`강남구`처럼 시/도가 없으면 주요 구 매핑으로 보정하고, 그래도 모르면 `미상`.

### 진료과목

`소아과`→`소아청소년과`, `정신과`→`정신건강의학과`, `비뇨기과`→`비뇨의학과`,
`마취과`→`마취통증의학과`, `방사선과`→`영상의학과`, `소화기내과`→`내과` 등 별칭을 표준명으로 통일합니다.
매칭되지 않으면 `기타`.

---

## 6. API

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/` | 대시보드 HTML |
| GET | `/api/health` | 상태 확인 (`{"status":"ok","jobs":30}`) |
| GET | `/api/meta` | 필터용 시/도·진료과목 목록과 건수 |
| GET | `/api/stats` | 요약 + 지역별/과목별 공고 수·평균·중앙값·최소·최대 |
| GET | `/api/jobs` | 공고 목록 (필터·검색·정렬·페이지네이션) |
| POST | `/api/upload` | CSV/JSON 업로드 (multipart `file`) |

### `/api/stats` 파라미터

`sido`, `specialty`, `after_tax`(`after`/`before`/`all`), `pay_basis`(`monthly`/`annual`), `q`

```json
{
  "summary": {
    "job_count": 30, "pay_count": 28,
    "avg_manwon": 1761.9, "median_manwon": 1700,
    "min_manwon": 1250, "max_manwon": 2916.7
  },
  "by_sido": [{"sido": "서울", "job_count": 9, "pay_count": 9, "avg_manwon": 2007.4, ...}],
  "by_region": [{"sido": "서울", "sigungu": "강남구", ...}],
  "by_specialty": [{"specialty": "성형외과", "avg_manwon": 2622.2, ...}]
}
```

`pay_basis=annual`이면 모든 값이 연봉 기준(월 환산 × 12)으로 바뀝니다. 금액 단위는 항상 **만원**.

### `/api/jobs` 파라미터

`sido`, `specialty`, `after_tax`, `q`, `sort`(`pay_desc`/`pay_asc`/`recent`/`default`), `limit`(1~2000), `offset`

예: `curl "http://127.0.0.1:8000/api/jobs?sido=서울&specialty=피부과&sort=pay_desc&limit=20"`

---

## 7. 대시보드 구성

- **요약 카드**: 총 공고 수, 월 환산 평균·중앙값, 최고 급여 지역/과목
- **필터**: 시/도, 진료과목, 세전/세후, 월·연 환산, 검색어, 정렬
- **차트**: 진료과목별 평균 급여(가로 막대), 지역별 평균 급여(세로 막대) — Chart.js CDN
- **공고 표**: 병원/공고, 지역, 과목, 원문 급여, 월 환산, 세전·세후 배지, 등록일, 원문 링크
- **업로드**: 버튼 또는 드래그 앤 드롭, 결과 토스트

Tailwind·Chart.js는 CDN을 사용합니다. 오프라인 환경에서도 레이아웃과 표는 동작하도록 기본 CSS를 포함했고,
차트 자리에만 안내 문구가 표시됩니다.

---

## 8. 파일 위치와 데이터

- DB: `data/jobs.db` (SQLite, 테이블 `jobs` 하나)
- 로그인 세션: `data/storage_state.json` (**비밀**)
- 수집 결과: `data/scraped_jobs.csv`
- 샘플: `sample_data/jobs_sample.csv`

DB를 초기화하려면 `data/jobs.db`를 삭제하고 서버를 다시 시작하세요. (샘플이 다시 들어갑니다)

---

## 9. 문제 해결

| 증상 | 해결 |
|---|---|
| `ModuleNotFoundError: No module named 'backend'` | `medigate-salary-site` 폴더에서 실행했는지 확인 (`uvicorn backend.main:app`) |
| 업로드가 "열 이름을 찾지 못했습니다" | 첫 줄에 `title` 또는 `hospital`(또는 제목/병원) 열이 있는지 확인 |
| 한글이 깨져 보임 | CSV를 UTF-8(BOM) 또는 CP949로 저장 (자동 인식) |
| `playwright` 로그인 창이 안 열림 | `pip install playwright` 후 `python -m playwright install chromium` |
| 수집 0건 | `--dump`로 HTML 저장 → `SELECTORS` 수정, 또는 `--pages 1 --headful`로 동작 확인 |
| 로그인 만료 | `python -m backend.scraper --login` 다시 실행 |
| 포트 8000 사용 중 | `uvicorn backend.main:app --port 8001` |

---

## 10. 범위와 한계

- 이 사이트는 **본인이 볼 수 있는 공고**를 정리하는 도구입니다. 로그인을 우회하거나 비공개 데이터를 수집하지 않습니다.
- 메디게이트 목록/상세 DOM은 로그인 후에만 볼 수 있어, 수집 스크립트는 실행 전 상태로는 셀렉터가 검증되지 않았습니다. 첫 실행 시 `--dump`로 확인하는 단계가 필요합니다.
- 급여 표기는 공고마다 기준(기본급/인센티브 포함, 세전/세후, 주당 진료일)이 달라 단순 비교에는 한계가 있습니다. 대시보드는 원문을 함께 보여 주고 월 환산은 참고값으로만 제시합니다.
- 샘플 데이터는 모두 가상이며 실제 병원·공고와 무관합니다.
