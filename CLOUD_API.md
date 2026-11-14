# 云端接口约定

Windows 只向 config.yaml 的 cloud.pull_url 发出 HTTPS POST，认证头 X-Token。
云端必须校验 token，限制请求体大小，不把 token 写入公开日志。任务中禁止带命令或 Python 代码。

## 拉取

请求：
```json
{"action":"fetch","limit":10}
```
返回：
```json
{"ok":true,"items":[{"msgId":"order-123-reminder-1","group":"自动通知测试群-唯一名称","text":"测试通知","expiresAt":"2026-12-01T10:00:00Z"}]}
```
msgId/group/text 为非空字符串，msgId 全局唯一，重放保持同一 ID 与内容，禁止复用 ID 修改内容。
expiresAt 推荐必填，ISO 8601 且带时区。未填时首次本地入队起默认一小时失效；它不能识别长期积压的云端旧任务，因此正式业务必须提供 expiresAt。
云端应原子领取任务、归属固定发送器，并保留任务，直到得到确定回执。
过期任务应停止下发。不要因为长时间没收到回执，就换 msgId 重新投递。

## 回执

请求：
```json
{"action":"ack","msgId":"order-123-reminder-1","ok":true,"error":""}
```
返回：
```json
{"ok":true}
```
成功/失败回执须幂等。ok=false 表示终止该任务，不得自动创建新 ID 重发。
若云端超时，可重放原 msgId；本地 done/failed/expired 会补回执，不再发送。
uncertain 不回执成功或失败，会保留人工核查状态；云端可能持续收到无回执，请将其展示为待核查而非自动重试新任务。
目前接口没有服务端 hold 状态；如果你的后端支持，后续可增加 hold 回执降低轮询噪音。

## 待联调清单

1. 测试密钥错误时拒绝拉取、回执。
2. 同一 ID 多次拉取只产生一次本地入队。
3. 断网或回执失败时，云端重放原 ID。
4. 超时/服务重启后，结果不确定任务不自动重发。
5. 先测试群再正式群；配置唯一群名；一次只运行一个发送器。
