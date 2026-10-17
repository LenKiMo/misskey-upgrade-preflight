# misskey-upgrade-preflight

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.8%2B-3776AB?logo=python&logoColor=white)
![Docker Compose](https://img.shields.io/badge/docker%20compose-v2-2496ED?logo=docker&logoColor=white)
![Tested on](https://img.shields.io/badge/tested%20on-Misskey%20%2F%20Sharkey-8A2BE2)
![Read-only](https://img.shields.io/badge/read--only-no%20migration%20executed-success)

**升级 Misskey 系实例前，先让候选镜像自己交代：它启动时会跑哪些数据库迁移、每条迁移的 SQL 是什么。**
不启动服务、不执行迁移、不写业务表——一条命令、几秒钟。

- <a href="#why">🎯 为什么需要这个</a>
- <a href="#what">🔍 它到底做什么</a>
- <a href="#quickstart">🚀 快速开始</a>
- <a href="#demo">📋 输出长什么样（真实输出）</a>
- <a href="#rules">⚖️ 判定规则与「只读」的准确边界</a>
- <a href="#scope">🧩 适用范围（已逐个核实）</a>
- <a href="#limits">⚠️ 已知限制</a>
- <a href="#companion">🩺 配套的切换/验收脚本</a>
- <a href="#faq">❓ FAQ</a>

---

## <a id="why"></a>🎯 为什么需要这个

自托管 Misskey / Sharkey 的升级是「`docker compose pull && up -d`」，迁移在容器启动时由 `migrateandstart` 自动跑。问题在于：

1. **你看不见它要跑什么**。库结构变更可能与版本号毫无关系——两个版本之间可能夹着一条扫全表的 `UPDATE`；
2. **出问题的时间点最坏**。迁移失败时服务已经停了，而「回滚」是否有用取决于迁移是否向后兼容；
3. **官方路径没有这一环**。`typeorm migration:show` 能列出待执行迁移，但不会告诉你每条迁移跑什么 SQL、镜像能不能连上现网的库。

这个工具把这一段补上：**在切换之前**，用候选镜像自己、以只读方式，回答上面的问题。

## <a id="what"></a>🔍 它到底做什么

| 步骤 | 做法 | 结果 |
| --- | --- | --- |
| 1. 读你的 compose | `docker compose config --format json` 拿到**实际生效**的配置（服务、网络、`.config` 挂载、env_file） | 不用你手抄路径 |
| 2. 在候选镜像里跑探针 | 一次性容器，接进同一个 compose 网络，只读挂载配置目录，`import` 该镜像自带的 `ormconfig.js` | TypeORM 用**类名**判定，这是权威答案，不是靠数文件猜 |
| 3. 提取每条迁移的 SQL | 把待执行迁移的 `up()` 对着一个**记录桩**跑一遍（`query()` 被替换成记录器，不连库） | 直接看到 `ALTER TABLE …` 原文；失败则回退到源码正则提取 |
| 4. 连通性 + 版本 | 有 `check_connect.js` 就跑它（真连 PG + 全部 Redis），并读 `package.json` / `node -v` | 提前发现「镜像读不到配置」「连不上 db」这类低级但致命的问题 |

**它不会做的事**：不执行 `migration:run`、不启动服务、不改任何文件、不碰运行中的容器。所有容器都是一次性 `--rm`，挂载一律 `:ro`。

## <a id="quickstart"></a>🚀 快速开始

要求：`python3 ≥ 3.8`（只用标准库）、`docker` + `docker compose v2`、对 docker 有权限的账号。

```bash
git clone https://github.com/<you>/misskey-upgrade-preflight.git
cd misskey-upgrade-preflight
chmod +x preflight.py verify.sh probe.sh

# 最简：在 compose 项目目录里跑，服务名自动识别
./preflight.py --project /srv/misskey --image misskey:2025.5.2-abc1234

# 有 overlay compose 文件（很常见：watchtower 排除标签之类）就都传进去
./preflight.py -d /srv/misskey \
    -f docker-compose.yml -f watchtower-exclude.yml \
    --service web --image misskey:2025.5.2-abc1234

# 给 CI / 脚本用
./preflight.py -d /srv/misskey --image misskey:new --json
```

退出码：`0` 预检完成（不管有没有待执行迁移）｜`1` 连通性检查失败｜`2` 用法/compose 错误｜`3` 镜像不是 Misskey 系或探针跑不起来。

## <a id="demo"></a>📋 输出长什么样（真实输出）

一个真实的例子：库已应用 417 条迁移，候选镜像自带 420 条。

```text
$ ./preflight.py --project . --image misskey:2025.5.2-dev-2ca6365c1c
== 候选镜像 misskey:2025.5.2-dev-2ca6365c1c
   项目     /srv/misskey（服务 web，网络 misskey_default）
   应用根   /misskey   配置源 /srv/misskey/.config
   已应用   417   待执行 3
   连通性   OK（PG + Redis 均已连上）

-- 待执行迁移（候选镜像启动时会按此顺序跑）
   1. FixProfileImageUrls1770023088000
      ALTER TABLE "user" ALTER COLUMN "avatarUrl" TYPE character varying(1024)
      ALTER TABLE "user" ALTER COLUMN "bannerUrl" TYPE character varying(1024)
      ALTER TABLE "user" ALTER COLUMN "backgroundUrl" TYPE character varying(1024)
   2. AddUserDeletedAt1774625336796
      ALTER TABLE "user" ADD "deletedAt" TIMESTAMP WITH TIME ZONE
      COMMENT ON COLUMN "user"."deletedAt" IS 'When the account was deleted.'
   3. KeepPageWhenDeletingThumbnail1774696127765
      ALTER TABLE "page" DROP CONSTRAINT "FK_a9ca79ad939bf06066b81c9d3aa"
      ALTER TABLE "page" ADD CONSTRAINT "FK_a9ca79ad939bf06066b81c9d3aa" FOREIGN KEY ("eyeCatchingImageId") REFERENCES "drive_file"("id") ON DELETE SET NULL ON UPDATE NO ACTION

-- 归类（启发式，仅供参考）
   [ddl] FixProfileImageUrls1770023088000: DDL（多数情况下秒级；加宽 varchar 只改元数据，不重写表）
   [ddl] AddUserDeletedAt1774625336796: DDL（多数情况下秒级；加宽 varchar 只改元数据，不重写表）
   [ddl] KeepPageWhenDeletingThumbnail1774696127765: DDL（多数情况下秒级；加宽 varchar 只改元数据，不重写表）

   判定：均为 DDL/索引类，备份后可直接切；停机 ≈ 容器重建 + 迁移 + 启动。
```

于是「这次升级能不能接受」变成一个可读的答案：3 条 DDL、没有数据回填、连通性 OK。配合 `probe.sh` 量的实际停机（这个实例实测 39 秒），才谈得上「准无缝」。

## <a id="rules"></a>⚖️ 判定规则与「只读」的准确边界

**归类是启发式**，规则按**语句首关键字**判断，输出仅供参考，不替代人读：

| 归类 | 触发 | 提示 |
| --- | --- | --- |
| `destructive` | `DROP TABLE` / `DROP COLUMN` / `TRUNCATE` | 回滚不能只改回镜像，必须能还原备份 |
| `backfill` | 语句以 `UPDATE` / `INSERT INTO` / `DELETE` 开头 | 可能扫全表：注意锁、时长、备份 |
| `index` | `CREATE INDEX CONCURRENTLY` | 不阻塞读写，但耗时 |
| `ddl` | `ALTER TABLE` / `CREATE` / `COMMENT` / `REINDEX` 等 | 多数秒级；加宽 varchar 只改元数据 |
| `unknown` | 没提取到 SQL | 请人工看迁移文件 |

**关于「只读」的准确说法**（重要，别被徽章骗了）：

- 探针**不会**执行任何迁移，也**不会**写业务表——`up()` 跑在一个把 `query()` 换成记录器的桩上。
- 唯一可能的副作用来自 TypeORM 自身：`getPendingMigrations()` 在 `migrations` 表**不存在**时会创建一张空表（[typeorm#10858](https://github.com/typeorm/typeorm/issues/10858)，已由 [PR #11672](https://github.com/typeorm/typeorm/pull/11672) 在 2026-01 修复并合并）。老版本 TypeORM 才有这个行为，且代价是「多一张空表」。
- 探针容器与会话里不会打印任何密钥：`environment` 会写进 0600 的临时 env 文件再 `--env-file` 传入，不出现在命令行/`ps` 里。

## <a id="scope"></a>🧩 适用范围（已逐个核实）

前置假设：**TypeORM 后端 + Misskey 的目录约定**（`<app>/packages/backend/ormconfig.js`、`<app>/.config/default.yml`）。满足这条的 fork 都能用。

| 项目 | 状态（核实于 2026-10） | 本工具 |
| --- | --- | --- |
| **Misskey** | 活跃（上游最近提交 2026-10；11k+ stars） | ✅ 原生目标 |
| **Sharkey** | 活跃（joinsharkey.org 有在任维护者；上游托管在 activitypub.software，不在 GitHub） | ✅ 已实测（含它的中文 fork） |
| **其 TypeORM 系 fork** | 跟随上游 develop 的派生 | ✅ 大概率可用（路径相同），未逐个实测 |
| **Firefish** | ❌ 已停止开发：维护者 naskya 于 2024-09-05 公告「进入 maintenance mode，年底 EOL」 | 技术上能跑（同样是 TypeORM），但**不建议**再用它开局 |
| **Iceshrimp.NET** | 活跃（v2026.1-beta，2026-04） | ❌ **不适用**：它是 .NET/EF Core 重写，没有 `ormconfig.js`，也不是 TypeORM |

> 写这份表时特意核对过：Firefish 已 EOL（不要照抄「Firefish/Calckey 系」这种旧清单），Iceshrimp.NET 虽然活跃但技术栈完全不同。其余还在维护的 fork 请以各自仓库的最近提交为准。

## <a id="limits"></a>⚠️ 已知限制

1. **需要 compose v2**（依赖 `docker compose config --format json`）。v1 的 `docker-compose` 不支持，会直接报错退出。
2. **镜像内的非 root 用户要能读 `.config`**。实测踩过一次：宿主上 `chmod 600` 且属主是 root 时，容器里 uid 991 读不到，报 `EACCES … /<app>/.config/default.yml`。修法：把配置目录/文件属主改成镜像内 UID（`chown -R 991:991`）或用 644。
3. **SQL 提取不是万能的**。迁移若用 TypeORM 的 Schema API（`queryRunner.createTable` 等）而非裸 SQL，工具只能列出方法名；`up()` 里如果有依赖查询结果的分支，静态调用会失败并回退到源码正则——两条路都失败时标 `unknown`，请人工看文件。
4. **判定是启发式**，不解析 SQL 语义。「一条 `ALTER TABLE … ADD COLUMN … DEFAULT` 在几千万行表上也能锁很久」这种情况，工具看不出来。
5. **只在 Linux 上测过**（`docker` + bash 环境）。Windows/macOS 上跑 docker CLI 理论上可行，未验证。

## <a id="companion"></a>🩺 配套的切换/验收脚本

| 文件 | 作用 |
| --- | --- |
| [`docs/cutover.md`](docs/cutover.md) | 切换手册：备份 → 切换 → 验收 → 回滚，含**回滚分级**（哪些迁移能靠改回镜像、哪些必须还原库） |
| `probe.sh` | 量停机的探针：每 2 秒一次、**每轮重取容器 IP**（重建会换 IP），并把「容器空缺」（000）与「应用未就绪」（5xx）分开统计 |
| `verify.sh` | 切换后验收：容器/镜像/健康、走容器 IP 直连的 `/healthz`、`/api/meta` 版本、`migrations` 条数、日志错误 |

典型的一轮：

```bash
./preflight.py -d /srv/misskey --image misskey:new        # 事前：要跑什么
./probe.sh misskey-web 3000 /healthz 2 600 &              # 事中：停了多久
docker compose up -d --no-deps web
./verify.sh misskey-web misskey_db 420                    # 事后：真的成了
```

## <a id="faq"></a>❓ FAQ

**只有 `typeorm migration:show` 不就够了？**
够看「有没有」，不够看「是什么」。本工具补的是 SQL 预览、风险归类、连通性验证，以及「一条命令把 compose 项目实际配置带进来」。

**为什么不用 `migration:run --dry-run`？**
TypeORM 没有真正的 dry-run；能「预览」的只有生成 SQL 的模式，而迁移是手写 `up()`，不执行就不知道会跑什么。对着桩执行 `up()` 是能同时做到「拿到 SQL」和「绝不连库」的两全做法。

**会不会把生产库搞坏？**
不会执行迁移、不写业务表。风险只有一条：老版本 TypeORM 可能建一张空的 `migrations` 表（见上文）。另外它**只读**访问库（`select count(*)` 与迁移元数据）。

**为什么必须接进 compose 网络？**
因为配置里的 `host: db` / `host: redis` 是 compose 内的服务名，探针容器必须能解析它们。工具会从 `docker compose config` 自动取网络名。

## 📄 License / AIGC

MIT，见 [LICENSE](LICENSE)。

🤖 **AIGC 声明**：本项目的代码、文档与示例输出在 AI 辅助下完成，经人工审阅、并在真实实例上逐项实测（包括上面那段输出）。所有结论都以可复现的命令与输出为依据；如果你跑出不一样的结果，那大概率是我的环境与你的不同——欢迎开 issue 带上 `--json` 的输出。
