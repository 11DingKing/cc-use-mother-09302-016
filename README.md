# 传统技艺师徒计划

本项目维护传统技艺师徒计划的领域约定、角色边界与样例数据，并提供完整后端服务：管理计划版本、导师资格与容量、阶段目标、学习证据、暂停与转导师申请、退出与跨学期续接、评定与监护授权。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/mentorship/`：后端核心（仅标准库）。
  - `db.py`：SQLite 连接、事务（`BEGIN IMMEDIATE`）与表结构。
  - `service.py`：全部业务规则。
  - `api.py`：JSON HTTP 接口（`http.server`）。
  - `errors.py`：领域错误与 HTTP 状态映射。
- `tools/check_contract.py`：命令行契约摘要检查。
- `tools/run_server.py`：启动后端服务。
- `tests/`：契约、业务规则、并发、重启持久性与 HTTP 端到端测试。

## 关键设计约定

- **名额原子占用**：录取在单事务内完成"守卫式占用"（`UPDATE ... WHERE capacity_used < capacity_total`）+ 录取落库 + 当期目标快照；仅 `active`/`paused` 状态占用名额。多人同时录取不超卖，进程重启后余额仍准确（另提供 `reconcile-capacity` 对账自愈）。
- **目标固定**：录取时把当期阶段目标复制为 `goal_snapshots`，计划版本后续变更不影响在读学生；转导师只换 `mentor_id`，目标与证据随录取走，不断档。
- **连续历史**：证据只增不改，补交以 `supersedes_id` 链接；所有状态变化写入 `events`；跨学期续接以 `continued_from_id` 串链，`/enrollments/{id}/history` 还原全程。
- **监护授权**：未成年人的录取、提交证据、暂停申请、档案查阅均需有效监护授权（范围、有效期、撤销均受控）。
- **评定可还原**：评定记录关联目标快照与证据清单（`assessment_evidence`），`/assessments/{id}/basis` 返回完整依据（含补交链）。

## 运行

```bash
python3 tools/run_server.py --db mentorship.sqlite3 --port 8080
```

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/plan-versions` | 创建计划版本（草稿） |
| POST | `/plan-versions/{id}/goals` | 添加阶段目标 |
| POST | `/plan-versions/{id}/publish` · `/archive` | 发布 / 归档 |
| POST | `/mentors` | 登记导师（容量、资格有效期） |
| POST | `/mentors/{id}/suspend` · `/reinstate` · `/invalidate` | 停教 / 恢复 / 失效 |
| GET | `/mentors/{id}/capacity` | 名额余额 |
| POST | `/mentors/{id}/reconcile-capacity` | 名额对账 |
| POST | `/students` | 登记学生 |
| POST | `/students/{id}/guardian-authorizations` | 新增监护授权 |
| POST | `/guardian-authorizations/{id}/revoke` | 撤销授权 |
| GET | `/students/{id}/portfolio?guardian=` | 学生档案（未成年人需监护人） |
| POST | `/enrollments` | 录取：原子占用名额并固定当期目标 |
| GET | `/enrollments/{id}` · `/history` | 录取详情 / 跨学期连续历史 |
| POST | `/enrollments/{id}/evidence` | 提交证据（`supersedes_id` 补交） |
| POST | `/enrollments/{id}/pause-requests` | 暂停/恢复申请 |
| POST | `/pause-requests/{id}/decide` | 审批暂停/恢复 |
| POST | `/enrollments/{id}/transfer-requests` | 转导师申请 |
| POST | `/transfer-requests/{id}/decide` | 审批转导师（同事务占新放旧） |
| POST | `/enrollments/{id}/withdraw` | 退出（释放名额） |
| POST | `/enrollments/{id}/continue` | 跨学期续接 |
| POST | `/enrollments/{id}/assessments` | 阶段/结业评定 |
| GET | `/assessments/{id}/basis` | 还原评定依据 |

错误统一为 `{"error": {"code", "message"}}`：400 输入不合法、403 缺少监护授权、404 不存在、409 状态冲突/名额已满。

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
