# -*- coding: utf-8 -*-
"""技能发布前置依赖检查（2026-11-14 定版，简化口径）。

声明在 SKILL.md frontmatter 的 requires 里，只写直接依赖：

    requires:
      python: [pandas>=2.0, openpyxl]
      mcp: [browser]

满足方只有两类（系统级的东西在工作区镜像构建时定死，不归这里管）：

- python：工作区镜像技能公共环境的清单（pip freeze）里版本满足，
  或技能包内自带兼容 wheel——解释器是 Python 3.11、生产为 arm64
  单架构，故纯轮（py3-none-any / cp311-abi3-any 等）直接可，平台轮
  须含 aarch64（x86_64 仅 wstest 测试环境用、带不带随意）；镜像已
  满足又自带 wheel 的，在发布响应里记 redundant 提示多余、不拦截；
- mcp：注册表里对发布租户可见的 MCP Server 名单里有这个名字。

镜像清单缺失（还没随注册表 ConfigMap 挂进来）时，python 的镜像比对
无从做起：未被 wheel 满足的项记 unverified 放行并在发布响应里显形，
不静默跳过；清单就位后同一路径自动转严格（不满足回 422）。
"""
import re

_NAME_RE = re.compile(r"^([A-Za-z0-9]|[A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9])$")
_CLAUSE_RE = re.compile(r"^(==|!=|>=|<=|~=|>|<)\s*([A-Za-z0-9][A-Za-z0-9.!*+-]*)$")
_WHEEL_RE = re.compile(
    r"^(?P<dist>[A-Za-z0-9._]+)-(?P<version>[A-Za-z0-9.!+]+)"
    r"-(?P<py>[A-Za-z0-9._]+)-(?P<abi>[A-Za-z0-9._]+)"
    r"-(?P<plat>[A-Za-z0-9._]+)\.whl$",
)


def normalize_name(name: str) -> str:
    """PEP 503 归一：小写、[-_.] 连续折叠为 -。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def _version_key(version: str) -> tuple:
    parts = []
    for seg in re.split(r"[.!+]", version):
        for tok in re.findall(r"\d+|[a-zA-Z]+", seg):
            parts.append((0, int(tok), "") if tok.isdigit() else (1, 0, tok))
    return tuple(parts)


def _cmp(a: str, b: str) -> int:
    ka, kb = _version_key(a), _version_key(b)
    n = max(len(ka), len(kb))
    ka += ((0, 0, ""),) * (n - len(ka))
    kb += ((0, 0, ""),) * (n - len(kb))
    return (ka > kb) - (ka < kb)


def parse_python_requirement(entry) -> tuple[str, list[tuple[str, str]]]:
    """'pandas>=2.0,<4' → ('pandas', [('>=','2.0'), ('<','4')])。

    支持 ==/!=/>=/<=/>/</~= 与逗号 AND、extras 忽略；环境标记（;）
    不支持。不合法抛 ValueError，消息可直接回给发布者。
    """
    if not isinstance(entry, str) or not entry.strip():
        raise ValueError(f"python 依赖条目不合法：{entry!r}")
    text = entry.strip()
    if ";" in text:
        raise ValueError(f"python 依赖不支持环境标记（;）：{entry}")
    m = re.match(r"^([A-Za-z0-9._-]+)(\[[^\]]*\])?\s*(.*)$", text)
    name, spec = m.group(1), m.group(3).strip()
    if not _NAME_RE.match(name):
        raise ValueError(f"python 依赖名不合法：{entry}")
    clauses: list[tuple[str, str]] = []
    if spec:
        for part in spec.split(","):
            cm = _CLAUSE_RE.match(part.strip())
            if not cm:
                raise ValueError(f"python 依赖版本约束不合法：{entry}")
            clauses.append((cm.group(1), cm.group(2)))
    return normalize_name(name), clauses


def satisfies(version: str, clauses: list[tuple[str, str]]) -> bool:
    for op, want in clauses:
        if want.endswith(".*"):  # ==1.2.* / !=1.2.* 前缀匹配
            prefix = want[:-2]
            hit = version == prefix or version.startswith(prefix + ".")
            if (op == "==") != hit:
                return False
            continue
        c = _cmp(version, want)
        if op == "==" and c != 0:
            return False
        if op == "!=" and c == 0:
            return False
        if op == ">=" and c < 0:
            return False
        if op == "<=" and c > 0:
            return False
        if op == ">" and c <= 0:
            return False
        if op == "<" and c >= 0:
            return False
        if op == "~=":  # ~=a.b → >=a.b, <(a+1).0；~=a.b.c → >=a.b.c, <a.(b+1)
            if c < 0:
                return False
            nums = [int(x) for x in re.findall(r"\d+", want)]
            if len(nums) >= 2:
                upper = nums[:-1]
                upper[-1] += 1
            elif nums:
                upper = [nums[0] + 1]
            else:
                return False
            if _cmp(version, ".".join(str(x) for x in upper)) >= 0:
                return False
    return True


def parse_requires(frontmatter: dict) -> dict:
    """frontmatter 的 requires 结构校验＋python 条目语法预检。

    返回 {"python": [原始条目...], "mcp": [名...]}；无声明返回空表。
    不合法抛 ValueError，消息可直接回给发布者。
    """
    req = frontmatter.get("requires")
    if req is None:
        return {"python": [], "mcp": []}
    if not isinstance(req, dict):
        raise ValueError("requires 须为映射（python/mcp 两键）")
    unknown = set(req) - {"python", "mcp"}
    if unknown:
        raise ValueError(
            f"requires 只支持 python 与 mcp 两类，未知类别：{sorted(unknown)}",
        )
    out: dict[str, list[str]] = {"python": [], "mcp": []}
    for kind in ("python", "mcp"):
        items = req.get(kind) or []
        if not isinstance(items, list) or not all(
            isinstance(x, str) and x.strip() for x in items
        ):
            raise ValueError(f"requires.{kind} 须为非空字符串列表")
        out[kind] = [x.strip() for x in items]
    for entry in out["python"]:
        parse_python_requirement(entry)  # 语法预检，错误在此暴露
    return out


def _interp_ok(py_tag: str, abi_tag: str) -> bool:
    for py in py_tag.split("."):
        for abi in abi_tag.split("."):
            if py in ("py3", "cp311", "cp3") or (
                abi == "abi3"
                and py.startswith("cp3")
                and py[3:].isdigit()
                and int(py[3:]) <= 311
            ):
                return True
    return False


def _wheel_set_verdict(
    wheels: list[str], want_name: str, clauses,
) -> tuple[bool, str]:
    """包内同名 wheel 合起来判定一条 python 依赖是否被自带 wheel 满足。

    专家在 x86 机器上用 pip download 按平台取轮，产物天然是一架构
    一个文件，故架构覆盖按【集合】判：版本满足约束且解释器标签
    兼容的文件里，有平台无关轮（any）即满足；否则须有生产架构
    aarch64 的文件（x86_64 只影响 wstest 测试环境、缺了不拦）。
    不满足返回给发布者看的原因。
    """
    seen = False
    version_bad: str | None = None
    interp_bad: str | None = None
    arches: set[str] = set()
    for path in wheels:
        m = _WHEEL_RE.match(path.rsplit("/", 1)[-1])
        if not m or normalize_name(m.group("dist")) != want_name:
            continue
        seen = True
        if not satisfies(m.group("version"), clauses):
            version_bad = m.group("version")
            continue
        if not _interp_ok(m.group("py"), m.group("abi")):
            interp_bad = f"{m.group('py')}-{m.group('abi')}"
            continue
        plats = m.group("plat").split(".")
        if any(p == "any" for p in plats):
            return True, "ok"
        if any("x86_64" in p for p in plats):
            arches.add("x86_64")
        if any("aarch64" in p for p in plats):
            arches.add("aarch64")
    if "aarch64" in arches:
        return True, "ok"
    if arches:
        return False, (
            "平台轮只有 x86_64，缺生产架构 aarch64"
            "（或换 py3-none-any 通用轮）"
        )
    if interp_bad:
        return False, (
            f"解释器标签 {interp_bad} 与工作区 Python 3.11 不兼容"
            "（须 cp311 / abi3 / py3 通用轮）"
        )
    if version_bad:
        return False, f"wheel 版本 {version_bad} 不满足约束"
    if seen:
        return False, "wheel 文件名不合规范"
    return False, "包内无 wheel"


def check_dependencies(
    requires: dict,
    package_paths: list[str],
    image_manifest: dict[str, str] | None,
    visible_mcp: set[str],
) -> dict:
    """逐项核对。返回 {"problems", "unverified", "redundant"}，
    条目形如 {"kind", "entry", "reason"}。problems 非空＝发布应拒绝；
    redundant 是镜像已满足却又自带 wheel 的多余项（只提示不拦截）。"""
    problems: list[dict] = []
    unverified: list[dict] = []
    redundant: list[dict] = []
    wheels = [p for p in package_paths if p.endswith(".whl")]
    for entry in requires.get("python", []):
        name, clauses = parse_python_requirement(entry)
        wheel_ok, wheel_why = _wheel_set_verdict(wheels, name, clauses)
        if image_manifest is None:
            if not wheel_ok:
                unverified.append({
                    "kind": "python", "entry": entry,
                    "reason": "镜像清单未配置，未核对镜像内置环境",
                })
            continue
        have = image_manifest.get(name)
        image_ok = have is not None and satisfies(have, clauses)
        if image_ok and wheel_ok:
            redundant.append({
                "kind": "python", "entry": entry,
                "reason": f"镜像技能环境已有 {have} 满足约束，包内 wheel 无需自带",
            })
            continue
        if image_ok or wheel_ok:
            continue
        if have is not None:
            reason = f"镜像技能环境为 {have}，不满足约束；{wheel_why}"
        else:
            reason = f"镜像技能环境无此包；{wheel_why}"
        problems.append({"kind": "python", "entry": entry, "reason": reason})
    for name in requires.get("mcp", []):
        if name not in visible_mcp:
            problems.append({
                "kind": "mcp", "entry": name,
                "reason": "注册表中无对本租户可见的同名 MCP Server",
            })
    return {
        "problems": problems, "unverified": unverified,
        "redundant": redundant,
    }
