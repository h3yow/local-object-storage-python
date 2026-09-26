# local-object-storage

仅依赖本地文件系统的对象存储服务（FastAPI 实现），在上传中断、重复提交与并发访问下
提供一致且可解释的结果。

## 运行与本地验证

```bash
uv sync                 # 安装依赖
uv run pytest -q        # 运行全部测试（21 个用例）
uv run pytest -q -s --log-cli-level=INFO   # 查看上传会话/版本/判定依据日志
OBJECT_STORE_ROOT=/tmp/objstore uv run python main.py   # 启动服务（默认 127.0.0.1）
```

数据目录布局（`OBJECT_STORE_ROOT`，默认临时目录）：

```
namespaces/<ns>/objects/<key>/v00000001.bin   # 已提交版本（原子 rename 落盘）
namespaces/<ns>/uploads/<session_id>/part-*   # 未完成的分段
```

## 上传语义（分段 / 断点续传）

1. `POST /namespaces/{ns}/uploads` 创建会话：声明 `key`、`expected_parts`、
   整体内容的 `final_sha256` 与可选 `metadata`。响应包含 `session_id` 与
   `base_version`（创建时看到的最新版本，用于并发判定）。
2. `PUT /uploads/{sid}/parts/{n}` 逐段上传：每段携带 `data_base64` 与该段的
   `sha256`。分段号必须在 `[1, expected_parts]` 内。
   - **幂等**：同一分段号重复提交相同内容直接成功，不重复计配额；
   - 同一分段号提交不同内容 → `409 part_conflict`；
   - 内容与声明摘要不一致 → `400 part_checksum_mismatch`。
   - 中断后可直接续传缺失分段，无需重来。
3. `POST /uploads/{sid}/complete` 完成：所有分段齐全后按序合并，整体 sha256
   必须与 `final_sha256` 一致，否则 `422 digest_mismatch`，会话作废并清理分段。
   未完成的对象**不会**出现在任何读取结果中。
4. `POST /uploads/{sid}/abort` 主动中止；会话有 TTL（默认 3600s），过期后任何
   操作返回 `410 upload_session_expired`。`POST /maintenance/reap` 回收过期会话，
   释放配额并删除分段文件，不留悬挂数据。

## 版本语义

- 每次成功 `complete` 产生一个单调递增的版本；同名覆盖不会丢失旧版本，
  `GET /namespaces/{ns}/objects/{key}?version=N` 可按版本读取，
  `GET .../versions` 列出全部版本（含元数据、摘要、来源会话）。
- **并发提交**：基于 `base_version` 的乐观并发控制——先到者胜出，
  其余收到 `409 commit_conflict`，错误详情中包含 `base_version` 与
  `current_version`（获胜版本），旧版本不会无解释地消失。客户端可基于
  最新版本重新创建会话重试。
- **删除**：`DELETE` 写入 tombstone 版本（版本号继续递增），最新读取返回
  `404 object_not_found`，历史版本仍可读取，删除行为本身留痕可追溯。

## 并发与配额规则

- 所有状态变更在同一把锁下原子完成，不会暴露部分写入。
- 配额按命名空间计：**已提交字节 + 在途（未提交）分段字节** 一起计入，
  因此并发上传无法通过"先都传完再提交"绕过配额；超限请求返回
  `413 quota_exceeded`，详情含 `used_bytes / inflight_bytes / quota_bytes`。
- 历史版本保留且计入配额；会话中止/过期/完成时在途字节立即释放。
- 授权按命名空间授予 token（`POST /namespaces/{ns}/grants`，read/write 分离），
  请求头携带 `X-Access-Token`。

## 错误分类

所有业务错误统一为 `{"error": {"reason", "message", "detail"}}`，`reason` 可程序化区分：

| HTTP | reason | 含义 |
|------|--------|------|
| 400 | `invalid_part_number` | 分段号超出声明范围 |
| 400 | `part_checksum_mismatch` | 分段内容与声明摘要不一致 |
| 401 | `unauthorized` | 缺少或无效凭证 |
| 403 | `forbidden` | 凭证无该操作权限 |
| 404 | `namespace_not_found` / `object_not_found` / `version_not_found` / `upload_session_not_found` | 资源不存在 |
| 409 | `part_conflict` | 同一分段号内容不一致 |
| 409 | `missing_parts` | 完成时仍有分段缺失（detail 列出缺失编号） |
| 409 | `commit_conflict` | 并发提交落败（detail 含获胜版本） |
| 409 | `upload_session_invalid_state` | 会话已完成/已中止 |
| 410 | `upload_session_expired` | 会话已过期 |
| 413 | `quota_exceeded` | 超出命名空间配额 |
| 422 | `digest_mismatch` | 合并内容与声明的整体摘要不一致 |

## 测试

`tests/` 覆盖：断点续传与重复分段（`test_upload_flow.py`）、摘要错误与会话回收、
并发覆盖的确定胜负与版本一致性（`test_versions.py`）、越权与超配额
（`test_auth_quota.py`，含 16 线程并发不突破配额）。服务日志（logger
`objectstore`）会打印上传会话 ID、对象版本号与每次判定依据（如
`commit_conflict base_version=… current_version=…`、`quota rejected used=…`），
测试通过 `caplog` 断言这些判定依据确实落日志。
