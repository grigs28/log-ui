#!/bin/bash
# 自动升级版本号 + 更新 CHANGELOG 日期
# 用法: ./scripts/version.sh [major|minor|patch]   默认 patch
set -e
cd "$(dirname "$0")/.."
PART="${1:-patch}"
FILE=CHANGELOG.md

CUR=$(grep -m1 '^## \[' "$FILE" | sed 's/## \[\(.*\)\].*/\1/')
IFS='.' read -r MAJ MIN PAT <<< "$CUR"

# 进位规则: 每段 0-9, 到 10 进位 (0.5.9 -> 0.6.0, 0.9.x -> 1.0.x)
case "$PART" in
  major) MAJ=$((MAJ+1)); MIN=0;    PAT=0;;
  minor) MIN=$((MIN+1)); PAT=0;;
  patch)
    if [ "$PAT" -ge 9 ]; then MIN=$((MIN+1)); PAT=0   # 小版本到 10 进位
    else PAT=$((PAT+1)); fi;;
  *) echo "用法: $0 [major|minor|patch]"; exit 1;;
esac

# minor 到 10 进 major
if [ "$MIN" -ge 10 ]; then MAJ=$((MAJ+1)); MIN=0; fi
NEW="$MAJ.$MIN.$PAT"
TODAY=$(date +%Y-%m-%d)

# 在文件头部（## [Unreleased] 之后或最上面的 ## 之前）插入新版本段
if head -10 "$FILE" | grep -q '^## \[Unreleased\]'; then
  # 把 Unreleased 段升级为新版本号+今天日期
  sed -i "0,/^## \[Unreleased\]/s//## [$NEW] - $TODAY/" "$FILE"
else
  # 在第一个 ## 前插入新段
  # 不预置空行：BEGIN{print ""} 会导致每次升版本在文件顶部累积一个空行
  awk -v ver="## [$NEW] - $TODAY" '/^## / && !done {print ver; done=1} {print}' "$FILE" > "$FILE.tmp" && mv "$FILE.tmp" "$FILE"
fi

echo "版本: $CUR -> $NEW ($TODAY)"
echo "下一步: 编辑 CHANGELOG.md 的 [$NEW] 段填入改动, 然后 git add+commit+push"
