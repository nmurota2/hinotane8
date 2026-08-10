"""戦略パッケージ。

新しい戦略を足すときは、このディレクトリに 1 ファイル追加して
``@register`` を付けたクラスを定義し、下の import に 1 行足すだけでよい。
設定 ``SCREENER_STRATEGIES`` に名前を書けば即座にスクリーニングとバックテストの
両方で使えるようになる。
"""

from .base import Strategy, StrategySignal, available, get_strategy, register
from .breakout import BreakoutStrategy
from .pullback import PullbackStrategy
from .reversal import ReversalStrategy

__all__ = [
    "BreakoutStrategy",
    "PullbackStrategy",
    "ReversalStrategy",
    "Strategy",
    "StrategySignal",
    "available",
    "get_strategy",
    "register",
]
