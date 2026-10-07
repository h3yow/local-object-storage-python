# local-object-storage

仅依赖**本地文件系统**的对象存储服务（FastAPI）。支持分段上传与断点续传、对象
版本管理（保留策略 / 按版本恢复 / 手动清理）、命名空间级访问授权与存储配额
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
| POST | `/namespaces` | 创建命名空间（配额、令牌、版本保留策略） |
| GET | `/namespaces/{ns}` | 配额用量（含在途字节） |
| PUT | `/namespaces/{ns}/policy` | 更新版本保留策略（立即对存量生效） |
| POST | `/namespaces/{ns}/uploads` | 创建分段上传会话 |
| GET | `/namespaces/{ns}/uploads/{sid}` | 查询会话状态（含在途字节） |
| PUT | `/namespaces/{ns}/uploads/{sid}/parts/{seq}` | 提交分段 |
| POST | `/namespaces/{ns}/uploads/{sid}/complete` | 完成上传 |
| POST | `/namespaces/{ns}/uploads/{sid}/abort` | 中止上传 |
| GET | `/namespaces/{ns}/objects` | 列对象（含版本链） |
| GET | `/namespaces/{ns}/objects/{name}` | 对象元数据（`?version=N`） |
| GET | `/namespaces/{ns}/objects/{name}/content` | 下载（`?version=N`） |
| DELETE | `/namespaces/{ns}/objects/{name}` | 逻辑删除（保留全部历史版本） |
| DELETE | `/namespaces/{ns}/objects/{name}?version=N` | 删除指定历史版本（当前版本除外） |
| POST | `/namespaces/{ns}/objects/{name}/restore` | 按版本恢复（body: `{"version": N}`） |
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
   不产生对象、不占配额，会话保持 `open`。缺段 → `parts_incomplete`（409）。
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
  created，版本号在历史链上继续递增。删除会把当前版本的配额占用释放回命名
  空间，但**所有版本的 blob 全部保留**——版本链中凡可查到的版本必须始终
  可读、可恢复；磁盘空间由保留策略或手动版本删除回收。重复删除幂等
  （`already_deleted=true`，不重复释放配额）。

## 历史版本保留与清理

- **保留策略**：命名空间级 `max_history_versions`（创建时指定或
  `PUT /namespaces/{ns}/policy` 更新，`0` 表示不限制）。语义为「每个对象最多
  保留 N 个版本（含当前版本）」：每次成功上传/恢复后，超出上限的**最老**
  历史版本立即被清理；策略收紧时立即对存量对象生效。当前生效版本永远不会
  被策略清理。
- **手动清理**：`DELETE /namespaces/{ns}/objects/{name}?version=N` 删除指定
  历史版本；删除当前生效版本返回 `cannot_delete_current_version`（409）。
- **可读性不变式**：清理只移除版本链条目；凡还在链上的版本，按版本号读取
  必须拿到完整内容。blob 只有在「不再被任何对象的任何版本引用」时才从磁盘
  删除（内容寻址 + 引用检查），同一份内容被其它对象或历史版本引用时不会
  被误删。
- **版本号**：只增不减、永不重用。被清理的版本号不会重新分配给后续写入，
  新版本号始终在既有历史（含被清理的）之上 +1。
- **配额口径**：`used_bytes` 只计「当前生效版本 + 在途分段」（与 v1 一致），
  历史版本不计入配额；因此清理历史版本释放的是**磁盘空间**（审计与返回值
  中记为 `bytes_freed`），`used_bytes` 不变。在途数据与当前版本的回收会
  同步释放配额（见下节）。

## 按版本恢复

`POST /namespaces/{ns}/objects/{name}/restore`，body `{"version": N}`：

- 把版本 N 恢复为一个**新的当前版本**（版本号 = 历史最大号 + 1），内容、
  Content-Type、元信息与源版本完全一致（共享同一内容寻址 blob），历史
  版本一条不丢；恢复后默认下载即为恢复出的内容。响应中 `restored_from`
  记录源版本。
- 三种情况可互相区分：对象不存在 → `object_not_found`（404）；目标版本
  不存在 → `version_not_found`（404，details 带 `available` 版本列表）；
  对象已被逻辑删除 → **允许恢复**，响应 `was_delete_marker=true`，删除
  标记被清除。
- 恢复产生的新当前版本计入配额（抵扣被替换的当前版本；已删除对象无抵扣），
  超配额返回 `quota_exceeded`（413）。
- **并发确定性**：恢复、删除、清理、继续上传都在同一把锁内串行，最终结果
  只取决于锁序，可解释；若目标版本先被保留策略清理，恢复方得到
  `version_not_found`；任何成功提交的版本不会无交代消失（清理均写审计）。

## 配额规则

- 配额在命名空间创建时以字节设定（`quota_bytes=0` 表示不限）。
- **占用口径**：`used_bytes`（当前生效版本的逻辑大小）+ `inflight_bytes`
  （已接收但未完成的分段字节）。两者都在 `GET /namespaces/{ns}` 可见。
- **在途计费**：分段提交（`PUT parts/{seq}`）时即在锁内判定
  `used - 可抵扣当前版本 + inflight + 本分段大小 > quota`，超限返回
  `quota_exceeded`（413），details 带 `reason=inflight_bytes` 与
  `used/inflight/projected`——因此并发上传不会在 complete 时才一起挤爆
  配额。被拒的分段不落盘、不占序号、不计费，可用同一序号重试。
  「可抵扣当前版本」指本会话目标对象的当前版本大小（complete 时会释放它），
  所以配额占满时同尺寸覆盖写仍可提交；抵扣额度只有一份，已被其它在途
  会话占用时不可重复使用。
- **在途释放**：complete 把会话的在途字节结转为 `used_bytes`（净额
  `新大小 - 被替换当前版本大小`）；abort、TTL 过期（惰性判定或
  purge-expired）把会话在途字节归还命名空间。释放按会话记账、幂等，
  不会重复释放或越积越多。
- complete 时仍有一次防御性配额判定（在锁内）：总占用
  `used + 其它会话 inflight` 不会超限。
- 并发写入因此不可能突破配额（测试：30 线程并发各传 10 字节、配额 100，
  恰好 10 个分段被接收、20 个得到 `quota_exceeded`，inflight 恰好 100；
  中止一半后剩余完成，used=50、inflight=0）。
- 删除对象会释放当前版本的配额占用（blob 保留，见「历史版本保留与清理」）。

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
| 409 | `version_conflict` | 乐观并发版本不匹配 |
| 409 | `cannot_delete_current_version` | 试图单独删除当前生效版本 |
| 413 | `quota_exceeded` | 写入/分段/恢复将突破配额（details 区分在途或完成时判定） |

## 过期清理与数据可追溯

- `POST /maintenance/purge-expired` 将所有 TTL 过期且仍 open 的会话标记为
  `expired`，删除其 `parts/` 目录；`session.json` 保留为留痕，不会产生对象或
  悬挂数据。abort 同理（状态 `aborted` + 回收分段）。过期与中止都会把会话的
  在途字节占用归还命名空间配额（按会话记账、幂等）。
- 每个关键动作（会话创建/分段接收/幂等重放/完成/拒绝原因/删除/版本清理/
  恢复/回收）都写 `audit.log`（JSONL）并通过 `object_storage` logger 打印，
  包含 session_id、object、version 与 `decision` 判定依据。

## 对老数据的兼容范围

- v1 的 `namespaces.json` 记录缺少 `inflight_bytes` /
  `max_history_versions` 字段：读取时按 `0`（无在途、不限保留）处理。
- v1 的 `session.json` 缺少 `inflight_bytes` 字段：按 `0` 处理；这类会话
  的分段是在旧版本下接收的，未计入在途配额，complete/abort/过期时也不会
  释放出不存在的占用。
- v1 的版本条目没有 `restored_from` 字段：读取时为 `null`。
- **行为变化**（相对 v1，均有测试锁定）：
  1. 超配额的拒绝点从 complete 前移到分段提交（在途计费），错误码同为
     `quota_exceeded`，details 中 `reason=inflight_bytes` 可区分；
  2. 逻辑删除不再回收当前版本的 blob（保证版本链可查即可读、可恢复），
     磁盘空间改由保留策略/手动版本删除回收；
  3. 重复删除同一对象变为幂等空操作（不再重复释放配额）。

## 本地验证

```bash
uv sync
uv run pytest -q          # 48 个用例
uv run pytest -s          # 同时查看会话/版本/判定依据日志
```

测试覆盖：

- 中断续传与重复分段幂等（`tests/test_multipart.py`）
- 分段摘要错误、对象摘要错误、乱序、缺段、过期/已关闭会话（错误码可区分）
- 并发覆盖确定性、乐观并发失败、旧版本可回溯（`tests/test_versions.py`，
  含 10 线程竞争）
- 越权（401/403/404 区分）、超配额（413）、并发配额不超限、
  删除释放与共享 blob 保护、过期会话回收（`tests/test_auth_quota.py`）
- 历史版本保留策略：自动清理、策略更新对存量生效、手动删版本、当前版本
  保护、共享 blob 不误删、版本号不重用（`tests/test_retention.py`）
- 按版本恢复：内容/类型/元信息一致、已删除对象恢复、三类错误区分、
  并发恢复/删除/清理确定性（`tests/test_restore.py`）
- 在途字节配额：分段即计费、中止/过期/完成释放与结转、幂等重放不计费、
  30 线程并发边界、满配额同尺寸覆盖（`tests/test_inflight_quota.py`）
- REST 端到端错误码与续传下载（`tests/test_api.py`）

## 局限

- 单进程并发模型；需要多实例时应替换为带文件锁/外部锁的实现。
- 令牌以明文存于注册表，适合本地/内网验证，不适合作为公网授权方案。
