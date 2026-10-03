# 计量校准证书追溯服务

可离线运行的计量校准证书签发与**量值溯源链**管理服务。以基准（标准）设备的合格证书为
追溯起点，普通校准证书必须引用一张在**校准时刻有效且合格**的标准设备证书；撤销上游
证书时，其全部下游证书级联失效。离线采集的校准记录可作为**交接批次**整批签发：
批次键幂等、整批原子落库、批内证书可相互引用。已签发证书的设备名称、操作员、备注
录入有误时，可登记**更正声明**：声明按登记顺序生效、只增不改，原证书内容不受影响。
向客户交付一组校准证书时，可创建**交付归档包**：以归档包编号与有序目标列表冻结
创建时刻每张证书的原始签发内容、从基准到目标的引用链与合并更正后的展示字段，
编号幂等、快照只增不改，并发撤销/更正下整包始终对应同一个一致业务时点。

- 技术栈：Python 3.11 · FastAPI · PostgreSQL 16 · psycopg3
- 关键业务不变量由**数据库约束 + 触发器 + 单事务**保证，不依赖应用层“先查后写”，
  因此在并发下依然成立。

---

## 目录结构

```
.
├── app/                 # FastAPI 应用
│   ├── main.py          # 入口、异常处理、健康检查
│   ├── routers.py       # HTTP 路由
│   ├── repository.py    # 核心业务规则（签发校验/成环检测/级联失效）
│   ├── schemas.py       # 请求/响应模型
│   ├── db.py            # 连接池
│   ├── config.py        # 环境变量配置
│   └── errors.py        # 统一业务错误
├── db/init.sql          # 数据库初始化（枚举、约束、触发器、视图、签发批次表、更正声明表、交付归档包表）
├── scripts/             # 冒烟测试脚本（可直接对运行中的服务执行）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

---

## 一键启动（Docker Compose）

```bash
docker compose up -d --build
```

首次启动时：

1. 启动 PostgreSQL 16，并在**空数据卷**上自动执行 `db/init.sql` 完成建表；
2. 等待数据库健康检查通过后启动 API。

服务地址与端口：

| 服务 | 地址 |
| --- | --- |
| API 基址 | <http://localhost:8080> |
| 健康检查 | <http://localhost:8080/health> |
| 交互式文档（Swagger） | <http://localhost:8080/docs> |
| OpenAPI 描述 | <http://localhost:8080/openapi.json> |
| PostgreSQL（仅绑定宿主机回环地址） | `127.0.0.1:5432` |

停止与清理：

```bash
docker compose down          # 停止，保留数据卷（证书记录保留）
docker compose down -v       # 停止并删除数据卷（下次启动重新初始化空库）
```

> 完全离线部署：在有网络的机器上先 `docker compose build`（或 `docker save` 导出
> `python:3.11-slim` 与 `postgres:16-alpine` 镜像），拷贝到内网环境 `docker compose up -d`
> 即可，运行期不访问任何外部地址。

---

## 配置

所有配置通过环境变量注入，可在项目根目录放置 `.env`（参考 `.env.example`）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_PORT` | `8080` | API 在宿主机暴露的端口 |
| `POSTGRES_USER` | `caltrace` | 数据库用户 |
| `POSTGRES_PASSWORD` | `caltrace` | 数据库密码（生产请修改） |
| `POSTGRES_DB` | `caltrace` | 数据库名 |
| `POSTGRES_PORT` | `127.0.0.1:5432` | 数据库在宿主机的绑定地址:端口 |
| `DB_HOST` / `DB_PORT` | `db` / `5432` | API 容器内连接数据库用（compose 已固定） |

### 不用 Compose、直连已有 PostgreSQL

```bash
pip install -r requirements.txt
psql "$DB_URL" -f db/init.sql          # 需 superuser 创建 btree_gist 扩展
DB_HOST=127.0.0.1 DB_PORT=5432 DB_NAME=caltrace DB_USER=caltrace DB_PASSWORD=secret \
  uvicorn app.main:app --host 0.0.0.0 --port 8080
```

要求 PostgreSQL 14+，当前库内可执行 `CREATE EXTENSION btree_gist`（contrib 自带）。

---

## 业务规则

1. **追溯起点**：基准设备证书（`kind=REFERENCE`）无上游引用，结论恒为合格。
2. **引用有效性**：普通证书（`kind=CALIBRATION`）必须引用一张标准设备证书，
   且该校准证书在本证书的**校准时刻**满足：状态生效、结论合格、校准时刻落在其
   有效期 `[valid_from, valid_to)` 内。已撤销（REVOKED）或已失效（INVALID）的
   证书不能再作为校准依据。
3. **引用链不可成环**：树状溯源结构 + 签发时防御性成环检测 + 递归 CTE `CYCLE` 保护。
4. **有效期不重叠**：同一设备处于 ACTIVE 的证书，有效期区间不得重叠
   （`EXCLUDE USING gist` 排他约束，半开区间 `[from, to)`，首尾相接允许）。
   证书被撤销/失效后区间释放，可重新签发。
5. **不可改写**：证书签发后业务字段禁止 UPDATE、禁止 DELETE（数据库触发器强制，
   直连数据库修改也会被拒绝）；状态只允许 `ACTIVE → REVOKED/INVALID`。
6. **级联失效**：撤销一张证书 → 其所有直接/间接下游 ACTIVE 证书在**同一事务**中
   置为 INVALID，受影响证书及完整引用链快照写入 `invalidations` 表，历史签发内容
   永久可查。
7. **并发安全**：
   - 相同证书编号并发签发 → 唯一约束保证仅一张成功，其余 `409 cert_duplicate`；
   - 同设备重叠区间并发签发 → 应用预检 `422 validity_overlap`，竞态穿透时由 GiST
     排他约束兜底返回 `409 validity_overlap_concurrent`；
   - 撤销与新签发并发 → 撤销事务持有被撤销证书行锁与全部下游行锁，
     新引用插入的外键检查会阻塞到撤销提交，随后按“引用已失效”被拒绝，
     不会出现“失效证书还能被引用”的窗口。
8. **批次签发（离线交接）**：`POST /api/v1/certificate-batches` 携带唯一批次键与
   非空证书列表，每项沿用单张签发的全部字段与规则；普通证书可引用库内证书或
   本批证书（**列表顺序不代表引用顺序**，服务按引用拓扑排序后逐张校验落库）。
   批内编号唯一、引用无环、上游当前生效且合格且校准时刻落在其有效期内、同设备
   生效有效期不重叠——**任一记录不合规则整批不落库**，错误响应以
   `details.cert_no` 定位出错编号。批次键为幂等键：同键同请求重放首次签发结果
   快照（即使证书事后被撤销），同键不同请求返回 `409 batch_conflict`；
   并发重复提交由批次键主键兜底，仅一个事务真正落库。
9. **更正声明（展示字段纠错）**：已签发证书的 `device_name` / `operator` / `remark`
   录入有误时，可登记更正声明（唯一声明编号 + 证书编号 + 非空修正字段集合 +
   非空原因）。仅这三个展示字段可更正；修正集合中字段值为 `null` 表示**显式置空**，
   与**未提交**的字段（保持原值）严格区分。每张证书的声明按成功登记顺序编号
   （`seq`，从 1 开始）并依序生效——同一字段后登记的声明覆盖先登记的。
   声明写入只增不改的 `correction_declarations` 表，**不改写原证书**：证书详情、
   追溯链、批次首次签发快照保持既有内容与响应契约；已撤销（REVOKED）或级联
   失效（INVALID）的证书同样可更正展示资料，其状态、有效期与签发、撤销判定
   不受影响。声明编号为幂等键：同编号同内容重试返回首次声明（HTTP 200），
   同编号不同内容返回 `409 correction_conflict`；同一证书的并发更正由证书行锁
   串行化，声明顺序不丢失、不乱序。
10. **客户交付归档包**：向客户交付一组校准证书时，以归档包编号 + 非空且编号
    互不重复的目标证书有序列表，冻结创建时刻可交付的追溯凭据。仅接受：每个
    目标均为普通校准证书（CALIBRATION）、状态 ACTIVE、结论 PASS，且其从基准
    到目标的**完整上游链**全部 ACTIVE、PASS 的组合；任一目标不存在或不合格
    （含任一上游已撤销/失效/不合格）则**整包不保存**，错误响应以
    `details.cert_no` 指出相关编号。归档包按提交顺序返回每张目标证书的
    **原始签发内容**、**从基准到目标的引用链**、以及**按已登记更正声明合并
    后的展示字段**，并保存创建时刻与这些内容的快照（只增不改、数据库封印）。
    可凭归档包编号查询该快照，未知编号返回 `404 delivery_archive_not_found`。
    归档包编号为幂等键：编号与有序列表都相同的重试返回首次快照（HTTP 200，
    即使之后目标被更正或撤销），同编号不同列表（含仅顺序不同）返回
    `409 delivery_archive_conflict`。并发撤销/更正时，归档在单事务内对目标及
    完整上游链加行锁，整包对应同一个一致业务时点，绝不混合新旧状态（与撤销
    形成等待环时自动整体重试）。

证书状态：`ACTIVE` 生效中 / `REVOKED` 被主动撤销 / `INVALID` 因上游撤销级联失效。

---

## HTTP API

所有时间字段为带时区 ISO 8601，例如 `2026-03-01T09:00:00+08:00`。

### 1. 登记基准设备合格证书（追溯起点）

```bash
curl -sS -X POST http://localhost:8080/api/v1/reference-certificates \
  -H 'Content-Type: application/json' \
  -d '{
    "cert_no": "REF-MASTER-001",
    "device_id": "DEV-MASTER",
    "device_name": "实验室最高标准器",
    "valid_from": "2026-01-01T00:00:00+08:00",
    "valid_to":   "2027-01-01T00:00:00+08:00",
    "calibrated_at": "2026-01-01T09:00:00+08:00",
    "operator": "张工",
    "remark": "年度基准登记"
  }'
```

`calibrated_at` 可省略，缺省取 `valid_from`。响应 `201`，含证书与其追溯链。

### 2. 签发普通校准证书

```bash
curl -sS -X POST http://localhost:8080/api/v1/certificates \
  -H 'Content-Type: application/json' \
  -d '{
    "cert_no": "CAL-WS-001",
    "device_id": "DEV-WS-A",
    "device_name": "车间万用表A",
    "valid_from": "2026-03-01T00:00:00+08:00",
    "valid_to":   "2027-03-01T00:00:00+08:00",
    "calibrated_at": "2026-03-05T10:00:00+08:00",
    "conclusion": "PASS",
    "parent_cert_no": "REF-MASTER-001",
    "operator": "李工"
  }'
```

再向下签发一层（引用普通证书）：

```bash
curl -sS -X POST http://localhost:8080/api/v1/certificates \
  -H 'Content-Type: application/json' \
  -d '{
    "cert_no": "CAL-CUS-001",
    "device_id": "DEV-CUS-B",
    "device_name": "客户仪表B",
    "valid_from": "2026-04-01T00:00:00+08:00",
    "valid_to":   "2026-10-01T00:00:00+08:00",
    "calibrated_at": "2026-04-02T10:00:00+08:00",
    "conclusion": "PASS",
    "parent_cert_no": "CAL-WS-001"
  }'
```

### 3. 查询追溯引用链

```bash
curl -sS http://localhost:8080/api/v1/certificates/CAL-CUS-001/chain | python3 -m json.tool
```

返回从基准到目标证书的有序节点链（`nodes[0]` 即追溯起点）。

### 4. 查询某证书的全部下游（撤销前影响面预览）

```bash
curl -sS http://localhost:8080/api/v1/certificates/REF-MASTER-001/downstream
```

### 5. 撤销证书（级联失效下游，返回受影响证书与引用链）

```bash
curl -sS -X POST http://localhost:8080/api/v1/certificates/REF-MASTER-001/revoke \
  -H 'Content-Type: application/json' \
  -d '{"reason": "年度比对发现超差", "operator": "质量负责人"}'
```

响应示例（节选）：

```json
{
  "batch_id": 1,
  "revoked": {"cert_no": "REF-MASTER-001", "status": "REVOKED", ...},
  "affected_count": 2,
  "affected": [
    {"cert_no": "CAL-WS-001",  "depth": 1, "chain": [{"cert_no": "REF-MASTER-001", ...}, {"cert_no": "CAL-WS-001", ...}]},
    {"cert_no": "CAL-CUS-001", "depth": 2, "chain": [{"cert_no": "REF-MASTER-001", ...}, {"cert_no": "CAL-WS-001", ...}, {"cert_no": "CAL-CUS-001", ...}]}
  ]
}
```

### 6. 查询证书 / 列表 / 设备历史 / 撤销批次

```bash
# 单张证书（无论 ACTIVE / REVOKED / INVALID，历史内容始终可查）
curl -sS http://localhost:8080/api/v1/certificates/CAL-CUS-001

# 列表，支持 device_id / status / kind 过滤与分页
curl -sS 'http://localhost:8080/api/v1/certificates?status=INVALID&limit=50&offset=0'

# 某设备的全部证书（含已撤销/已失效的历史记录）
curl -sS http://localhost:8080/api/v1/devices/DEV-CUS-B/certificates

# 撤销批次列表 / 详情
curl -sS http://localhost:8080/api/v1/revocations
curl -sS http://localhost:8080/api/v1/revocations/1
```

### 7. 批量签发交接批次（整批原子、批次键幂等）

离线采集的一组校准记录作为一个交接批次一次签发。每项通过 `kind` 区分基准
（`REFERENCE`）或普通（`CALIBRATION`）证书，字段与单张签发一致；普通证书的
`parent_cert_no` 可引用库内证书或本批证书，**列表顺序不代表引用顺序**：

```bash
curl -sS -X POST http://localhost:8080/api/v1/certificate-batches \
  -H 'Content-Type: application/json' \
  -d '{
    "batch_key": "HANDOFF-2026-10-03-001",
    "certificates": [
      {"kind": "CALIBRATION", "cert_no": "CAL-B-002", "device_id": "DEV-B-2",
       "valid_from": "2026-05-01T00:00:00+08:00", "valid_to": "2027-05-01T00:00:00+08:00",
       "calibrated_at": "2026-05-02T09:00:00+08:00", "conclusion": "PASS",
       "parent_cert_no": "CAL-B-001"},
      {"kind": "REFERENCE", "cert_no": "REF-B-000", "device_id": "DEV-B-0",
       "valid_from": "2026-01-01T00:00:00+08:00", "valid_to": "2027-01-01T00:00:00+08:00",
       "calibrated_at": "2026-01-01T09:00:00+08:00"},
      {"kind": "CALIBRATION", "cert_no": "CAL-B-001", "device_id": "DEV-B-1",
       "valid_from": "2026-03-01T00:00:00+08:00", "valid_to": "2027-03-01T00:00:00+08:00",
       "calibrated_at": "2026-03-02T09:00:00+08:00", "conclusion": "PASS",
       "parent_cert_no": "REF-B-000"}
    ]
  }'
```

首次成功响应 `201`（`items` 按请求列表顺序返回，各含从基准到自身的追溯链）：

```json
{
  "batch_key": "HANDOFF-2026-10-03-001",
  "count": 3,
  "issued_at": "2026-10-03T10:20:30.123456+00:00",
  "items": [
    {"certificate": {"cert_no": "CAL-B-002", ...},
     "chain": {"cert_no": "CAL-B-002", "root_cert_no": "REF-B-000", "length": 3, "nodes": [...]}},
    {"certificate": {"cert_no": "REF-B-000", ...}, "chain": {...}},
    {"certificate": {"cert_no": "CAL-B-001", ...}, "chain": {...}}
  ]
}
```

幂等与冲突约定：

- **同键同请求重试** → `200`，返回首次签发结果快照（即使证书事后被撤销）；
- **同键不同请求** → `409 batch_conflict`；
- **任一记录不合规** → 整批不落库，错误响应 `details.cert_no` 定位出错编号，
  错误码与单张签发一致（如 `reference_not_active`、`validity_overlap`），
  批内特有问题使用 `batch_cert_duplicate`（批内编号重复）与
  `reference_cycle`（批内引用成环）。

### 8. 查询签发批次（首次签发结果快照）

```bash
curl -sS http://localhost:8080/api/v1/certificate-batches/HANDOFF-2026-10-03-001 | python3 -m json.tool
```

返回与签发响应同构的快照，不受事后撤销影响；批次不存在返回 `404 batch_not_found`。

### 9. 登记更正声明（纠正展示字段录入错误）

证书签发后业务字段不可改写；若 `device_name` / `operator` / `remark` 录入有误，
通过更正声明纠错（仅这三个展示字段可更正）：

```bash
curl -sS -X POST http://localhost:8080/api/v1/correction-declarations \
  -H 'Content-Type: application/json' \
  -d '{
    "declaration_no": "CORR-2026-0001",
    "cert_no": "CAL-WS-001",
    "corrections": {
      "device_name": "车间万用表A（FLUKE-17B）",
      "operator": null
    },
    "reason": "设备名称少录型号，操作员误填需置空"
  }'
```

- `corrections` 为**非空**修正字段集合：只接受 `device_name` / `operator` / `remark`；
  字段值为 `null` 表示**显式置空**，**未提交**的字段保持原值，两者严格区分；
- 首次登记响应 `201`；同编号同内容重试响应 `200`，返回首次声明（不重复登记）；
  同编号不同内容响应 `409 correction_conflict`；
- 已撤销 / 已失效证书同样可更正，其状态、有效期与签发、撤销判定不受影响。

响应（`200`/`201`）包含声明顺序、原证书、声明历史与合并后的当前展示：

```json
{
  "declaration": {
    "declaration_no": "CORR-2026-0001",
    "cert_no": "CAL-WS-001",
    "seq": 1,
    "corrections": {"device_name": "车间万用表A（FLUKE-17B）", "operator": null},
    "reason": "设备名称少录型号，操作员误填需置空",
    "created_at": "2026-10-03T10:25:30.123456+00:00"
  },
  "certificate": {"cert_no": "CAL-WS-001", "device_name": "车间万用表A", "operator": "李工", ...},
  "declarations": [
    {"declaration_no": "CORR-2026-0001", "seq": 1, ...}
  ],
  "display": {"device_name": "车间万用表A（FLUKE-17B）", "operator": null, "remark": null}
}
```

其中 `certificate` 始终是**签发时的原始内容**（不受更正影响）；`declarations` 为该证书
全部声明按登记顺序（`seq`）升序排列；`display` 为按序合并后的当前展示——同一字段
后登记的声明覆盖先登记的。

### 10. 查询证书的更正视图（声明历史 + 合并展示）

```bash
curl -sS http://localhost:8080/api/v1/certificates/CAL-WS-001/corrections | python3 -m json.tool
```

返回 `cert_no`、`certificate`（原证书）、`declarations`（声明历史，按 `seq` 升序）与
`display`（合并后的当前展示）；证书不存在返回 `404 cert_not_found`。

### 11. 按声明编号查询

```bash
curl -sS http://localhost:8080/api/v1/correction-declarations/CORR-2026-0001 | python3 -m json.tool
```

返回首次登记的声明及所属证书的当前更正视图（与登记响应同构）；
声明不存在返回 `404 declaration_not_found`。

> 注意：`GET /api/v1/certificates/{cert_no}`、追溯链与批次快照始终返回签发时的
> 原始字段值；合并后的展示值只通过上述更正相关接口获取。

### 12. 创建客户交付归档包（冻结当时可交付的追溯凭据）

向客户交付一组校准证书时，以归档包编号 + 非空、编号互不重复的目标证书**有序
列表**冻结当时的可交付凭据。列表顺序即返回顺序：

```bash
curl -sS -X POST http://localhost:8080/api/v1/delivery-archives \
  -H 'Content-Type: application/json' \
  -d '{"archive_no": "DELIVERY-2026-10-03-001", "cert_nos": ["CAL-CUS-001", "CAL-WS-001"]}'
```

接受条件（任一不满足则**整包不保存**，错误响应以 `details.cert_no` 点名）：

- `cert_nos` 非空且编号互不重复（重复 → `422 delivery_cert_duplicate`）；
- 每张目标证书都存在（不存在 → `404 cert_not_found`）；
- 目标均为普通校准证书（`CALIBRATION`；基准证书 →
  `422 delivery_target_not_calibration`）；
- 目标自身 ACTIVE（`422 delivery_target_not_active`）且 PASS
  （`422 delivery_target_not_qualified`）；
- 其从基准到目标的**完整上游链**每张证书都 ACTIVE（上游已撤销/失效 →
  `422 delivery_upstream_not_active`）且 PASS（上游不合格 →
  `422 delivery_upstream_not_qualified`）。

首次成功响应 `201`，`items` 按提交顺序返回，每项含目标证书的**原始签发内容**
`certificate`、**从基准到目标的引用链** `chain`、以及**按创建时刻已登记更正声明
合并后的展示字段** `display`；并记录创建时刻 `created_at`：

```json
{
  "archive_no": "DELIVERY-2026-10-03-001",
  "count": 2,
  "created_at": "2026-10-03T02:20:30.123456+00:00",
  "items": [
    {
      "cert_no": "CAL-CUS-001",
      "certificate": {"cert_no": "CAL-CUS-001", "device_name": "客户仪表B", "...": "原始签发内容"},
      "chain": {"cert_no": "CAL-CUS-001", "root_cert_no": "REF-MASTER-001", "length": 3,
                "nodes": [ {"certificate": {"cert_no": "REF-MASTER-001", "...": "原始内容"}},
                           {"certificate": {"cert_no": "CAL-WS-001", "...": "原始内容"}},
                           {"certificate": {"cert_no": "CAL-CUS-001", "...": "原始内容"}} ]},
      "display": {"device_name": "客户仪表B", "operator": "王工", "remark": null}
    },
    {"cert_no": "CAL-WS-001", "certificate": {"...": "原始签发内容"},
     "chain": {"...": "基准->CAL-WS-001"},
     "display": {"device_name": "车间万用表A（FLUKE-17B）", "operator": null, "remark": null}}
  ]
}
```

- `certificate` 与 `chain.nodes[*].certificate` 始终是**签发时的原始内容**，不受
  更正影响；`display` 才是按声明合并后的展示（仅 `device_name` / `operator` /
  `remark` 三个字段）。
- 快照与创建时刻一并冻结，之后无论更正还是撤销/级联失效都**不会改变**该归档包。

### 13. 查询交付归档包（首次冻结快照）与重试约定

```bash
curl -sS http://localhost:8080/api/v1/delivery-archives/DELIVERY-2026-10-03-001 | python3 -m json.tool
```

幂等与冲突约定（与签发批次、更正声明一致）：

- **同归档包编号 + 同有序列表重试** → `200`，返回首次冻结快照（与首次响应逐字节
  一致）；**即使之后目标或其上游被更正、撤销**，也返回归档当时的内容；
- **同编号不同列表**（含元素不同或仅顺序不同）→ `409 delivery_archive_conflict`，
  `details.existing_cert_nos` 给出已存列表；
- **未知归档包编号查询** → `404 delivery_archive_not_found`；
- 资格不满足 / 编号重复 / 目标不存在 → 整包不保存，归档包编号不被占用，修正后
  可用原编号重新提交。

> 并发提示：归档在单条数据库事务内对“目标 + 各自完整上游链”的证书行统一加锁，
> 与撤销事务互斥——快照读取期间相关撤销无法提交，反之亦然，因此整包对应同一个
> 一致业务时点，不会出现链上半新半旧；极端情况下归档与撤销形成等待环，由数据库
> 死锁检测中止一方，服务端自动整体重试（极少数持续冲突返回 `503 delivery_busy`，
> 客户端按幂等约定用相同请求重试即可）。

---

## 错误响应

统一格式：`{"error": {"code": "...", "message": "...", "details": {...}}}`

| HTTP | code | 触发场景 |
| --- | --- | --- |
| 404 | `cert_not_found` / `device_not_found` / `revocation_not_found` / `batch_not_found` / `declaration_not_found` / `delivery_archive_not_found` | 目标不存在 |
| 409 | `cert_duplicate` | 证书编号已存在（含并发重复签发、批次与库内冲突） |
| 409 | `batch_conflict` | 同一批次键提交了内容不同的签发请求 |
| 409 | `correction_conflict` | 同一声明编号提交了内容不同的更正请求 |
| 409 | `delivery_archive_conflict` | 同一归档包编号提交了目标列表（或顺序）不同的交付请求 |
| 422 | `batch_cert_duplicate` | 批次内证书编号重复 |
| 422 | `delivery_cert_duplicate` | 交付列表中证书编号重复（`details.cert_no`/`first_index`/`index` 点名） |
| 422 | `delivery_target_not_calibration` | 交付目标不是普通校准证书（如基准证书） |
| 422 | `delivery_target_not_active` | 交付目标当前非 ACTIVE（已撤销/级联失效），整包不保存 |
| 422 | `delivery_target_not_qualified` | 交付目标结论非 PASS，整包不保存 |
| 422 | `delivery_upstream_not_active` | 交付目标的某张完整上游链证书非 ACTIVE，整包不保存 |
| 422 | `delivery_upstream_not_qualified` | 交付目标的某张完整上游链证书结论非 PASS，整包不保存 |
| 503 | `delivery_busy` | 归档与并发撤销持续冲突、多次重试仍未完成（可用相同请求幂等重试） |
| 422 | `correction_empty` | 更正声明的修正字段集合为空 |
| 422 | `correction_field_not_allowed` | 更正了 device_name/operator/remark 之外的字段（`details.fields` 点名） |
| 422 | `correction_field_invalid` | 更正字段值越界（如长度超限） |
| 422 | `correction_reason_required` | 更正原因为空或纯空白 |
| 422 | `invalid_reference` | 引用的标准证书不存在（库内与本批均找不到） |
| 422 | `reference_not_active` | 引用了已撤销/已失效证书（失效证书不得作依据） |
| 422 | `reference_not_qualified` | 引用的标准证书结论为不合格 |
| 422 | `reference_out_of_validity` | 校准时刻不在标准证书有效期内 |
| 422 | `reference_cycle` | 引用关系将形成环路（含批内成环） |
| 422 | `validity_overlap` | 同设备已生效证书有效期重叠（含批内重叠） |
| 409 | `validity_overlap_concurrent` | 并发签发重叠，排他约束兜底拒绝 |
| 409 | `cert_not_active` | 撤销一张非 ACTIVE 证书 |

> 批次签发中由单条记录触发的校验错误与单张签发同码，并在 `details.cert_no`
> 中给出出错证书编号（必要时附 `details.parent_cert_no` / `details.cycle` 等）。

---

## 自测

服务启动后执行（仅依赖 bash + curl / python3 标准库）：

```bash
scripts/smoke-test.sh                 # 17 项主流程与错误路径
python3 scripts/extra_checks.py       # 21 项并发/边界/中段撤销补充检查
python3 scripts/batch_checks.py       # 批次签发：原子性/乱序引用/幂等重放/键冲突/并发
python3 scripts/correction_checks.py  # 更正声明：顺序生效/显式置空/幂等/冲突/并发
python3 scripts/delivery_checks.py    # 交付归档包：资格/整包不落库/快照冻结/幂等/冲突/并发一致时点
```

覆盖：基准登记、多级签发、无效引用、越界校准时刻、区间重叠（含首尾相接）、
同编号并发与重叠区间并发（3 线程仅 1 成功）、中段撤销、级联失效、失效证书
禁止引用、撤销后重签、历史可查、撤销批次回放，数据库层 UPDATE/DELETE 封印，
以及批次签发的整批原子性、批内乱序引用与拓扑校验、同键重放（含事后撤销）、
同键冲突、并发重复提交（5 线程仅 1 次首次签发）与批次查询；
更正声明的登记与顺序生效、显式置空与未提交的区分、原证书/追溯链/批次快照
不受更正影响、已撤销/已失效证书可更正且状态判定不变、同编号同内容重放
（含键序无关）、同编号异内容冲突、同一证书并发更正（8 线程 seq 1..8 不丢失
不乱序）、并发同编号（同内容仅 1 次登记、异内容仅 1 次成功）与全部错误路径；
交付归档包的资格校验（目标/完整上游链 ACTIVE 且 PASS、目标必须 CALIBRATION、
编号互不重复、缺失点名、整包不落库）、原始内容+引用链+合并展示按序冻结、
撤销/更正后同编号同列表重放仍返回首次快照、同编号异列表（含仅换序）冲突、
未知编号 404，以及并发同编号（同列表仅 1 次落库）、并发撤销 ↔ 归档下整包
始终对应同一一致时点（不混合新旧状态、无 5xx、被拒不落库）。
