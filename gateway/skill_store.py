# -*- coding: utf-8 -*-
"""动态技能仓储（网关侧）：业务专家发布的技能包，免镜像、免重启。

背景与口径（2026-11-13 定版，取代"技能本体只许躺在网关镜像里"）：
- 静态层保留：skills_store 目录＋skills.yaml 声明，demo/内置技能
  继续走文件；动态发布的技能经管理端点 POST /v1/admin/skills 进
  本仓储，gateway.registry 在建会话注入时把两层合并读取，对新建
  会话立即生效，不打镜像、不重启。
- 元数据（声明、版本指针、状态、发布人）一律落库（gw_skills /
  gw_skill_versions，与 usage 同库口径）；技能包本体两种后端：
  db（整包 zip 存 LONGBLOB，零新增基础设施）｜s3（内网 S3 兼容
  OSS 只存工件、索引仍在库）。SKILL_BLOB_BACKEND 选择，注入侧经
  本模块统一取包，不感知后端。
- 作用域：技能行里的 tenant 是归属租户；字面值 "global" 表示全
  租户通用。可见性与同名覆盖规则在 gateway.registry 执行，本
  模块只管存取与包校验。
- 版本：每次发布 = 一个不可变新版本（整包 zip），current_version
  是指针；回滚 = 拨指针。已有会话工作区里是上传当时的文件快照，
  与本仓储之后的变化无关（会话钉住语义不变）。

连接（AGENT_BASE_SKILLS_URL，同步驱动 URL），解析顺序与 usage
一致：显式 env → 从 SERVICE_DB_URL 推导 → 本地 sqlite 兜底。
"""
import hashlib
import io
import json
import logging
import re
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import yaml
from sqlalchemy import (
    Column,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    select,
)
from sqlalchemy.dialects import mysql

from common.config import env, resolve_path

logger = logging.getLogger(__name__)

GLOBAL_TENANT = "global"

MAX_PACKAGE_BYTES = 10 * 1024 * 1024  # zip 整包上限
MAX_FILES = 200
MAX_UNZIPPED_BYTES = 50 * 1024 * 1024

_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")

_METADATA = MetaData()
SKILLS = Table(
    "gw_skills",
    _METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("tenant", String(128), nullable=False),
    Column("name", String(128), nullable=False),
    # 注入时机：default（建会话自动注入）｜optional（仅登记）
    Column("injection", String(16), nullable=False, default="default"),
    Column("current_version", Integer, nullable=True),
    Column("status", String(16), nullable=False, default="published"),
    Column("publisher", String(128), nullable=False, default=""),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    UniqueConstraint("tenant", "name", name="uq_gw_skills_tenant_name"),
)
SKILL_VERSIONS = Table(
    "gw_skill_versions",
    _METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("skill_id", Integer, nullable=False),
    Column("version", Integer, nullable=False),
    Column("blob_backend", String(8), nullable=False, default="db"),
    Column("blob_ref", String(512), nullable=False, default=""),
    Column(
        "package",
        LargeBinary().with_variant(mysql.LONGBLOB(), "mysql"),
        nullable=True,
    ),
    Column("manifest", Text, nullable=False, default="[]"),
    Column("sha256", String(64), nullable=False, default=""),
    Column("size", Integer, nullable=False, default=0),
    Column("note", String(512), nullable=False, default=""),
    Column("created_by", String(128), nullable=False, default=""),
    Column("created_at", String(40), nullable=False),
    UniqueConstraint("skill_id", "version", name="uq_gw_skill_versions"),
    Index("idx_gw_skill_versions_skill", "skill_id"),
)


def default_skills_url() -> str:
    """按模块 docstring 的顺序解析技能仓储库连接 URL。"""
    url = env("AGENT_BASE_SKILLS_URL")
    if url:
        return url
    service_url = env("SERVICE_DB_URL", "")
    if service_url:
        return service_url.replace("+aiomysql", "+pymysql").replace(
            "+aiosqlite", "",
        )
    return f"sqlite:///{resolve_path('data/skills.db')}"


class SkillPackageError(ValueError):
    """技能包不合法（发布端点据此回 422）。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    try:
        data = yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def validate_and_normalize_package(zip_bytes: bytes) -> dict:
    """校验技能包并重打成规范 zip（条目排序、剥单层顶目录）。

    规范形态：SKILL.md 在包根；frontmatter 的 name 是技能名（唯一
    身份来源，与框架/技能生态的通行约定一致）。返回
    {name, description, manifest:[{path,size,sha256}], zip_bytes}。
    不合法抛 SkillPackageError，消息可直接回给发布者。
    """
    if len(zip_bytes) > MAX_PACKAGE_BYTES:
        raise SkillPackageError(
            f"技能包超过上限 {MAX_PACKAGE_BYTES // 1024 // 1024}MB",
        )
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise SkillPackageError("不是合法的 zip 包") from exc
    raw: dict[str, bytes] = {}
    total = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or ".." in name.split("/"):
            raise SkillPackageError(f"非法路径：{info.filename}")
        if name in raw:
            raise SkillPackageError(f"重复文件：{name}")
        data = zf.read(info)
        total += len(data)
        if total > MAX_UNZIPPED_BYTES:
            raise SkillPackageError("解包后总大小超过上限 50MB")
        raw[name] = data
    if len(raw) > MAX_FILES:
        raise SkillPackageError(f"文件数超过上限 {MAX_FILES}")
    if not raw:
        raise SkillPackageError("空包")
    # 剥单层顶目录：全部文件都在同一个顶层目录下且 SKILL.md 在其内
    tops = {p.split("/")[0] for p in raw}
    if "SKILL.md" not in raw and len(tops) == 1:
        top = next(iter(tops))
        if f"{top}/SKILL.md" in raw:
            raw = {p[len(top) + 1:]: d for p, d in raw.items()}
    if "SKILL.md" not in raw:
        raise SkillPackageError("包根缺少 SKILL.md")
    try:
        skill_md = raw["SKILL.md"].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillPackageError("SKILL.md 不是 UTF-8 文本") from exc
    fm = _parse_frontmatter(skill_md)
    name = str(fm.get("name") or "").strip()
    if not _NAME_RE.match(name):
        raise SkillPackageError(
            "SKILL.md frontmatter 的 name 缺失或不合法"
            "（须为字母数字开头、只含字母数字与 . _ -）",
        )
    manifest = [
        {
            "path": p,
            "size": len(d),
            "sha256": hashlib.sha256(d).hexdigest(),
        }
        for p, d in sorted(raw.items())
    ]
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for item in manifest:
            info = zipfile.ZipInfo(item["path"], date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            z.writestr(info, raw[item["path"]])
    return {
        "name": name,
        "description": str(fm.get("description") or ""),
        "manifest": manifest,
        "zip_bytes": out.getvalue(),
    }


def unzip_package(zip_bytes: bytes) -> list[tuple[str, bytes]]:
    """仓储里的规范 zip → [(相对路径, 内容)]，路径排序（注入顺序）。"""
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    out = []
    for info in zf.infolist():
        if not info.is_dir():
            out.append((info.filename, zf.read(info)))
    out.sort(key=lambda kv: kv[0])
    return out


class SkillStore:
    """线程安全的动态技能仓储；不可用时 healthy=False（发布端点
    据此回 503、注册表合并时跳过动态层，静态层不受影响）。"""

    def __init__(self, url: str | None = None) -> None:
        self.url = url or default_skills_url()
        self.blob_backend = env("SKILL_BLOB_BACKEND", "db").lower()
        self._lock = threading.Lock()
        self._engine = None
        self._s3 = None
        self._s3_bucket = env("SKILL_OSS_BUCKET")
        try:
            if self.url.startswith("sqlite:///"):
                Path(self.url[len("sqlite:///"):]).parent.mkdir(
                    parents=True, exist_ok=True,
                )
            self._engine = create_engine(
                self.url, pool_pre_ping=True, future=True,
            )
            with self._lock, self._engine.begin() as conn:
                _METADATA.create_all(conn, tables=[SKILLS, SKILL_VERSIONS])
            if self.blob_backend == "s3":
                self._init_s3()
        except Exception as exc:  # 建库/建桶失败不拖垮网关
            logger.warning("技能仓储不可用（%s）：%s", self.url, exc)
            self._engine = None

    def _init_s3(self) -> None:
        if not self._s3_bucket:
            raise RuntimeError("SKILL_BLOB_BACKEND=s3 但未配 SKILL_OSS_BUCKET")
        import boto3  # 懒加载：db 后端不需要装

        kwargs: dict = {}
        endpoint = env("SKILL_OSS_ENDPOINT_URL")
        if endpoint:
            kwargs["endpoint_url"] = endpoint
        region = env("SKILL_OSS_REGION")
        if region:
            kwargs["region_name"] = region
        access = env("SKILL_OSS_ACCESS_KEY")
        secret = env("SKILL_OSS_SECRET_KEY")
        if access and secret:
            kwargs["aws_access_key_id"] = access
            kwargs["aws_secret_access_key"] = secret
        self._s3 = boto3.client("s3", **kwargs)
        self._s3.head_bucket(Bucket=self._s3_bucket)

    @property
    def healthy(self) -> bool:
        return self._engine is not None

    # ------------------------------------------------------------ 本体存取
    def _blob_put(self, key: str, data: bytes) -> tuple[str, str, bytes | None]:
        if self.blob_backend == "s3":
            self._s3.put_object(Bucket=self._s3_bucket, Key=key, Body=data)
            return "s3", key, None
        return "db", "", data

    def _blob_get(self, backend: str, ref: str, inline: bytes | None) -> bytes:
        if backend == "s3":
            obj = self._s3.get_object(Bucket=self._s3_bucket, Key=ref)
            return obj["Body"].read()
        return inline or b""

    # ------------------------------------------------------------ 发布
    def publish(
        self,
        *,
        tenant: str,
        name: str,
        injection: str,
        zip_bytes: bytes,
        manifest: list[dict],
        publisher: str,
        note: str = "",
        activate: bool = True,
    ) -> dict:
        """落一个新版本；技能行不存在则创建（状态 published）。

        activate=True 时把 current_version 拨到新版本。同名技能的
        injection 以最新一次发布为准（声明随包走）。
        """
        if not self.healthy:
            raise RuntimeError("skill store unavailable")
        now = _now()
        digest = hashlib.sha256(zip_bytes).hexdigest()
        with self._lock, self._engine.begin() as conn:
            row = conn.execute(
                select(SKILLS).where(
                    SKILLS.c.tenant == tenant, SKILLS.c.name == name,
                ),
            ).mappings().first()
            if row is None:
                res = conn.execute(
                    SKILLS.insert().values(
                        tenant=tenant,
                        name=name,
                        injection=injection,
                        current_version=None,
                        status="published",
                        publisher=publisher,
                        created_at=now,
                        updated_at=now,
                    ),
                )
                skill_id = res.inserted_primary_key[0]
                version = 1
            else:
                skill_id = row["id"]
                version = (conn.execute(
                    select(SKILL_VERSIONS.c.version).where(
                        SKILL_VERSIONS.c.skill_id == skill_id,
                    ).order_by(SKILL_VERSIONS.c.version.desc()).limit(1),
                ).scalar() or 0) + 1
            key = f"{tenant}/{name}/{version}/package.zip"
            backend, ref, inline = self._blob_put(key, zip_bytes)
            conn.execute(
                SKILL_VERSIONS.insert().values(
                    skill_id=skill_id,
                    version=version,
                    blob_backend=backend,
                    blob_ref=ref,
                    package=inline,
                    manifest=json.dumps(manifest, ensure_ascii=False),
                    sha256=digest,
                    size=len(zip_bytes),
                    note=note[:512],
                    created_by=publisher,
                    created_at=now,
                ),
            )
            updates = {"injection": injection, "updated_at": now}
            if activate:
                updates["current_version"] = version
                updates["status"] = "published"
            conn.execute(
                SKILLS.update().where(SKILLS.c.id == skill_id).values(**updates),
            )
        return {
            "tenant": tenant,
            "name": name,
            "injection": injection,
            "version": version,
            "current_version": version if activate else None,
            "size": len(zip_bytes),
            "sha256": digest,
            "files": len(manifest),
        }

    # ------------------------------------------------------------ 查询
    def _skill_row(self, conn, tenant: str, name: str) -> dict | None:
        row = conn.execute(
            select(SKILLS).where(
                SKILLS.c.tenant == tenant, SKILLS.c.name == name,
            ),
        ).mappings().first()
        return dict(row) if row else None

    def get_skill(self, tenant: str, name: str) -> dict | None:
        if not self.healthy:
            return None
        with self._lock, self._engine.connect() as conn:
            return self._skill_row(conn, tenant, name)

    def list_skills(self, tenants: list[str]) -> list[dict]:
        """给定归属集合（本租户＋global）下全部技能行（含状态）。"""
        if not self.healthy or not tenants:
            return []
        with self._lock, self._engine.connect() as conn:
            rows = conn.execute(
                select(SKILLS).where(SKILLS.c.tenant.in_(tenants)),
            ).mappings().all()
        return [dict(r) for r in rows]

    def get_package(
        self,
        tenant: str,
        name: str,
        version: int | None = None,
    ) -> dict | None:
        """取某个版本的包：{version, zip_bytes, manifest}。

        version=None 取 current_version 指向的版本（指针为空或技能
        不存在返回 None）。
        """
        if not self.healthy:
            return None
        with self._lock, self._engine.connect() as conn:
            skill = self._skill_row(conn, tenant, name)
            if skill is None:
                return None
            ver = version if version is not None else skill["current_version"]
            if ver is None:
                return None
            row = conn.execute(
                select(SKILL_VERSIONS).where(
                    SKILL_VERSIONS.c.skill_id == skill["id"],
                    SKILL_VERSIONS.c.version == ver,
                ),
            ).mappings().first()
            if row is None:
                return None
            row = dict(row)
        data = self._blob_get(row["blob_backend"], row["blob_ref"], row["package"])
        return {
            "version": row["version"],
            "zip_bytes": data,
            "manifest": json.loads(row["manifest"] or "[]"),
        }

    def list_versions(self, tenant: str, name: str) -> list[dict] | None:
        if not self.healthy:
            return None
        with self._lock, self._engine.connect() as conn:
            skill = self._skill_row(conn, tenant, name)
            if skill is None:
                return None
            rows = conn.execute(
                select(
                    SKILL_VERSIONS.c.version,
                    SKILL_VERSIONS.c.size,
                    SKILL_VERSIONS.c.sha256,
                    SKILL_VERSIONS.c.note,
                    SKILL_VERSIONS.c.created_by,
                    SKILL_VERSIONS.c.created_at,
                ).where(SKILL_VERSIONS.c.skill_id == skill["id"])
                .order_by(SKILL_VERSIONS.c.version),
            ).mappings().all()
        return [
            {**dict(r), "current": r["version"] == skill["current_version"]}
            for r in rows
        ]

    # ------------------------------------------------------------ 管理
    def set_current(self, tenant: str, name: str, version: int) -> bool:
        """拨版本指针（激活/回滚）。版本不存在返回 False。"""
        if not self.healthy:
            return False
        with self._lock, self._engine.begin() as conn:
            skill = self._skill_row(conn, tenant, name)
            if skill is None:
                return False
            exists = conn.execute(
                select(SKILL_VERSIONS.c.version).where(
                    SKILL_VERSIONS.c.skill_id == skill["id"],
                    SKILL_VERSIONS.c.version == version,
                ),
            ).first()
            if exists is None:
                return False
            conn.execute(
                SKILLS.update().where(SKILLS.c.id == skill["id"]).values(
                    current_version=version,
                    status="published",
                    updated_at=_now(),
                ),
            )
        return True

    def set_status(self, tenant: str, name: str, status: str) -> bool:
        if not self.healthy or status not in ("published", "disabled"):
            return False
        with self._lock, self._engine.begin() as conn:
            res = conn.execute(
                SKILLS.update().where(
                    SKILLS.c.tenant == tenant, SKILLS.c.name == name,
                ).values(status=status, updated_at=_now()),
            )
        return bool(res.rowcount)


_default_store: SkillStore | None = None
_default_lock = threading.Lock()


def get_skill_store() -> SkillStore:
    """进程级单例（网关多副本各连同一库；副本内共用一个实例）。"""
    global _default_store
    if _default_store is None:
        with _default_lock:
            if _default_store is None:
                _default_store = SkillStore()
    return _default_store
