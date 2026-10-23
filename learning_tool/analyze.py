from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd


DATA_FILE = Path("data/prices.csv")


def calculate_max_drawdown(price_series: pd.Series) -> float:
    """计算价格序列的最大回撤。"""
    cumulative_max = price_series.cummax()
    drawdown = price_series / cumulative_max - 1
    return drawdown.min()


def main() -> None:
    prices = pd.read_csv(DATA_FILE, parse_dates=["date"])
    prices = prices.sort_values("date").set_index("date")

    daily_returns = prices["close"].pct_change().dropna()
    annualized_volatility = daily_returns.std() * (252**0.5)
    max_drawdown = calculate_max_drawdown(prices["close"])

    print(f"数据起始日期：{prices.index.min().date()}")
    print(f"数据结束日期：{prices.index.max().date()}")
    print(f"累计收益率：{prices['close'].iloc[-1] / prices['close'].iloc[0] - 1:.2%}")
    print(f"年化波动率：{annualized_volatility:.2%}")
    print(f"最大回撤：{max_drawdown:.2%}")

    prices["close"].plot(title="Price Curve")
    plt.xlabel("Date")
    plt.ylabel("Price")
    plt.tight_layout()
    plt.show()


if __name__ == "__main__":
    main()