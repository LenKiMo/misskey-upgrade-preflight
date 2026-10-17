# 切换手册：换镜像（含停机预算与回滚分级）

配套 `preflight.py`（切换前）与 `verify.sh` / `probe.sh`（切换中/切换后）。适用于 Misskey 系的 Docker Compose 部署。

> 全文假设：compose 项目目录 `/srv/misskey`、服务名 `web`、容器名 `misskey-web`、db 容器 `misskey_db`。
> 按自己的实际值替换。

## 0. 为什么「回滚方案」要先看 preflight 的输出

很多升级指南写「出问题就改回旧镜像」，这句话只在**迁移向后兼容**时成立。preflight 会把待执行迁移的 SQL 打出来，据此把回滚分成三档：

| preflight 归类 | 含义 | 回滚方式 |
| --- | --- | --- |
| `ddl` / `index` | 加列、加宽类型、建索引、改约束 | **改回旧镜像即可**：旧版本会忽略新列/更宽的类型/多出来的 migrations 行 |
| `backfill` | `UPDATE` / `INSERT INTO … SELECT` / `DELETE` | 改回旧镜像通常也行，但**数据已被改写**，要不要还原看你是否接受这份改写 |
| `destructive` | `DROP TABLE` / `DROP COLUMN` / `TRUNCATE` | 改回镜像**救不回来**；唯一退路是 `pg_dump` 还原 |

所以「备份必做」不是仪式：它只在第三种情况下真正救命，而 preflight 会在你动手前告诉你会不会落到第三种。

## 1. 切换前（可以提前做，不影响线上）

```bash
cd /srv/misskey
TS=$(date +%Y%m%d-%H%M%S)

# 1.1 配置与 compose 存档
cp -a docker-compose.yml "/root/backups/docker-compose.yml.${TS}"
cp -a .config/default.yml "/root/backups/default.yml.${TS}"

# 1.2 旧镜像钉成命名 tag —— 不要依赖第三方 tag 一直在
docker tag "$(docker inspect -f '{{.Config.Image}}' misskey-web)" "misskey:rollback-${TS}"

# 1.3 逻辑备份（库越大越慢；先看 pg_dump 能否在容器里跑通）
docker exec misskey_db pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB" > "/root/backups/misskey-${TS}.dump"
sha256sum "/root/backups/misskey-${TS}.dump"

# 1.4 只读预检（候选镜像）
./preflight.py --project . --image misskey:<新 tag>

# 1.5 冒烟：候选镜像能否用现网配置连上 PG/Redis（preflight 已含这一步；也可单独跑）
docker run --rm --network "$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}' misskey-web | head -c 40)" \
  --env-file .config/docker.env -v "$PWD/.config:/misskey/.config:ro" \
  misskey:<新 tag> pnpm check:connect; echo "exit=$?"
```

`preflight` 报 `pending 0` 的镜像，启动时不会动库结构；报 `pending N` 就把 SQL 逐条看一眼，确认没有落在 `destructive`。

## 2. 切换（只重建 web，db/redis 不动）

```bash
cd /srv/misskey

# 2.1 起探针（另开一个会话，或后面接 nohup/systemd-run）
./probe.sh misskey-web 3000 /healthz 2 600 &

# 2.2 换 image 行：先把新 compose 写到临时文件、校验、再原子替换
install -m 644 docker-compose.yml.new docker-compose.yml
docker compose config >/dev/null && echo "compose OK"

# 2.3 只重建 web
docker compose up -d --no-deps web

# 2.4 盯日志：迁移会在这期间跑完
docker logs -f --tail 50 misskey-web
```

要点：

- **`--no-deps`**：不要把 db/redis 一起重建。它们没有变化，重建只会制造额外的连接中断与 IP 变更。
- **迁移是前向的**：`migrateandstart` 在容器启动时先 `migration:run` 再启动服务。所以「容器起来了」不等于「已经服务」，`/healthz` 在启动期会返回 503 —— 探针把这段单独统计就是为此。
- 想再压短停机：切之前用候选镜像跑一次 one-off 迁移（旧版本继续服务），再重建容器：

```bash
docker compose run --rm --no-deps web pnpm migrate
```

  这招的前提同样是 preflight 显示迁移向后兼容（否则旧版本会在「新 schema + 老代码」下出错）。

## 3. 切换后

```bash
./verify.sh misskey-web misskey_db <期望迁移条数>
```

验收要点（`verify.sh` 全做了）：

1. 容器健康状态、镜像 ID、启动时间；
2. 走**容器 IP** 直连 `/healthz`（绕开反代/CDN，避免缓存或白名单把故障测成正常）；
3. `POST /api/meta` 的 `version` 是新版本；
4. `migrations` 表条数 = 旧条数 + preflight 报的待执行数（这是硬证据）；
5. 日志里没有新增的 ERROR。

再人工点一遍：登录、时间线、发一条私密帖、搜索、通知、上传。搜索特别值得试——Misskey 系 fork 常在全文检索上做改动（`fulltextSearch.provider` 与之配套的索引）。

## 4. 回滚

```bash
cd /srv/misskey
# 把 image 行改回 1.2 里钉的 rollback tag，然后：
docker compose up -d --no-deps web
docker logs -f --tail 50 misskey-web
```

- 一、二档（`ddl`/`index`/`backfill`）：**不需要还原数据库**。旧版本对多出来的列/更宽 varchar/多出的迁移行是宽容的。
- 第三档（`destructive`）：还原备份，`pg_restore -c --if-exists`。这会把切换后产生的数据一起丢掉，先确认能接受。

## 5. 停机数字的读法

`probe.sh` 的汇总会给出两段：

```
  失败采样 19 次 × 2s ≈ 38s 不可用
  容器空缺起点：2026-01-01 00:52:32
  应用未就绪起点：2026-01-01 00:53:03
  恢复 200：2026-01-01 00:53:14
```

- **容器空缺**那段 = `docker compose` 删旧建新的时间；
- **应用未就绪**那段 = 迁移 + 应用启动（`/healthz` 503）。

一个中等规模实例（60 万行 note、库 9.6 GB）跑 3 条 DDL 迁移的实测：`compose up` 12 秒，总不可用 **39 秒**（空缺 32 s + 未就绪 7 s）。你的数字会随库大小、迁移内容、磁盘性能变化——所以**每次都量**，别沿用别人的数字。
