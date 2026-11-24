"""메디게이트 초빙정보 수집 스크립트 (Playwright).

메디게이트 초빙정보(/recruit)는 의사 회원 로그인 후에만 목록이 보인다.
그래서 이 스크립트는 두 단계로 동작한다.

1) 로그인 세션 저장 (사람이 직접 로그인)
       python -m backend.scraper --login
   -> 크롬 창이 열리면 메디게이트에 직접 로그인한다.
   -> 로그인 완료를 감지하면 data/storage_state.json 에 세션을 저장한다.

2) 공고 수집
       python -m backend.scraper --collect --pages 5
   -> 저장된 세션으로 /recruit 목록을 돌며 data/scraped_jobs.csv 를 만든다.
   -> 만든 CSV는 웹 대시보드의 "CSV 업로드"로 그대로 올리면 된다.

주의
* 사이트 이용약관과 robots 정책을 확인하고, 요청 간 1~2초 대기를 유지한다.
* 로그인 세션 파일(storage_state.json)에는 계정 세션이 들어 있으므로
  외부에 공유하거나 저장소에 커밋하지 않는다.
* 아래 SELECTORS는 2026-11 기준 화면을 전제로 한 "예시"다. 사이트 개편으로
  구조가 바뀌면 F12(개발자 도구)로 실제 class/id를 확인해 값을 맞춰 준다.
  셀렉터가 하나도 맞지 않으면 generic 모드가 링크/카드 텍스트만으로
  최대한 추출하고, `--dump` 로 저장한 HTML을 보고 SELECTORS를 고치면 된다.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
PROFILE_DIR = DATA_DIR / "chrome_profile"
STATE_PATH = DATA_DIR / "storage_state.json"
DEFAULT_OUT = DATA_DIR / "scraped_jobs.csv"

LOGIN_URL = "https://www.medigate.net/auth/login?retUrl=%2Frecruit%2Flist"
LIST_URL = "https://www.medigate.net/recruit/list"

# ---------------------------------------------------------------------------
# ▼▼▼ 사이트 개편 시 여기만 고치면 된다 (F12 개발자 도구로 확인) ▼▼▼
# ---------------------------------------------------------------------------
SELECTORS: dict[str, str] = {
    # 목록의 공고 카드 1개 (실제 메디게이트 /recruit 구조: a[href^='/recruit/'])
    "list_item": "a[href^='/recruit/']",
    # 카드 안의 상세 링크
    "item_link": "a[href*='/recruit/']",
    # 카드 안 각 필드 (없으면 카드 전체 텍스트에서 자동 추정)
    "title": ".tit, .title, strong",
    "hospital": ".name, .hospital, .clinic",
    "region": ".area, .region, .local",
    "specialty": ".part, .specialty, .dept",
    "pay": ".pay, .salary, .money",
    # 다음 페이지 버튼
    "next_page": "a.next, button.next, .paging a.on + a",
    # 상세 페이지에서 급여를 담는 영역 (--detail 옵션 사용 시)
    "detail_pay": ".pay, .salary, .recruit_view .info",
    "detail_title": "h1, .view_tit, .tit",
    "detail_hospital": ".name, .hospital",
}
# ▲▲▲ 여기까지 ▲▲▲

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

SIDO_KEYWORDS = [
    "서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종",
    "경기", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주",
]

SALARY_MARKERS = ("월급", "월 ", "연봉", "세후", "세전", "실수령", "억", "만원", "협의")


def _log(message: str) -> None:
    print(message, flush=True)


def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:  # pragma: no cover
        _log("playwright가 설치되어 있지 않습니다. 다음을 먼저 실행해 주세요:")
        _log("    pip install -r requirements.txt")
        _log("    python -m playwright install chromium")
        raise SystemExit(1)
    return sync_playwright


# ---------------------------------------------------------------------------
# 1) 로그인 세션 저장
# ---------------------------------------------------------------------------

def login_and_save_state(timeout_sec: int = 600) -> None:
    import threading

    sync_playwright = _require_playwright()
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    enter_pressed = threading.Event()

    def _wait_enter() -> None:
        try:
            input()
            enter_pressed.set()
        except EOFError:
            pass

    with sync_playwright() as p:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        context = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            user_agent=HEADERS["User-Agent"],
            locale="ko-KR",
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded")

        _log("")
        _log("=" * 70)
        _log("브라우저가 열렸습니다. 메디게이트에 직접 로그인해 주세요.")
        _log("  * 아이디/비밀번호는 이 스크립트가 저장하지 않습니다.")
        _log("  * 로그인이 끝나고 초빙정보 화면이 보이면 자동으로 저장됩니다.")
        _log("  * 자동 감지가 안 되면 브라우저를 닫지 말고 터미널에서 Enter를 눌러 주세요.")
        _log(f"  * 최대 {timeout_sec}초 대기합니다. (중단: Ctrl+C)")
        _log("=" * 70)

        t = threading.Thread(target=_wait_enter, daemon=True)
        t.start()

        deadline = time.time() + timeout_sec
        logged_in = False
        while time.time() < deadline:
            if enter_pressed.is_set():
                logged_in = True
                break
            open_pages = [pg for pg in context.pages if not pg.is_closed()]
            if not open_pages:
                break
            for pg in open_pages:
                url = pg.url or ""
                if "/auth/login" not in url and "medigate" in url and url != "about:blank":
                    try:
                        pg.wait_for_load_state("domcontentloaded", timeout=5000)
                    except Exception:
                        pass
                    logged_in = True
                    break
            if logged_in:
                break
            time.sleep(0.5)

        if not logged_in:
            _log("로그인 완료를 감지하지 못했습니다. 세션을 저장하지 않고 종료합니다.")
            _log("(로그인 후 브라우저 창을 먼저 닫지 마시고, 초빙정보 화면이 뜬 뒤 Enter를 눌러 주세요.)")
            context.close()
            raise SystemExit(2)

        time.sleep(1.5)  # 쿠키/토큰 반영 대기
        try:
            context.storage_state(path=str(STATE_PATH))
        except Exception:
            pass
        _log(f"로그인 프로필을 저장했습니다: {PROFILE_DIR}")
        _log("이 폴더는 계정 세션이므로 공유하지 마세요.")
        context.close()


# ---------------------------------------------------------------------------
# 2) 수집
# ---------------------------------------------------------------------------

def _text_of(card, selector: str) -> str:
    if not selector:
        return ""
    try:
        element = card.query_selector(selector)
    except Exception:
        element = None
    if element is None:
        return ""
    try:
        return " ".join((element.inner_text() or "").split())
    except Exception:
        return ""


def guess_region(text: str) -> str:
    compact = " ".join(text.split())
    for sido in SIDO_KEYWORDS:
        match = re.search(rf"{sido}[가-힣]*\s*[가-힣]*구|[가-힣]*{sido}", compact)
        if match:
            # 시/도 + 다음 1~2토큰을 지역으로 사용
            idx = compact.find(sido)
            tail = compact[idx:].split(" ")
            return " ".join(tail[:3]).strip(",|·-")
    return ""


def guess_salary(text: str) -> str:
    """카드/상세 텍스트에서 급여로 보이는 조각만 뽑아낸다."""
    segments = re.split(r"[\n|•·]", text)
    for segment in segments:
        clean = " ".join(segment.split())
        if not clean or not re.search(r"\d|협의", clean):
            continue
        if any(marker in clean for marker in SALARY_MARKERS):
            # 급여 문구만 남긴다 (앞의 라벨 제거)
            match = re.search(
                r"((?:세후|세전|실수령)?\s*(?:월급|월|연봉|연)?\s*"
                r"[\d,.]+\s*(?:억|천만|만원|만)?\s*(?:[~∼-]\s*[\d,.]+\s*(?:억|천만|만원|만)?)?"
                r"\s*(?:만원|원)?\s*(?:\((?:세후|세전)\))?)",
                clean,
            )
            if match and re.search(r"\d", match.group(1)):
                return match.group(1).strip()
            return clean[:60]
    return ""


def guess_specialty_raw(text: str) -> str:
    try:  # 패키지 실행
        from .parser import SPECIALTIES
    except ImportError:  # 단독 실행
        from parser import SPECIALTIES  # type: ignore

    for name in sorted(SPECIALTIES, key=len, reverse=True):
        if name in text:
            return name
    for alias in ("소아과", "정신과", "비뇨기과", "마취과", "방사선과", "가정의학"):
        if alias in text:
            return alias
    return ""


def _extract_from_card(card, page_url: str) -> Optional[dict[str, Any]]:
    link = None
    try:
        link = card.query_selector(SELECTORS["item_link"])
    except Exception:
        link = None

    href = ""
    try:
        tag = card.evaluate("el => el.tagName.toLowerCase()")
        if tag == "a":
            href = card.get_attribute("href") or ""
    except Exception:
        pass
    if not href and link is not None:
        href = link.get_attribute("href") or ""
    if href and not re.search(r"/recruit/\d+", href):
        return None
    url = urljoin(page_url, href) if href else ""
    rec_id_m = re.search(r"/recruit/(\d+)", href)
    rec_id = rec_id_m.group(1) if rec_id_m else ""

    try:
        card_text = card.inner_text() or ""
    except Exception:
        card_text = ""
    lines = [ln.strip() for ln in card_text.splitlines() if ln.strip()]

    title = _text_of(card, SELECTORS["title"])
    hospital = _text_of(card, SELECTORS["hospital"])
    region = _text_of(card, SELECTORS["region"])
    specialty = _text_of(card, SELECTORS["specialty"])
    pay = _text_of(card, SELECTORS["pay"])
    posted_at = ""

    # 메디게이트 카드 기본 줄 순서: [제목, 지역, 병원명, 마감일]
    if lines:
        if not title:
            title = lines[0]
        if not region and len(lines) > 1:
            region = lines[1]
        if not hospital and len(lines) > 2:
            hospital = lines[2]
        if len(lines) > 3:
            posted_at = lines[3]

    if not title and link is not None:
        try:
            title = " ".join((link.inner_text() or "").split())
        except Exception:
            title = ""

    title = title or " ".join(card_text.split())[:80]
    if not region:
        region = guess_region(card_text)
    if not specialty:
        specialty = guess_specialty_raw(title or card_text)
    if not pay:
        pay = guess_salary(title or card_text)

    if not (title or url):
        return None
    return {
        "id": rec_id,
        "title": title,
        "hospital": hospital,
        "region": region,
        "specialty": specialty,
        "pay_text": pay,
        "employment": "초빙",
        "posted_at": posted_at,
        "url": url,
    }


def _generic_extract(page) -> list[dict[str, Any]]:
    """SELECTORS가 안 맞을 때: /recruit/ 상세 링크를 기준으로 최소 정보만 뽑는다."""
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    anchors = page.query_selector_all("a[href*='/recruit/']")
    for anchor in anchors:
        href = anchor.get_attribute("href") or ""
        if not re.search(r"/recruit/(?:view|detail)?/?\d+|/recruit/\d+", href):
            # 숫자 ID가 없는 링크(카테고리 등)는 건너뛴다.
            if not re.search(r"\d{3,}", href):
                continue
        url = urljoin(page.url, href)
        if url in seen:
            continue
        seen.add(url)
        try:
            text = anchor.inner_text() or ""
            container = anchor.evaluate(
                "el => (el.closest('li, article, .item, .card') || el.parentElement || el).innerText"
            ) or text
        except Exception:
            container = text
        title = " ".join(text.split()) or " ".join(container.split())[:80]
        results.append(
            {
                "id": "",
                "title": title,
                "hospital": "",
                "region": guess_region(container),
                "specialty": guess_specialty_raw(container),
                "pay_text": guess_salary(container),
                "employment": "",
                "posted_at": "",
                "url": url,
            }
        )
    return results


def _fill_detail_api(page, item: dict[str, Any]) -> dict[str, Any] | None:
    """상세 API(apis.medigate.net/w/recruit/<id>)로 급여·과목·주소를 빠르게 가져온다."""
    rec_id = item.get("id") or ""
    if not rec_id and item.get("url"):
        m = re.search(r"/recruit/(\d+)", item["url"])
        if m:
            rec_id = m.group(1)
    if not rec_id:
        return None

    try:
        payload = page.evaluate(
            """async (id) => {
                const res = await fetch(`https://apis.medigate.net/w/recruit/${id}?pageType=null`, {
                    credentials: 'include'
                });
                if (!res.ok) return null;
                return await res.json();
            }""",
            rec_id,
        )
    except Exception:
        return None

    if not isinstance(payload, dict) or payload.get("code") != 200:
        return None

    data = payload.get("data") or {}
    rec = data.get("recruit") or {}
    org = data.get("recruitClientOrg") or {}
    if not rec:
        return None

    pay_type_name = (rec.get("payTypeName") or "").strip()
    pay_raw = (rec.get("pay") or "").strip()
    pay_value = rec.get("payValue")
    if pay_raw:
        pay_text = f"{pay_type_name} {pay_raw}".strip()
    elif pay_value:
        pay_text = f"{pay_type_name} {pay_value}만원".strip()
    else:
        pay_text = item.get("pay_text") or guess_salary(rec.get("title") or "")

    hospital = (rec.get("orgName") or org.get("orgName") or item.get("hospital") or "").strip()
    loc_fallback = f"{rec.get('hopLocName') or ''} {rec.get('hopCityName') or ''}".strip()
    region = (org.get("hosAddr") or loc_fallback or item.get("region") or "").strip()
    specialty = (rec.get("spcNames") or item.get("specialty") or "").strip()
    employment = " ".join(x for x in [rec.get("inviteTypeName"), rec.get("regularFlagName")] if x).strip()
    posted_at = (rec.get("displayHopeDate") or (rec.get("createAt") or "")[:10] or item.get("posted_at") or "").strip()

    item["id"] = str(rec.get("boardIdx") or rec_id)
    if rec.get("title"):
        item["title"] = str(rec["title"]).strip()
    if hospital:
        item["hospital"] = hospital
    if pay_text:
        item["pay_text"] = pay_text
    if region:
        item["region"] = region
    if specialty:
        item["specialty"] = specialty
    if employment:
        item["employment"] = employment
    if posted_at:
        item["posted_at"] = posted_at
    return item


def _fill_detail(page, item: dict[str, Any]) -> dict[str, Any]:
    """상세 페이지를 열어 급여/병원/지역을 보강한다."""
    if not item.get("url"):
        return item
    try:
        page.goto(item["url"], wait_until="domcontentloaded", timeout=30000)
        time.sleep(1.2)
        body = page.inner_text("body")
    except Exception as exc:
        _log(f"  ! 상세 페이지 실패: {item['url']} ({exc})")
        return item

    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    label_map: dict[str, str] = {}
    target_labels = {"초빙과목", "초빙유형", "급여", "주소", "기관명", "근무시간"}
    for idx, ln in enumerate(lines[:-1]):
        if ln in target_labels and ln not in label_map:
            label_map[ln] = lines[idx + 1]

    title = _text_of(page, SELECTORS["detail_title"])
    hospital = _text_of(page, SELECTORS["detail_hospital"]) or label_map.get("기관명", "").split("(")[0].strip()
    pay = label_map.get("급여", "") or _text_of(page, SELECTORS["detail_pay"]) or guess_salary(body)
    region = label_map.get("주소", "") or guess_region(body)
    specialty = label_map.get("초빙과목", "") or guess_specialty_raw(body)
    employment = label_map.get("초빙유형", "")

    if title:
        item["title"] = title
    if hospital:
        item["hospital"] = hospital
    if pay:
        item["pay_text"] = pay
    if region:
        item["region"] = region
    if specialty:
        item["specialty"] = specialty
    if employment:
        item["employment"] = employment
    return item


def _items_from_list_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """목록 API JSON(data.recruitList.items)을 공통 공고 dict 목록으로 변환한다."""
    rl = (payload.get("data") or {}).get("recruitList") or {}
    raw_items = rl.get("items") or []
    results: list[dict[str, Any]] = []
    for rec in raw_items:
        if not isinstance(rec, dict):
            continue
        board_idx = rec.get("boardIdx")
        rec_id = str(board_idx) if board_idx is not None else ""
        url = f"https://www.medigate.net/recruit/{rec_id}" if rec_id else ""
        loc = f"{rec.get('hopLocName') or ''} {rec.get('hopCityName') or ''}".strip()
        employment = " ".join(
            x for x in [rec.get("inviteTypeName"), rec.get("regularFlagName")] if x
        ).strip()
        title = str(rec.get("title") or "").strip()
        results.append(
            {
                "id": rec_id,
                "title": title,
                "hospital": str(rec.get("orgName") or "").strip(),
                "region": loc,
                "specialty": str(rec.get("spcNames") or "").strip(),
                "pay_text": guess_salary(title),
                "employment": employment,
                "posted_at": str(rec.get("createAt") or "")[:10],
                "url": url,
            }
        )
    return results


def _build_next_page_request(api_info: dict[str, Any], page_no: int) -> tuple[str, str, str | None]:
    """캡처한 목록 API 요청에서 페이지 번호만 page_no로 바꾼 (url, method, body)를 만든다."""
    import json
    from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

    url = api_info["url"]
    method = (api_info.get("method") or "GET").upper()
    post_data = api_info.get("post_data")

    if method == "POST" and post_data:
        try:
            body_obj = json.loads(post_data)
            if isinstance(body_obj, dict):
                replaced = False
                for key in ("page", "pageNo", "curPage", "currentPage", "pageNum"):
                    if key in body_obj:
                        body_obj[key] = page_no
                        replaced = True
                        break
                if not replaced:
                    body_obj["page"] = page_no
                return url, method, json.dumps(body_obj, ensure_ascii=False)
        except Exception:
            pass

    parsed = urlparse(url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    replaced = False
    for key in ("page", "pageNo", "curPage", "currentPage", "pageNum"):
        if key in qs:
            qs[key] = [str(page_no)]
            replaced = True
            break
    if not replaced:
        qs["page"] = [str(page_no)]
    new_query = urlencode(qs, doseq=True)
    new_url = urlunparse(parsed._replace(query=new_query))
    return new_url, method, post_data


def collect(
    pages: int,
    headful: bool,
    detail: bool,
    out_path: Path,
    max_items: int,
    start_page: int = 1,
) -> int:
    sync_playwright = _require_playwright()
    if not PROFILE_DIR.exists() and not STATE_PATH.exists():
        _log(f"로그인 세션이 없습니다: {PROFILE_DIR}")
        _log("먼저 다음을 실행해 메디게이트에 로그인해 주세요:  python3 -m backend.scraper --login")
        return 2

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    collected: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    captured_api: dict[str, Any] = {}

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=not headful,
            user_agent=HEADERS["User-Agent"],
            locale="ko-KR",
        )
        page = context.pages[0] if context.pages else context.new_page()
        captured_headers: dict[str, str] = {}

        def _on_request(req) -> None:
            if "apis.medigate.net" not in req.url:
                return
            for k, v in (req.headers or {}).items():
                kl = k.lower()
                if kl in ("authorization", "x-auth-token", "x-access-token") and v:
                    captured_headers[k] = v

        def _on_response(resp) -> None:
            if captured_api:
                return
            try:
                if "apis.medigate.net" not in resp.url or resp.status != 200:
                    return
                ctype = (resp.headers or {}).get("content-type", "")
                if "json" not in ctype:
                    return
                data = resp.json()
                if isinstance(data, dict):
                    rl = (data.get("data") or {}).get("recruitList")
                    if isinstance(rl, dict) and isinstance(rl.get("items"), list) and rl["items"]:
                        captured_api["url"] = resp.request.url
                        captured_api["method"] = resp.request.method
                        captured_api["post_data"] = resp.request.post_data
                        captured_api["first_payload"] = data
            except Exception:
                pass

        page.on("request", _on_request)
        page.on("response", _on_response)

        _log(f"목록 페이지 여는 중: {LIST_URL}")
        page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(2.5)

        if "/auth/login" in page.url:
            if headful:
                _log("로그인 창이 열렸습니다. 브라우저에서 로그인하시고 초빙정보 목록이 뜨면 터미널에서 Enter를 눌러 주세요.")
                input()
                if "/auth/login" in page.url:
                    page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60000)
                    time.sleep(2.5)
            else:
                _log("로그인 페이지로 이동했습니다. --headful 옵션을 붙여서 다시 실행해 주세요.")
                context.close()
                return 2

        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        if captured_api:
            _log(f"목록 API 자동 감지 완료: {captured_api['method']} {captured_api['url']}")

        start_page = max(1, start_page)
        end_page = start_page + pages - 1
        for page_no in range(start_page, end_page + 1):
            items: list[dict[str, Any]] = []
            api_used = False
            direct_api_url = f"https://apis.medigate.net/w/recruit/list?pageNo={page_no}&pageSize=50"
            try:
                res_obj = page.evaluate(
                    """async ({url, extraHeaders}) => {
                        const headers = Object.assign({ 'Accept': 'application/json' }, extraHeaders || {});
                        const res = await fetch(url, { credentials: 'include', headers });
                        const text = await res.text();
                        return { ok: res.ok, status: res.status, text };
                    }""",
                    {"url": direct_api_url, "extraHeaders": captured_headers},
                )
                if isinstance(res_obj, dict) and res_obj.get("ok"):
                    import json as _json
                    payload = _json.loads(res_obj.get("text") or "{}")
                    if isinstance(payload, dict):
                        items = _items_from_list_payload(payload)
                        if items:
                            api_used = True
                            _log(f"[{page_no}/{end_page}] 목록 API에서 {len(items)}건 수신 (pageSize=50)")
                        else:
                            _log(f"[{page_no}/{end_page}] 목록 API 응답에 items 없음: {str(res_obj.get('text') or '')[:150]}")
                else:
                    status = res_obj.get("status") if isinstance(res_obj, dict) else "?"
                    snippet = str((res_obj or {}).get("text") or "")[:150]
                    _log(f"[{page_no}/{end_page}] 목록 API 응답 오류(status={status}): {snippet}")
            except Exception as exc:
                _log(f"[{page_no}/{end_page}] 직접 목록 API 호출 실패({exc})")

            if not items and captured_api:
                if page_no == 1:
                    items = _items_from_list_payload(captured_api["first_payload"])
                    if items:
                        api_used = True
                    _log(f"[{page_no}/{end_page}] 자동 감지 API에서 {len(items)}건 수신")
                else:
                    req_url, req_method, req_body = _build_next_page_request(captured_api, page_no)
                    try:
                        payload = page.evaluate(
                            """async ({url, method, body}) => {
                                const opts = { method, credentials: 'include' };
                                if (method === 'POST' && body) {
                                    opts.headers = { 'Content-Type': 'application/json' };
                                    opts.body = body;
                                }
                                const res = await fetch(url, opts);
                                if (!res.ok) return null;
                                return await res.json();
                            }""",
                            {"url": req_url, "method": req_method, "body": req_body},
                        )
                        if isinstance(payload, dict):
                            items = _items_from_list_payload(payload)
                            if items:
                                api_used = True
                            _log(f"[{page_no}/{end_page}] 자동 감지 API에서 {len(items)}건 수신")
                    except Exception as exc:
                        _log(f"[{page_no}/{end_page}] 목록 API 호출 실패({exc}), 화면 추출로 전환")

            if not items:
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass

                cards = page.query_selector_all(SELECTORS["list_item"])
                if cards:
                    for card in cards:
                        item = _extract_from_card(card, page.url)
                        if item:
                            items.append(item)
                    _log(f"[{page_no}/{end_page}] 카드 {len(items)}건 추출 (selector 모드)")
                else:
                    items = _generic_extract(page)
                    _log(f"[{page_no}/{end_page}] 링크 {len(items)}건 추출 (generic 모드)")

            added_this_page = 0
            for item in items:
                key = item.get("url") or f"{item.get('title')}|{item.get('hospital')}"
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                if detail:
                    api_item = _fill_detail_api(page, item)
                    if api_item is not None:
                        item = api_item
                        time.sleep(0.15)
                    else:
                        item = _fill_detail(page, item)
                        time.sleep(1.0)
                collected.append(item)
                added_this_page += 1
                if len(collected) >= max_items:
                    break

            _log(f"[{page_no}/{end_page}] 새 공고 {added_this_page}건 추가 (누적 {len(collected)}건)")

            if len(collected) >= max_items:
                _log(f"최대 수집 건수({max_items})에 도달해 종료합니다.")
                break

            if api_used and added_this_page > 0:
                time.sleep(0.5)
                continue

            # 다음 페이지 이동: 1) 숫자 버튼(page_no+1) 2) 다음/더보기 버튼 3) 스크롤
            next_num = str(page_no + 1)
            moved = False
            try:
                moved = page.evaluate(
                    """(num) => {
                        const btns = Array.from(document.querySelectorAll('button, a'));
                        const target = btns.find(el => (el.innerText || '').trim() === num && el.offsetParent !== null);
                        if (target) { target.click(); return true; }
                        return false;
                    }""",
                    next_num,
                )
            except Exception:
                moved = False

            if not moved:
                next_button = None
                for selector in SELECTORS["next_page"].split(","):
                    try:
                        next_button = page.query_selector(selector.strip())
                    except Exception:
                        next_button = None
                    if next_button:
                        break
                if next_button is not None:
                    try:
                        next_button.click()
                        moved = True
                    except Exception:
                        moved = False

            if not moved:
                prev_count = len(page.query_selector_all(SELECTORS["list_item"]))
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(2.0)
                    after_count = len(page.query_selector_all(SELECTORS["list_item"]))
                    if after_count > prev_count:
                        moved = True
                except Exception:
                    moved = False

            if not moved:
                _log("다음 페이지 버튼을 찾지 못해 수집을 마칩니다. (마지막 페이지로 간주)")
                break
            time.sleep(2.0)  # 페이지 로딩 대기

        context.close()

    if not collected:
        _log("수집된 공고가 없습니다. --dump 로 HTML을 저장해 SELECTORS를 확인해 보세요.")
        return 3

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["id", "title", "hospital", "region", "specialty", "pay_text", "employment", "posted_at", "url"]
    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for idx, item in enumerate(collected, 1):
            item["id"] = item.get("id") or f"M{idx:04d}"
            writer.writerow({k: item.get(k, "") for k in fields})

    _log(f"저장 완료: {out_path} ({len(collected)}건)")
    _log("웹 대시보드의 'CSV 업로드' 버튼으로 이 파일을 올리면 바로 집계됩니다.")
    return 0


def check_login() -> int:
    sync_playwright = _require_playwright()
    if not PROFILE_DIR.exists() and not STATE_PATH.exists():
        _log("저장된 세션이 없습니다. --login 먼저 실행해 주세요.")
        return 2
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=True,
            user_agent=HEADERS["User-Agent"],
            locale="ko-KR",
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(2.0)
        ok = "/auth/login" not in page.url
        _log(f"현재 URL: {page.url}")
        _log("로그인 상태: " + ("정상" if ok else "만료됨 (--login 필요)"))
        context.close()
    return 0 if ok else 2


def dump_html() -> int:
    sync_playwright = _require_playwright()
    if not PROFILE_DIR.exists() and not STATE_PATH.exists():
        _log("저장된 세션이 없습니다. --login 먼저 실행해 주세요.")
        return 2
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = DATA_DIR / "debug_list.html"
    _log(f"초빙정보 페이지를 여는 중입니다: {LIST_URL}")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=True,
            user_agent=HEADERS["User-Agent"],
            locale="ko-KR",
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        time.sleep(2.5)
        _log(f"현재 URL: {page.url}")
        out.write_text(page.content(), encoding="utf-8")
        context.close()
    _log(f"목록 HTML 저장 완료: {out}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="메디게이트 초빙정보 수집기")
    parser.add_argument("--login", action="store_true", help="브라우저를 열어 직접 로그인하고 세션 저장")
    parser.add_argument("--collect", action="store_true", help="저장된 세션으로 공고 수집")
    parser.add_argument("--check", action="store_true", help="저장된 세션이 유효한지 확인")
    parser.add_argument("--dump", action="store_true", help="목록 HTML을 data/debug_list.html 로 저장")
    parser.add_argument("--start-page", type=int, default=1, help="수집 시작 페이지 번호 (기본 1)")
    parser.add_argument("--pages", type=int, default=3, help="수집할 목록 페이지 수 (기본 3)")
    parser.add_argument("--max-items", type=int, default=300, help="최대 수집 건수 (기본 300)")
    parser.add_argument("--headful", action="store_true", help="수집 시에도 브라우저 창 표시")
    parser.add_argument("--detail", action="store_true", help="각 공고 상세 페이지까지 열어 급여 보강 (느림)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="출력 CSV 경로")
    parser.add_argument("--login-timeout", type=int, default=600, help="로그인 대기 초 (기본 600)")
    args = parser.parse_args(argv)

    if args.login:
        login_and_save_state(args.login_timeout)
        return 0
    if args.check:
        return check_login()
    if args.dump:
        return dump_html()
    if args.collect:
        return collect(args.pages, args.headful, args.detail, args.out, args.max_items, args.start_page)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
