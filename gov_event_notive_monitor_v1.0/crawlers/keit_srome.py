# crawlers/keit.py
from __future__ import annotations
import re
from datetime import datetime
from typing import List, Dict, Any, Optional
import requests
from bs4 import BeautifulSoup

LIST_URL = "https://srome.keit.re.kr/srome/biz/perform/opnnPrpsl/retrieveTaskAnncmListView.do"

def _norm_date(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    s = s.strip().replace(".", "-").replace("/", "-")
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
    except Exception:
        return s[:10] or None

def _parse_dt(s: Optional[str]) -> datetime:
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except Exception:
        return datetime.min

def _iris_link_from_ancm(ancm_id: str, year: str | None = None) -> str:
    """
    IRIS 상세는 GET로 직접 접근 가능(대부분).
    진행/종료 탭에 따라 보기가 다를 수 있어 'ancmIng' 기본 제공.
    필요하면 메타에 'ancmEnd'도 같이 넣어두자.
    """
    num = ancm_id[1:]
    return f"https://www.iris.go.kr/contents/retrieveBsnsAncmView.do?ancmId=0{num}&ancmPrg=ancmIng"

def fetch_keit_srome_notices(
    max_pages: int = 2,
    prgm_id: str = "XPG201040000",
    timeout: int = 15,
) -> List[Dict[str, Any]]:
    """
    KEIT SROME '과제공고' 목록 파싱 → 통일 스키마로 반환
    """
    items: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()  # ancmId 기준 중복 제거

    sess = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; KEITCrawler/1.0)",
        "Accept-Language": "ko",
    }

    # onclick 예: f_detail('I14917','2025') 또는 "f_detail('I14917', '2025'); return false;"
    onclick_re = re.compile(r"f_detail\(\s*'([^']+)'\s*,\s*'(\d{4})'\s*\)", re.I)

    for page in range(1, max_pages + 1):
        params = {"prgmId": prgm_id, "pageIndex": page}
        r = sess.get(LIST_URL, params=params, headers=headers, timeout=timeout)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        for box in soup.select(".table_list .table_box"):
            # 제목
            title_tag = box.select_one(".table_box_detail .subject .title")
            if not title_tag:
                continue
            title = title_tag.get_text(strip=True)

            # 상세 링크 (onclick 파싱)
            a = box.select_one(".table_box_detail .subject a")
            onclick = (a.get("onclick") or "") if a else ""
            m = onclick_re.search(onclick)

            link = None
            iris_link = None
            reg_year = None
            ancm_id = None

            if m:
                ancm_id, reg_year = m.group(1), m.group(2)

                # SROME 상세 (직접 클릭용으로는 비권장: POST/세션 요구)
                srome_link = (
                    "https://srome.keit.re.kr/srome/biz/perform/opnnPrpsl/"
                    f"retrieveTaskAnncmView.do?ancmId={ancm_id}&bsnsYy={reg_year}&prgmId={prgm_id}"
                )

                iris_link = None
                if ancm_id.startswith("I") and ancm_id[1:].isdigit():
                    iris_link = _iris_link_from_ancm(ancm_id, reg_year)
                # (여기까지: 링크 생성)

            # 등록일/접수기간
            reg_date = None
            recv_period = None
            for ptag in box.select(".table_box_detail .info p"):
                label = ptag.select_one(".label")
                val = ptag.select_one(".value")
                if not label or not val:
                    continue
                lab = label.get_text(strip=True)
                v = val.get_text(strip=True)
                if "등록일" in lab:
                    reg_date = _norm_date(v)
                elif "접수기간" in lab:
                    recv_period = v.strip()

            items.append(
                {
                    "source": "KEIT",
                    "title": title,
                    # ✅ 클릭용은 IRIS를 우선, 없으면 SROME
                    "link": iris_link or srome_link or "",
                    "date": reg_date,
                    "institution": "산업통상자원부 > 한국산업기술평가원",
                    "meta": {
                        "공고ID": ancm_id or "",
                        "공고연도": reg_year or "",
                        "공고명": title,
                        "공고일자": reg_date or "",
                        "접수기간": recv_period or "",
                        "소관부처": "산업통상자원부",
                        "전문기관": "한국산업기술평가원",
                        # 🔁 백업 링크도 같이 보관(프론트에서 필요시 노출)
                        "backup_links": {
                            "srome": srome_link or "",
                            "iris_ing": iris_link or "",
                            "iris_end": (iris_link or "").replace("ancmIng", "ancmEnd") if iris_link else "",
                        },
                    },
                }
            )

    # 최신순 정렬 (등록일 없으면 뒤로)
    items.sort(key=lambda it: _parse_dt(it.get("date")), reverse=True)
    return items


# ---------------------- 메인 실행부 (단독 테스트용) ----------------------
if __name__ == "__main__":
    import argparse, json

    parser = argparse.ArgumentParser(description="KEIT SROME 공고 크롤러 테스트")
    parser.add_argument("--pages", type=int, default=1, help="가져올 페이지 수 (기본 1)")
    parser.add_argument("--prgm", type=str, default="XPG201040000", help="SROME prgmId")
    parser.add_argument("--limit", type=int, default=20, help="출력 개수 제한 (기본 20)")
    parser.add_argument("--json", action="store_true", help="JSON 형태로 출력")
    args = parser.parse_args()

    data = fetch_keit_srome_notices(max_pages=args.pages, prgm_id=args.prgm)
    if args.json:
        print(json.dumps(data[: args.limit], ensure_ascii=False, indent=2))
    else:
        print(f"[KEIT] 수집 {len(data)}건 (표시 {min(len(data), args.limit)}건) — 최신순")
        for row in data[: args.limit]:
            print(f"{row.get('date','')} | {row.get('title','')} | {row.get('link','')}")
