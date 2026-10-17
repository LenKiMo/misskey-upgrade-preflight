#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
preflight.py —— Misskey 系（Misskey / Sharkey 及其 fork）实例升级前的只读预检。

它回答三个问题，全程不碰运行中的容器、不执行迁移：
  1. 这个候选镜像启动时会执行哪些数据库迁移？（TypeORM 的权威答案，不是靠文件数猜）
  2. 每条迁移到底跑什么 SQL？（静态提取：把 up() 对着一个记录桩跑一遍，不连库）
  3. 它能不能用现网配置连上 PostgreSQL / Redis？镜像里是什么版本？

依赖：python3(>=3.8) + docker + docker compose v2。除 docker 外只用标准库。

用法见 --help 或 README。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap

MARK = "##PREFLIGHT_JSON##"

# 静态提取探针：在候选镜像里跑，只 import 迁移类并对着记录桩调用 up()。
# 唯一可能的副作用来自 TypeORM 自身：getPendingMigrations() 在 migrations 表
# 不存在的老版本 TypeORM 上会建一张空表（typeorm#10858，2026-01 已由 #11672 修复）。
PROBE_TEMPLATE = r"""
import { MigrationExecutor } from 'typeorm';

const ORM = __ORM_PATH__;
const out = { ok: false };
try {
  const mod = await import(ORM);
  const ds = mod.default;
  await ds.initialize();

  // 已应用条数（表不存在会抛，用来区分「库没初始化」）
  try {
    const r = await ds.query('select count(*)::int as n from migrations');
    out.applied = r[0].n;
  } catch (e) {
    out.applied = null;
    out.appliedError = String((e && e.message) || e);
  }

  const pending = await new MigrationExecutor(ds).getPendingMigrations();
  out.pending = [];

  // 注意：不同 TypeORM 版本这里返回**迁移实例**或**迁移类**，两种都要能处理
  const fs = await import('node:fs/promises');
  const migDir = new URL('./migration/', import.meta.url);  // 相对本探针（放在 packages/backend/ 下）

  async function sqlFromSource(name) {
    // 回退：在迁移源码里找 class <name>，正则提取 query(...) 的字符串参数
    try {
      const files = await fs.readdir(migDir);
      for (const f of files) {
        if (!f.endsWith('.js')) continue;
        const src = await fs.readFile(new URL(f, migDir), 'utf8');
        if (!src.includes(name)) continue;
        const out = [];
        const re = /\.query\(\s*(`|'|")([\s\S]*?)\1\s*[,)]/g;
        let m2;
        while ((m2 = re.exec(src)) !== null) out.push(m2[2].replace(/\s+/g, ' ').trim());
        if (out.length) return out;
      }
    } catch { /* ignore */ }
    return null;
  }

  for (const m of pending) {
    // TypeORM 0.3.x 返回包装对象 {id,timestamp,name,instance,transaction}；
    // 老版本/其它分支直接返回实例或类，三种形态都兼容。
    const inst = m.instance ?? m;
    const name = m.name ?? inst.name ?? inst.constructor?.name ?? 'unknown';
    const item = { name, sql: [], schemaApi: [], note: null };
    try {
      if (typeof inst.up !== 'function') throw new Error('迁移对象上没有 up()');
      // 记录桩：query() 抓 SQL，其它方法记名字（TypeORM Schema API 无法还原成 SQL）
      const calls = [];
      const rec = new Proxy({}, {
        get(_t, prop) {
          if (prop === 'then' || typeof prop === 'symbol') return undefined;
          return async (...args) => { calls.push([String(prop), args]); };
        },
      });
      await inst.up(rec);
      for (const [fn, args] of calls) {
        if (fn === 'query') {
          item.sql.push(String(args[0]).replace(/\s+/g, ' ').trim());
        } else {
          item.schemaApi.push(fn);
        }
      }
    } catch (e) {
      item.note = 'up() 直接调用失败（' + String((e && e.message) || e) + '），改用源码回退提取';
      const fb = await sqlFromSource(name);
      if (fb && fb.length) item.sql = fb;
      else item.note += '；源码回退也没提取到 SQL';
    }
    out.pending.push(item);
  }

  out.ok = true;
  await ds.destroy();
} catch (e) {
  out.error = String((e && e.message) || e);
}
console.log('__MARK__' + JSON.stringify(out));
""".replace("__MARK__", MARK)


def die(msg, code=2):
    print("preflight: " + msg, file=sys.stderr)
    sys.exit(code)


def run(cmd, **kw):
    """跑命令，返回 (rc, stdout, stderr)。"""
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    return p.returncode, p.stdout, p.stderr


# ---------------------------------------------------------------- compose 探测

def compose_config(project_dir, compose_files):
    """用 `docker compose config --format json` 拿到「实际生效」的配置（路径已解析为绝对）。"""
    cmd = ["docker", "compose", "--project-directory", project_dir]
    for f in compose_files:
        cmd += ["-f", f]
    cmd += ["config", "--format", "json"]
    rc, out, err = run(cmd)
    if rc != 0:
        # 老版本 compose 支持 `--format json` 之前只有 --json；再退回 YAML 就不解析了，直接报错
        if "unknown flag" in (err or "") or "not a docker command" in (err or ""):
            die("需要 docker compose v2（`docker compose config --format json` 不可用）。原始错误：\n" + err, 2)
        die("compose 配置解析失败（先自己跑一遍看问题）：\n" + (err or out), 2)
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        die("docker compose config 没有返回 JSON，Compose 版本可能过旧。", 2)


def detect_compose_files(project_dir):
    names = ["docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"]
    found = [os.path.join(project_dir, n) for n in names if os.path.isfile(os.path.join(project_dir, n))]
    if not found:
        die("目录里找不到 compose 文件（docker-compose.yml / compose.yaml …），用 -f 显式指定。", 2)
    return found[:1]


def pick_service(cfg, wanted):
    services = cfg.get("services") or {}
    if not services:
        die("compose 里没有 services。", 2)
    if wanted:
        if wanted not in services:
            die(f"compose 里没有服务 {wanted!r}（有：{', '.join(services)}）", 2)
        return wanted, services[wanted]
    # 自动挑：有 .config 类挂载 + 有 image/env_file 的那个
    best = None
    for name, svc in services.items():
        mounts = [v for v in (svc.get("volumes") or []) if isinstance(v, dict) and v.get("type") == "bind"]
        hit = any(str(v.get("target", "")).rstrip("/").endswith("/.config") for v in mounts)
        score = (2 if hit else 0) + (1 if svc.get("env_file") else 0)
        if hit and (best is None or score > best[0]):
            best = (score, name, svc)
    if best is None:
        die("自动识别失败：没有哪个服务挂了 Misskey 的 .config 目录，请用 --service 指定。", 2)
    return best[1], best[2]


def network_names(cfg, svc):
    """把服务的 networks 键映射成真实 docker 网络名。"""
    top = cfg.get("networks") or {}
    out = []
    nets = svc.get("networks") or {}
    keys = list(nets.keys()) if isinstance(nets, dict) else list(nets)
    for k in keys:
        spec = top.get(k) or {}
        out.append(spec.get("name") or f"{cfg.get('name', '')}_{k}".lstrip("_"))
    return out


def find_app_root(svc):
    """从 .config 的挂载目标反推应用根目录（Misskey 约定 <app>/.config/default.yml）。"""
    for v in svc.get("volumes") or []:
        if not isinstance(v, dict):
            continue
        tgt = str(v.get("target", ""))
        if tgt.rstrip("/").endswith("/.config"):
            return tgt.rstrip("/")[: -len("/.config")], v
    return None, None


# ---------------------------------------------------------------- 探针执行

def build_env_file(svc, tmpdir):
    """把 compose 解析后的 environment 写进 0600 临时文件（避免密钥出现在命令行/ps）。"""
    env = svc.get("environment") or {}
    if not env:
        return None
    pairs = []
    if isinstance(env, dict):
        pairs = [(k, v) for k, v in env.items() if v is not None]
    else:  # 老格式：["KEY=value", ...]
        for item in env:
            if isinstance(item, str) and "=" in item:
                k, v = item.split("=", 1)
                pairs.append((k.strip(), v))
    if not pairs:
        return None
    path = os.path.join(tmpdir, "probe.env")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        for k, v in pairs:
            fh.write(f"{k}={v}\n")
    return path


def probe(image, svc, cfg, network, app_root, connect_timeout):
    tmpdir = tempfile.mkdtemp(prefix="preflight-")
    try:
        orm_rel = "packages/backend/ormconfig.js"
        probe_path = os.path.join(tmpdir, "preflight-probe.mjs")
        with open(probe_path, "w") as fh:
            fh.write(PROBE_TEMPLATE.replace("__ORM_PATH__", json.dumps(f"{app_root}/{orm_rel}")))
        os.chmod(probe_path, 0o644)

        cmd = ["docker", "run", "--rm", "--network", network,
               "--entrypoint", "sh",
               "-v", f"{probe_path}:{app_root}/{orm_rel.rsplit('/', 1)[0]}/.preflight-probe.mjs:ro",
               "--label", "misskey-preflight=1"]

        for ef in (svc.get("env_file") or []):
            p = ef.get("path") if isinstance(ef, dict) else ef
            if p:
                cmd += ["--env-file", p]
        envf = build_env_file(svc, tmpdir)
        if envf:
            cmd += ["--env-file", envf]

        for v in svc.get("volumes") or []:
            if isinstance(v, dict) and v.get("type") == "bind" and v.get("source") and v.get("target"):
                # 预检一律只读挂载：探针不该写任何东西
                cmd += ["-v", f"{v['source']}:{v['target']}:ro"]

        if connect_timeout:
            cmd += ["-e", "MK_PREFLIGHT_CONNECT_TIMEOUT=1"]

        cmd += [image, "-c", f"cd {app_root}/{orm_rel.rsplit('/', 1)[0]} && node .preflight-probe.mjs"]
        rc, out, err = run(cmd)
        payload = None
        for line in (out or "").splitlines():
            if line.startswith(MARK):
                try:
                    payload = json.loads(line[len(MARK):])
                except json.JSONDecodeError:
                    pass
        return rc, out, err, payload
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def check_connect(image, svc, cfg, network, app_root):
    """镜像里若有 Misskey 的 check_connect.js（真实连 PG + 全部 Redis），跑一次；无输出 rc=0 即通过。"""
    script = f"{app_root}/packages/backend/scripts/check_connect.js"
    cmd = ["docker", "run", "--rm", "--network", network, "--entrypoint", "sh", "--label", "misskey-preflight=1"]
    for ef in (svc.get("env_file") or []):
        p = ef.get("path") if isinstance(ef, dict) else ef
        if p:
            cmd += ["--env-file", p]
    for v in svc.get("volumes") or []:
        if isinstance(v, dict) and v.get("type") == "bind" and v.get("source") and v.get("target"):
            cmd += ["-v", f"{v['source']}:{v['target']}:ro"]
    cmd += [image, "-c", f"test -f {script} && cd {app_root}/packages/backend && node scripts/check_connect.js; ec=$?; "
                         f"[ $ec -eq 127 ] && exit 99; exit $ec"]
    rc, out, err = run(cmd)
    return rc, out, err


# ---------------------------------------------------------------- 风险启发式

def classify(sql_list):
    """对迁移 SQL 做粗分类（启发式，仅用于提示，不替代人读）。

    按**语句首关键字**判断，不要在整个字符串里搜关键字：`ON DELETE SET NULL` /
    `ON UPDATE NO ACTION` 这类外键子句会把 DDL 误判成数据回填。
    """
    if not sql_list:
        return "unknown", "SQL 未提取到，请人工查看该迁移文件"
    kinds = set()
    for raw in sql_list:
        s = raw.strip().lower().lstrip("(")
        s = " ".join(s.split())
        if not s:
            continue
        head2 = " ".join(s.split()[:2])
        head1 = s.split()[0] if s.split() else ""
        if head1 == "drop" or head1 == "truncate":
            kinds.add("destructive")
        elif head2.startswith("alter table") and re.search(r"\bdrop\s+(column|constraint)\b", s):
            kinds.add("destructive" if "drop column" in s else "ddl")
        elif head1 in ("update", "delete") or head2.startswith("insert into"):
            kinds.add("backfill")
        elif "index concurrently" in head2 + " " + " ".join(s.split()[:4]):
            kinds.add("index")
        elif head1 in ("create", "alter", "comment", "set", "analyze", "reindex", "vacuum"):
            kinds.add("ddl")
        else:
            kinds.add("ok")
    if "destructive" in kinds:
        return "destructive", "含破坏性语句（DROP/TRUNCATE）→ 回滚不能再靠「改回镜像」"
    if "backfill" in kinds:
        return "backfill", "含数据回填/改写（UPDATE/INSERT/DELETE 语句）→ 可能扫全表，注意锁与时长"
    if "index" in kinds:
        return "index", "并发建索引（不阻塞读写，但耗时）"
    if "ddl" in kinds:
        return "ddl", "DDL（多数情况下秒级；加宽 varchar 只改元数据，不重写表）"
    return "ok", "未发现高风险语句"


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(
        description="Misskey 系实例升级前的只读预检：列出候选镜像待执行的迁移及其 SQL，并验证连通性。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            示例：
              %(prog)s --project /srv/sharkey --image sharkey:2025.5.2-abc1234
              %(prog)s -d /srv/sharkey -f docker-compose.yml -f watchtower-exclude.yml \\
                        --service web --image sharkey:latest --json

            只读声明：本工具不执行迁移、不写业务表；唯一可能的副作用是旧版 TypeORM 的
            getPendingMigrations() 在 migrations 表不存在时会建一张空表（typeorm#10858，已于 2026-01 修复）。
            """))
    ap.add_argument("--project", "-d", default=".", help="compose 项目目录（默认当前目录）")
    ap.add_argument("-f", "--compose", action="append", default=[], help="compose 文件（可重复）")
    ap.add_argument("--service", "-s", help="要检查的服务名（默认自动识别）")
    ap.add_argument("--image", "-i", required=True, help="候选镜像 tag")
    ap.add_argument("--network", help="覆盖自动识别的网络名")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    ap.add_argument("--no-sql", action="store_true", help="不打印迁移 SQL（只看条数）")
    ap.add_argument("--no-connect", action="store_true", help="跳过 PG/Redis 连通性检查")
    args = ap.parse_args()

    project_dir = os.path.abspath(args.project)
    if not os.path.isdir(project_dir):
        die(f"目录不存在：{project_dir}")
    compose_files = [os.path.abspath(f) for f in args.compose] or detect_compose_files(project_dir)

    cfg = compose_config(project_dir, compose_files)
    svc_name, svc = pick_service(cfg, args.service)
    app_root, cfg_mount = find_app_root(svc)
    if not app_root:
        die(f"服务 {svc_name} 没有挂载 .config 目录，看起来不是 Misskey 系的部署。", 3)

    nets = network_names(cfg, svc)
    network = args.network or (nets[0] if nets else "host")

    rc, out, err, payload = probe(args.image, svc, cfg, network, app_root, not args.no_connect)
    if payload is None:
        print(out or "", end="")
        print(err or "", end="", file=sys.stderr)
        die(f"探针在候选镜像里没能跑起来（rc={rc}）。常见原因：镜像不是 Misskey 系、"
            f"{app_root}/packages/backend/ormconfig.js 不存在、或镜像里没有 node。", 3)
    if not payload.get("ok"):
        die("探针失败：" + str(payload.get("error")), 3)

    pending = payload.get("pending") or []
    report = {
        "image": args.image,
        "project": project_dir,
        "compose_files": compose_files,
        "service": svc_name,
        "network": network,
        "app_root": app_root,
        "config_source": (cfg_mount or {}).get("source"),
        "migrations_applied": payload.get("applied"),
        "migrations_pending": len(pending),
        "pending": pending,
        "connect": None,
    }

    connect_rc = None
    if not args.no_connect:
        connect_rc, c_out, c_err = check_connect(args.image, svc, cfg, network, app_root)
        report["connect"] = {"rc": connect_rc,
                            "skipped": connect_rc == 99,
                            "detail": (c_err or c_out or "").strip()[-800:]}

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print(f"== 候选镜像 {args.image}")
        print(f"   项目     {project_dir}（服务 {svc_name}，网络 {network}）")
        print(f"   应用根   {app_root}   配置源 {(cfg_mount or {}).get('source')}")
        applied = report["migrations_applied"]
        print(f"   已应用   {applied if applied is not None else '未知（migrations 表不存在？）'}   待执行 {len(pending)}")
        if connect_rc is None:
            print("   连通性   跳过")
        elif connect_rc == 99:
            print("   连通性   跳过（镜像里没有 check_connect.js）")
        elif connect_rc == 0:
            print("   连通性   OK（PG + Redis 均已连上）")
        else:
            print(f"   连通性   ✗ 失败 rc={connect_rc}")

        if pending:
            print("\n-- 待执行迁移（候选镜像启动时会按此顺序跑）")
            for i, m in enumerate(pending, 1):
                print(f"   {i}. {m['name']}")
                if args.no_sql:
                    continue
                for s in m.get("sql") or []:
                    print("      " + (s[:300] + ("…" if len(s) > 300 else "")))
                if m.get("schemaApi"):
                    print(f"      [Schema API] {', '.join(sorted(set(m['schemaApi'])))} —— 无法静态还原 SQL")
                if m.get("note"):
                    print("      " + m["note"])
            verdicts = [classify((m.get("sql") or [])) for m in pending]
            worst = [v for v in verdicts if v[0] != "ok"]
            print("\n-- 归类（启发式，仅供参考）")
            for m, (kind, why) in zip(pending, verdicts):
                print(f"   [{kind}] {m['name']}: {why}")
            if any(k == "destructive" for k, _ in verdicts):
                print("\n   判定：含破坏性迁移 —— 先做逻辑备份，回滚要走「还原数据库」而不是「改回镜像」。")
            elif any(k == "backfill" for k, _ in verdicts):
                print("\n   判定：含数据回填 —— 备份必做，建议真窗口执行并盯锁等待。")
            else:
                print("\n   判定：均为 DDL/索引类，备份后可直接切；停机 ≈ 容器重建 + 迁移 + 启动。")
        else:
            print("\n-- 没有待执行迁移：这个镜像启动时不需要动库结构。")

    if connect_rc not in (None, 0, 99):
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
