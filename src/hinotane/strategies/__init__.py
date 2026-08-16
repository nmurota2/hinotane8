"""戦略パッケージ。

新しい戦略を足すときは、このディレクトリに 1 ファイル追加して
``@register`` を付けたクラスを定義し、下の import に 1 行足すだけでよい。
設定 ``SCREENER_STRATEGIES`` に名前を書けば即座にスクリーニングとバックテストの
両方で使えるようになる。

``research.py`` に入っているものは **検証用の対照群** で、実運用には使わない。
名前が ``研究:`` で始まるので、通知やスクリーニングに紛れ込んでも人間が気づける。
"""

from .base import Strategy, StrategySignal, available, get_strategy, register
from .breakout import BreakoutStrategy
from .pullback import PullbackStrategy
from .research import (
    TrendFixedStopStrategy,
    TrendLooseEntryStrategy,
    TrendNoStopStrategy,
    TrendRandomPickStrategy,
    TrendRegimeFilterStrategy,
)
from .reversal import ReversalStrategy
from .trend import TrendStrategy

__all__ = [
    "BreakoutStrategy",
    "PullbackStrategy",
    "ReversalStrategy",
    "Strategy",
    "StrategySignal",
    "TrendFixedStopStrategy",
    "TrendLooseEntryStrategy",
    "TrendNoStopStrategy",
    "TrendRandomPickStrategy",
    "TrendRegimeFilterStrategy",
    "TrendStrategy",
    "available",
    "get_strategy",
    "register",
]
