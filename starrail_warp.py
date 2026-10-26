#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""崩坏：星穹铁道 —— 跃迁（抽卡）记录获取工具。

纯标准库实现，无第三方依赖。

用法:
    python starrail_warp.py                 # 自动找链接 -> 全量拉取 -> 落盘 -> 打印统计
    python starrail_warp.py --url "<链接>"   # 手动指定跃迁记录链接
    python starrail_warp.py --stats-only     # 只读本地已有数据，不联网
    python starrail_warp.py --out ./data     # 指定数据目录
    python starrail_warp.py --lang en-us     # 指定返回语言(默认 zh-cn)

获取跃迁链接:
    1) 脚本会自动从游戏日志 / webCaches 里提取（需先在游戏内打开一次「跃迁 -> 查看详情」）；
       Windows 原生和 WSL 都支持（WSL 下自动跨 /mnt/c 读 Windows 侧日志）；
    2) 自动失败时会提示手动粘贴链接；
    3) 链接里的 authkey 大约 24 小时过期，过期后重进游戏打开跃迁记录再取一次即可。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------- 常量

# 实测（2026-10）：
#   国服   https://public-operation-hkrpg.mihoyo.com/common/gacha_record/api/getGachaLog
#   海外服 https://api-os-takumi.mihoyo.com/common/gacha_record/api/getGachaLog
#   ⚠️ 常见的 api-takumi.mihoyo.com 对国服返回 404，不要用。
API_HOST_CN = "https://public-operation-hkrpg.mihoyo.com"
API_HOST_OS = "https://api-os-takumi.mihoyo.com"
API_PATH = "/common/gacha_record/api/getGachaLog"
API_PATH_COLLAB = "/common/gacha_record/api/getLdGachaLog"  # 联动池走这个接口

# (gacha_type, 中文名, 是否联动池)
GACHA_TYPES: Sequence[Tuple[str, str, bool]] = (
    ("11", "角色活动跃迁", False),
    ("12", "光锥活动跃迁", False),
    ("1", "群星跃迁（常驻）", False),
    ("2", "始发跃迁（新手）", False),
    ("21", "角色联动跃迁", True),
    ("22", "光锥联动跃迁", True),
)

DEFAULT_LANG = "zh-cn"
PAGE_SIZE = 20          # 官方接口单次上限
REQUEST_GAP = 0.35      # 每次请求间隔，避免被限流
TIMEOUT = 15
MAX_RETRY = 3

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

RETCODE_HINT = {
    0: "成功",
    -1: "内部错误",
    -100: "参数错误 / 链接无效",
    -101: "authkey 过期或无效（重进游戏打开跃迁记录重新取链接）",
    -102: "访问过于频繁，稍后重试",
    -110: "该 UID 无此卡池数据",
    -502: "服务端异常（多因 authkey 无效），请稍后重试",
}

URL_RE = re.compile(r"https?://[^\s\"'<>\x00]*authkey=[^\s\"'<>\x00]*")
GAME_PATH_RE = re.compile(r"Loading player data from (.+?)[/\\]data\.unity3d")


# ---------------------------------------------------------------- 网络

def http_get_json(url: str) -> dict:
    """GET 一个 JSON 接口，带重试。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://webstatic.mihoyo.com/",
        "x-rpc-app_version": "1.0.0",
    })
    last_err: Optional[Exception] = None
    for attempt in range(1, MAX_RETRY + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            # 4xx 基本是链接本身的问题，重试没意义
            if 400 <= exc.code < 500:
                raise RuntimeError(f"HTTP {exc.code}") from exc
            last_err = exc
        except Exception as exc:  # noqa: BLE001
            last_err = exc
        if attempt < MAX_RETRY:
            time.sleep(REQUEST_GAP * attempt * 3)
    raise RuntimeError(f"请求失败：{last_err}")


# ---------------------------------------------------------------- 链接获取

def is_file_safe(p: Path) -> bool:
    """is_file() 的安全版。

    Windows 上路径段里出现半角冒号（如兜底候选 `崩坏: 星穹铁道`）会报
    WinError 123「文件名、目录名或卷标语法不正确」，Python 3.9 的
    pathlib.is_file() 不吞这种 OSError，会把整个流程炸掉 → 这里统一兜住。
    """
    try:
        return p.is_file()
    except OSError:
        return False


def is_wsl() -> bool:
    """是否运行在 WSL 里（游戏本体在 Windows 侧，日志要跨 /mnt/c 去读）。"""
    try:
        return "microsoft" in Path("/proc/version").read_text(
            encoding="utf-8", errors="ignore").lower()
    except OSError:
        return False


def win_to_wsl_path(p: str) -> str:
    """把 Windows 路径 D:/xxx 或 D:\\xxx 换算成 WSL 的 /mnt/d/xxx。"""
    p = p.replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/(.*)$", p)
    if m:
        return f"/mnt/{m.group(1).lower()}/{m.group(2)}"
    return p


def local_low_roots() -> List[Path]:
    """所有可能存放游戏日志的根目录。

    - Windows 原生：AppData\\LocalLow
    - macOS：~/Library/Application Support
    - WSL：/mnt/c/Users/*/AppData/LocalLow（游戏在 Windows 侧）
    """
    roots: List[Path] = []
    if sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA")
        if local:
            roots.append(Path(local).parent / "LocalLow")
        roots.append(Path.home() / "AppData" / "LocalLow")
    elif sys.platform == "darwin":
        roots.append(Path.home() / "Library" / "Application Support")
    elif is_wsl():
        for user_dir in Path("/mnt/c/Users").iterdir():
            # 排除 Public / Default 这类系统账户目录
            if user_dir.is_dir() and user_dir.name.lower() not in (
                    "public", "default", "defaultuser0", "all users"):
                roots.append(user_dir / "AppData" / "LocalLow")
    return [r for r in roots if r.is_dir()]


def candidate_log_files() -> List[Path]:
    """候选的日志路径（国服 / 海外服 / 兜底扫描）。"""
    names = [
        "Cognosphere/Star Rail/Player.log",             # 海外服
        "Cognosphere/Honkai Star Rail/Player.log",
        "miHoYo/崩坏：星穹铁道/Player.log",              # 国服（全角冒号，实际目录名）
        "miHoYo/崩坏: 星穹铁道/Player.log",              # 半角冒号变体，仅兜底
        "miHoYo/Star Rail/Player.log",
    ]
    found: List[Path] = []
    for base in local_low_roots():
        found.extend(base / n for n in names if is_file_safe(base / n))
        if not any(p.parent.parent == base for p in found):
            # 兜底：扫描 LocalLow/*/ 下的日志
            for pattern in ("*/Player.log", "*/output_log.txt"):
                found.extend(sorted(base.glob(pattern)))
    # 同一个日志可能同时被「精确候选」和「兜底扫描」命中，去重保序
    return list(dict.fromkeys(p for p in found if is_file_safe(p)))


def game_dirs_from_log(log_path: Path) -> List[str]:
    text = log_path.read_text(encoding="utf-8", errors="ignore")
    dirs: List[str] = []
    for m in GAME_PATH_RE.finditer(text):
        d = m.group(1).replace("\\", "/").strip()
        if is_wsl():
            # 日志里是 Windows 路径（如 D:/miHoYo Launcher/...），WSL 下换算成 /mnt/d/...
            d = win_to_wsl_path(d)
        if d and d not in dirs:
            dirs.append(d)
    return dirs


def cache_files(game_dir: str) -> List[Path]:
    root = Path(game_dir)
    hits: List[Path] = []
    # 国服目录结构里 webCaches 可能在游戏根目录，也可能在 StarRail_Data 下
    for pattern in ("webCaches/Cache/Cache_Data/data_2",
                    "webCaches/*/Cache/Cache_Data/data_2",
                    "StarRail_Data/webCaches/Cache/Cache_Data/data_2",
                    "StarRail_Data/webCaches/*/Cache/Cache_Data/data_2"):
        hits.extend(root.glob(pattern))
    hits = [p for p in hits if is_file_safe(p)]
    try:
        return sorted(set(hits), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return hits


def extract_urls(text: str) -> List[str]:
    """从文本里抓出所有带 authkey 的链接（倒序，最新的优先）。"""
    urls: List[str] = []
    for m in URL_RE.finditer(text):
        u = m.group(0).rstrip("&")
        if "authkey=" in u and u not in urls:
            urls.append(u)
    urls.reverse()
    return urls


def auto_find_urls(log_hint: Optional[str] = None) -> List[str]:
    """自动从日志 + webCaches 提取候选链接。"""
    logs = [Path(log_hint)] if log_hint else candidate_log_files()
    urls: List[str] = []

    for log in logs:
        if not is_file_safe(log):
            continue
        urls.extend(extract_urls(log.read_text(encoding="utf-8", errors="ignore")))
        for gdir in game_dirs_from_log(log):
            for cache in cache_files(gdir)[:3]:
                try:
                    raw = cache.read_bytes().decode("utf-8", errors="ignore")
                except OSError:
                    continue
                for chunk in raw.split("1/0/"):
                    if "authkey=" in chunk:
                        urls.extend(extract_urls(chunk))

    # 去重保序
    out: List[str] = []
    for u in urls:
        if u not in out:
            out.append(u)
    return out


# ---------------------------------------------------------------- 链接 → API

class GachaAPI:
    """把一个跃迁记录链接转成可分页调用的 API。"""

    def __init__(self, raw_url: str, lang: str = DEFAULT_LANG):
        parsed = urllib.parse.urlparse(raw_url.strip())
        query = urllib.parse.parse_qs(parsed.query)
        if "authkey" not in query:
            raise ValueError("链接里没有 authkey 参数，确认复制的是完整的跃迁记录链接")

        self.authkey = query["authkey"][0]
        self.authkey_ver = query.get("authkey_ver", ["1"])[0]
        self.sign_type = query.get("sign_type", ["2"])[0]
        self.game_biz = query.get("game_biz", ["hkrpg_cn"])[0]
        self.gacha_id = query.get("gacha_id", [None])[0]
        self.lang = query.get("lang", [lang])[0] or lang
        # 海外服用 os 域名；国服用 public-operation 域名
        self.host = API_HOST_OS if "global" in self.game_biz else API_HOST_CN

    def build(self, gacha_type: str, page: int, end_id: str, collab: bool) -> str:
        params = {
            "authkey": self.authkey,
            "authkey_ver": self.authkey_ver,
            "sign_type": self.sign_type,
            "lang": self.lang,
            "game_biz": self.game_biz,
            "gacha_type": gacha_type,
            "page": str(page),
            "size": str(PAGE_SIZE),
            "end_id": str(end_id),
        }
        if self.gacha_id:
            params["gacha_id"] = self.gacha_id
        path = API_PATH_COLLAB if collab else API_PATH
        return f"{self.host}{path}?{urllib.parse.urlencode(params)}"

    def probe(self) -> Tuple[bool, str]:
        """用 1 条记录验证链接是否还有效。"""
        try:
            data = http_get_json(self.build("11", 1, "0", False))
        except RuntimeError as exc:
            return False, str(exc)
        code = data.get("retcode", -999)
        if code == 0:
            return True, "ok"
        return False, f"retcode={code} {RETCODE_HINT.get(code, data.get('message', ''))}"

    def fetch_pool(self, gacha_type: str, collab: bool) -> List[dict]:
        """分页拉完一个卡池。联动池接口在部分服不存在（404），静默当作 0 条。"""
        records: List[dict] = []
        end_id = "0"
        for _ in range(1, 5000):
            try:
                data = http_get_json(self.build(gacha_type, 1, end_id, collab))
            except RuntimeError as exc:
                if collab and "404" in str(exc):
                    return []
                raise
            code = data.get("retcode", -999)
            if code != 0:
                raise RuntimeError(
                    f"卡池 {gacha_type} 拉取失败：retcode={code} "
                    f"{RETCODE_HINT.get(code, data.get('message', ''))}"
                )
            lst = (data.get("data") or {}).get("list") or []
            if not lst:
                break
            records.extend(lst)
            if len(lst) < PAGE_SIZE:
                break
            end_id = lst[-1]["id"]
            time.sleep(REQUEST_GAP)
        return records


# ---------------------------------------------------------------- 存储

class Store:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, uid: str) -> Path:
        return self.root / f"warp_{uid}.json"

    def load(self, uid: str) -> Dict[str, List[dict]]:
        p = self.path(uid)
        if is_file_safe(p):
            return json.loads(p.read_text(encoding="utf-8"))
        return {}

    def save(self, uid: str, data: Dict[str, List[dict]]) -> Path:
        p = self.path(uid)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return p


def merge(old: Dict[str, List[dict]], new: Dict[str, List[dict]]) -> Tuple[Dict[str, List[dict]], int]:
    """按记录 id 去重合并，返回 (合并结果, 新增条数)。"""
    merged: Dict[str, List[dict]] = {}
    added = 0
    for pool in {*old.keys(), *new.keys()}:
        seen: Dict[str, dict] = {}
        for rec in old.get(pool, []):
            seen[str(rec.get("id"))] = rec
        for rec in new.get(pool, []):
            key = str(rec.get("id"))
            if key not in seen:
                added += 1
            seen[key] = rec
        merged[pool] = sorted(seen.values(), key=lambda r: int(r.get("id") or 0))
    return merged, added


def export_csv(data: Dict[str, List[dict]], out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "uid", "卡池", "时间", "名称", "类型", "星级", "item_id"])
        for pool, records in data.items():
            for r in records:
                w.writerow([
                    r.get("id", ""), r.get("uid", ""), pool, r.get("time", ""),
                    r.get("name", ""), r.get("item_type", ""),
                    r.get("rank_type", ""), r.get("item_id", ""),
                ])
    return out


# ---------------------------------------------------------------- 统计

def bar(n: int, scale: float, width: int = 24) -> str:
    return "█" * min(width, int(n * scale))


def pool_stats(name: str, records: List[dict]) -> Dict[str, object]:
    total = len(records)
    five = [r for r in records if str(r.get("rank_type")) == "5"]
    four = [r for r in records if str(r.get("rank_type")) == "4"]
    three = [r for r in records if str(r.get("rank_type")) == "3"]
    since = 0
    for i in range(total - 1, -1, -1):
        if str(records[i].get("rank_type")) == "5":
            break
        since += 1
    gaps = []
    prev = -1
    for i, r in enumerate(records):
        if str(r.get("rank_type")) == "5":
            if prev >= 0:
                gaps.append(i - prev)
            prev = i
    return {
        "名称": name,
        "总数": total,
        "5星": len(five),
        "4星": len(four),
        "3星": len(three),
        "5星率": f"{(len(five) / total * 100):.2f}%" if total else "-",
        "当前垫数": since,
        "平均5星间隔": f"{sum(gaps) / len(gaps):.1f}" if gaps else "-",
        "五星列表": [(r.get("time", ""), r.get("name", ""), r.get("item_type", "")) for r in five],
    }


def print_stats(data: Dict[str, List[dict]]) -> None:
    stats = []
    for gtype, name, _ in GACHA_TYPES:
        if name in data:
            stats.append(pool_stats(name, data[name]))
    # 兼容历史文件里的其它卡池名
    known = {n for _, n, _ in GACHA_TYPES}
    for name in data:
        if name not in known and data[name]:
            stats.append(pool_stats(name, data[name]))

    if not stats:
        print("没有任何记录。")
        return

    print("\n" + "=" * 62)
    print("跃迁统计")
    print("=" * 62)
    grand = sum(s["总数"] for s in stats)  # type: ignore[arg-type]
    print(f"总计 {grand} 抽\n")
    for s in stats:
        if not s["总数"]:
            continue
        print(f"【{s['名称']}】{s['总数']} 抽  "
              f"5★{s['5星']}  4★{s['4星']}  3★{s['3星']}  5★率 {s['5星率']}")
        print(f"    当前垫数：{s['当前垫数']} 抽   平均5★间隔：{s['平均5星间隔']}")
        if s["五星列表"]:
            print("    5★记录：")
            for t, nm, it in s["五星列表"]:  # type: ignore[union-attr]
                print(f"      {t}  {nm}（{it}）")
        print()

    scale = 20.0 / max(1, max(int(s["总数"]) for s in stats))
    print("各池抽数分布：")
    for s in stats:
        print(f"  {s['名称']:<12} {bar(int(s['总数']), scale)} {s['总数']}")


# ---------------------------------------------------------------- 主流程

def resolve_url(args: argparse.Namespace, store: Store) -> Optional[str]:
    cache_file = store.root / "last_url.txt"

    if args.url:
        return args.url.strip()

    if not args.no_cache and is_file_safe(cache_file):
        cached = cache_file.read_text(encoding="utf-8").strip()
        if cached:
            print("使用上次保存的跃迁链接（--url 可覆盖）。")
            return cached

    if not args.no_auto:
        print("正在从游戏日志 / webCaches 自动提取跃迁链接...")
        for url in auto_find_urls(args.log):
            try:
                api = GachaAPI(url, args.lang)
            except ValueError:
                continue
            ok, msg = api.probe()
            if ok:
                print("✓ 已找到有效链接（来自本机游戏缓存）。")
                return url
            print(f"  跳过无效链接：{msg}")

    print("\n自动提取失败。请按下面的步骤手动获取链接：")
    print("  1. 打开游戏 → 跃迁 → 左下角「查看详情」")
    print("  2. 等待页面加载后，把浏览器/工具里以 https:// 开头、含 authkey= 的链接复制出来")
    print("  3. 粘贴到下面（回车确认）\n")
    try:
        pasted = input("跃迁链接> ").strip()
    except EOFError:
        return None
    return pasted or None


def fetch_all(api: GachaAPI) -> Tuple[Dict[str, List[dict]], str]:
    data: Dict[str, List[dict]] = {}
    uid = ""
    for gtype, name, collab in GACHA_TYPES:
        print(f"  拉取 {name} ...", flush=True)
        try:
            records = api.fetch_pool(gtype, collab)
        except RuntimeError as exc:
            print(f"    ! {exc}")
            records = []
        if records:
            uid = uid or str(records[0].get("uid", ""))
        data[name] = records
        print(f"    {len(records)} 条")
        time.sleep(REQUEST_GAP)
    return data, uid


def main() -> int:
    ap = argparse.ArgumentParser(
        description="获取崩坏：星穹铁道的跃迁（抽卡）记录",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--url", help="跃迁记录链接（含 authkey）")
    ap.add_argument("--out", default=None, help="数据目录，默认 ~/.starrail_warp")
    ap.add_argument("--lang", default=DEFAULT_LANG, help=f"返回语言，默认 {DEFAULT_LANG}")
    ap.add_argument("--log", default=None, help="手动指定 Player.log 路径")
    ap.add_argument("--no-auto", action="store_true", help="跳过自动提取链接")
    ap.add_argument("--no-cache", action="store_true", help="忽略上次保存的链接")
    ap.add_argument("--stats-only", action="store_true", help="只看本地统计，不联网")
    ap.add_argument("--csv", action="store_true", help="额外导出 CSV")
    args = ap.parse_args()

    root = Path(args.out) if args.out else Path.home() / ".starrail_warp"
    store = Store(root)

    if args.stats_only:
        files = sorted(store.root.glob("warp_*.json"))
        if not files:
            print(f"{store.root} 下没有本地数据。")
            return 1
        for f in files:
            data = json.loads(f.read_text(encoding="utf-8"))
            print(f"\nUID {f.stem.replace('warp_', '')}（{f}）")
            print_stats(data)
        return 0

    url = resolve_url(args, store)
    if not url:
        print("没有可用的跃迁链接，退出。")
        return 2

    try:
        api = GachaAPI(url, args.lang)
    except ValueError as exc:
        print(f"链接无效：{exc}")
        return 2

    ok, msg = api.probe()
    if not ok:
        print(f"链接不可用：{msg}")
        return 1
    print(f"链接有效（{api.host}，game_biz={api.game_biz}）\n")

    try:
        fresh, uid = fetch_all(api)
    except RuntimeError as exc:
        print(f"拉取失败：{exc}")
        return 1

    if not uid:
        print("没有拉到任何记录，可能该账号确实没有跃迁数据。")
        return 0

    (store.root / "last_url.txt").write_text(url, encoding="utf-8")

    old = store.load(uid)
    merged, added = merge(old, fresh)
    saved = store.save(uid, merged)
    print(f"\nUID {uid}：本次 {sum(len(v) for v in fresh.values())} 条，"
          f"新增 {added} 条，累计 {sum(len(v) for v in merged.values())} 条")
    print(f"数据文件：{saved}")
    if args.csv:
        print(f"CSV：{export_csv(merged, store.root / f'warp_{uid}.csv')}")

    print_stats(merged)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断。")
        sys.exit(130)
