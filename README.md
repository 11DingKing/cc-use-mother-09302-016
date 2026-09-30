# 传统技艺师徒计划后端

学校把一次展示延伸成跨学期师徒计划后，需要管理**计划版本、导师资格与容量、阶段目标、学习证据、
暂停与转导师申请**。本服务解决由此产生的核心问题：

- 导师中途停教、学生转给新导师时，**阶段目标与学习证据不断档**；
- 学生加入时**原子占用名额**并**固定当期阶段目标**；
- 补交、退出、导师失效、跨学期续接均形成**连续历史**；
- 未成年人访问受**监护授权**约束；
- 多人同时录取或**进程重启**后，容量余额仍然准确；
- 评定结果保存依据快照，事后可**还原依据**并检测评定后变更。

技术栈：Python 3.11 标准库 + SQLite，**零第三方依赖**。

## 目录

- `domain/contract.json`：领域角色、状态、不变量与样例（既有契约）。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/apprenticeship/`：后端主体
  - `schema.py`：SQLite 表结构与容量护栏触发器
  - `db.py`：连接（WAL、外键、忙等）与初始化
  - `service.py`：领域服务（事务、占座、历史、目标冻结、监护闸门、评定还原）
  - `http_app.py`：标准库 HTTP API（`ThreadingHTTPServer`）
  - `cli.py`：初始化 / 种子数据 / 查询命令
  - `errors.py`、`util.py`
- `tools/check_contract.py`：契约命令行摘要检查。
- `tests/`：契约回归、领域端到端（含 20 线程并发录取、重启复原）、HTTP 集成测试。

## 快速开始

```bash
# 初始化并写入演示数据（两名导师、一个已发布计划、两名学生）
PYTHONPATH=src python3 -m apprenticeship.cli --db data/app.db seed

# 启动 HTTP 服务
PYTHONPATH=src python3 -m apprenticeship.http_app --db data/app.db --port 8080
```

## 关键设计

### 1. 容量：只追加台账 + 事务串行化 + 数据库触发器

- 每次占座/释放都向 `seat_ledger` 追加一条 `delta ∈ {+1,-1}`，**余额恒等于 `SUM(delta)`**，
  不依赖任何内存计数器，因此进程重启后余额直接从台账复原。
- 所有写事务以 `BEGIN IMMEDIATE` 开始，SQLite 写锁使录取串行化；配合 `busy_timeout`，
  并发录取只会排队，不会超发。
- 触发器 `trg_seat_upper` / `trg_seat_lower` 在数据库层兜底：余额超过 `quota` 或变为负数时
  整个事务被 ABORT。
- 暂停**保留名额**（不写台账）；退出、导师失效、转出版本写 `-1`；转入、录取、续接写 `+1`。

### 2. 阶段目标：录取时冻结

- 计划发布后阶段不可修改，调整目标必须新建计划版本（`plans.predecessor_id` 串接学期）。
- 录取/续接时把当期阶段**整行复制**到 `enrollment_goals`，并对目标清单计算 `goals_hash`。
- 此后计划改版、导师更换都不影响在学学籍；证据只能对照该学籍冻结的目标提交。

### 3. 连续历史：只追加事件流

`enrollment_events` 为每个学籍保存有序事件：`admitted → paused/resumed → mentor_invalidated
→ transferred → evidence_submitted/reviewed → assessed → withdrawn / continued_out`。
转导师只更新 `mentor_id` 指针，目标与证据不动；跨学期续接新建学籍并以
`predecessor_enrollment_id` 链接，`lineage` 可沿链回溯全部学期。

### 4. 补交与评定依据还原

- 证据晚于阶段截止日或带 `make_up_note` 时自动标记 `is_late=1`（补交）。
- 评定时把**冻结目标 + 全部证据（含哈希、补交标记、评审意见）**快照存入 `assessments.basis`，
  并记录 `basis_hash`。
- `GET /assessments/{id}/reconstruct` 同时返回：评定时快照（复验哈希，防篡改）与按当前库
  重建的依据，二者比对可知评定后证据是否发生变化。

### 5. 监护授权

- 学生有出生日期且未满 18 周岁时，录取与跨学期续接都要求存在未撤销、未过期的
  `guardian_consents`；读取学籍/证据必须提供匹配的 `access_code`。
- 授权可撤销、可过期；成年学生不受限。

## 主要 HTTP 接口

所有请求/响应均为 JSON；错误返回 `{"error": ..., "message": ...}`，状态码：
400 参数错误、403 未授权、404 不存在、409 状态/容量冲突。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/mentors` `/mentors/{id}/suspend` `/resume` `/invalidate` | 导师资格；失效自动释放在读名额 |
| POST | `/plans` | 新建计划版本（可带 `predecessor_id`） |
| POST | `/plans/{id}/stages` | 草稿阶段目标 |
| PUT/GET | `/plans/{id}/mentors/{mid}/capacity` | 设置/查询名额（返回 used/remaining） |
| POST | `/plans/{id}/publish` `/close` | 发布（发布后目标不可变）/结项 |
| POST | `/students` `/students/{id}/consents` `/consents/revoke` | 学生与监护授权 |
| GET | `/students/{id}/access?access_code=` | 监护闸门检查 |
| POST | `/enrollments/admit` | 原子占座录取，冻结当期目标 |
| GET | `/enrollments/{id}?access_code=` | 学籍 + 冻结目标 + 连续历史 |
| GET | `/enrollments/{id}/history` `/lineage` | 事件流 / 跨学期链条 |
| POST | `/enrollments/{id}/pause-requests` | 暂停/复学申请（`kind=pause|resume`） |
| POST | `/pause-requests/{id}/decision` | `{"approve": true}` |
| POST | `/enrollments/{id}/transfer-requests` | 转导师申请（校验目标导师资格与名额） |
| POST | `/transfer-requests/{id}/decision` | 批准则原子移座、续历史、目标不变 |
| POST | `/enrollments/{id}/withdraw` | 退出并释放名额 |
| POST | `/enrollments/{id}/continue` | 跨学期续接（归档旧学籍、占新版本名额） |
| POST/GET | `/enrollments/{id}/evidences` | 证据提交（逾期/补交自动标记）/列表 |
| POST | `/evidences/{id}/review` | 证据受理或驳回 |
| POST | `/enrollments/{id}/assessments` | 评定（依据快照入库） |
| GET | `/assessments/{id}/reconstruct` | 还原评定依据并比对当前状态 |

## 验证

```bash
# 全部回归（契约 + 领域 + 并发 + 重启 + HTTP）
python3 -m unittest discover -s tests -v

# 编译检查
python3 -m compileall -q src tools tests

# 契约摘要
python3 tools/check_contract.py domain/contract.json
```

测试中的硬保证：

- `ConcurrentAdmissionTest`：20 个线程对 5 个名额同时发起录取，恰好 5 人成功、15 人收到
  `CapacityFullError`，余额为 5。
- `RestartPersistenceTest`：录取与退出后关闭连接重新打开（模拟进程重启），余额由台账复原，
  事件历史完整。
- `MentorInvalidationTest`：导师失效释放名额、学生进入 `mentor_invalid`，目标与证据保留，
  转导师后历史连续。
- `CrossTermTest`：续接后旧学籍归档、旧座释放、新学籍冻结新版本目标，`lineage` 跨学期可回溯。
- `AssessmentReconstructionTest`：评定快照哈希可复验，并能检出评定后新增证据。
- `GuardianConsentTest` / `test_http`：未成年人无授权录取 403、读取必须携带访问码。
