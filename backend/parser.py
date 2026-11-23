"""메디게이트 채용 공고 텍스트 정규화 모듈.

이 모듈은 외부 라이브러리에 의존하지 않는 순수 파이썬 함수만 담는다.
스크래퍼, CSV 업로드 API, 단위 테스트가 모두 같은 규칙을 사용한다.

급여 표기 규칙 요약
-------------------
* 단위가 없는 숫자는 의사 채용 공고 관행에 따라 "만원"으로 해석한다.
  예) "월 1,800~2,200"  ->  월 1,800만원 ~ 2,200만원
* "억", "천만", "만원"이 붙은 숫자는 각각 만원 단위로 환산한다.
  예) "연봉 2.4억"      ->  연 24,000만원 (월 환산 2,000만원)
      "1억 2,000만원"   ->  연 12,000만원
* "세후", "실수령"이 있으면 is_after_tax=True, "세전"이거나 표기가 없으면 False.
  (공고에 아무 표기가 없으면 통상 세전으로 본다.)
* "협의", "면접 후 결정", "추후 협의" 등 금액이 없는 표기는 pay_type="협의".
"""

from __future__ import annotations

import re
from typing import Optional

__all__ = [
    "parse_salary",
    "normalize_region",
    "normalize_specialty",
    "SPECIALTIES",
    "SIDO_LIST",
]


# ---------------------------------------------------------------------------
# 공통 유틸
# ---------------------------------------------------------------------------

def _to_float(value: str) -> Optional[float]:
    try:
        return float(value.replace(",", ""))
    except (TypeError, ValueError):
        return None


def _round1(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return round(value, 1)


# ---------------------------------------------------------------------------
# 급여 파싱
# ---------------------------------------------------------------------------

_NEGOTIABLE_PATTERNS = (
    "협의",
    "협의가능",
    "협의 가능",
    "면접후",
    "면접 후",
    "추후",
    "추후결정",
    "추후 결정",
    "상담후",
    "상담 후",
    "내규",
    "규정에",
    "별도",
    "미정",
)

_AFTER_TAX_PATTERNS = ("세후", "세 후", "실수령", "실 수령", "네트", "net", "NET", "Net")
_BEFORE_TAX_PATTERNS = ("세전", "세 전", "gross", "GROSS", "Gross", "thousand")

# 금액 뒤에 붙는 단위. 긴 단위부터 인식해야 "만원"이 "원"보다 먼저 잡힌다.
_UNIT_FACTORS = (
    ("억원", 10000.0),
    ("억", 10000.0),
    ("천만원", 1000.0),
    ("천만", 1000.0),
    ("만원", 1.0),
    ("만", 1.0),
)

_NUMBER_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(억원|억|천만원|천만|만원|만)?")
_NON_SALARY_SUFFIX_RE = re.compile(r"^(?:분|일|주|명|인|호선|호|층|차|회|년|월|시|세|평|개월|시간|대)")
_RANGE_SPLIT_RE = re.compile(r"~|∼|～|-|–|—|부터|에서|/|∣")


def _unit_factor(unit: Optional[str]) -> Optional[float]:
    if not unit:
        return None
    for name, factor in _UNIT_FACTORS:
        if unit == name:
            return factor
    return None


def _parse_amount_part(part: str) -> Optional[float]:
    """범위의 한쪽("1억 2,000만", "1,800", "3.5억")을 만원 단위로 환산한다."""
    matches = list(_NUMBER_RE.finditer(part))
    if not matches:
        return None

    total = 0.0
    found = False
    for match in matches:
        number = _to_float(match.group(1))
        if number is None or number <= 0:
            continue
        unit = match.group(2)
        if unit is None:
            after = part[match.end():].lstrip()
            if _NON_SALARY_SUFFIX_RE.match(after):
                continue
        factor = _unit_factor(unit)
        if factor is None:
            # "NET 2.0", "세후 1.7"처럼 소수점 한 자리(0.5~9.9)만 적힌 경우 천만원 단위로 본다.
            if "." in match.group(1) and 0.5 <= number <= 9.9:
                factor = 1000.0
            else:
                # 단위가 없으면 만원 단위로 본다. (의사 채용 공고 관행)
                factor = 1.0
        total += number * factor
        found = True

    return total if found else None


def parse_salary(text: Optional[str]) -> dict:
    """급여 문자열을 구조화된 dict로 변환한다.

    반환 키
        pay_type            : "월급" | "연봉" | "협의" | "미상"
        pay_basis           : "monthly" | "annual" | "negotiable" | "unknown"
        min_manwon          : 표기 기준 최소 금액(만원)
        max_manwon          : 표기 기준 최대 금액(만원)
        mid_manwon          : (min+max)/2 (만원)
        is_after_tax        : 세후/실수령 여부
        monthly_min_manwon  : 월 환산 최소(만원)  *연봉은 /12
        monthly_max_manwon  : 월 환산 최대(만원)
        monthly_mid_manwon  : 월 환산 중간값(만원)
        raw                 : 원본 문자열
    """
    raw = (text or "").strip()
    empty = {
        "pay_type": "미상",
        "pay_basis": "unknown",
        "min_manwon": None,
        "max_manwon": None,
        "mid_manwon": None,
        "monthly_min_manwon": None,
        "monthly_max_manwon": None,
        "monthly_mid_manwon": None,
        "is_after_tax": False,
        "raw": raw,
    }
    if not raw:
        return empty

    lowered = raw.lower()
    is_after_tax = any(p.lower() in lowered for p in _AFTER_TAX_PATTERNS)

    # 금액이 없는 "협의" 계열 표기
    compact_for_keyword = raw.replace(" ", "")
    if any(p.replace(" ", "") in compact_for_keyword for p in _NEGOTIABLE_PATTERNS):
        if not re.search(r"\d", raw):
            return {**empty, "pay_type": "협의", "pay_basis": "negotiable"}

    # 급여 기준(월급/연봉) 판단
    has_month = bool(re.search(r"월급|월\s*[0-9０-９]|월\s*[세실]|/\s*월|월\s*\d|월\s*$", raw)) or "월급" in raw
    # "월 1,800만원"처럼 숫자 앞에 오는 경우를 폭넓게 잡는다.
    if re.search(r"월\s*[\d,]", raw):
        has_month = True

    has_annual = any(k in raw for k in ("연봉", "연 소득", "연소득", "년봉"))
    if re.search(r"연\s*[\d,]", raw):
        has_annual = True
    if "억" in raw:
        has_annual = True

    if has_month:
        pay_type, pay_basis = "월급", "monthly"
    elif has_annual:
        pay_type, pay_basis = "연봉", "annual"
    else:
        # 표기가 없으면 국내 의사 채용 공고의 다수 관행을 따라 월급으로 본다.
        pay_type, pay_basis = "월급", "monthly"

    # 금액 추출: 괄호는 공백으로 풀고 인센티브 부가 표기(+인센200~300 등)를 분리한다.
    cleaned = raw
    cleaned = cleaned.replace("만 원", "만원").replace("억 원", "억원")
    cleaned = re.sub(r"\+\s*(?:인센(?:티브)?|@|α)[^\s,;)]*", " ", cleaned, flags=re.I)
    cleaned = cleaned.replace("(", " ").replace(")", " ")
    cleaned = cleaned.replace(",", "")

    # 범위 기호 앞뒤의 한글 조사/단위 문구 정리
    parts = [p for p in _RANGE_SPLIT_RE.split(cleaned) if p and p.strip()]
    amounts = []
    for part in parts:
        amount = _parse_amount_part(part)
        if amount is None or amount < 10:  # 10만원 미만 숫자는 날짜/횟수일 가능성
            continue
        # 한 문자열에 "월 1,800만원 / 연봉 2.2억"처럼 두 기준이 섞인 경우를 보정
        part_is_annual = bool(re.search(r"억|연봉|연\s*[\d,]", part))
        part_is_monthly = bool(re.search(r"월", part))
        if pay_basis == "monthly" and part_is_annual and not part_is_monthly:
            amount = amount / 12
        elif pay_basis == "annual" and part_is_monthly:
            amount = amount * 12
        amounts.append(amount)

    if not amounts:
        # 숫자는 있으나 금액으로 해석하지 못한 경우
        if re.search(r"\d", raw):
            return {**empty, "is_after_tax": is_after_tax}
        return {**empty, "pay_type": "협의", "pay_basis": "negotiable", "is_after_tax": is_after_tax}

    min_manwon = min(amounts)
    max_manwon = max(amounts)
    mid_manwon = (min_manwon + max_manwon) / 2

    if pay_basis == "annual":
        monthly_min = min_manwon / 12
        monthly_max = max_manwon / 12
        monthly_mid = mid_manwon / 12
    else:
        monthly_min = min_manwon
        monthly_max = max_manwon
        monthly_mid = mid_manwon

    return {
        "pay_type": pay_type,
        "pay_basis": pay_basis,
        "min_manwon": _round1(min_manwon),
        "max_manwon": _round1(max_manwon),
        "mid_manwon": _round1(mid_manwon),
        "monthly_min_manwon": _round1(monthly_min),
        "monthly_max_manwon": _round1(monthly_max),
        "monthly_mid_manwon": _round1(monthly_mid),
        "is_after_tax": is_after_tax,
        "raw": raw,
    }


# ---------------------------------------------------------------------------
# 지역 정규화
# ---------------------------------------------------------------------------

SIDO_LIST = [
    "서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종",
    "경기", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주",
]

_SIDO_ALIASES = {
    "서울특별시": "서울", "서울시": "서울", "서울": "서울",
    "부산광역시": "부산", "부산시": "부산", "부산": "부산",
    "대구광역시": "대구", "대구시": "대구", "대구": "대구",
    "인천광역시": "인천", "인천시": "인천", "인천": "인천",
    "광주광역시": "광주", "광주시": "광주", "광주": "광주",
    "대전광역시": "대전", "대전시": "대전", "대전": "대전",
    "울산광역시": "울산", "울산시": "울산", "울산": "울산",
    "세종특별자치시": "세종", "세종시": "세종", "세종": "세종",
    "경기도": "경기", "경기": "경기",
    "강원특별자치도": "강원", "강원도": "강원", "강원": "강원",
    "충청북도": "충북", "충북": "충북", "충청남도": "충남", "충남": "충남",
    "전라북도": "전북", "전북특별자치도": "전북", "전북": "전북",
    "전라남도": "전남", "전남": "전남",
    "전남광주": "전남", "전남/광주": "전남", "광주전남": "전남",
    "경상북도": "경북", "경북": "경북", "경상남도": "경남", "경남": "경남",
    "제주특별자치도": "제주", "제주도": "제주", "제주시": "제주", "제주": "제주",
}

# 시/도 없이 구 단위만 적힌 경우를 위한 주요 구 → 시/도 보조 매핑.
# ("강남구"만 적힌 공고를 서울로 묶어 주는 용도이며, 확실한 경우만 넣는다.)
_GU_TO_SIDO = {
    "강남구": "서울", "서초구": "서울", "송파구": "서울", "강동구": "서울",
    "마포구": "서울", "용산구": "서울", "종로구": "서울", "중구": "서울",
    "성동구": "서울", "광진구": "서울", "노원구": "서울", "강서구": "서울",
    "영등포구": "서울", "양천구": "서울", "구로구": "서울", "동작구": "서울",
    "관악구": "서울", "서대문구": "서울", "은평구": "서울", "성북구": "서울",
    "도봉구": "서울", "중랑구": "서울", "강북구": "서울",
    "해운대구": "부산", "수영구": "부산", "부산진구": "부산", "동래구": "부산",
    "연제구": "부산", "남구": "부산", "금정구": "부산",
    "수성구": "대구", "달서구": "대구", "중구(대구)": "대구",
    "유성구": "대전", "서구(대전)": "대전",
    "일산동구": "경기", "일산서구": "경기", "분당구": "경기",
    "수지구": "경기", "기흥구": "경기", "영통구": "경기", "권선구": "경기",
}


def normalize_region(text: Optional[str]) -> dict:
    """지역 문자열을 {"sido": "경기", "sigungu": "성남시", "detail": "분당구", "raw": ...}로 분해한다."""
    raw = (text or "").strip()
    result = {"sido": "미상", "sigungu": "", "detail": "", "raw": raw}
    if not raw:
        return result

    normalized = re.sub(r"\s+", " ", raw)
    tokens = normalized.split(" ")
    tokens = [t for t in tokens if t and t not in {"-", "/", ","}]

    sido = None
    rest_tokens = tokens

    # 가장 긴 별칭부터 매칭 (예: "서울특별시" > "서울시" > "서울")
    for alias in sorted(_SIDO_ALIASES, key=len, reverse=True):
        if not tokens:
            break
        first = tokens[0]
        if first == alias:
            sido = _SIDO_ALIASES[alias]
            rest_tokens = tokens[1:]
            break
        if first.startswith(alias) and len(first) > len(alias):
            # "서울강남구"처럼 붙여 쓴 경우
            sido = _SIDO_ALIASES[alias]
            rest_tokens = [first[len(alias):]] + tokens[1:]
            break

    if sido is None:
        # 시/도 표기 없이 구/시만 있는 경우 보조 매핑
        for token in tokens:
            if token in _GU_TO_SIDO:
                sido = _GU_TO_SIDO[token]
                break
        if sido is None:
            return {**result, "sigungu": normalized}

    sigungu = ""
    detail = ""
    if rest_tokens:
        sigungu = rest_tokens[0].strip(",/")
        detail = " ".join(rest_tokens[1:]).strip(",/")

    # "성남시분당구"처럼 한 토큰에 시+구가 붙은 경우 분리
    if not detail and sigungu:
        match = re.match(r"^(.+?(?:시|군))(.+?(?:구|읍|면|동))$", sigungu)
        if match:
            sigungu, detail = match.group(1), match.group(2)

    return {"sido": sido, "sigungu": sigungu, "detail": detail, "raw": raw}


# ---------------------------------------------------------------------------
# 진료과목 정규화
# ---------------------------------------------------------------------------

SPECIALTIES = [
    "내과", "정형외과", "피부과", "성형외과", "가정의학과", "소아청소년과",
    "정신건강의학과", "영상의학과", "마취통증의학과", "산부인과", "외과",
    "신경외과", "신경과", "안과", "이비인후과", "비뇨의학과", "재활의학과",
    "응급의학과", "직업환경의학과", "진단검사의학과", "병리과",
    "방사선종양학과", "핵의학과", "결핵과",
]

# 별칭은 "긴 문자열 우선"으로 매칭한다. (예: "소화기내과" -> 내과)
_SPECIALTY_ALIASES = {
    # 내과 계열
    "소화기내과": "내과", "순환기내과": "내과", "호흡기내과": "내과",
    "내분비내과": "내과", "신장내과": "내과", "혈액종양내과": "내과",
    "감염내과": "내과", "류마티스내과": "내과", "알레르기내과": "내과",
    "내과": "내과",
    # 정형외과
    "정형외과": "정형외과", "정형": "정형외과", "orthopedics": "정형외과",
    # 피부과
    "피부과": "피부과", "dermatology": "피부과", "dermat": "피부과",
    # 성형외과
    "성형외과": "성형외과", "성형": "성형외과", "plastic": "성형외과",
    # 가정의학과
    "가정의학과": "가정의학과", "가정의학": "가정의학과", "가정과": "가정의학과",
    # 소아청소년과
    "소아청소년과": "소아청소년과", "소아과": "소아청소년과",
    "소아": "소아청소년과", "pediatrics": "소아청소년과",
    # 정신건강의학과
    "정신건강의학과": "정신건강의학과", "정신건강의학": "정신건강의학과",
    "정신과": "정신건강의학과", "psychiatry": "정신건강의학과",
    # 영상의학과
    "영상의학과": "영상의학과", "영상의학": "영상의학과", "방사선과": "영상의학과",
    "radiology": "영상의학과",
    # 마취통증의학과
    "마취통증의학과": "마취통증의학과", "마취통증의학": "마취통증의학과",
    "마취과": "마취통증의학과", "통증의학과": "마취통증의학과",
    "anesthesiology": "마취통증의학과",
    # 산부인과
    "산부인과": "산부인과", "obgyn": "산부인과", "ob/gyn": "산부인과",
    # 외과
    "일반외과": "외과", "외과": "외과",
    # 신경외과
    "신경외과": "신경외과", "신경외과의": "신경외과",
    # 신경과
    "신경과": "신경과", "neurology": "신경과",
    # 안과
    "안과": "안과", "ophthalmology": "안과",
    # 이비인후과
    "이비인후과": "이비인후과", "이비인후": "이비인후과", "otorhinolaryngology": "이비인후과", "이비인후과(ent)": "이비인후과", "(ent)": "이비인후과",
    # 비뇨의학과
    "비뇨의학과": "비뇨의학과", "비뇨기과": "비뇨의학과", "비뇨": "비뇨의학과",
    # 재활의학과
    "재활의학과": "재활의학과", "재활의학": "재활의학과", "재활과": "재활의학과",
    # 응급의학과
    "응급의학과": "응급의학과", "응급의학": "응급의학과",
    # 직업환경의학과
    "직업환경의학과": "직업환경의학과", "직업의학과": "직업환경의학과",
    # 진단검사의학과
    "진단검사의학과": "진단검사의학과", "진단검사": "진단검사의학과",
    # 병리과
    "병리과": "병리과", "해부병리": "병리과",
    # 방사선종양학과
    "방사선종양학과": "방사선종양학과", "방사선종양": "방사선종양학과",
    # 핵의학과
    "핵의학과": "핵의학과",
    # 결핵과
    "결핵과": "결핵과",
}


def normalize_specialty(text: Optional[str]) -> str:
    """진료과목 문자열을 표준 명칭으로 변환한다. 매칭 실패 시 "기타"."""
    raw = (text or "").strip()
    if not raw:
        return "기타"

    lowered = raw.lower()
    for alias in sorted(_SPECIALTY_ALIASES, key=len, reverse=True):
        if alias in lowered:
            return _SPECIALTY_ALIASES[alias]
    return "기타"
