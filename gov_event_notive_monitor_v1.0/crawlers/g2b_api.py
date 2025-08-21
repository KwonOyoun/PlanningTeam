# crawlers/g2b_api.py
# -*- coding: utf-8 -*-

from __future__ import annotations
import os, re, time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from zoneinfo import ZoneInfo  # Windows면 'tzdata' 설치 필요: pip install tzdata

# ──────────────────────────────────────────────────────────────────────────────
# 환경변수
# ──────────────────────────────────────────────────────────────────────────────
load_dotenv()
SERVICE_KEY = os.getenv("G2B_API_KEY")
KST = ZoneInfo("Asia/Seoul")

# 제목 키워드(기본): 여기 들어있는 단어 중 하나라도 포함되면 통과
DEFAULT_TITLE_KEYWORDS = ["의료기기", "헬스케어"]

# ──────────────────────────────────────────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────────────────────────────────────────
def _now() -> datetime:
    # tz-aware KST 시각
    return datetime.now(KST)

def _txt(el) -> str:
    return el.get_text(strip=True) if el else ""

def _parse_dt_loose(s: str) -> Optional[datetime]:
    """여러 포맷을 느슨하게 파싱하고 KST로 tz-aware 반환"""
    s = (s or "").strip()
    fmts = [
        "%Y-%m-%d %H:%M", "%Y.%m.%d %H:%M", "%Y/%m/%d %H:%M",
        "%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d",
    ]
    for f in fmts:
        try:
            dt = datetime.strptime(s[:16], f)
            return dt.replace(tzinfo=KST)
        except Exception:
            pass
    return None

def _ceil_div(a: int, b: int) -> int:
    return (a + b - 1) // b if b else 0

# ──────────────────────────────────────────────────────────────────────────────
# G2B API 클라이언트
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class G2BClient:
    service_key: str
    sess: requests.Session = requests.Session()

    BASE = "http://apis.data.go.kr/1230000/ao/PubDataOpnStdService"
    EP_NOTICE = "getDataSetOpnStdBidPblancInfo"  # 공고정보

    def __post_init__(self):
        self.sess.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0 Safari/537.36"),
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6",
        })

    def _get(self, endpoint: str, params: Dict[str, str]) -> str:
        url = f"{self.BASE}/{endpoint}?{urlencode({**params, 'ServiceKey': self.service_key})}"
        r = self.sess.get(url, timeout=25)
        r.raise_for_status()
        return r.text

    # A) 게시(공고)일시 구간 조회
    def get_by_posted(self, start_dt: datetime, end_dt: datetime, *, rows: int, page: int) -> str:
        if (end_dt - start_dt).days > 31:
            start_dt = end_dt - timedelta(days=31)
        params = {
            "bidNtceBgnDt": start_dt.strftime("%Y%m%d%H%M"),
            "bidNtceEndDt": end_dt.strftime("%Y%m%d%H%M"),
            "numOfRows": str(rows),
            "pageNo": str(page),
            # 힌트(무시될 수 있음)
            "bsnsDivCd": "5",  # 용역
        }
        return self._get(self.EP_NOTICE, params)

    # B) (구) 입찰시작~마감일시 구간 조회
    def get_by_deadline(self, start_dt: datetime, end_dt: datetime, *, rows: int, page: int) -> str:
        params = {
            "bidBeginDate": start_dt.strftime("%Y%m%d%H%M"),
            "bidClseDate":  end_dt.strftime("%Y%m%d%H%M"),
            "numOfRows": str(rows),
            "pageNo": str(page),
        }
        return self._get(self.EP_NOTICE, params)

# ──────────────────────────────────────────────────────────────────────────────
# 상세페이지에서 '게시일시' 긁기
# ──────────────────────────────────────────────────────────────────────────────
DETAIL_URL_TMPL = ("https://www.g2b.go.kr/ep/invitation/publish/"
                   "bidInfoDtl.do?bidno={no}&bidseq={seq}&releaseYn=Y")

_LABEL_PAT = re.compile("(게시일시|공고게시일시|공고일시|입찰공고일시|등록일|등록일시)")

def _detail_url_from_item(m: Dict[str, str]) -> Optional[str]:
    url = (m.get("bidNtceUrl") or "").strip()
    if url:
        return url
    no  = (m.get("bidNtceNo")  or "").strip()
    seq = (m.get("bidNtceOrd") or "").strip() or "00"
    if no:
        return DETAIL_URL_TMPL.format(no=no, seq=seq)
    return None

def _scrape_posted_dt(url: str, sess: requests.Session, timeout: int = 20) -> Optional[datetime]:
    """상세 페이지 HTML에서 게시일/공고일시를 최대한 찾아 KST tz-aware로 반환"""
    try:
        r = sess.get(url, timeout=timeout)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # 1) 라벨 옆 값
        for lab in soup.find_all(text=_LABEL_PAT):
            node = getattr(lab, "parent", None)
            if not node:
                continue
            candidates = [
                node.find_next_sibling(),
                node.parent.find_next_sibling() if getattr(node, "parent", None) else None,
            ]
            for cand in candidates:
                dt = _parse_dt_loose(_txt(cand))
                if dt:
                    return dt

        # 2) 문서 전체에서 정규식
        text = soup.get_text(" ", strip=True)
        m = re.search(r"(게시일시|공고게시일시|공고일시|입찰공고일시|등록일|등록일시)\s*[:：]?\s*"
                      r"([0-9]{4}[-./][0-9]{2}[-./][0-9]{2})(\s*[0-9]{2}:[0-9]{2})?", text)
        if m:
            raw = (m.group(2) + (m.group(3) or "")).strip()
            return _parse_dt_loose(raw)
    except Exception:
        pass
    return None

# ──────────────────────────────────────────────────────────────────────────────
# 메인 수집기
# ──────────────────────────────────────────────────────────────────────────────
def fetch_g2b_service_notices(
    *,
    days_back: int = 5,       # 최근 N일
    rows: int = 100,          # 페이지당 행수
    scan_pages: int = 80,     # 역페이징으로 최대 몇 페이지 스캔할지(모드별)
    prefer: str = "mix",      # "ntce" | "clse" | "mix"
    max_details: int = 300,   # 상세 페이지 조회 상한(게시일시 보정용)
    pause_sec: float = 0.1,   # API/상세 호출 사이 대기
    verbose: bool = False,
    keywords: Optional[List[str]] = None,  # ← 제목 필터(기본: 의료기기/헬스케어)
) -> List[Dict[str, Any]]:
    """
    • 한국시간(KST) 기준
    • 최근 N일 '게시'된 '용역' 공고만 대상
    • 마감일시가 현재 시각 이전(< now)은 제외 (오늘 마감은 포함)
    • ntce(게시일시) → clse(마감구간) 순으로 역페이징 스캔하여 최신부터 확보
    • 상세 페이지에서 '게시일시'를 최대한 보정
    • 제목에 keywords 중 하나라도 포함된 경우만 수집
    """
    if not SERVICE_KEY:
        raise RuntimeError("G2B_API_KEY(.env)이 설정되지 않았습니다.")

    kw_list = [k for k in (keywords if keywords is not None else DEFAULT_TITLE_KEYWORDS) if k]

    api  = G2BClient(SERVICE_KEY)
    now  = _now()
    # 오늘 00:00부터 days_back일 범위
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_back-1)
    end   = now

    mode_order = {"ntce": ["ntce"], "clse": ["clse"], "mix": ["ntce", "clse"]}[prefer]

    def _parse_items(xml: str) -> Tuple[List[Dict[str, str]], int]:
        root = ET.fromstring(xml)
        code = (root.findtext(".//header/resultCode") or "").strip()
        msg  = (root.findtext(".//header/resultMsg")  or "").strip()
        if code and code != "00":
            if verbose:
                print(f"[G2B] API 오류 code={code} msg={msg}")
            return [], 0
        items = root.findall(".//body/items/item") or root.findall(".//item")
        total = int((root.findtext(".//body/totalCount") or "0").strip() or 0)
        rows_ = [{c.tag: (c.text or "").strip() for c in it} for it in items]
        return rows_, total

    def _collect(mode: str) -> List[Dict[str, Any]]:
        cand: List[Dict[str, Any]] = []
        seen_no_seq: set[Tuple[str, str]] = set()

        # 1) totalCount 파악용 1페이지
        xml1 = api.get_by_posted(start, end, rows=rows, page=1) if mode == "ntce" \
            else api.get_by_deadline(start, end, rows=rows, page=1)
        items1, total = _parse_items(xml1)
        total_pages = max(1, _ceil_div(total, rows))
        if verbose:
            from collections import Counter
            dist = Counter((x.get("bsnsDivNm") or "").strip() for x in items1)
            print(f"[G2B] {mode} p1 rows={len(items1)} total={total} dist={dist.most_common(5)}")

        # 2) 최신부터 역페이징
        last = total_pages
        first = max(1, last - scan_pages + 1)
        page_range = range(last, first - 1, -1)

        for page in page_range:
            try:
                xml = api.get_by_posted(start, end, rows=rows, page=page) if mode == "ntce" \
                    else api.get_by_deadline(start, end, rows=rows, page=page)
                items, _ = _parse_items(xml)
                if not items:
                    continue

                for it in items:
                    # 업무구분: '용역' 포함(일반/학술/기타용역 커버)
                    bsns = (it.get("bsnsDivNm") or "").strip()
                    if "용역" not in bsns:
                        continue

                    title = (it.get("bidNtceNm") or "").strip()
                    # 🔎 제목 키워드 필터(하나라도 포함되면 통과)
                    if kw_list and not any(kw in title for kw in kw_list):
                        continue

                    # 마감일시(오늘 마감 포함, 이미 지난 것만 제외)
                    d, t = (it.get("bidClseDate") or "").strip(), (it.get("bidClseTm") or "").strip()
                    if not d or not t:
                        continue
                    try:
                        clse_dt = datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M").replace(tzinfo=KST)
                        if clse_dt < now:
                            continue
                    except Exception:
                        continue

                    # 중복 제거
                    no  = (it.get("bidNtceNo")  or "").strip()
                    seq = (it.get("bidNtceOrd") or "").strip()
                    key = (no, seq)
                    if no and key in seen_no_seq:
                        continue
                    if no:
                        seen_no_seq.add(key)

                    # API에서 '게시일시'가 오면 우선 사용
                    api_posted = None
                    nd, nt = (it.get("bidNtceDate") or "").strip(), (it.get("bidNtceBgn") or "").strip()
                    if nd:
                        api_posted = _parse_dt_loose((nd + (" " + nt if nt else "")).strip())

                    cand.append({
                        "title": title,
                        "institution": (it.get("ntceInsttNm") or "").strip(),
                        "link": _detail_url_from_item(it) or "",
                        "meta": it,
                        "_clse_dt": clse_dt,
                        "_posted": api_posted,
                        "_need_detail": (mode == "clse") or (api_posted is None),
                    })

                time.sleep(pause_sec)
            except Exception as e:
                if verbose:
                    print(f"[G2B] {mode} page {page} error:", e)
                time.sleep(pause_sec)
                continue

        return cand

    final: List[Dict[str, Any]] = []
    for mode in mode_order:
        cand = _collect(mode)

        # 상세 페이지로 '게시일시' 보정
        fixed = 0
        for c in cand:
            if c["_need_detail"] and c["link"] and fixed < max_details and c.get("_posted") is None:
                dt = _scrape_posted_dt(c["link"], api.sess, timeout=20)
                if dt:
                    c["_posted"] = dt
                fixed += 1

        # 최근 N일(게시일시) + 마감 미경과 필터링
        for c in cand:
            p = c["_posted"]
            if not p:
                continue
            if not (start <= p <= end):
                continue
            if c["_clse_dt"] < now:
                continue

            final.append({
                "title": c["title"],
                "date": p.strftime("%Y-%m-%d"),                 # 화면 정렬용(일 단위)
                "end_date": c["_clse_dt"].strftime("%Y-%m-%d %H:%M"),
                "link": c["link"],
                "institution": c["institution"],
                "notice_posted_at": p.strftime("%Y-%m-%d %H:%M"),  # 게시일시(표시)
                "meta": c["meta"],
            })

        if final:
            break  # 현재 모드에서 충분히 확보되면 다음 모드로 안 넘어감

    # 정렬: 게시일시(문자) → datetime 파싱 후 내림차순
    def _key(x):
        return _parse_dt_loose(x.get("notice_posted_at") or x.get("date") or "") or datetime.min.replace(tzinfo=KST)

    final.sort(key=_key, reverse=True)
    return final

# --- backward-compat wrapper for main.py -------------------------------------
def fetch_g2b_notices(max_pages: int = 5,
                      rows: int = 50,
                      days: int = 5,
                      query: Optional[object] = None,
                      prefer: str = "mix"):
    """
    main.py 호환용 래퍼.
    - query가 문자열이면 공백/쉼표로 분할하여 키워드로 사용
    - query가 리스트/튜플/세트면 그대로 키워드로 사용
    - 미지정 시 기본 키워드(DEFAULT_TITLE_KEYWORDS) 적용
    """
    # 최신 페이지부터 더 깊게 스캔
    scan_pages = max(20, int(max_pages) * 20)

    # query → keywords 변환
    keywords: Optional[List[str]] = None
    if isinstance(query, str) and query.strip():
        import re as _re
        keywords = [s for s in _re.split(r"[,\s]+", query.strip()) if s]
    elif isinstance(query, (list, tuple, set)):
        keywords = [str(s) for s in query if str(s).strip()]
    else:
        keywords = None  # 기본 키워드 사용

    items = fetch_g2b_service_notices(
        days_back=days,
        rows=rows,
        scan_pages=scan_pages,
        prefer=prefer,
        max_details=300,
        pause_sec=0.1,
        verbose=False,
        keywords=keywords,  # ← 제목 필터 반영
    )
    return items

# ──────────────────────────────────────────────────────────────────────────────
# CLI 테스트
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    items = fetch_g2b_service_notices(
        days_back=5,          # 최근 5일
        rows=100,             # 페이지당 100행
        scan_pages=80,        # 최신 쪽부터 80페이지 스캔
        prefer="mix",         # ntce 먼저, 부족하면 clse 보강
        max_details=300,
        pause_sec=0.1,
        verbose=True,         # 디버그 로그 보고 싶으면 True
        keywords=None,        # None이면 기본 ["의료기기","헬스케어"] 적용
    )
    print(f"[G2B] 최근 {5}일(제목 키워드 필터) : {len(items)}건")
    for it in items[:80]:
        print(f"{it['notice_posted_at']} | {it['title']} | {it['institution']} | 마감 {it['end_date']}")
