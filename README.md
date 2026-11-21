# 古籍制版 · 字模工位管理

几册古籍陆续送到同一制版工位、共用外字字模。本服务管理分册用字与优先字库清单，
在每次“交印”时算出工位上字模的**增减单**：哪些字模要补备、哪些可以撤下。

## 业务规则

1. 上传**按优先顺序排列的字库清单**（CSV：`字库,版次,字模号,字`），文件顺序即优先级。
2. 上传**分册用字表**（CSV：`字`，每格可多字、可无表头）。
3. 每个字取**第一个收录它的字库**中的字模，按 **(字库, 版次, 字模号)** 识别字模。
4. 交印一册时**只更新该册**选用的字模；其余册沿用各自上次交印的清单（从未交印的册不参与共需）。
5. 比对交印前后**各册共需字模的并集**，生成增减单；只有最后一个使用某字模的册撤下它时，才出现“减”项。
6. **并发交印按生效次序逐单出单**：进程内互斥锁 + SQLite `BEGIN IMMEDIATE`/WAL/忙等待双重保证，
   每单提交前后都校验并集与各册 active 清单一致，不一致即回滚报错。
7. **重复确认复用原单**：
   - 带 `Idempotency-Key` 请求头：同键永远返回同一单；
   - 不带键：同册、同用字、解析目标与当前已交印清单完全相同（无变化）时，复用该册最近一单；
   - 按单号 `GET /api/orders/{单号}` 可原样重取，`.../download` 下载 CSV。
8. **缺字或保存失败时整笔回滚**，各册仍沿用原清单，不产生新单。

## 目录

```
app/            FastAPI 应用（database/parsers/service/main）
web/index.html  单页操作界面（上传、交印、重取、下载、状态总览）
samples/        网页与接口可直接使用的样例 CSV（1 份字库清单 + 2 册用字表）
tests/          pytest 端到端测试（10 个用例）
Dockerfile / docker-compose.yml
```

## Docker Compose 启动

```bash
docker compose up --build
# 打开 http://localhost:8000/
# SQLite 数据持久化在命名卷 matrix-data -> /data/matrix.db
```

页面上可直接下载 `/samples/` 下的样例文件，按 ① 字库清单 → ② 用字表 → ③ 交印 的顺序操作。

## 本地运行（无 Docker 时）

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 测试
pip install -r requirements-dev.txt
pytest -q
```

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/libraries` |  multipart 上传字库清单 CSV，全量替换当前生效清单 |
| POST | `/api/volumes/{册名}/chars` | 上传该册用字表 CSV，全量替换用字 |
| POST | `/api/volumes/{册名}/submit` | 交印并出增减单；可选 `Idempotency-Key` |
| GET  | `/api/orders?volume=册名` | 按生效次序列出单据 |
| GET  | `/api/orders/{单号}` | 按单号原样重取 JSON |
| GET  | `/api/orders/{单号}/download` | 下载增减单 CSV（带 BOM，Excel 直开） |
| GET  | `/api/state` | 当前字库、各册用字/已交印字模、共需并集 |
| POST | `/api/reset` | 清空全部数据（演示用） |
| GET  | `/docs` | Swagger 交互文档 |

增减单 JSON 关键字段：`added` / `removed`（共需池层面的增减，含字库/版次/字模号/字）、
`pool_before` / `pool_after`（交印前后共需并集）、`volume_before` / `volume_after`（该册清单）。

## 数据模型（SQLite）

- `libraries`（字库+版次，优先级，current 标记）、`glyphs`（字库内字模号→字）
- `volumes` / `volume_chars`（册与用字，保留用字顺序）
- `active_matrices`（各册**上次交印**生效的字模清单）
- `submit_seq`（全局生效序号）、`orders`（单号、幂等键、指纹、整单 JSON 快照）

## 做完了什么 / 验过什么 / 未完成

### 已完成

- FastAPI + SQLite 全量功能：字库/用字上传、优先选库、交印出单、单据重取与 CSV 下载、状态总览、单页网页与样例数据。
- 并发串行化（应用锁 + 数据库立即事务 + WAL + 30s 忙等待）、幂等键与无键判重、缺字/保存失败回滚。
- Dockerfile 与 docker-compose（命名卷持久化、健康检查）。

### 已验证（本环境实际运行）

- `pytest` 10 个用例全部通过，含题目指定的**两册先后弃用同一字模**：
  甲先弃时乙仍需 → 无减单；乙再弃（最后使用者）→ 减单含该字模；每单 pool 与各册并集逐单一致。
- 真实启动 uvicorn 用样例 CSV 冒烟：上传→两册交印（共享 6 字，池 14→24）→重复确认复用→
  幂等键复用→缺字 422 且沿用原清单→甲/乙先后弃“寒”→CSV 下载与按单号重取内容一致。
- 4 册多线程并发交印：单号连续、逐单共需 3/6/9/12 与当时各册并集一致。
- 编码回退（UTF-8 BOM / GB18030）、无表头用字表、坏表头 400、未知册/单号 404、保存失败回滚。

### 未完成 / 限制

- 本环境没有 Docker 守护进程，`docker compose up --build` 仅做了文件级准备，未实机构建运行；
  Dockerfile 基于 `python:3.11-slim` 与已验证的依赖版本，逻辑上与本地运行一致。
- 应用级互斥锁只在单进程内生效；多进程/多副本部署时依赖 SQLite 的立即事务与忙等待兜底，
  建议仍以单副本 + 卷持久化方式运行（compose 默认即如此）。
- 字库清单替换为管理操作：替换后已交印册仍锁定在旧字模（保留历史可追溯），
  重新交印时才按新清单解析；未对替换操作加单独权限控制。
- 没有账号/鉴权与操作审计（题目未要求）；`/api/reset` 仅适用于演示环境。
