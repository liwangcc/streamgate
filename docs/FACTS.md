# 已验证事实（不是设计建议）

本文件只记录事实与证据。怎么用这些事实，由实现者决定。

## 1. 音频容量

按中文口播 4.5 字/秒（约 270 字/分钟）：

| 回复长度 | 语音时长 | 单通道上限 |
|---|---|---|
| 30 字 | 6.7s | 9.0 条/分钟 |
| 50 字 | 11.1s | 5.4 条/分钟 |
| 80 字 | 17.8s | 3.4 条/分钟 |

含义：到达率超过单通道上限，队列无界增长；增加生成线程不改变播放通道的容量。

## 2. AI-Vtuber（v4，2026-10-31 代码）

- `config.json:145-146`：`comment_forget_duration=1.0`、`comment_forget_reserve_num=1`。
  `my_handle.py:3390-3400` 的 append/pop 逻辑 → 每 1 秒窗口只保留最后一条。
- `my_handle.py:3403-3425`：`handle_lock` 下 `for data in batch` 串行处理；
  注释原文：“处理一条弹幕要跑 LLM（默认 60s 超时）和音频合成……一条慢回复能把整个直播间的弹幕接收卡住。”
- `audio/service.py:576-578`、`610-612`：合成队列 `message_queue_max_len=50`、播放队列 `voice_tmp_path_queue_max_len=100`；
  插到队尾且超出上限时丢弃新数据。
- `config.json:170-185`：默认 `priority_mapping` 中 `comment`/`gift`/`follow`/`entrance` 均为 20。
- `app/api.py:34-46`：`POST /send`，body 形如：
  ```json
  {"type":"comment","data":{"username":"...","content":"...","platform":"douyin"}}
  ```
  内部调用 `process_data(data_json, "comment")`，再进入上面的 1 秒窗口。

## 3. Open-LLM-VTuber

官方 Bilibili 直播指南原文：

> AI 会依次阅读并回复收到的弹幕（目前，没有被回复的弹幕会被添加到队列中，当消息速度过快时，AI 会因为队列拥塞而产生较大的回复延迟）。

## 4. 抖音合规

- 抖音《关于人工智能生成内容的平台规范暨行业倡议》：
  “使用已注册的虚拟人形象进行直播时，必须由真人驱动进行实时互动，不允许完全由 AI 驱动进行互动。”
- 《人工智能生成合成内容标识办法》2025-09-01 施行，AI 生成内容需显著标识。
- 抖音开放平台的“直播间评论互动能力”只对挂载的“弹幕小玩法”开放；
  普通账号没有“读全部弹幕 + 自动发送”的公开接口。市面方案多靠逆向或 UI 自动化，有风控与封号风险。

## 5. DeepSeek 价格（可选，若使用 LLM）

`deepseek-flash`（V4.1-Flash-1017），元/百万 token，空闲/高峰：

- 输入缓存命中：0.02 / 0.04
- 输入缓存未命中：1 / 2
- 输出：4 / 8
- 支持非思考模式（`thinking: {"type": "disabled"}`）、JSON Output、并发上限 2500。

含义：成本上限可计算；是否用 LLM 由实现者决定。

## 6. 参考项目

| 项目 | 许可 | 有 | 没有 |
|---|---|---|---|
| AI-Vtuber | GPL-3.0 | 多平台采集、LLM/TTS、类型优先级、黑名单/违禁/去重 | 内容打分、容量配额、合并、抢占 |
| Fay | GPL-3.0 | 每用户执行线程、单音频队列 | 优先级、合并、TTL、抢占 |
| Open-LLM-VTuber | MIT | 对话 Agent、B 站实验直播 | 调度、选择、背压 |
| AIGCPanel | Apache-2.0 | 多平台弹幕监控、数字人 | 调度层 |
| oba-live-tool | MIT | 抖音小店场控、AI 回复、重试 | 内容打分、容量配额 |
| DouyinLiveWebFetcher | AGPL-3.0 | 抖音弹幕采集 | 回复链路 |
