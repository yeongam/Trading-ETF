# 토스증권 US 해외주식 자동매매 시스템

Playwright 기반 토스증권 웹 자동화로 미국 주식을 자동으로 발굴하고 매매하는 시스템입니다.

![Python](https://img.shields.io/badge/Python-3.11-blue)
![Playwright](https://img.shields.io/badge/Playwright-Automation-green)
![FastAPI](https://img.shields.io/badge/FastAPI-Dashboard-teal)
![License](https://img.shields.io/badge/License-MIT-yellow)

---

## 주요 기능

- **자동 종목 발굴** — 토스증권 실시간 거래대금 차트에서 US 상위 종목 자동 스크래핑
- **다중 지표 전략** — RSI, MACD, 볼린저 밴드, 이동평균, 스토캐스틱, 거래량 6개 지표 동시 확인
- **리스크 관리** — 손절 1.5% / 익절 3% / 트레일링 스탑 1% / 예산 캡 10만원
- **실시간 대시보드** — 수익 현황, 승률, 매매 이력을 웹 UI로 확인
- **모의매매 지원** — `dry_run` 모드로 실제 돈 없이 전략 검증
- **미국장 시간 자동 감지** — EDT/EST 자동 반영

---

## 시스템 구조

```
Trading/
├── main.py                  # 진입점
├── config.json              # 설정 파일
└── src/
    ├── broker/
    │   ├── base.py          # 브로커 인터페이스
    │   └── toss.py          # 토스증권 Playwright 자동화
    ├── strategy/
    │   └── multi_confirm.py # 다중 지표 확인 전략
    ├── screener/
    │   ├── krx.py           # 국내 종목 스크리닝 (pykrx)
    │   └── filter.py        # 가격·거래량·모멘텀 필터
    ├── dashboard/
    │   ├── app.py           # FastAPI 대시보드 서버
    │   └── templates/       # 웹 UI
    ├── risk.py              # 리스크 매니저
    ├── scheduler.py         # 매매 스케줄러
    └── config.py            # 설정 데이터클래스
```

---

## 설치 및 실행

### 1. 요구사항

- Python 3.11+
- 토스증권 계정 + 토스 앱 (로그인 인증용)

### 2. 패키지 설치

```bash
pip install -r requirements.txt
playwright install chromium
```

### 3. 설정

`config.json` 수정:

```json
{
  "toss": {
    "login_url": "https://www.tossinvest.com",
    "headless": false,
    "slow_mo": 100,
    "timeout": 30000
  },
  "trading": {
    "watchlist": [],
    "check_interval": 60,
    "total_budget": 100000,
    "market": "us",
    "dry_run": true
  },
  "dashboard": {
    "host": "127.0.0.1",
    "port": 8080
  }
}
```

| 항목 | 설명 |
|------|------|
| `watchlist` | 감시 종목 코드 목록. 비워두면 거래대금 차트에서 자동 발굴 |
| `total_budget` | 최대 투자 예산 (원) |
| `market` | `"us"` = 미국장, `"ko"` = 한국장 |
| `dry_run` | `true` = 모의매매, `false` = 실제 매매 |

### 4. 실행

```bash
python3.11 main.py
```

브라우저가 열리면 **토스 앱에서 로그인 인증**을 완료합니다.
이후 대시보드는 `http://127.0.0.1:8080` 에서 확인합니다.

---

## 대시보드

| 항목 | 설명 |
|------|------|
| 상태 | 실행 중 / 중지됨 / 모의매매 |
| 총 실현 손익 | 청산 완료 기준 누적 수익 |
| 예산 사용 | 현재 투자금 / 총 예산 |
| 승률 | 수익 거래 / 전체 거래 |
| 매매 이력 | 시각, 종목, 구분, 수량, 손익, 사유 |

대시보드에서 **모의매매 체크박스 해제 → 설정 저장** 으로 서버 재시작 없이 실제 매매로 전환 가능합니다.

---

## 매매 전략

**MultiConfirmStrategy** — 6개 지표 중 3개 이상이 동일 방향을 가리킬 때만 진입

| 지표 | 매수 조건 | 매도 조건 |
|------|-----------|-----------|
| RSI | 25~40 구간 반등 | 60~75 구간 반락 |
| MACD | 히스토그램 양전환 | 히스토그램 음전환 |
| 볼린저 밴드 | 하단 터치 후 반등 | 상단 터치 후 반락 |
| 이동평균 | 5일선 > 20일선 돌파 | 5일선 < 20일선 하락 |
| 스토캐스틱 | %K < 30 골든크로스 | %K > 70 데드크로스 |
| 거래량 | 평균 대비 1.5배 이상 | — |

데이터가 35개 쌓인 이후부터 신호 발생 (60초 주기 기준 약 35분)

---

## 리스크 관리

- 손절: 진입가 대비 **-1.5%**
- 익절: 진입가 대비 **+3.0%**
- 트레일링 스탑: 최고가 대비 **-1.0%**
- 최대 동시 보유: **5종목**
- 일일 최대 손실: **-5%** 초과 시 당일 매매 중단
- 3연속 손실 시 쿨다운

---

## 주의사항

> 토스증권은 공식 API를 제공하지 않으므로 웹 브라우저 자동화(Playwright)를 사용합니다.
> 토스증권 서비스 정책 변경 또는 DOM 구조 변경 시 셀렉터 조정이 필요할 수 있습니다.
> 이 프로젝트는 교육 목적으로 제작되었으며, 실제 투자 손실에 대한 책임은 사용자에게 있습니다.

---

## 기술 스택

- **Python 3.11**
- **Playwright** — 브라우저 자동화
- **FastAPI + Uvicorn** — 대시보드 서버
- **pykrx** — 국내 주식 데이터
- **zoneinfo** — 미국 시간대 처리
