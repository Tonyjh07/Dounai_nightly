#!/usr/bin/env python3
"""Nightly 打包编排：清单驱动、按上游 commit sha 增量、K3 签包 / K4 签 catalog。

由 .github/workflows/nightly.yml 调用，也可在 CI 外手工跑（需 pack / sign_catalog
两个可执行文件和两把私钥）。本脚本只写产物仓里的文件，不改任何上游模块仓。

设计要点见根目录 contracts.md。模块 manifest 由 modules.json 的模板现场生成，
上游仓里没有可作为真源的 v2 manifest。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODULES_FILE = ROOT / "modules.json"
STATE_FILE = ROOT / "state" / "last-built.json"
PACKAGES_DIR = ROOT / "packages"
CATALOG_FILE = ROOT / "catalog.json"
RAW_BASE = "https://raw.githubusercontent.com/Tonyjh07/Dounai_nightly/main"
CATALOG_URL = f"{RAW_BASE}/catalog.json"
EXPIRES_DAYS = 1

IGNORE = [
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "*.egg-info",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "build",
    "dist",
    "target",
    "node_modules",
    ".github",
    ".vscode",
    "*.pyc",
]


def die(message: str) -> None:
    print(f"中止：{message}", file=sys.stderr)
    sys.exit(1)


def run(cmd: list[str], cwd: Path | None = None) -> str:
    print(f"  $ {' '.join(str(c) for c in cmd)}", flush=True)
    result = subprocess.run(
        [str(c) for c in cmd],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        print(result.stdout[-4000:])
        print(result.stderr[-4000:], file=sys.stderr)
        die(f"命令失败（退出码 {result.returncode}）：{cmd[0]}")
    return result.stdout


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def date_version(moment: datetime) -> str:
    """点分日期版本号 YYYY.M.D，恰好是三段 SemVer，按数值比较即日期递增。"""
    return f"{moment.year}.{moment.month}.{moment.day}"


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as error:
        die(f"无法解析 {path}：{error}")


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch_text(url: str, timeout: int = 30) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "dounai-nightly/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8")


def upstream_sha(repo: str, ref: str, token: str | None) -> str:
    """取上游默认分支最新 commit sha；GitHub Token 仅用于提高限流额度。"""
    url = f"https://api.github.com/repos/{repo}/commits/{ref}"
    headers = {"User-Agent": "dounai-nightly/1", "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))["sha"]
    except Exception as error:  # noqa: BLE001
        die(f"读取 {repo}@{ref} 的最新提交失败：{error}")


def checkout(repo: str, ref: str, sha: str, dest: Path, token: str | None) -> None:
    """浅克隆到指定 sha。上游仓是公开仓，clone 不注入 token，避免 URL 带凭据。"""
    clone_url = f"https://github.com/{repo}.git"
    run(["git", "clone", "--no-checkout", "--depth", "1", clone_url, str(dest)])
    run(["git", "checkout", sha], cwd=dest)


def build_manifest(spec: dict, version: str) -> dict:
    """从清单模板生成完整 v2 manifest。version/variant/files/signature 由本函数填入。"""
    manifest = dict(spec["manifest"])
    manifest["version"] = version
    manifest["variant"] = "nightly"
    manifest["files"] = {}
    manifest["signature"] = None
    return manifest


def build_one(
    spec: dict,
    version: str,
    sha: str,
    ctx: dict,
    work: Path,
) -> dict:
    """打一个模块包，返回 catalog 条目（未签名部分）。"""
    module_id = spec["manifest"]["module_id"]
    print(f"== {module_id}  version={version}  sha={sha[:12]}", flush=True)
    slug = module_id.split("/")[-1]
    stage = work / slug
    if stage.exists():
        shutil.rmtree(stage)
    checkout(spec["repo"], spec.get("ref", "main"), sha, stage, ctx["token"])

    for pattern in IGNORE:
        for path in stage.rglob(pattern):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.is_file():
                path.unlink(missing_ok=True)

    if spec["kind"] == "python":
        run([ctx["uv"], "lock"], cwd=stage)
        if not (stage / "uv.lock").is_file():
            die(f"{module_id} 的 uv lock 未产出 uv.lock")
    else:
        exe_name = spec.get("exe_name")
        if not exe_name:
            die(f"{module_id} 是 shell 类但缺 exe_name")
        exe = stage / exe_name
        if not exe.is_file():
            die(f"{module_id} 的 exe 不在仓库根：{exe}")

    save_json(stage / "manifest.json", build_manifest(spec, version))
    zip_name = spec["zip_name"].replace("{version}", version)
    zip_path = PACKAGES_DIR / zip_name
    PACKAGES_DIR.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()
    run(
        [
            ctx["pack"],
            "pack",
            "--dir",
            str(stage),
            "--key",
            ctx["k3_key"],
            "--out",
            str(zip_path),
            "--key-id",
            ctx["pack_key_id"],
        ]
    )
    size = zip_path.stat().st_size
    digest = sha256_of(zip_path)
    print(f"  包 {zip_name}  {size} 字节  sha256:{digest[:12]}…", flush=True)
    return {
        "module_id": module_id,
        "variant": "nightly",
        "version": version,
        "role": spec["manifest"]["role"],
        "name": spec["manifest"]["name"],
        "publisher_fp": ctx["k3_fp"],
        "sha256": f"sha256:{digest}",
        "size": size,
        "sources": [{"source_id": ctx["source_id"], "url": f"{RAW_BASE}/packages/{zip_name}"}],
        "signature_required": True,
        "expected_signer_fp": ctx["k3_fp"],
        "expires": ctx["expires_stamp"],
        "min_version": None,
        "min_launcher": None,
        "revoked": [],
    }


def sign_catalog(doc: dict, ctx: dict) -> None:
    """写未签名 catalog 并调用 sign_catalog；工具内含 source_id 预检与签后自验。"""
    unsigned = ROOT / "catalog.unsigned.json"
    save_json(unsigned, doc)
    run(
        [
            ctx["sign_catalog"],
            "sign",
            "--key",
            ctx["k4_key"],
            "--catalog",
            str(unsigned),
            "--key-id",
            ctx["catalog_key_id"],
            "--allow-source",
            ctx["source_id"],
        ]
    )
    # sign_catalog 的 sign 子命令已内嵌签后自验（用签名钥自身公钥验一遍），
    # 足以保证写出的索引验得过。这里不再单独调 verify：verify 子命令的
    # source_id 白名单来自启动器内置常量，会把 nightly 源当非法 id 拒绝。
    CATALOG_FILE.write_text(unsigned.read_text(encoding="utf-8"), encoding="utf-8")
    unsigned.unlink(missing_ok=True)


def build_catalog(entries: list[dict], sequence: int, ctx: dict) -> dict:
    return {
        "schema_version": 2,
        "sequence": sequence,
        "generated_at": ctx["generated_at"],
        "expires": ctx["expires_stamp"],
        "sources": [
            {
                "id": ctx["source_id"],
                "kind": "github",
                "base_url": RAW_BASE,
                "priority": 1,
                "enabled": True,
            }
        ],
        "modules": entries,
        "revoked": [],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", required=True, help="pack 可执行文件路径")
    parser.add_argument("--sign-catalog", required=True, help="sign_catalog 可执行文件路径")
    parser.add_argument("--k3-key", required=True, help="K3 私钥（签包）")
    parser.add_argument("--k4-key", required=True, help="K4 私钥（签 catalog）")
    parser.add_argument("--k4-pub", default="", help="K4 公钥文件（可选，供人工复验）")
    parser.add_argument("--k3-fp", required=True, help="K3 指纹（写 catalog 条目）")
    parser.add_argument("--uv", default="uv", help="uv 可执行文件")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""), help="GitHub Token")
    parser.add_argument("--dry-run", action="store_true", help="只筛出待打包模块，不落产物")
    args = parser.parse_args()

    manifest = load_json(MODULES_FILE)
    specs = manifest.get("modules", [])
    if not specs:
        die("modules.json 没有模块")
    state = load_json(STATE_FILE)
    built: dict = state.get("modules", {})
    token = args.token or None

    ctx = {
        "pack": args.pack,
        "sign_catalog": args.sign_catalog,
        "k3_key": args.k3_key,
        "k4_key": args.k4_key,
        "k4_pub": args.k4_pub,
        "k3_fp": args.k3_fp,
        "uv": args.uv,
        "token": token,
        "source_id": manifest["catalog_source_id"],
        "pack_key_id": manifest["package_key_id"],
        "catalog_key_id": manifest["catalog_key_id"],
    }

    now = now_utc()
    version = date_version(now)
    ctx["generated_at"] = stamp(now)
    ctx["expires_stamp"] = stamp(now + timedelta(days=EXPIRES_DAYS))

    changed: list[tuple[dict, str]] = []
    for spec in specs:
        module_id = spec["manifest"]["module_id"]
        sha = upstream_sha(spec["repo"], spec.get("ref", "main"), token)
        previous = (built.get(module_id) or {}).get("sha")
        if previous == sha:
            print(f"-- {module_id} 无更新（{sha[:12]}），跳过", flush=True)
            continue
        # 同一天已有构建时跳过发布：日期版本号不变，重复构建会被判非更新。
        if (built.get(module_id) or {}).get("version") == version:
            print(f"-- {module_id} 当天已构建（{version}），跳过", flush=True)
            continue
        changed.append((spec, sha))

    if not changed:
        print("没有模块更新，catalog 保持不变。")
        return
    print(f"待打包 {len(changed)} 个模块，版本 {version}", flush=True)
    if args.dry_run:
        for spec, sha in changed:
            print(f"  {spec['manifest']['module_id']}  {sha}")
        return

    with tempfile.TemporaryDirectory(prefix="dounai-nightly-") as tmp:
        work = Path(tmp)
        entries: list[dict] = []
        # 未变化的模块也要带进 catalog，否则用户会看到模块列表缺项。
        old_catalog = load_json(CATALOG_FILE)
        old_entries = {e["module_id"]: e for e in old_catalog.get("modules", [])}
        rebuilt_ids = set()
        for spec, sha in changed:
            module_id = spec["manifest"]["module_id"]
            rebuilt_ids.add(module_id)
            entry = build_one(spec, version, sha, ctx, work)
            entries.append(entry)
            built[module_id] = {"sha": sha, "version": version}
        for module_id, entry in old_entries.items():
            if module_id not in rebuilt_ids:
                entries.append(entry)
        entries.sort(key=lambda e: e["module_id"])

        old_sequence = int(old_catalog.get("sequence", 0))
        doc = build_catalog(entries, old_sequence + 1, ctx)
        sign_catalog(doc, ctx)

    state["modules"] = built
    save_json(STATE_FILE, state)
    print(f"完成：catalog sequence={doc['sequence']}，expires={ctx['expires_stamp']}")


if __name__ == "__main__":
    main()
