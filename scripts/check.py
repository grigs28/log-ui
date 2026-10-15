#!/usr/bin/env python3
"""提交前自检：静态检查 + 路由冒烟。

为什么有这个脚本
----------------
2026-10-15 出过一次线上 500：批量改路由签名时，`disks_view` 的参数替换没匹配上，
但函数体里 `_disks_back(back)` 的替换成功了 —— 于是引用了不存在的 `back`。
`py_compile` 只查语法，查不出未定义名，所以一路部署到了生产。
本脚本把这类错误挡在提交前。

用法
----
    ./scripts/check.py            # 静态检查 + 路由冒烟（默认）
    ./scripts/check.py --static   # 只做静态检查（不需要 fastapi）

**绝不碰真实数据**：冒烟前把 disks.PATH 重定向到临时文件，
disks.yaml 的读写全部发生在临时目录里。
"""
import argparse
import ast
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PY_FILES = sorted(str(p.relative_to(BASE)) for p in (BASE / "app").glob("*.py"))


def static_checks() -> int:
    """1) 语法  2) pyflakes（未定义名 / 未用导入）  3) 路由参数与函数体一致性。"""
    bad = 0

    for f in PY_FILES:
        r = subprocess.run([sys.executable, "-m", "py_compile", str(BASE / f)],
                           capture_output=True, text=True)
        if r.returncode:
            print(f"  ❌ 语法 {f}\n{r.stderr}")
            bad += 1

    try:
        import pyflakes  # noqa: F401
        r = subprocess.run([sys.executable, "-m", "pyflakes", *PY_FILES],
                           cwd=BASE, capture_output=True, text=True)
        out = (r.stdout + r.stderr).strip()
        if out:
            print("  ❌ pyflakes:\n" + "\n".join("     " + l for l in out.splitlines()))
            bad += 1
        else:
            print("  ✅ pyflakes（无未定义名 / 未用导入）")
    except ImportError:
        print("  ⚠️  未安装 pyflakes，跳过（pip install pyflakes 可启用）")

    # 路由函数体里用到的表单字段，必须在签名里声明 —— 正是那次 500 的成因。
    # 用 AST 取函数边界（早先用正则切 @app. 块，会把夹在两个路由中间的普通函数
    # 算进上一个函数体，误报；pyflakes 能查未定义名，这里额外把「表单字段」
    # 这一类点名，报错信息更贴近病因）。
    src = (BASE / "app" / "main.py").read_text(encoding="utf-8")
    form_fields = {"back", "server", "per_row", "rows", "orient", "count"}
    flagged = 0
    for fn in [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef)]:
        params = {a.arg for a in list(fn.args.args) + list(fn.args.kwonlyargs)}
        # 函数内自己绑定的名字（局部变量、循环变量、推导式变量、嵌套 def/class、
        # except as）都算已定义，否则 servers_page() 里的局部 rows 会被误判成表单字段
        bound = {n.id for n in ast.walk(fn)
                 if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del))}
        bound |= {n.name for n in ast.walk(fn)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        bound |= {n.name for n in ast.walk(fn)
                  if isinstance(n, ast.ExceptHandler) and n.name}
        used = {n.id for n in ast.walk(fn)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        for var in sorted(used & form_fields - params - bound):
            print(f"  ❌ {fn.name}() 函数体用了 '{var}'，签名里没声明")
            flagged += 1
    if flagged:
        bad += 1
    else:
        print("  ✅ 路由表单字段与函数签名一致")
    return bad


# 模板里必须存在的功能标记。用脚本做字符串替换时，切片范围算错会把整段
# 功能悄悄删掉（语法仍然合法、自检也过）—— 已经栽过两次：
#   · Python 里删掉了 fetch_hosts/_epoch（靠 pyflakes 抓到）
#   · 模板里删掉了拖动排序 + 回跳锚点的两段 JS（pyflakes 管不到，直到用户发现）
# 这里按功能点名，删了就报错。
FEATURE_MARKERS = {
    "templates/disks.html": [
        ("拖动排序", "dragstart"),
        ("拖动保存接口", "/disks/order"),
        ("保存顺序提示条", "ordersave"),
        ("操作后回跳卡片", "srv-"),
        ("每排/排数联动", "slot-count"),
        ("单盘动作按钮", "bay-act"),
        ("动作输出弹窗", "actmodal"),
        ("进度条", "actbar-fill"),
        ("物理盘位属性", "data-dev"),
    ],
    "templates/base.html": [("脚本块", "{% block scripts %}")],
}


def template_markers() -> int:
    bad = 0
    for f, markers in FEATURE_MARKERS.items():
        txt = (BASE / f).read_text(encoding="utf-8")
        missing = [name for name, tok in markers if tok not in txt]
        if missing:
            print(f"  ❌ {f} 缺少功能标记: {', '.join(missing)}")
            bad += 1
        else:
            print(f"  ✅ {f} 功能标记齐全（{len(markers)} 项）")
    return bad


def smoke_routes() -> int:
    """打一遍 /disks 全部路由，确认没有 5xx。数据全在临时目录里。"""
    sys.path.insert(0, str(BASE))
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        print("  ⚠️  无 fastapi/httpx，跳过路由冒烟")
        return 0

    with tempfile.TemporaryDirectory() as td:
        from app import disks
        disks.PATH = Path(td) / "disks.yaml"          # ← 关键：绝不写真实档案
        fake = {"by_host": {"t-nod01": {
            "ip": "10.0.0.1",
            "disks": {f"/dev/sd{c}": {"osd": str(100 + i), "attr": "", "status": "✓ 健康"}
                      for i, c in enumerate("bcdef")},
            "poll_errors": []}}, "runs": []}
        disks.poll_logs = lambda: fake
        disks.fetch_hosts = lambda: [{"name": "t-nod02", "ip": "10.0.0.2"}]
        disks.upsert_server("t-nod01", "10.0.0.1", 8)
        disks.archive_unfiled("t-nod01")

        import app.main as M
        M._require_user = lambda req: {"username": "check", "is_admin": True}
        c = TestClient(M.app, follow_redirects=False)
        s = disks.merged()["servers"][0]

        cases = [
            ("GET",  "/disks", None),
            ("GET",  "/disks?focus=t-nod01", None),
            ("POST", "/disks/view", {"server": s["name"], "mode": s["mode"],
                                     "orient": s["orient"], "per_row": str(s["h_per_row"]),
                                     "rows": str(s["h_rows"] or 1), "ip": s["ip"],
                                     "back": s["name"]}),
            ("POST", "/disks/archive", {"server": s["name"], "back": s["name"]}),
            ("POST", "/disks/order", {"server": s["name"], "back": s["name"],
                                      "dev": ["/dev/sdb"], "osd": ["1"],
                                      "note": [""], "size": ["1.8T"]}),
            ("POST", "/disks/save", {"server": s["name"], "back": s["name"],
                                     "slot": ["0"], "dev": ["/dev/sdb"], "osd": ["1"],
                                     "note": [""], "size": ["1.8T"]}),
            ("POST", "/disks/server/add", {"name": "t-nod03", "ip": "10.0.0.3",
                                           "per_row": "8"}),
            ("GET",  "/", None),
            ("GET",  "/servers", None),
            ("GET",  "/settings", None),
        ]
        bad = 0
        for method, url, data in cases:
            try:
                r = c.request(method, url, data=data)
                code = r.status_code
            except Exception as e:                       # 未捕获异常 = 线上 500
                print(f"  ❌ {method} {url} → 异常 {type(e).__name__}: {e}")
                bad += 1
                continue
            ok = code < 500
            print(f"  {'✅' if ok else '❌'} {method} {url} → {code}")
            if not ok:
                bad += 1
        return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--static", action="store_true", help="只做静态检查")
    a = ap.parse_args()

    print("静态检查")
    bad = static_checks()
    print("\n模板功能标记")
    bad += template_markers()
    if not a.static:
        print("\n路由冒烟（数据写入临时目录，不碰 config/disks.yaml）")
        bad += smoke_routes()
    print("\n" + ("✅ 全部通过" if not bad else f"❌ {bad} 项未通过"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
