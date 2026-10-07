# local-object-storage

仅依赖**本地文件系统**的对象存储服务（FastAPI）。支持分段上传与断点续传、对象
版本管理（历史版本保留策略、按版本清理与恢复）、命名空间级访问授权与存储配额
（含在途字节计费），并对上传中断、重复提交与并发访问提供确定、可解释的结果。

无外部数据库、无 Redis：数据全部落在 `OBJECTSTORAGE_ROOT`（默认 `./.objectstore`）
目录下，所有元数据写入均为「临时文件 + `os.replace`」原子替换，并追加 JSONL 审计
日志。

> 并发模型：单进程部署（FastAPI 同步路由），进程内以一把 `RLock` 串行化所有
> “读—判定—写”序列。因此覆盖胜负确定、配额不会被并发突破。多 worker/多进程
> 部署不在本实现的支持范围内。

## 目录布局

```
<root>/
  namespaces.json                 # 命名空间注册表（令牌、配额、已用字节）
  audit.log                       # 追加式 JSONL 审计日志
  ns/<namespace>/
    objects/<urlquoted-name>.json # 单对象元数据：版本链 + 删除标记
    blobs/<sha256>                # 内容寻址的不可变对象数据
    uploads/<session_id>/
      session.json                # 会话状态（open/completed/aborted/expired）
      parts/<seq>                 # 已接收分段
```

## 快速开始

```bash
uv sync
OBJECTSTORAGE_ROOT=./.objectstore uv run uvicorn main:app --port 8000
# 健康检查
curl -s localhost:8000/health
```

所有业务接口以 `/namespaces/{namespace}/...` 为前缀，令牌通过
`X-Namespace-Token` 请求头传递（命名空间创建时登记的令牌之一）。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/namespaces` | 创建命名空间（配额、令牌、历史版本保留策略） |
| GET | `/namespaces/{ns}` | 配额用量（`used_bytes` + `inflight_bytes`） |
| POST | `/namespaces/{ns}/uploads` | 创建分段上传会话 |
| GET | `/namespaces/{ns}/uploads/{sid}` | 查询会话状态（含已接收字节） |
| PUT | `/namespaces/{ns}/uploads/{sid}/parts/{seq}` | 提交分段（在途计费点） |
| POST | `/namespaces/{ns}/uploads/{sid}/complete` | 完成上传 |
| POST | `/namespaces/{ns}/uploads/{sid}/abort` | 中止上传（归还在途占用） |
| GET | `/namespaces/{ns}/objects` | 列对象（含版本链） |
| GET | `/namespaces/{ns}/objects/{name}` | 对象元数据（`?version=N`） |
| GET | `/namespaces/{ns}/objects/{name}/content` | 下载（`?version=N`） |
| DELETE | `/namespaces/{ns}/objects/{name}` | 逻辑删除（保留历史版本） |
| DELETE | `/namespaces/{ns}/objects/{name}/versions/{n}` | 删除指定历史版本 |
| POST | `/namespaces/{ns}/objects/{name}/restore` | 按版本恢复为新的当前版本 |
| POST | `/namespaces/{ns}/maintenance/purge-expired` | 回收过期未完成会话 |

## 分段上传与断点续传语义

1. **创建会话**：声明 `object_name`、`total_parts`、整对象 `final_checksum`
   （sha256 hex）、可选 `expected_version`。会话默认 TTL 24h。
2. **分段提交**：`PUT .../parts/{seq}`，请求体为原始字节，必须带头
   `X-Part-Checksum: <sha256(part)>`。
   - 分段必须按 `1,2,3,...` **严格升序首次提交**；跳跃、乱序、超过
     `total_parts` 一律返回 `part_out_of_order`（409），响应 details 中带
     `expected_next` 与已接收序号。
   - 分段字节与声明摘要不符 → `part_checksum_mismatch`（422），该分段不落盘、
     不占用序号，可用同一序号重试。
   - **幂等重放**：重复提交「当前最后一个已接收序号」且摘要/大小一致 →
     HTTP 200，响应 `"replayed": true`，不重复计入。
   - 同序号但摘要不同 → `part_conflict`（409），与幂等重放明确区分。
3. **完成**：所有分段到齐后调用 `complete`。服务顺序组装并重新计算整对象
   sha256，与 `final_checksum` 不符 → `object_checksum_mismatch`（422），
   不产生对象，会话保持 `open`（已接收分段的在途占用保留，直到中止或过期
   才归还）。缺段 → `parts_incomplete`（409）。
4. **会话失效**：会话不存在 → `upload_session_not_found`（404）；TTL 过期 →
   `upload_session_expired`（410）；已完成/已中止后再传分段 →
   `upload_session_closed`（409，details 标注 completed/aborted）。
5. **不可见性保证**：只有组装成功且摘要校验通过、blob 落盘后才写对象元数据，
   因此未完成的上传永远不会出现在列表/下载接口中。
6. **续传**：中断后先 GET 会话查看 `received_parts`，重放最后一分段后继续
   提交后续序号即可。

### curl 示例

```bash
H='X-Namespace-Token: token-a'
curl -s -XPOST localhost:8000/namespaces -d '{"name":"team-a","quota_bytes":1048576,"tokens":["token-a"]]}' -H 'Content-Type: application/json'

head -c 1048576 /dev/urandom > big.bin
SID=$(curl -s -XPOST localhost:8000/namespaces/team-a/uploads -H "$H" -H 'Content-Type: application/json' \
  -d "{\"object_name\":\"big.bin\",\"total_parts\":2,\"final_checksum\":\"$(sha256sum big.bin | cut -d' ' -f1)\"}" | python -c 'import sys,json;print(json.load(sys.stdin)["session_id"])')

split -n 2 -d big.bin part-
for i in 0 1; do
  curl -s -XPUT "localhost:8000/namespaces/team-a/uploads/$SID/parts/$((i+1))" \
    -H "$H" -H "X-Part-Checksum: $(sha256sum part-0$i | cut -d' ' -f1)" \
    --data-binary @part-0$i
done
curl -s -XPOST "localhost:8000/namespaces/team-a/uploads/$SID/complete" -H "$H"
```

## 版本与覆盖语义

- 每次成功完成上传产生一个**单调递增**版本号（v1, v2, ...）。覆盖写响应
  `outcome=created|replaced`、`replaced_version`，并附 `decision` 判定依据；
  历史版本元数据与 blob 全部保留，可 `?version=N` 读取。
- 对象元数据先于 blob？**顺序相反**：blob（内容寻址、不可变）先就位，再原子
  替换对象元数据文件，因此任何时刻都不会读到「元数据有了、数据还没写完」的
  部分写入。
- **并发覆盖有确定胜负**：所有 complete 在同一把锁内串行执行，按提交顺序依次
  分配 v=N+1。测试中 10 个并发覆盖产生连续版本 2..11，无一丢失，每个成功提交
  都可在版本链中回溯，最终 current 指向锁排序的最后一个写入者。
- **乐观并发（可选）**：创建会话时传 `expected_version`（对象不存在传 `0`）。
  complete 时若当前版本已变化 → `version_conflict`（409），details 给出
  `expected_version` 与 `actual_version`；失败方不产生任何版本、不暴露部分
  写入。
- **删除**为逻辑删除：置删除标记，默认下载返回 `object_not_found`（details 中
  `delete_marker=true`），但版本链保留、可按版本访问；删除后重新上传视为
  created，版本号在历史链上继续递增。删除**不回收 blob、不释放配额**——
  所有保留版本都必须保持可读（"版本信息还在、内容必然可取"），空间只能
  通过下节的版本清理来释放。

## 历史版本保留与清理

- **保留策略**：创建命名空间时可设 `max_history_versions`（每个对象最多保留
  多少个*历史*（非当前）版本，`null`/缺省 = 不限，`0` = 只留当前版本）。
  每次新提交（complete/restore）在同一锁内自动裁剪最旧的超额历史版本。
  已删除对象同样适用：其删除时的最新版本作为"当前版本"留痕，历史部分按
  策略裁剪。
- **手动清理**：`DELETE .../objects/{name}/versions/{n}` 删除指定历史版本。
  对象未被删除时，**当前生效版本不允许直接删**（`version_in_use`，409）；
  对象已删除后没有生效版本，所有历史版本都可逐个清理。目标版本不存在 →
  `version_not_found`（404），对象不存在 → `object_not_found`（404）。
- **清理的正确性**：
  - 凡是还能被查到的版本，`?version=N` 读取必须拿到完整内容——裁剪只移除
    版本条目本身，留存版本的 blob 绝不动；
  - 被清理版本的 blob 只有在**全命名空间无任何保留版本引用**时才删除
    （内容寻址共享保护），其逻辑大小同步从 `used_bytes` 释放；
  - 已产生的版本号**永不重排、永不复用**：新版本号 = `current_version + 1`，
    被清理留下的序号空洞不会被后来的写入占用。
- 清理动作（自动/手动）都写审计日志并在 `decision` 中记录被清理的版本号
  与清理后的 `used_bytes`。

## 按版本恢复

`POST /namespaces/{ns}/objects/{name}/restore`，请求体
`{"version": N, "expected_version": M?}`：

- 把版本 N 的内容、Content-Type、元信息**原样复制**为一个新的当前版本
  （版本号 = 历史链最大值 + 1），历史一条不丢；恢复后默认下载即为恢复内容；
  已删除对象恢复后删除标记自动清除。
- 三种目标状态可互相区分：
  - 对象不存在 → `object_not_found`（404）；
  - 版本不存在（已清理或从未产生）→ `version_not_found`（404），details
    列出现存版本号；
  - 对象已被删除 → **恢复成功**且响应 `outcome=restored_from_deleted`、
    `was_delete_marked=true`（未删除则为 `restored`/`false`）。
- 恢复产生的新版本同样计入配额（`used+inflight+size <= quota` 判定），
  超额 → `quota_exceeded`（413）；可选 `expected_version` 提供乐观并发，
  不符 → `version_conflict`（409）。
- **并发确定性**：恢复、删除、版本清理、继续上传全部在同一把锁内串行。
  并发恢复同一版本会产生多个各自独立的新版本（内容相同、版本号各异），
  没有任何成功提交会无交代地消失；并发清理掉目标版本后，恢复方得到
  `version_not_found`。

## 配额规则

- 配额在命名空间创建时以字节设定（`quota_bytes=0` 表示不限）。
- **计费口径**（v2 起）：
  - `used_bytes` = 所有对象**全部保留版本**的逻辑大小之和（历史版本也占
    配额，清理才释放）；相同内容被多个版本/对象引用时按引用次数计。
  - `inflight_bytes` = 所有 open 会话**已接收但未完成**的分段字节之和。
  - 不变式：`used_bytes + inflight_bytes <= quota`，在锁内判定，并发上传
    也无法突破上限。
- **判定时机**：分段提交（`PUT parts/{seq}`）时检查
  `used + inflight + 本分段大小 <= quota`，超限 → `quota_exceeded`（413），
  details 含 `used/inflight/quota/projected/incoming_size`，与其它失败原因
  可区分；被拒分段不落盘、不占序号。complete 不再做配额判定（所有字节
  已在接收时获准），只把在途占用转为已用占用。
- **在途占用的归还**：complete（转为 used）、abort、会话 TTL 过期（惰性
  过期或 `purge-expired` 回收）都会释放对应 `inflight_bytes`，不会越积
  越多；提交失败（如整对象摘要不符）时分段仍保留在 open 会话中、占用
  保持计入，直到客户端中止或会话过期。
- **覆盖写需要瞬时余量**：上传期间旧版本与新分段并存，因此把对象从 6B
  覆盖为 10B 需要至少 16B 的 `used+inflight` 余量；若命名空间设
  `max_history_versions=0`，提交完成后旧版本立即被清理，稳态只占新版本
  大小。
- 删除对象**不释放**配额（历史版本全部保留且可读）；释放路径只有版本
  清理（手动 `DELETE .../versions/{n}` 或保留策略自动裁剪）。
- 服务启动时按磁盘真相**对账**重算每个命名空间的 `used/inflight`
  （见"对老数据的兼容"）。

## 对老数据的兼容

- v1 落盘的数据无需迁移：启动对账会按新口径重算 `used_bytes`
  （v1 只计当前版本，v2 起计全部保留版本，对账后即一致）与
  `inflight_bytes`（open 会话的已接收分段重新计入，终止态会话归零）。
- 旧 `namespaces.json` 缺少 `inflight_bytes`/`max_history_versions` 字段时
  按 `0`/`null`（不限）处理；旧会话记录缺少 `inflight_bytes` 字段时由
  对账按 `parts/` 目录补齐。
- 行为变化（v1 → v2）：超配额拒绝从 complete 前移到分段提交；删除对象
  不再释放配额（改由版本清理释放）。涉及这两点的旧测试已按新语义更新，
  断言意图（不超限、不误删、可区分）保持不变。

## 授权规则

- 未带头 / 空令牌 → `missing_credentials`（401）。
- 命名空间不存在 → `namespace_not_found`（404）。
- 令牌不在该命名空间令牌列表中 → `namespace_access_denied`（403）。
- 每个命名空间持有独立令牌集合，跨命名空间令牌无效。

## 错误分类（稳定 error_code）

所有错误响应体形如：

```json
{"error": {"code": "part_out_of_order",
           "message": "parts must be submitted ...",
           "details": {"part": 3, "expected_next": 2, "received": [1]}}}
```

| HTTP | error_code | 触发条件 |
| --- | --- | --- |
| 401 | `missing_credentials` | 未提供令牌 |
| 403 | `namespace_access_denied` | 令牌不属于该命名空间 |
| 404 | `namespace_not_found` / `object_not_found` / `version_not_found` / `upload_session_not_found` | 对应资源不存在 |
| 409 | `part_out_of_order` | 分段乱序/跳跃/越界 |
| 422 | `part_checksum_mismatch` | 分段摘要不符 |
| 409 | `part_conflict` | 同序号分段摘要不同 |
| 409 | `parts_incomplete` | 完成时有缺段 |
| 422 | `object_checksum_mismatch` | 组装后摘要不符 |
| 410 | `upload_session_expired` | 会话超过 TTL |
| 409 | `upload_session_closed` | 会话已完成/中止 |
| 409 | `version_conflict` | 乐观并发版本不匹配（上传/恢复） |
| 409 | `version_in_use` | 试图删除当前生效版本 |
| 413 | `quota_exceeded` | 分段接收/恢复将突破配额（含在途字节） |

## 过期清理与数据可追溯

- `POST /maintenance/purge-expired` 将所有 TTL 过期且仍 open 的会话标记为
  `expired`，删除其 `parts/` 目录并归还 `inflight_bytes`；`session.json`
  保留为留痕，不会产生对象或悬挂数据。abort 同理（状态 `aborted` +
  回收分段 + 归还在途占用）。
- 每个关键动作（会话创建/分段接收/幂等重放/完成/拒绝原因/删除/版本清理/
  恢复/回收）都写 `audit.log`（JSONL）并通过 `object_storage` logger 打印，
  包含 session_id、object、version 与 `decision` 判定依据。

## 本地验证

```bash
uv sync
uv run pytest -q          # 42 个用例
uv run pytest -s          # 同时查看会话/版本/判定依据日志
```

测试覆盖：

- 中断续传与重复分段幂等（`tests/test_multipart.py`）
- 分段摘要错误、对象摘要错误、乱序、缺段、过期/已关闭会话（错误码可区分）
- 并发覆盖确定性、乐观并发失败、旧版本可回溯（`tests/test_versions.py`，
  含 10 线程竞争）
- 越权（401/403/404 区分）、超配额（413）、20 线程并发配额不超限、
  删除留痕与版本清理释放、共享 blob 保护、过期会话回收
  （`tests/test_auth_quota.py`）
- 历史版本保留策略自动裁剪、手动版本清理与当前版本保护、版本号不复用、
  清理后留存版本完整可读（`tests/test_retention.py`）
- 按版本恢复（含已删除对象）、三种目标状态可区分、恢复计入配额、
  并发恢复/删除/上传确定性（`tests/test_restore.py`）
- 在途字节计入配额、complete 转账式记账、abort/expire/purge 归还、
  提交失败不泄漏、并发在途边界（`tests/test_inflight_quota.py`）
- REST 端到端错误码与续传下载（`tests/test_api.py`）

## 局限

- 单进程并发模型；需要多实例时应替换为带文件锁/外部锁的实现。
- 令牌以明文存于注册表，适合本地/内网验证，不适合作为公网授权方案。
