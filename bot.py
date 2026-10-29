"""Multi-account auto-trade bot for the Polyester testnet (BTC-USDT).

Strategies:
  flip - alternate: buy BTC with all USDT, then sell all BTC, repeat.
  dca  - market-buy every interval.
  rsi  - market-buy when RSI crosses up out of oversold, market-sell when it
         crosses down out of overbought.

Accounts live in accounts.json; every key there overrides the .env default for
that account only. Each account's VIP tier and taker fee are read from the API.
Dry run is the default. Set DRY_RUN=0 (globally or per account) to trade.
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import signal
import time
import uuid
from dataclasses import dataclass, fields
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

from polyester import (
    AsyncPolyester,
    POLYESTER_TESTNET_ENVIRONMENT,
    Quantity,
    format_ledger_u128,
)
from polyester.errors import PolyesterRateLimitError, PolyesterServerError

HERE = Path(__file__).parent
# The SDK preset points at api-testnet.polyester.com, which has no DNS record.
DEFAULT_API_URL = "https://api.testnet.polyester.com"
DEFAULT_WS_URL = "wss://api.testnet.polyester.com"
PRICE_TICKS = Decimal(1_000_000_000)
SYMBOL = "BTC-USDT"
FEE_REFRESH_SEC = 600
ORDER_TIMEOUT_SEC = 20
SETTLE_MAX_WAITS = 10
EMPTY_READS_TO_STOP = 5
RETRYABLE_HINTS = ("unavailable", "overloaded", "timeout", "timed out", "upstream", "internal")


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.split(" #", 1)[0]
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


load_dotenv(HERE / ".env")


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def as_bool(value: object) -> bool:
    return str(value).strip().lower() not in ("0", "false", "no", "")


@dataclass
class Config:
    name: str = "main"
    api_key_id: str = ""
    api_private_key: str = ""
    account_id: str = ""
    sub_account_id: str = ""
    strategy: str = "flip"
    dry_run: bool = True
    interval_sec: float = 6.0
    quote_per_trade: str = "max"  # "max" = whole available USDT balance
    max_trades: int = 0  # 0 = unlimited
    max_slippage_bps: int = 100
    # Stop this account once its total value (in USDT) falls below this. 0 = off.
    stop_below_usdt: Decimal = Decimal(0)
    timeframe: str = "1m"
    rsi_period: int = 14
    rsi_oversold: Decimal = Decimal(30)
    rsi_overbought: Decimal = Decimal(70)
    sell_fraction: Decimal = Decimal(1)

    @property
    def has_key(self) -> bool:
        return bool(self.api_key_id and self.api_private_key and self.account_id)


def coerce(cfg: Config, key: str, value: object) -> None:
    default = getattr(Config, key, None)
    if isinstance(default, bool):
        value = as_bool(value)
    elif isinstance(default, int):
        value = int(value)
    elif isinstance(default, float):
        value = float(value)
    elif isinstance(default, Decimal):
        value = Decimal(str(value))
    else:
        value = str(value).strip()
    setattr(cfg, key, value)


def defaults_from_env() -> dict[str, str]:
    out = {}
    for f in fields(Config):
        if f.name == "name":
            continue
        name = f.name.upper()
        if f.name in ("api_key_id", "api_private_key", "account_id", "sub_account_id"):
            name = "POLYESTER_" + name
        if env(name):
            out[f.name] = env(name)
    return out


def load_configs() -> list[Config]:
    defaults = defaults_from_env()
    default_path = HERE / "accounts.csv"
    if not default_path.exists():
        default_path = HERE / "accounts.json"
    path = Path(env("ACCOUNTS_FILE", str(default_path)))
    entries = [{}]
    if path.exists() and path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as f:
            header = f.readline()
            f.seek(0)
            # Excel in some locales saves with ";" instead of ",".
            delimiter = max(",;\t", key=header.count)
            # Blank cells mean "use the .env default".
            entries = [{k.strip(): v.strip() for k, v in row.items() if k and v and v.strip()}
                       for row in csv.DictReader(f, delimiter=delimiter)]
        entries = [e for e in entries if e]
    elif path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data["accounts"] if isinstance(data, dict) else data
    configs = []
    for i, entry in enumerate(entries, 1):
        if not as_bool(entry.get("enabled", True)):
            continue
        cfg = Config(name=str(entry.get("name") or f"akun{i}"))
        for key, value in {**defaults, **entry}.items():
            if key in ("name", "enabled"):
                continue
            if not hasattr(cfg, key):
                raise SystemExit(f"[{cfg.name}] unknown setting {key!r} in {path.name}")
            coerce(cfg, key, value)
        configs.append(cfg)
    return configs


def align_down(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def fmt(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def rsi(closes: list[Decimal], period: int) -> list[Decimal]:
    """Wilder RSI; returns one value per close starting at index `period`."""
    if len(closes) <= period:
        return []
    gains, losses = [], []
    for prev, cur in zip(closes, closes[1:]):
        delta = cur - prev
        gains.append(max(delta, Decimal(0)))
        losses.append(max(-delta, Decimal(0)))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out = []

    def value() -> Decimal:
        if avg_loss == 0:
            return Decimal(100)
        return 100 - 100 / (1 + avg_gain / avg_loss)

    out.append(value())
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        out.append(value())
    return out


class Bot:
    def __init__(self, cfg: Config, client: AsyncPolyester, pair: dict) -> None:
        self.cfg = cfg
        self.client = client
        self.step = Decimal(str(pair.get("stepSize") or "0.000001"))
        self.min_notional = Decimal(str(pair.get("minNotionalQuote") or "5"))
        self.symbol_id = int(pair["symbolId"])
        self.base = pair["baseAsset"]
        self.quote = pair["quoteAsset"]
        self.trades = 0
        self.fees_paid = Decimal(0)
        self.volume = Decimal(0)
        self.taker_pct: Decimal | None = None
        self.vip_tier: int | None = None
        self.fees_checked = float("-inf")
        self.holding: str | None = None
        self.last_total: Decimal | None = None
        self.settle_waits = 0
        self.empty_reads = 0

    def log(self, msg: str) -> None:
        print(f"[{datetime.now():%H:%M:%S}] [{self.cfg.name}] {msg}", flush=True)

    @property
    def simulated(self) -> bool:
        """Dry run without credentials: no balance or fee reads are possible."""
        return self.cfg.dry_run and not self.cfg.has_key

    async def refresh_fees(self) -> None:
        if self.simulated or time.monotonic() - self.fees_checked < FEE_REFRESH_SEC:
            return
        self.fees_checked = time.monotonic()
        try:
            rates = await self.client.fees.get_spot_fee_rates(symbol_id=self.symbol_id)
            row = next((r for r in rates.fee_rates if r.symbol_id == self.symbol_id), None)
            if row is None:
                return
            changed = self.vip_tier is not None and row.vip_tier != self.vip_tier
            self.taker_pct = Decimal(row.taker_fee_rate_percent)
            self.vip_tier = row.vip_tier
            msg = (f"VIP{row.vip_tier}: taker {fmt(self.taker_pct)}%, "
                   f"maker {fmt(Decimal(row.maker_fee_rate_percent))}%, "
                   f"round-trip cost ~{fmt(self.taker_pct * 2)}% + slippage")
            self.log(("VIP changed -> " if changed else "") + msg)
        except Exception as exc:
            self.log(f"Could not read fee rates: {type(exc).__name__}: {exc}")

    async def last_price(self) -> Decimal:
        overview = await self.client.market_overview.list(limit=200)
        for market in overview.markets:
            if market.symbol == SYMBOL and market.last_price is not None:
                return Decimal(market.last_price.ticks) / PRICE_TICKS
        raise RuntimeError(f"No last price for {SYMBOL}")

    async def balances(self) -> tuple[Decimal, Decimal]:
        """Available (quote, base) trading balances."""
        if self.simulated:
            return Decimal(0), Decimal(0)
        ids = {self.client.catalogs.ledger_id_for_asset(a): a for a in (self.quote, self.base)}
        found = {self.quote: Decimal(0), self.base: Decimal(0)}
        for row in (await self.client.balances.list()).balances:
            asset = ids.get(int(row.asset_id))
            if asset and row.available is not None:
                found[asset] = Decimal(format_ledger_u128(row.available))
        return found[self.quote], found[self.base]

    def order_id(self, side: str) -> str:
        return f"bot-{side}-{uuid.uuid4().hex[:12]}"

    async def place(self, side: str, **order) -> str:
        """Send one market order and describe the server's answer.

        The testnet often answers "Service temporarily unavailable" even though the
        order filled, and balances update a few seconds later. So an ambiguous
        answer is not retried here: the next step re-reads balances, which shows
        whether it filled, and sends the order again only if it did not.
        """
        try:
            result = await asyncio.wait_for(
                self.client.orders.create(
                    symbol=SYMBOL, side=side, order_type="market",
                    max_slippage_bps=self.cfg.max_slippage_bps,
                    client_order_id=self.order_id(side), **order,
                ),
                ORDER_TIMEOUT_SEC,
            )
            return f"accepted id={result.order_id}"
        except Exception as exc:
            text = f"{getattr(exc, 'code', '') or ''} {exc}"
            ambiguous = isinstance(
                exc, (PolyesterServerError, PolyesterRateLimitError, asyncio.TimeoutError)
            ) or any(h in text.lower() for h in RETRYABLE_HINTS)
            if not ambiguous:
                raise
            return f"sent, server said {exc!s:.60}; balance will confirm"

    def record(self, notional: Decimal) -> str:
        self.trades += 1
        self.volume += notional
        if self.taker_pct is None:
            return ""
        fee = notional * self.taker_pct / 100
        self.fees_paid += fee
        return f", est. fee ~{fmt(fee.quantize(Decimal('0.0001')))} {self.quote}"

    def observe(self, usdt: Decimal, btc: Decimal, price: Decimal) -> None:
        """Count a live trade whenever most of the value has moved to the other asset."""
        holding = "usdt" if usdt >= btc * price else "btc"
        if self.holding is not None and holding != self.holding:
            notional = usdt + btc * price
            fill = self.record(notional)
            self.log(f"Filled: now holding {holding.upper()} (trade #{self.trades}{fill})")
        self.holding = holding

    async def buy(self, price: Decimal, usdt: Decimal) -> None:
        cfg = self.cfg
        if self.simulated:
            self.log(f"[DRY RUN] BUY {SYMBOL} with "
                     f"{'all available' if cfg.quote_per_trade == 'max' else cfg.quote_per_trade} "
                     f"{self.quote}")
            self.trades += 1
            return
        if cfg.quote_per_trade == "max":
            amount = align_down(usdt, Decimal("0.01"))
        else:
            amount = Decimal(cfg.quote_per_trade)
        if amount < self.min_notional:
            self.log(f"Buy size {fmt(amount)} {self.quote} < min notional {self.min_notional}; skip")
            return
        if usdt < amount:
            self.log(f"Not enough {self.quote}: have {fmt(usdt)}, need {fmt(amount)}")
            return
        if cfg.dry_run:
            self.log(f"[DRY RUN] BUY {SYMBOL} for {fmt(amount)} {self.quote} "
                     f"(~{fmt(align_down(amount / price, self.step))} {self.base}){self.record(amount)}")
            return
        scale = self.client.catalogs.quote_quantity_scale_for_symbol(SYMBOL)
        outcome = await self.place(
            "buy",
            max_quote_debit=Quantity.from_quote_decimal_str(fmt(amount), scale, symbol=SYMBOL),
        )
        self.log(f"BUY {fmt(amount)} {self.quote} -> {outcome}")

    async def sell(self, price: Decimal, btc: Decimal) -> None:
        cfg = self.cfg
        if self.simulated:
            self.log(f"[DRY RUN] SELL all available {self.base}")
            self.trades += 1
            return
        qty = align_down(btc * cfg.sell_fraction, self.step)
        if qty <= 0 or qty * price < self.min_notional:
            self.log(f"Nothing to sell (balance {fmt(btc)} {self.base}, "
                     f"min notional {self.min_notional} {self.quote})")
            return
        if cfg.dry_run:
            self.log(f"[DRY RUN] SELL {fmt(qty)} {self.base} "
                     f"(~{fmt(qty * price)} {self.quote}){self.record(qty * price)}")
            return
        outcome = await self.place("sell", qty=fmt(qty))
        self.log(f"SELL {fmt(qty)} {self.base} -> {outcome}")

    async def rsi_signal(self) -> tuple[str, Decimal | None]:
        cfg = self.cfg
        candles = await self.client.market_data.get_candles(
            symbol=SYMBOL, timeframe=cfg.timeframe, limit=max(100, cfg.rsi_period * 3)
        )
        ordered = sorted(candles.candles, key=lambda c: c.ts_sec)
        closes = [Decimal(c.close) for c in ordered if c.close]
        values = rsi(closes, cfg.rsi_period)
        if len(values) < 2:
            return "hold", None
        prev, cur = values[-2], values[-1]
        if prev <= cfg.rsi_oversold < cur:
            return "buy", cur
        if prev >= cfg.rsi_overbought > cur:
            return "sell", cur
        return "hold", cur

    async def tick(self) -> bool:
        """Run one step. Returns False when the account should stop."""
        cfg = self.cfg
        await self.refresh_fees()
        price = await self.last_price()
        usdt, btc = await self.balances()
        total = usdt + btc * price
        if not self.simulated:
            self.log(f"price={fmt(price)} balance: {fmt(usdt)} {self.quote}, "
                     f"{fmt(btc)} {self.base} (~{fmt(total.quantize(Decimal('0.01')))} {self.quote})")
            # Balance reads lag fills: one side can be debited before the other is
            # credited. Fees only shave a fraction per trade, so a sudden halving of
            # the total means the read is mid-update; wait instead of acting on it.
            if self.last_total and total < self.last_total / 2 and self.settle_waits < SETTLE_MAX_WAITS:
                self.settle_waits += 1
                self.log("Balance still updating; waiting")
                return True
            self.last_total, self.settle_waits = total, 0
            if not cfg.dry_run:
                self.observe(usdt, btc, price)
            if cfg.max_trades and self.trades >= cfg.max_trades:
                return False
            if cfg.stop_below_usdt and total < cfg.stop_below_usdt:
                self.log(f"Total value below stop_below_usdt={cfg.stop_below_usdt}; stopping")
                return False

        if cfg.strategy == "flip":
            # Side follows whichever balance is tradable, so a restart resumes correctly.
            if self.simulated:
                side = "buy" if self.trades % 2 == 0 else "sell"
            elif max(usdt, btc * price) < self.min_notional:
                # Require a few reads in a row so a lagging read can't stop the account.
                self.empty_reads += 1
                if self.empty_reads < EMPTY_READS_TO_STOP:
                    self.log("Balance looks empty; re-checking")
                    return True
                self.log(f"Balance used up (both below min order {self.min_notional} "
                         f"{self.quote}); stopping")
                return False
            else:
                self.empty_reads = 0
                # Trade whichever side holds most of the value, so leftovers from a
                # partial fill don't cause two buys (or two sells) in a row.
                side = "buy" if usdt >= btc * price else "sell"
            if side == "buy":
                await self.buy(price, usdt)
            else:
                await self.sell(price, btc)
        elif cfg.strategy == "dca":
            await self.buy(price, usdt)
        elif cfg.strategy == "rsi":
            action, value = await self.rsi_signal()
            shown = f"{value:.2f}" if value is not None else "n/a"
            self.log(f"RSI({cfg.rsi_period},{cfg.timeframe})={shown} -> {action}")
            if action == "buy":
                await self.buy(price, usdt)
            elif action == "sell":
                await self.sell(price, btc)
        else:
            self.log(f"Unknown strategy {cfg.strategy!r} (use flip, dca or rsi)")
            return False
        return True

    def summary(self) -> str:
        text = f"{self.trades} trades, volume ~{fmt(self.volume.quantize(Decimal('0.01')))} {self.quote}"
        if self.taker_pct is not None:
            text += f", est. fees ~{fmt(self.fees_paid.quantize(Decimal('0.01')))} {self.quote}"
        return text


async def run_account(cfg: Config, stop: asyncio.Event, delay: float) -> None:
    def log(msg: str) -> None:
        print(f"[{datetime.now():%H:%M:%S}] [{cfg.name}] {msg}", flush=True)

    if not cfg.dry_run and not cfg.has_key:
        log("dry_run=0 needs api_key_id, api_private_key and account_id; skipping account")
        return
    try:
        await asyncio.wait_for(stop.wait(), timeout=delay)
        return
    except asyncio.TimeoutError:
        pass

    environment = POLYESTER_TESTNET_ENVIRONMENT.with_urls(
        api_url=env("POLYESTER_API_URL", DEFAULT_API_URL),
        websocket_url=env("POLYESTER_WS_URL", DEFAULT_WS_URL),
    )
    kwargs: dict = {"environment": environment}
    if cfg.has_key:
        kwargs.update(api_key_id=cfg.api_key_id, api_private_key=cfg.api_private_key,
                      default_account_id=cfg.account_id, default_sub_account_id=cfg.sub_account_id)

    try:
        async with AsyncPolyester(**kwargs) as client:
            await client.wait_for_catalogs()
            spot = await client.market_data.get_spot_config()
            pair = next(p for p in spot.raw.get("pairs", []) if p.get("symbol") == SYMBOL)
            bot = Bot(cfg, client, pair)
            bot.log(f"Start: strategy={cfg.strategy} quote/trade={cfg.quote_per_trade} "
                    f"interval={cfg.interval_sec}s slippage={cfg.max_slippage_bps}bps "
                    f"dry_run={cfg.dry_run}")
            while not stop.is_set():
                try:
                    if not await bot.tick():
                        break
                except Exception as exc:  # keep running through transient API errors
                    if "insufficient funds" in str(exc).lower():
                        bot.log("Insufficient funds: balance not updated yet; retrying next step")
                    else:
                        bot.log(f"Error: {type(exc).__name__}: {exc}")
                if cfg.max_trades and bot.trades >= cfg.max_trades:
                    bot.log(f"Reached max_trades={cfg.max_trades}")
                    break
                # Full pause after each step so balances settle before the next order.
                wait = cfg.interval_sec
                try:
                    await asyncio.wait_for(stop.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass
            bot.log(f"Stopped: {bot.summary()}")
    except Exception as exc:
        log(f"Fatal: {type(exc).__name__}: {exc}")


async def main() -> None:
    configs = load_configs()
    if not configs:
        raise SystemExit("No enabled accounts")
    names = [c.name for c in configs]
    if len(set(names)) != len(names):
        raise SystemExit("Account names must be unique")
    print(f"Running {len(configs)} account(s): {', '.join(names)}", flush=True)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows: Ctrl+C raises KeyboardInterrupt
            pass

    # Stagger starts so accounts don't all hit the API at the same instant.
    stagger = float(env("START_STAGGER_SEC", "0.5"))
    await asyncio.gather(*(run_account(c, stop, i * stagger) for i, c in enumerate(configs)))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped", flush=True)
