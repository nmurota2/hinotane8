"""``python -m hinotane`` で起動できるようにする。

pip のインストール登録（editable install）が壊れていても、
ソースの場所さえ分かれば動かせる経路を残しておくため。
リポジトリ直下の ``hinotane.sh`` がこの経路を使う。
"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
