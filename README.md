# GPT Autotrader — Alpaca Paper Trading Bot

미국주식/ETF용 자동매매 봇 템플릿입니다. 기본값은 **종이매매(paper trading)** + **주문 실행 꺼짐(place_orders: false)** 입니다.

이 봇은 돈을 보장하지 않습니다. 실전 투입 전 반드시 종이매매와 소액 테스트를 하세요.

## 기능

- Alpaca Paper Trading / Live Trading API 연결
- SMA 추세 + RSI 필터 기반 자동 매수/매도 신호
- 주문 실행 전 스캔 모드 지원
- 포지션당 최대 비중 제한
- 전체 노출 제한
- 일일 손실 제한
- 손절/익절
- 라이브 계좌 오발주 방지 확인값
- 로그와 상태 파일 저장

## 설치

```bash
cd gpt_autotrader
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
cp config.example.yaml config.yaml
```

`.env`에 Alpaca Paper API 키를 넣으세요.

```bash
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
```

## 먼저 스캔만 하기

주문을 절대 넣지 않고 신호만 봅니다.

```bash
python -m gpt_autotrader scan --config config.yaml
```

출력 옵션:

```bash
python -m gpt_autotrader scan --config config.yaml --signals active --top 20
python -m gpt_autotrader scan --config config.yaml --signals buy --top 0
python -m gpt_autotrader scan --config config.yaml --held --signals all
python -m gpt_autotrader scan --config config.yaml --symbols MRVL SOXL --signals active
python -m gpt_autotrader scan --config config.yaml --plain
python -m gpt_autotrader scan --config config.yaml --verbose
```

- `--signals active`: BUY/SELL만 표시
- `--signals buy`: BUY만 표시
- `--held`: 현재 보유 종목만 빠르게 스캔
- `--symbols MRVL SOXL`: 지정한 종목만 빠르게 스캔
- `--top 20`: 상위 20개만 표시
- `--top 0`: 조건에 맞는 모든 행 표시
- `--plain`: 컬러 터미널 출력 끄기
- `--verbose`: 상세 실행 로그 표시

일봉 데이터는 종목별로 `.cache/daily_bars/symbols`에 저장됩니다. 첫 실행은 Alpaca에서 과거 데이터를 받느라 느릴 수 있지만, 다음 실행부터는 저장된 데이터 이후의 날짜만 추가로 받아옵니다. 저장 데이터는 `lookback_days` 기준으로 오래된 구간을 자동으로 잘라냅니다.

## 종이매매 주문 켜기

`config.yaml`에서 다음처럼 바꿉니다.

```yaml
paper: true
place_orders: true
```

그다음 1회 실행:

```bash
python -m gpt_autotrader trade --config config.yaml --once
```

장중 반복 실행:

```bash
python -m gpt_autotrader trade --config config.yaml --interval-seconds 900
```

## 실계좌 사용

권장하지 않습니다. 그래도 하려면 세 가지를 모두 해야 합니다.

1. `config.yaml`에서 `paper: false`
2. `.env`에서 실계좌 API 키 사용
3. `.env`에 `CONFIRM_LIVE_TRADING_I_ACCEPT_RISK=yes`

실계좌에서는 오발주, API 장애, 슬리피지, 체결 지연, 급락, 세금/수수료/환율 리스크가 모두 사용자 책임입니다.

## 전략 개요

매수 후보:

- 종가 > 20일 SMA > 50일 SMA
- RSI가 너무 과열되지 않음
- 이미 보유 중이 아님
- 전체 노출/포지션 제한 안에 있음

매도 후보:

- 종가가 20일 SMA 아래로 이탈
- RSI 약세
- 손절선 도달
- 익절선 도달

## 파일 구조

```text
gpt_autotrader/
  __main__.py          # CLI 진입점
  bot.py               # 자동매매 루프
  broker_alpaca.py     # Alpaca API 래퍼
  config.py            # 설정 로딩
  indicators.py        # SMA/RSI 계산
  risk.py              # 리스크 관리
  state.py             # 상태 저장
  strategy.py          # 매수/매도 신호
```
# ssalmuk
