"""parser.py 단위 테스트.

실행:
    cd medigate-salary-site
    python -m pytest -q
"""

from __future__ import annotations

import pytest

from backend.parser import normalize_region, normalize_specialty, parse_salary


class TestParseSalary:
    @pytest.mark.parametrize(
        "text",
        ["월 1,800만원", "월 1800만원", "월1800만원", "월급 1,800만원", "월 2000만 원"],
    )
    def test_monthly_single_amount(self, text):
        result = parse_salary(text)
        assert result["pay_type"] == "월급"
        assert result["pay_basis"] == "monthly"
        assert result["min_manwon"] == 1800 or result["min_manwon"] == 2000
        assert result["min_manwon"] == result["max_manwon"] == result["mid_manwon"]

    def test_monthly_value_is_1800(self):
        result = parse_salary("월 1,800만원")
        assert result["min_manwon"] == 1800
        assert result["monthly_mid_manwon"] == 1800
        assert result["is_after_tax"] is False

    def test_after_tax_range_without_unit(self):
        result = parse_salary("세후 1,800~2,200")
        assert result["pay_type"] == "월급"
        assert result["min_manwon"] == 1800
        assert result["max_manwon"] == 2200
        assert result["mid_manwon"] == 2000
        assert result["is_after_tax"] is True
        assert result["monthly_mid_manwon"] == 2000

    def test_annual_eok(self):
        result = parse_salary("연봉 2.4억")
        assert result["pay_type"] == "연봉"
        assert result["pay_basis"] == "annual"
        assert result["min_manwon"] == 24000
        assert result["max_manwon"] == 24000
        assert result["monthly_mid_manwon"] == 2000

    def test_annual_eok_range(self):
        result = parse_salary("연 3억~3.5억")
        assert result["pay_type"] == "연봉"
        assert result["min_manwon"] == 30000
        assert result["max_manwon"] == 35000
        assert result["monthly_min_manwon"] == 2500
        assert result["monthly_max_manwon"] == pytest.approx(2916.7, abs=0.1)

    def test_negotiable(self):
        for text in ["협의", "면접 후 결정", "추후 협의", "내규에 따름"]:
            result = parse_salary(text)
            assert result["pay_type"] == "협의", text
            assert result["pay_basis"] == "negotiable"
            assert result["min_manwon"] is None
            assert result["monthly_mid_manwon"] is None

    def test_after_tax_in_parentheses(self):
        result = parse_salary("월 2,000~2,400만원 (세후)")
        assert result["min_manwon"] == 2000
        assert result["max_manwon"] == 2400
        assert result["is_after_tax"] is True

    def test_annual_compound_amount(self):
        result = parse_salary("연봉 1억 8,000만원")
        assert result["pay_type"] == "연봉"
        assert result["min_manwon"] == 18000
        assert result["monthly_mid_manwon"] == 1500

    def test_after_tax_single(self):
        result = parse_salary("실수령 월 1,600만원")
        assert result["min_manwon"] == 1600
        assert result["is_after_tax"] is True

    def test_incentive_ignored(self):
        result = parse_salary("월 1,300만원 + 인센티브")
        assert result["min_manwon"] == 1300
        assert result["max_manwon"] == 1300

    def test_month_and_annual_mixed(self):
        result = parse_salary("월 1,800만원 / 연봉 2.2억")
        assert result["pay_type"] == "월급"
        assert 1800 <= result["min_manwon"] <= 1900
        assert 1800 <= result["max_manwon"] <= 1900

    def test_empty(self):
        assert parse_salary("")["pay_type"] == "미상"
        assert parse_salary(None)["pay_type"] == "미상"

    def test_annual_bare_number(self):
        result = parse_salary("연봉 7000")
        assert result["pay_type"] == "연봉"
        assert result["min_manwon"] == 7000
        assert result["monthly_mid_manwon"] == pytest.approx(583.3, abs=0.1)

    def test_thousand_man_unit(self):
        result = parse_salary("연봉 1억2천만원")
        assert result["min_manwon"] == 12000


class TestNormalizeRegion:
    @pytest.mark.parametrize(
        "text,sido,sigungu",
        [
            ("서울시 강남구", "서울", "강남구"),
            ("서울특별시 서초구", "서울", "서초구"),
            ("경기 성남시 분당구", "경기", "성남시"),
            ("경기도 수원시 영통구", "경기", "수원시"),
            ("부산 해운대구", "부산", "해운대구"),
            ("대전 유성구", "대전", "유성구"),
            ("제주 제주시", "제주", "제주시"),
            ("세종", "세종", ""),
            ("강남구", "서울", "강남구"),
        ],
    )
    def test_region_split(self, text, sido, sigungu):
        result = normalize_region(text)
        assert result["sido"] == sido
        assert result["sigungu"] == sigungu

    def test_detail_is_kept(self):
        result = normalize_region("경기 성남시 분당구")
        assert result["detail"] == "분당구"
        assert result["raw"] == "경기 성남시 분당구"

    def test_empty(self):
        result = normalize_region("")
        assert result["sido"] == "미상"


class TestNormalizeSpecialty:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("내과", "내과"),
            ("내과 전문의", "내과"),
            ("소화기내과", "내과"),
            ("정형외과", "정형외과"),
            ("피부과", "피부과"),
            ("성형외과", "성형외과"),
            ("가정의학과", "가정의학과"),
            ("소아과", "소아청소년과"),
            ("소아청소년과", "소아청소년과"),
            ("정신과", "정신건강의학과"),
            ("정신건강의학과", "정신건강의학과"),
            ("영상의학과", "영상의학과"),
            ("방사선과", "영상의학과"),
            ("마취통증의학과", "마취통증의학과"),
            ("마취과", "마취통증의학과"),
            ("이비인후과", "이비인후과"),
            ("비뇨기과", "비뇨의학과"),
            ("산부인과", "산부인과"),
            ("응급의학과", "응급의학과"),
        ],
    )
    def test_specialty(self, text, expected):
        assert normalize_specialty(text) == expected

    def test_unknown(self):
        assert normalize_specialty("") == "기타"
        assert normalize_specialty("기타") == "기타"
