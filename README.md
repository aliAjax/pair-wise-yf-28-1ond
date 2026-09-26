# 临床试验随机分配与盲法服务

仅使用 Python 3.11+ 标准库的独立随机化服务。支持分层区组随机、试验方案锁定、隐藏分组、外部编号并发幂等、中心隔离、安全信号暂停/恢复入组、受试者完成/退出登记、双人揭盲和审计。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

访问 <http://127.0.0.1:8104>，默认数据库 `randomization.db`。测试：

```bash
python3 -m unittest -v
```

演示用户：`site1`、`site2`（研究中心），`coord`（协调员），`monitor1`、`monitor2`（监查员）。

## 主要接口

- `POST /api/trials`：创建草稿试验，指定分组、分层因素、区组长度和随机种子。
- `POST /api/trials/{id}/protocol`：入组前修改方案；一旦入组即锁定。
- `POST /api/trials/{id}/start`：开始入组。
- `POST /api/trials/{id}/pause`：**协调员**填写安全信号原因后暂停入组（试验状态 `paused`）。暂停期间新入组请求返回 `409 enrollment_paused`，错误体 `error.details` 携带当前阻断原因、暂停人与时间；已入组受试者的随访、完成/退出登记和揭盲不受影响，同一外部编号的重复提交仍幂等。
- `POST /api/trials/{id}/resume`：整改结束后**协调员**补写处理说明恢复入组。随机序列不重置、已占用编号不回收，恢复后接着原序列发放。
- `POST /api/participants/{id}/status`：分中心登记受试者 `completed`/`withdrawn`（仅本人所在中心、仅 `enrolled` 可登记，需填写原因）。原因、时间、登记人写入受试者记录与审计日志，占用的随机编号不回收。
- `GET /api/unblinding-requests`：揭盲申请列表（中心只看本中心；监查员/协调员用于双人审批）。
- `POST /api/trials/{id}/enroll`：按当前用户中心入组；响应只返回分配编号，不返回分组。
- `GET /api/trials/{id}/participants`：分中心返回数据，中心用户看不到其他中心。
- `POST /api/participants/{id}/unblinding-requests`：发起揭盲。
- `POST /api/unblinding-requests/{id}/approve`：两人独立审批；同一人不能审批两次。
- `GET /api/trials/{id}/summary`：中心级汇总、暂停/恢复记录（`enrollment_hold`、`hold_history`）和审计记录。

暂停与恢复原因保存于 `enrollment_holds` 表并在审计日志中留下 `enrollment.pause` / `enrollment.resume` 记录；页面（http://127.0.0.1:8104）按角色提供暂停/恢复表单、入组阻断提示、受试者完成/退出登记和记录查看。旧版数据库在启动时自动迁移（`trials` 增加 `paused` 状态、`participants` 增加状态变更三列、新建 `enrollment_holds` 表）。

随机表按“试验种子 + 中心 + 分层因素”确定性生成，每个区组为分组数的整数倍并打乱；分配在 SQLite `BEGIN IMMEDIATE` 事务中原子占用。实现适合作为流程原型，不替代经认证的临床试验随机化系统。
